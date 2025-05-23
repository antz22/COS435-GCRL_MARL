import jax
import time
import numpy as np
import jax.numpy as jnp
import flax.linen as nn
from brax import envs
from typing import NamedTuple, List
from collections import namedtuple

def generate_unroll(actor_step, training_state, env, env_state, unroll_length, extra_fields=()):
    """Collect trajectories of given unroll_length."""

    @jax.jit
    def f(carry, unused_t):
        state = carry
        nstate, transition = actor_step(training_state, env, state, extra_fields=extra_fields)
#        jax.debug.print("dist to target: {}", nstate.metrics['distance_from_target'])
        return nstate, transition

    final_state, data = jax.lax.scan(f, env_state, (), length=unroll_length)
    return final_state, data

class CrlEvaluator():
    def __init__(self, actor_step, eval_env, num_eval_envs, episode_length, key):
        self._key = key
        self._eval_walltime = 0.
        self._eval_env = eval_env
        
        eval_env = envs.training.EvalWrapper(eval_env)

        def generate_eval_unroll(training_state, key):
            reset_keys = jax.random.split(key, num_eval_envs)
            eval_first_state = eval_env.reset(reset_keys)
            return generate_unroll(
                actor_step,
                training_state,
                eval_env,
                eval_first_state,
                unroll_length=episode_length)[0]

        self._generate_eval_unroll = jax.jit(generate_eval_unroll)
        self._steps_per_unroll = episode_length * num_eval_envs

    def run_evaluation(self, training_state, training_metrics, aggregate_episodes=True):
        """Run one epoch of evaluation."""
        self._key, unroll_key = jax.random.split(self._key)

        t = time.time()
        eval_state = self._generate_eval_unroll(training_state, unroll_key)
        eval_metrics = eval_state.info["eval_metrics"]
        eval_metrics.active_episodes.block_until_ready()
        
        # Track episode returns
        if "returned_episode_returns" in eval_state.info:
            returns = eval_state.info["returned_episode_returns"]
            # Assuming the same structure as in the original code
            # Adjust the slicing based on your specific environment setup
            num_adversaries = self._eval_env.env.env.env.env.num_adversaries
            mean_returns = returns[:, :num_adversaries].mean(axis=(0, 1))

        epoch_eval_time = time.time() - t
        metrics = {}
        aggregating_fns = [
            (np.mean, ""),
            # (np.std, "_std"),
            # (np.max, "_max"),
            # (np.min, "_min"),
        ]

        for (fn, suffix) in aggregating_fns:
            metrics.update(
                {
                    f"eval/episode_{name}{suffix}": (
                        fn(eval_metrics.episode_metrics[name]) if aggregate_episodes else eval_metrics.episode_metrics[name]
                    )
                    for name in ['reward', 'success', 'success_easy', 'dist', 'distance_from_origin']
                }
            )

        # Track episode returns
        if "returned_episode_returns" in eval_state.info:
            returns = eval_state.info["returned_episode_returns"]
            # Assuming the same structure as in the original code
            # Adjust the slicing based on your specific environment setup
            num_adversaries = self._eval_env.env.env.env.env.num_adversaries
            mean_returns = returns[:, :num_adversaries].mean(axis=(0, 1))
            metrics["eval/mean_returned_episode_returns"] = mean_returns

        # We check in how many env there was at least one step where there was success
        if "success" in eval_metrics.episode_metrics:
            metrics["eval/episode_success_any"] = np.mean(
                eval_metrics.episode_metrics["success"] > 0.0
            )

        metrics["eval/avg_episode_length"] = np.mean(eval_metrics.episode_steps)
        metrics["eval/epoch_eval_time"] = epoch_eval_time
        metrics["eval/sps"] = self._steps_per_unroll / epoch_eval_time
        self._eval_walltime = self._eval_walltime + epoch_eval_time
        metrics = {"eval/walltime": self._eval_walltime, **training_metrics, **metrics}

        return metrics 
