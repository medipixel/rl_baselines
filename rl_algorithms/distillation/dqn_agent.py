# -*- coding: utf-8 -*-
"""DQN distillation class for collect teacher's data and train student.

- Author: Kyunghwan Kim, Minseop Kim
- Contact: kh.kim@medipixel.io, minseop.kim@medipixel.io
- Paper: https://storage.googleapis.com/deepmind-media/dqn/DQNNaturePaper.pdf (DQN)
         https://arxiv.org/pdf/1511.06295.pdf (Policy Distillation)
"""

import os
import pickle
import time
from typing import Tuple

import numpy as np
import torch
import torch.nn.functional as F
from tqdm import tqdm
import wandb

from rl_algorithms.common.buffer.distillation_buffer import DistillationBuffer
from rl_algorithms.dqn.agent import DQNAgent
from rl_algorithms.registry import AGENTS, build_learner

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


@AGENTS.register_module
class DistillationDQN(DQNAgent):
    """DQN for policy distillation.
       Use _test function to collect teacher's distillation data.
       Use train_distillation function to train student model.
    """

    # pylint: disable=attribute-defined-outside-init
    def _initialize(self):
        """Initialize non-common things."""

        # You must choose one of them.
        assert (
            self.args.teacher
            + self.args.student
            + self.args.add_expert_q
            + self.args.test
            == 1
        )

        if self.args.student or self.args.test:
            # Training student or generating distillation data(test) requires DistillationBuffer.

            self.softmax_tau = 0.01
            self.learner = build_learner(self.learner_cfg)
            self.buffer_path = self.hyper_params.buffer_path
            if not self.args.student:
                self.buffer_path = (
                    f"./data/distillation_buffer/{self.log_cfg.env_name}/"
                    + f"{self.log_cfg.agent}/{self.log_cfg.curr_time}/"
                )
                if self.args.distillation_buffer_path:
                    self.buffer_path = "./" + self.args.distillation_buffer_path
                os.makedirs(self.buffer_path, exist_ok=True)

            self.memory = DistillationBuffer(
                self.hyper_params.batch_size, self.buffer_path, self.log_cfg.curr_time,
            )
        else:
            # Since raining teacher do not require DistillationBuffer,
            # it overloads DQNAgent._initialize.

            DQNAgent._initialize(self)
            self.save_distillation_dir = (
                "./data/distillation_buffer/"
                + self.env_info.name
                + time.strftime("/%Y%m%d%H%M%S/")
            )
            os.makedirs(self.save_distillation_dir)
            self.save_count = 0

    def select_action(self, state: np.ndarray, is_test=False) -> np.ndarray:
        """Select an action from the input space."""

        if self.args.teacher:
            if is_test:
                return DQNAgent.select_action(self, state)
            else:
                # Save states during training teacher.
                if not os.path.exists(
                    self.save_distillation_dir + "{}/".format(self.i_episode)
                ):
                    os.mkdir(self.save_distillation_dir + "{}/".format(self.i_episode))
                current_ep_dir = (
                    self.save_distillation_dir
                    + "{}/{}.pkl".format(self.i_episode, self.save_count)
                    + ""
                )
                with open(current_ep_dir, "wb") as f:
                    pickle.dump([state], f, protocol=pickle.HIGHEST_PROTOCOL)
                self.save_count += 1
                return DQNAgent.select_action(self, state)
        else:
            self.curr_state = state
            # epsilon greedy policy
            # pylint: disable=comparison-with-callable
            state = self._preprocess_state(state)
            q_values = self.learner.dqn(state)

            if not self.args.test and self.epsilon > np.random.random():
                selected_action = np.array(self.env.action_space.sample())
            else:
                selected_action = q_values.argmax()
                selected_action = selected_action.detach().cpu().numpy()
            return selected_action, q_values.squeeze().detach().cpu().numpy()

    def step(
        self, action: np.ndarray, q_values: np.ndarray = None
    ) -> Tuple[np.ndarray, np.float64, bool, dict]:
        """Take an action and store distillation data to buffer storage."""
        if self.args.test and not self.args.teacher and not self.args.student:
            next_state, reward, done, info = self.env.step(action)

            data = (self.curr_state, q_values)

            self.memory.add(data)
            return next_state, reward, done, info
        else:
            return DQNAgent.step(self, action)

    def _test(self, interim_test: bool = False):
        """Test teacher and collect distillation data."""

        if interim_test:
            test_num = self.args.interim_test_num
        else:
            test_num = self.args.episode_num
        if self.args.teacher:
            score_list = []
            for i_episode in range(test_num):
                state = self.env.reset()
                done = False
                score = 0
                step = 0

                while not done:
                    if self.args.render:
                        self.env.render()

                    action = self.select_action(state, is_test=True)
                    next_state, reward, done, _ = self.step(action)

                    state = next_state
                    score += reward
                    step += 1

                print(
                    "[INFO] test %d\tstep: %d\ttotal score: %d"
                    % (i_episode, step, score)
                )
                score_list.append(score)
        else:
            for i_episode in range(test_num):
                state = self.env.reset()
                done = False
                score = 0
                step = 0

                while not done and self.memory.idx != self.hyper_params.buffer_size:
                    if self.args.render:
                        self.env.render()

                    action, q_value = self.select_action(state, is_test=True)
                    next_state, reward, done, _ = self.step(action, q_value)

                    state = next_state
                    score += reward
                    step += 1

                print(
                    "[INFO] test %d\tstep: %d\ttotal score: %d\tbuffer_size: %d"
                    % (i_episode, step, score, self.memory.idx)
                )

                if self.args.log:
                    wandb.log({"test score": score})

                if self.memory.idx == self.hyper_params.buffer_size:
                    print("[INFO] Buffer saved completely. (%s)" % (self.buffer_path))
                    break

    def update_distillation(self) -> Tuple[torch.Tensor, ...]:
        """Make relaxed softmax target and KL-Div loss and updates student model's params."""
        states, q_values = self.memory.sample_for_diltillation()

        states = states.float().to(device)
        q_values = q_values.float().to(device)

        if torch.cuda.is_available():
            states = states.cuda(non_blocking=True)
            q_values = q_values.cuda(non_blocking=True)

        pred_q = self.learner.dqn(states)
        target = F.softmax(q_values / self.softmax_tau, dim=1)
        log_softmax_pred_q = F.log_softmax(pred_q, dim=1)
        loss = F.kl_div(log_softmax_pred_q, target, reduction="sum")

        self.learner.dqn_optim.zero_grad()
        loss.backward()
        self.learner.dqn_optim.step()

        return loss.item(), pred_q.mean().item()

    def train(self):
        """Train the student model from teacher's data."""
        if self.args.student:
            self.memory.reset_dataloader()
            assert self.memory.buffer_size >= self.hyper_params.batch_size
            if self.args.log:
                self.set_wandb()

            iter_1 = self.memory.buffer_size // self.hyper_params.batch_size
            train_steps = iter_1 * self.hyper_params.epochs
            print(
                f"[INFO] Total epochs: {self.hyper_params.epochs}\t Train steps: {train_steps}"
            )
            n_epoch = 0
            for steps in range(train_steps):
                loss = self.update_distillation()

                if self.args.log:
                    wandb.log({"dqn loss": loss[0], "avg q values": loss[1]})

                if steps % iter_1 == 0:
                    print(
                        f"Training {n_epoch} epochs, {steps} steps.. "
                        + f"loss: {loss[0]}, avg_q_value: {loss[1]}"
                    )
                    self.learner.save_params(steps)
                    n_epoch += 1
                    self.memory.reset_dataloader()

            self.learner.save_params(steps)

        elif self.args.add_expert_q:
            # Add expert's q to the train phase states.

            # Gather train phase states.
            self.file_name_list = []
            for _dir in self.hyper_params.buffer_path:
                sub_dirs = os.listdir(_dir)
                for _subdir in sub_dirs:
                    current_dir = "./" + _dir + "/" + _subdir + "/"
                    tmp = os.listdir(current_dir)
                    self.file_name_list += [[current_dir, x] for x in tmp]

            for _dir in tqdm(self.file_name_list):
                with open(_dir[0] + _dir[1], "rb") as f:
                    state = pickle.load(f)[0]

                torch_state = torch.from_numpy(state).float().to(device)
                pred_q = self.learner.dqn(torch_state).squeeze().detach().cpu().numpy()

                with open(self.save_distillation_dir + _dir[1], "wb") as f:
                    pickle.dump([state, pred_q], f, protocol=pickle.HIGHEST_PROTOCOL)

        elif self.args.teacher:
            DQNAgent.train(self)
