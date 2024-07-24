# taken and modified from https://github.com/huggingface/trl/blob/main/trl/
import gc
import math
import os
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Dict, Literal, Optional, Tuple, Union

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from accelerate import Accelerator
from accelerate.utils import broadcast, gather_object
from datasets import Dataset
from torch.utils.data import DataLoader
from transformers import (
    DataCollatorWithPadding,
    GenerationConfig,
    PreTrainedTokenizer,
    Trainer,
    TrainerControl,
    TrainerState,
    TrainingArguments,
)
from transformers.integrations import get_reporting_integration_callbacks
from transformers.trainer_callback import CallbackHandler, DefaultFlowCallback

from .model_utils import (
    batch_generation,
    disable_dropout_in_model,
    exact_div,
    first_true_indices,
    forward,
    get_reward,
    prepare_deepspeed,
    print_rich_table,
    truncate_response,
    unwrap_model_for_generation,
)


@dataclass
class OnlineDPOConfig(TrainingArguments):
    # common config
    exp_name: str = os.path.basename(__file__)[: -len(".py")]
    """the name of this experiment"""
    run_name: Optional[str] = None
    """a unique name of this run"""
    sanity_check: bool = False
    """wether to run in debug mode"""

    # batch size related config
    num_mini_batches: int = 1
    """Number of minibatches to split a batch into"""
    total_episodes: Optional[int] = None
    """The total number of episodes in the dataset"""
    local_rollout_forward_batch_size: int = 64
    """per rank no grad forward pass in the rollout phase"""
    num_sample_generations: int = 10
    """the number of debugging samples generations (i.e., `generate_completions` calls) throughout training"""

    # other config
    base_model: str = "EleutherAI/pythia-160m"
    """the name of the pretrained model to use"""
    response_length: int = 53
    """the length of the response"""
    stop_token: Optional[Literal["eos"]] = None
    """the stop token"""
    stop_token_id: Optional[int] = None
    """the truncation token id"""
    temperature: float = 0.7
    """the sampling temperature"""
    penalty_reward_value: int = -1
    """the reward value for responses that do not contain `stop_token_id`"""
    non_eos_penalty: bool = False
    """whether to penalize responses that do not contain `stop_token_id`"""
    reward_model_path: str = "EleutherAI/pythia-160m"
    """the path to the reward model"""
    sft_model_path: str = "EleutherAI/pythia-160m"
    """the path to the sft model"""

    # online DPO config
    num_epochs: int = 4
    """the number of epochs to train"""
    num_generation_per_prompt: int = 2
    """the number of generations per prompt (currently only support 2)"""
    beta: float = 0.05
    """the beta value for the DPO algorithm"""
    loss_type: Literal["sigmoid", "ipo"] = "sigmoid"
    """the loss type for the DPO algorithm"""

    # various batch sizes
    world_size: Optional[int] = None
    """The number of processes (GPUs) to use"""
    num_updates: Optional[int] = None
    """The number of updates to train"""
    micro_batch_size: Optional[int] = None
    """The micro batch size across devices (HF's `per_device_train_batch_size` * `world_size`)"""
    local_batch_size: Optional[int] = None
    """The batch size per GPU (HF's `per_device_train_batch_size` * `gradient_accumulation_steps`)"""
    batch_size: Optional[int] = None
    """The batch size across devices (HF's `per_device_train_batch_size` * `world_size` * `gradient_accumulation_steps`)"""
    local_mini_batch_size: Optional[int] = None
    """the mini batch size per GPU"""
    mini_batch_size: Optional[int] = None
    """the mini batch size across GPUs"""


INVALID_LOGPROB = 1.0


class OnlineDPOTrainer(Trainer):
    def __init__(
        self,
        config: OnlineDPOConfig,
        tokenizer: PreTrainedTokenizer,
        policy: nn.Module,
        ref_policy: nn.Module,
        reward_model: nn.Module,
        train_dataset: Dataset,
        data_collator: Optional[DataCollatorWithPadding] = None,
        eval_dataset: Optional[Union[Dataset, Dict[str, Dataset]]] = None,
        # less commonly used
        optimizers: Tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LambdaLR] = (None, None),
    ) -> None:
        self.args = config
        args = config
        self.tokenizer = tokenizer
        self.policy = policy

        self.policy.generation_config.eos_token_id = (
            None  # disable `pad_token_id` and `eos_token_id` because we just want to
        )
        self.policy.generation_config.pad_token_id = None  # generate tokens without truncation / padding

        self.ref_policy = ref_policy
        self.reward_model = reward_model
        self.train_dataset = train_dataset
        self.train_dataset_len = len(train_dataset)
        self.data_collator = data_collator
        self.eval_dataset = eval_dataset
        self.optimizer, self.lr_scheduler = optimizers

        #########
        # calculate various batch sizes
        #########
        if args.total_episodes is None:  # allow the users to define episodes in terms of epochs.
            args.total_episodes = int(args.num_train_epochs * self.train_dataset_len)
        accelerator = Accelerator(gradient_accumulation_steps=args.gradient_accumulation_steps)
        self.accelerator = accelerator
        args.world_size = accelerator.num_processes
        args.local_batch_size = (
            args.per_device_train_batch_size * args.gradient_accumulation_steps * args.num_mini_batches
        )
        args.micro_batch_size = int(args.per_device_train_batch_size * args.world_size)
        args.batch_size = int(args.local_batch_size * args.world_size)
        args.mini_batch_size = exact_div(
            args.batch_size, args.num_mini_batches, "`batch_size` must be a multiple of `num_mini_batches`"
        )
        args.local_mini_batch_size = exact_div(
            args.local_batch_size, args.num_mini_batches, "`local_batch_size` must be a multiple of `num_mini_batches`"
        )
        # `per_rank_rollout_batch_size` is our `args.local_batch_size`
        # `per_rank_minibatch_size` is our `args.local_mini_batch_size`
        args.num_total_batches = math.ceil(
            args.total_episodes / args.batch_size
        )  # we may train for more than `total_episodes`
        time_tensor = torch.tensor(int(time.time()), device=accelerator.device)
        time_int = broadcast(time_tensor, 0).item()  # avoid different timestamps across processes
        args.run_name = f"{args.exp_name}__{args.seed}__{time_int}"
        self.local_seed = args.seed + accelerator.process_index * 100003  # Prime
        if args.num_sample_generations > 0:
            self.sample_generations_freq = max(1, args.num_total_batches // args.num_sample_generations)
        self.local_dataloader_batch_size = exact_div(
            args.local_batch_size,
            args.num_generation_per_prompt,
            "`local_batch_size` must be a multiple of `num_generation_per_prompt`",
        )  # DPO logic: repeats the same prompt args.rloo_k times

        ### DPO stuff
        self.beta = config.beta
        self.loss_type = config.loss_type

        #########
        # setup model, optimizer, and others
        #########
        for module in [policy, ref_policy, reward_model]:
            disable_dropout_in_model(module)
        if args.stop_token and args.stop_token == "eos":
            args.stop_token_id = tokenizer.eos_token_id
        self.model = policy
        self.create_optimizer_and_scheduler(
            num_training_steps=args.num_total_batches
        )  # note that we are calling `self.lr_scheduler.step()` manually only at the batch level

        #########
        ### trainer specifics
        #########
        self.state = TrainerState(
            is_local_process_zero=self.is_local_process_zero(),
            is_world_process_zero=self.is_world_process_zero(),
        )
        DEFAULT_CALLBACKS = [DefaultFlowCallback]
        default_callbacks = DEFAULT_CALLBACKS + get_reporting_integration_callbacks(self.args.report_to)
        self.callbacks = default_callbacks
        self.callback_handler = CallbackHandler(
            self.callbacks, self.model, self.tokenizer, self.optimizer, self.lr_scheduler
        )
        self.control = TrainerControl()
        self.current_flos = 0
        self.hp_search_backend = None
        self.is_deepspeed_enabled = getattr(self.accelerator.state, "deepspeed_plugin", None) is not None
        self.is_fsdp_enabled = getattr(self.accelerator.state, "fsdp_plugin", None) is not None
        # Create distant repo and output directory if needed
        self.hub_model_id = None
        if self.args.push_to_hub:
            self.init_hf_repo()
        if self.args.should_save:
            os.makedirs(self.args.output_dir, exist_ok=True)
        self.backup_model = None

        #########
        ### setup dataloader
        #########
        self.dataloader = DataLoader(
            self.train_dataset,
            batch_size=self.local_dataloader_batch_size,
            shuffle=True,
            collate_fn=DataCollatorWithPadding(tokenizer),
            drop_last=True,  # needed; otherwise the last batch will be of ragged shape
        )
        # sync random states for DataLoader(shuffle=True) before `accelerator.prepare`
        # see https://gist.github.com/vwxyzjn/2581bff1e48e185e0b85b6dfe1def79c
        torch.manual_seed(args.seed)
        self.model, self.optimizer, self.dataloader = accelerator.prepare(self.model, self.optimizer, self.dataloader)
        torch.manual_seed(self.local_seed)  # reset the local seed again

        self.eval_dataloader = DataLoader(
            self.eval_dataset,
            batch_size=args.per_device_eval_batch_size,
            collate_fn=DataCollatorWithPadding(self.tokenizer),
            drop_last=True,
        )  # no need to shuffle eval dataset
        self.eval_dataloader = accelerator.prepare(self.eval_dataloader)

        if self.is_deepspeed_enabled:
            self.reward_model = prepare_deepspeed(
                self.reward_model, args.per_device_train_batch_size, args.fp16, args.bf16
            )
            self.ref_policy = prepare_deepspeed(
                self.ref_policy, args.per_device_train_batch_size, args.fp16, args.bf16
            )
            self.deepspeed = self.model
        else:
            self.ref_policy = self.ref_policy.to(self.accelerator.device)
            self.reward_model = self.reward_model.to(self.accelerator.device)

    def get_train_dataloader(self) -> DataLoader:
        return self.dataloader

    def get_eval_dataloader(self) -> DataLoader:
        return self.eval_dataloader

    def train(self):
        args = self.args
        accelerator = self.accelerator
        optimizer = self.optimizer
        model = self.model
        ref_policy = self.ref_policy
        reward_model = self.reward_model
        tokenizer = self.tokenizer
        dataloader = self.dataloader
        device = accelerator.device

        def repeat_generator():
            while True:
                yield from dataloader

        iter_dataloader = iter(repeat_generator())
        generation_config = GenerationConfig(
            max_new_tokens=args.response_length,
            min_new_tokens=args.response_length,
            temperature=(args.temperature + 1e-7),
            top_k=0.0,
            top_p=1.0,
            do_sample=True,
        )

        accelerator.print("===training policy===")
        start_time = time.time()
        stats_shape = (args.num_epochs, args.num_mini_batches, args.gradient_accumulation_steps)
        loss_stats = torch.zeros(stats_shape, device=device)
        chosen_rewards_stats = torch.zeros(stats_shape, device=device)
        rejected_rewards_stats = torch.zeros(stats_shape, device=device)
        chosen_logprobs_stats = torch.zeros(stats_shape, device=device)
        rejected_logprobs_stats = torch.zeros(stats_shape, device=device)
        model.train()

        # trainer state initialization
        episode = 0
        for update in range(1, args.num_total_batches + 1):
            episode += 1 * args.batch_size
            self.lr_scheduler.step()
            data = next(iter_dataloader)
            with torch.no_grad():
                queries = data["input_ids"].to(device)
                queries = queries.repeat(args.num_generation_per_prompt, 1)
                context_length = queries.shape[1]
                responses = []
                postprocessed_responses = []
                logprobs = []
                ref_logprobs = []
                scores = []
                sequence_lengths = []
                with unwrap_model_for_generation(model, self.accelerator) as unwrapped_model:
                    query_responses, logitss = batch_generation(
                        unwrapped_model,
                        queries,
                        args.local_rollout_forward_batch_size,
                        tokenizer.pad_token_id,
                        generation_config,
                    )

                for i in range(0, queries.shape[0], args.local_rollout_forward_batch_size):
                    query = queries[i : i + args.local_rollout_forward_batch_size]
                    query_response = query_responses[i : i + args.local_rollout_forward_batch_size]
                    response = query_response[:, context_length:]
                    logits = logitss[i : i + args.local_rollout_forward_batch_size]
                    all_logprob = F.log_softmax(logits, dim=-1)
                    logprob = torch.gather(all_logprob, 2, response.unsqueeze(-1)).squeeze(-1)
                    del logits, all_logprob
                    torch.cuda.empty_cache()

                    ref_output = forward(ref_policy, query_response, tokenizer.pad_token_id)
                    ref_logits = ref_output.logits[:, context_length - 1 : -1]
                    ref_logits /= args.temperature + 1e-7
                    ref_all_logprob = F.log_softmax(ref_logits, dim=-1)
                    ref_logprob = torch.gather(ref_all_logprob, 2, response.unsqueeze(-1)).squeeze(-1)
                    del ref_output, ref_logits, ref_all_logprob
                    torch.cuda.empty_cache()

                    # Response Processing 1. truncate response after the first occurrence of `stop_token_id`
                    postprocessed_response = response
                    if args.stop_token_id is not None:  # handle the edge case when stop_token_id exists but is 0
                        postprocessed_response = truncate_response(
                            args.stop_token_id, tokenizer.pad_token_id, response
                        )

                    # Response Processing 2. run reward model on the truncated responses
                    postprocessed_query_response = torch.cat((query, postprocessed_response), 1)
                    sequence_length = first_true_indices(postprocessed_response == tokenizer.pad_token_id) - 1
                    _, score, _ = get_reward(
                        reward_model, postprocessed_query_response, tokenizer.pad_token_id, context_length
                    )

                    responses.append(response)
                    postprocessed_responses.append(postprocessed_response)
                    logprobs.append(logprob)
                    ref_logprobs.append(ref_logprob)
                    sequence_lengths.append(sequence_length)
                    scores.append(score)
                responses = torch.cat(responses, 0)
                postprocessed_responses = torch.cat(postprocessed_responses, 0)
                logprobs = torch.cat(logprobs, 0)
                ref_logprobs = torch.cat(ref_logprobs, 0)
                sequence_lengths = torch.cat(sequence_lengths, 0)
                scores = torch.cat(scores, 0)
                del (logprob, ref_logprob, score, unwrapped_model)
                torch.cuda.empty_cache()
                gc.collect()

                # Response Processing 3. filter response. Ensure that the sample contains stop_token_id
                # responses not passing that filter will receive a low (fixed) score
                # only query humans on responses that pass that filter
                contain_eos_token = torch.any(postprocessed_responses == tokenizer.eos_token_id, dim=-1)
                if args.non_eos_penalty:
                    scores = torch.where(contain_eos_token, scores, torch.full_like(scores, args.penalty_reward_value))
                # accelerator.print(f"{scores=}, {(contain_eos_token.sum() / len(contain_eos_token))=}")

                # be very careful with `padding_mask_p1`; see https://excalidraw.com/#json=LWnzG4w2k5DjF_EOL_xPt,e2w3a-hFJ_gX5vOfeyXGTw
                response_idxs = torch.arange(responses.shape[1], device=responses.device).repeat(responses.shape[0], 1)
                padding_mask = response_idxs > sequence_lengths.unsqueeze(1)
                logprobs = torch.masked_fill(logprobs, padding_mask, INVALID_LOGPROB)
                ref_logprobs = torch.masked_fill(ref_logprobs, padding_mask, INVALID_LOGPROB)

                # 4. compute rewards
                kl = logprobs - ref_logprobs
                non_score_reward = (-args.beta * kl).sum(1)
                rlhf_reward = scores + non_score_reward

                # num_examples should be same as args.local_batch_size divided by 2
                num_examples = scores.size(0) // 2
                first_half = scores[:num_examples]
                second_half = scores[num_examples:]

                num_examples_range = torch.arange(num_examples).to(scores.device)

                chosen_indices = torch.where(
                    first_half >= second_half, num_examples_range.clone(), num_examples_range.clone() + num_examples
                )
                rejected_indices = torch.where(
                    first_half < second_half, num_examples_range.clone(), num_examples_range.clone() + num_examples
                )

                scores_margin = scores[chosen_indices] - scores[rejected_indices]
            # Do multiple epochs of PPO training, with a fresh random shuffle in each epoch
            for epoch_idx in range(args.num_epochs):
                b_inds = np.random.permutation(args.local_batch_size // args.num_generation_per_prompt)
                minibatch_idx = 0
                for mini_batch_start in range(
                    0,
                    args.local_batch_size // args.num_generation_per_prompt,
                    args.local_mini_batch_size // args.num_generation_per_prompt,
                ):
                    mini_batch_end = mini_batch_start + args.local_mini_batch_size // args.num_generation_per_prompt
                    mini_batch_inds = b_inds[mini_batch_start:mini_batch_end]
                    gradient_accumulation_idx = 0
                    for micro_batch_start in range(
                        0,
                        args.local_mini_batch_size // args.num_generation_per_prompt,
                        args.per_device_train_batch_size,
                    ):
                        with accelerator.accumulate(model):
                            micro_batch_end = micro_batch_start + args.per_device_train_batch_size
                            micro_batch_inds = mini_batch_inds[micro_batch_start:micro_batch_end]
                            ## chosen
                            chosen_mb_inds = chosen_indices[micro_batch_inds]
                            chosen_responses = responses[chosen_mb_inds]

                            ## rejected
                            rejected_mb_inds = rejected_indices[micro_batch_inds]
                            rejected_responses = responses[rejected_mb_inds]

                            concat_mb_inds = torch.cat((chosen_mb_inds, rejected_mb_inds), dim=0)
                            concat_query_responses = query_responses[concat_mb_inds]
                            concat_output = forward(model, concat_query_responses, tokenizer.pad_token_id)
                            num_examples = chosen_mb_inds.shape[0]
                            chosen_logits = concat_output.logits[:num_examples]
                            rejected_logits = concat_output.logits[num_examples:]

                            # chosen
                            chosen_logits = chosen_logits[:, context_length - 1 : -1]
                            chosen_logits /= args.temperature + 1e-7
                            chosen_all_logprobs = F.log_softmax(chosen_logits, dim=-1)
                            chosen_logprobs = torch.gather(
                                chosen_all_logprobs, 2, chosen_responses.unsqueeze(-1)
                            ).squeeze(-1)
                            chosen_logprobs = torch.masked_fill(
                                chosen_logprobs, padding_mask[chosen_mb_inds], INVALID_LOGPROB
                            )
                            chosen_ref_logprobs = ref_logprobs[chosen_mb_inds]
                            chosen_logprobs_sum = (chosen_logprobs * ~padding_mask[chosen_mb_inds]).sum(1)
                            chosen_ref_logprobs_sum = (chosen_ref_logprobs * ~padding_mask[chosen_mb_inds]).sum(1)

                            # rejected
                            rejected_logits = rejected_logits[:, context_length - 1 : -1]
                            rejected_logits /= args.temperature + 1e-7
                            rejected_all_logprobs = F.log_softmax(rejected_logits, dim=-1)
                            rejected_logprobs = torch.gather(
                                rejected_all_logprobs, 2, rejected_responses.unsqueeze(-1)
                            ).squeeze(-1)
                            rejected_logprobs = torch.masked_fill(
                                rejected_logprobs, padding_mask[rejected_mb_inds], INVALID_LOGPROB
                            )
                            rejected_ref_logprobs = ref_logprobs[rejected_mb_inds]
                            rejected_logprobs_sum = (rejected_logprobs * ~padding_mask[rejected_mb_inds]).sum(1)
                            rejected_ref_logprobs_sum = (rejected_ref_logprobs * ~padding_mask[rejected_mb_inds]).sum(
                                1
                            )

                            pi_logratios = chosen_logprobs_sum - rejected_logprobs_sum
                            ref_logratios = chosen_ref_logprobs_sum - rejected_ref_logprobs_sum

                            logits = pi_logratios - ref_logratios

                            if self.loss_type == "sigmoid":
                                losses = -F.logsigmoid(self.beta * logits)
                            elif self.loss_type == "ipo":
                                losses = (logits - 1 / (2 * self.beta)) ** 2
                            else:
                                raise NotImplementedError(f"invalid loss type {self.loss_type}")

                            loss = losses.mean()
                            accelerator.backward(loss)
                            optimizer.step()
                            optimizer.zero_grad()
                            with torch.no_grad():
                                chosen_rewards = self.beta * (chosen_logprobs_sum - chosen_ref_logprobs_sum)
                                rejected_rewards = self.beta * (rejected_logprobs_sum - rejected_ref_logprobs_sum)
                                loss_stats[epoch_idx, minibatch_idx, gradient_accumulation_idx] = loss
                                chosen_rewards_stats[epoch_idx, minibatch_idx, gradient_accumulation_idx] = (
                                    chosen_rewards.mean()
                                )
                                rejected_rewards_stats[epoch_idx, minibatch_idx, gradient_accumulation_idx] = (
                                    rejected_rewards.mean()
                                )
                                chosen_logprobs_stats[epoch_idx, minibatch_idx, gradient_accumulation_idx] = (
                                    chosen_logprobs_sum.mean()
                                )
                                rejected_logprobs_stats[epoch_idx, minibatch_idx, gradient_accumulation_idx] = (
                                    rejected_logprobs_sum.mean()
                                )
                        gradient_accumulation_idx += 1
                    minibatch_idx += 1
                    self.state.global_step += 1
                    # del everything and empty cache
                    # fmt: off
                    del (
                        loss, logits,
                        concat_output, concat_query_responses,
                        chosen_logits, rejected_logits,
                        chosen_logprobs, rejected_logprobs,
                        chosen_responses, rejected_responses,
                    )
                    # fmt: on
                    torch.cuda.empty_cache()
            with torch.no_grad():
                mean_kl = kl.sum(1).mean()
                mean_entropy = (-logprobs).sum(1).mean()
                mean_non_score_reward = non_score_reward.mean()
                eps = int(episode / (time.time() - start_time))
                g_chosen_reward = self.accelerator.gather(chosen_rewards_stats)
                g_rejected_reward = self.accelerator.gather(rejected_rewards_stats)
                metrics = {}
                metrics["eps"] = eps
                metrics["objective/kl"] = self.accelerator.gather(mean_kl).mean().item()
                metrics["objective/entropy"] = self.accelerator.gather(mean_entropy).mean().item()
                metrics["objective/non_score_reward"] = self.accelerator.gather(mean_non_score_reward).mean().item()
                metrics["objective/rlhf_reward"] = self.accelerator.gather(rlhf_reward).mean().item()
                metrics["objective/scores"] = self.accelerator.gather(scores.mean()).mean().item()
                metrics["objective/scores_margin"] = self.accelerator.gather(scores_margin.mean()).mean().item()
                metrics["rewards/chosen"] = g_chosen_reward.mean().item()
                metrics["rewards/rejected"] = g_rejected_reward.mean().item()
                metrics["rewards/accuracies"] = (g_chosen_reward > g_rejected_reward).float().mean().item()
                metrics["rewards/margins"] = (g_chosen_reward - g_rejected_reward).mean().item()
                metrics["loss/policy_avg"] = self.accelerator.gather(loss_stats).mean().item()
                metrics["logps/chosen"] = self.accelerator.gather(chosen_logprobs_stats).mean().item()
                metrics["logps/rejected"] = self.accelerator.gather(rejected_logprobs_stats).mean().item()
                metrics["val/num_eos_tokens"] = (responses == tokenizer.eos_token_id).sum().item()
                metrics["lr"] = self.lr_scheduler.get_last_lr()[0]
                metrics["episode"] = episode
                self.state.epoch = episode / self.train_dataset_len  # used by self.log
                self.state.global_step += 1
                self.log(metrics)

            del (kl, mean_kl, mean_entropy, scores, scores_margin)

    def generate_completions(self, sampling: bool = False):
        args = self.args
        tokenizer = self.tokenizer
        generation_config = GenerationConfig(
            max_new_tokens=self.args.response_length,
            temperature=(0.01 + 1e-7),
            top_k=0.0,
            top_p=1.0,
            do_sample=True,
        )

        table = defaultdict(list)
        with unwrap_model_for_generation(self.model, self.accelerator) as unwrapped_model:
            for batch in self.eval_dataloader:
                query = batch["input_ids"]
                with torch.no_grad():
                    context_length = query.shape[1]
                    query_response, _ = batch_generation(
                        unwrapped_model,
                        query,
                        query.shape[0],
                        tokenizer.pad_token_id,
                        generation_config,
                    )
                    response = query_response[:, context_length:]
                    postprocessed_response = response
                    if args.stop_token_id is not None:  # handle the edge case when stop_token_id exists but is 0
                        postprocessed_response = truncate_response(
                            args.stop_token_id, tokenizer.pad_token_id, response
                        )
                    table["query"].extend(gather_object(tokenizer.batch_decode(query, skip_special_tokens=True)))
                    table["model response"].extend(gather_object(tokenizer.batch_decode(postprocessed_response)))

                    postprocessed_query_response = torch.cat((query, postprocessed_response), 1)
                    _, score, _ = get_reward(
                        self.reward_model, postprocessed_query_response, tokenizer.pad_token_id, context_length
                    )
                    table["score"].extend(self.accelerator.gather(score).float().cpu().numpy())

                if sampling:
                    break
        df = pd.DataFrame(table)
        if self.accelerator.process_index == 0:
            print_rich_table(df.iloc[0 : 0 + 5])
        if "wandb" in args.report_to:
            import wandb

            if wandb.run is not None:
                wandb.log({"completions": wandb.Table(dataframe=df)})
