from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from ray.rllib.dqn.common.schedules import ConstantSchedule, LinearSchedule
from ray.rllib.dqn.dqn import DQNAgent
from ray.rllib.ddpg.ddpg_policy_graph import DDPGPolicyGraph

OPTIMIZER_SHARED_CONFIGS = [
    "buffer_size", "prioritized_replay", "prioritized_replay_alpha",
    "prioritized_replay_beta", "prioritized_replay_eps", "sample_batch_size",
    "train_batch_size", "learning_starts", "clip_rewards"
]

DEFAULT_CONFIG = {
    # === Model ===
    # Hidden layer sizes of the policy networks
    "actor_hiddens": [64, 64],
    # Hidden layer sizes of the policy networks
    "critic_hiddens": [64, 64],
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
    # OU-noise scale
    "noise_scale": 0.1,
    # theta
    "exploration_theta": 0.15,
    # sigma
    "exploration_sigma": 0.2,
    # Update the target network every `target_network_update_freq` steps.
    "target_network_update_freq": 0,
    # Update the target by \tau * policy + (1-\tau) * target_policy
    "tau": 0.002,

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
    "actor_lr": 1e-4,
    "critic_lr": 1e-3,
    # If True, use huber loss instead of squared loss for critic network
    # Conventionally, no need to clip gradients if using a huber loss
    "use_huber": False,
    # Threshold of a huber loss
    "huber_threshold": 1.0,
    # Weights for L2 regularization
    "l2_reg": 1e-6,
    # If not None, clip gradients during optimization at this value
    "grad_norm_clipping": None,
    # How many steps of the model to sample before learning starts.
    "learning_starts": 1500,
    # Update the replay buffer with this many samples at once. Note that this
    # setting applies per-worker if num_workers > 1.
    "sample_batch_size": 1,
    # Size of a batched sampled from replay buffer for training. Note that
    # if async_updates is set, then each worker returns gradients for a
    # batch of this size.
    "train_batch_size": 256,

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


class DDPGAgent(DQNAgent):
    _agent_name = "DDPG"
    _allow_unknown_subkeys = [
        "model", "optimizer", "tf_session_args", "env_config"]
    _default_config = DEFAULT_CONFIG
    _policy_graph = DDPGPolicyGraph

    def _make_exploration_schedule(self, worker_index):
        # Override DQN's schedule to take into account `noise_scale`
        if self.config["per_worker_exploration"]:
            assert self.config["num_workers"] > 1, \
                "This requires multiple workers"
            return ConstantSchedule(
                self.config["noise_scale"] * 0.4 **
                (1 + worker_index / float(self.config["num_workers"] - 1) * 7))
        else:
            return LinearSchedule(
                schedule_timesteps=int(self.config["exploration_fraction"] *
                                       self.config["schedule_max_timesteps"]),
                initial_p=self.config["noise_scale"] * 1.0,
                final_p=self.config["noise_scale"] *
                self.config["exploration_final_eps"])
