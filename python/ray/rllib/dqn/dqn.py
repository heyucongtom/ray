from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import pickle
import os

import ray
from ray.rllib import optimizers
from ray.rllib.dqn.common.schedules import ConstantSchedule, LinearSchedule
from ray.rllib.dqn.dqn_policy_graph import DQNPolicyGraph
from ray.rllib.utils.common_policy_evaluator import CommonPolicyEvaluator, \
    collect_metrics
from ray.rllib.agent import Agent
from ray.tune.trial import Resources


OPTIMIZER_SHARED_CONFIGS = [
    "buffer_size", "prioritized_replay", "prioritized_replay_alpha",
    "prioritized_replay_beta", "prioritized_replay_eps", "sample_batch_size",
    "train_batch_size", "learning_starts", "clip_rewards"]

DEFAULT_CONFIG = {
    # === Model ===
    # Whether to use dueling dqn
    "dueling": True,
    # Whether to use double dqn
    "double_q": True,
    # Hidden layer sizes of the state and action value networks
    "hiddens": [256],
    # N-step Q learning
    "n_step": 1,
    # Config options to pass to the model constructor
    "model": {},
    # Discount factor for the MDP
    "gamma": 0.99,
    # Arguments to pass to the env creator
    "env_config": {},

    # === Exploration ===
    # Max num timesteps for annealing schedules. Exploration is annealed from
    # 1.0 to exploration_fraction over this number of timesteps scaled by
    # exploration_fraction
    "schedule_max_timesteps": 100000,
    # Number of env steps to optimize for before returning
    "timesteps_per_iteration": 1000,
    # Fraction of entire training period over which the exploration rate is
    # annealed
    "exploration_fraction": 0.1,
    # Final value of random action probability
    "exploration_final_eps": 0.02,
    # Update the target network every `target_network_update_freq` steps.
    "target_network_update_freq": 500,

    # === Replay buffer ===
    # Size of the replay buffer. Note that if async_updates is set, then
    # each worker will have a replay buffer of this size.
    "buffer_size": 50000,
    # If True prioritized replay buffer will be used.
    "prioritized_replay": True,
    # Alpha parameter for prioritized replay buffer.
    "prioritized_replay_alpha": 0.6,
    # Beta parameter for sampling from prioritized replay buffer.
    "prioritized_replay_beta": 0.4,
    # Epsilon to add to the TD errors when updating priorities.
    "prioritized_replay_eps": 1e-6,
    # Whether to clip rewards to [-1, 1] prior to adding to the replay buffer.
    "clip_rewards": True,

    # === Optimization ===
    # Learning rate for adam optimizer
    "lr": 5e-4,
    # If not None, clip gradients during optimization at this value
    "grad_norm_clipping": 40,
    # How many steps of the model to sample before learning starts.
    "learning_starts": 1000,
    # Update the replay buffer with this many samples at once. Note that
    # this setting applies per-worker if num_workers > 1.
    "sample_batch_size": 4,
    # Size of a batched sampled from replay buffer for training. Note that
    # if async_updates is set, then each worker returns gradients for a
    # batch of this size.
    "train_batch_size": 32,

    # === Parallelism ===
    # Whether to use a GPU for local optimization.
    "gpu": False,
    # Number of workers for collecting samples with. This only makes sense
    # to increase if your environment is particularly slow to sample, or if
    # you"re using the Async or Ape-X optimizers.
    "num_workers": 0,
    # Number of environments to evaluate vectorwise per worker.
    "num_envs": 1,
    # Whether to allocate GPUs for workers (if > 0).
    "num_gpus_per_worker": 0,
    # Whether to allocate CPUs for workers (if > 0).
    "num_cpus_per_worker": 1,
    # Optimizer class to use.
    "optimizer_class": "LocalSyncReplayOptimizer",
    # Config to pass to the optimizer.
    "optimizer_config": {},
    # Whether to use a distribution of epsilons across workers for exploration.
    "per_worker_exploration": False,
    # Whether to compute priorities on workers.
    "worker_side_prioritization": False
}


class DQNAgent(Agent):
    _agent_name = "DQN"
    _allow_unknown_subkeys = [
        "model", "optimizer", "tf_session_args", "env_config"]
    _default_config = DEFAULT_CONFIG
    _policy_graph = DQNPolicyGraph

    @classmethod
    def default_resource_request(cls, config):
        cf = dict(cls._default_config, **config)
        return Resources(
            cpu=1, gpu=cf["gpu"] and 1 or 0,
            extra_cpu=cf["num_cpus_per_worker"] * cf["num_workers"],
            extra_gpu=cf["num_gpus_per_worker"] * cf["num_workers"])

    def _init(self):
        adjusted_batch_size = (
            self.config["sample_batch_size"] + self.config["n_step"] - 1)
        self.local_evaluator = CommonPolicyEvaluator(
            self.env_creator, self._policy_graph,
            batch_steps=adjusted_batch_size,
            batch_mode="truncate_episodes", preprocessor_pref="deepmind",
            compress_observations=True,
            registry=self.registry, env_config=self.config["env_config"],
            model_config=self.config["model"], policy_config=self.config,
            num_envs=self.config["num_envs"])
        remote_cls = CommonPolicyEvaluator.as_remote(
            num_cpus=self.config["num_cpus_per_worker"],
            num_gpus=self.config["num_gpus_per_worker"])
        self.remote_evaluators = [
            remote_cls.remote(
                self.env_creator, self._policy_graph,
                batch_steps=adjusted_batch_size,
                batch_mode="truncate_episodes", preprocessor_pref="deepmind",
                compress_observations=True,
                registry=self.registry, env_config=self.config["env_config"],
                model_config=self.config["model"], policy_config=self.config,
                num_envs=self.config["num_envs"])
            for _ in range(self.config["num_workers"])]

        self.exploration0 = self._make_exploration_schedule(0)
        self.explorations = [
            self._make_exploration_schedule(i)
            for i in range(self.config["num_workers"])]

        for k in OPTIMIZER_SHARED_CONFIGS:
            if k not in self.config["optimizer_config"]:
                self.config["optimizer_config"][k] = self.config[k]

        self.optimizer = getattr(optimizers, self.config["optimizer_class"])(
            self.config["optimizer_config"], self.local_evaluator,
            self.remote_evaluators)

        self.last_target_update_ts = 0
        self.num_target_updates = 0

    def _make_exploration_schedule(self, worker_index):
        # Use either a different `eps` per worker, or a linear schedule.
        if self.config["per_worker_exploration"]:
            assert self.config["num_workers"] > 1, \
                "This requires multiple workers"
            return ConstantSchedule(
                0.4 ** (
                    1 + worker_index / float(
                        self.config["num_workers"] - 1) * 7))
        return LinearSchedule(
            schedule_timesteps=int(
                self.config["exploration_fraction"] *
                self.config["schedule_max_timesteps"]),
            initial_p=1.0,
            final_p=self.config["exploration_final_eps"])

    @property
    def global_timestep(self):
        return self.optimizer.num_steps_sampled

    def update_target_if_needed(self):
        if self.global_timestep - self.last_target_update_ts > \
                self.config["target_network_update_freq"]:
            self.local_evaluator.for_policy(lambda p: p.update_target())
            self.last_target_update_ts = self.global_timestep
            self.num_target_updates += 1

    def _train(self):
        start_timestep = self.global_timestep

        while (self.global_timestep - start_timestep <
               self.config["timesteps_per_iteration"]):
            self.optimizer.step()
            self.update_target_if_needed()

        exp_vals = [self.exploration0.value(self.global_timestep)]
        self.local_evaluator.for_policy(
            lambda p: p.set_epsilon(exp_vals[0]))
        for i, e in enumerate(self.remote_evaluators):
            exp_val = self.explorations[i].value(self.global_timestep)
            e.for_policy.remote(lambda p: p.set_epsilon(exp_val))
            exp_vals.append(exp_val)

        result = collect_metrics(
            self.local_evaluator, self.remote_evaluators)
        return result._replace(
            info=dict({
                "min_exploration": min(exp_vals),
                "max_exploration": max(exp_vals),
                "num_target_updates": self.num_target_updates,
            }, **self.optimizer.stats()))

    def _stop(self):
        # workaround for https://github.com/ray-project/ray/issues/1516
        for ev in self.remote_evaluators:
            ev.__ray_terminate__.remote()

    def _save(self, checkpoint_dir):
        checkpoint_path = os.path.join(
            checkpoint_dir, "checkpoint-{}".format(self.iteration))
        extra_data = [
            self.local_evaluator.save(),
            ray.get([e.save.remote() for e in self.remote_evaluators]),
            self.optimizer.save(),
            self.num_target_updates,
            self.last_target_update_ts]
        pickle.dump(extra_data, open(checkpoint_path + ".extra_data", "wb"))
        return checkpoint_path

    def _restore(self, checkpoint_path):
        extra_data = pickle.load(open(checkpoint_path + ".extra_data", "rb"))
        self.local_evaluator.restore(extra_data[0])
        ray.get([
            e.restore.remote(d) for (d, e)
            in zip(extra_data[1], self.remote_evaluators)])
        self.optimizer.restore(extra_data[2])
        self.num_target_updates = extra_data[3]
        self.last_target_update_ts = extra_data[4]

    def compute_action(self, observation, state=None):
        if state is None:
            state = []
        return self.local_evaluator.for_policy(
            lambda p: p.compute_single_action(
                observation, state, is_training=False)[0])
