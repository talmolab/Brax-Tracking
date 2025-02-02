import dataclasses
from typing import Any, Callable, Sequence, Tuple
import warnings

from brax.training import networks
from brax.training import types
from brax.training import distribution
from brax.training.networks import MLP
import brax.training.agents.ppo.networks as ppo_networks
from brax.training.types import PRNGKey

import jax
import jax.numpy as jnp
from jax import random

import flax
from flax import linen as nn


class VariationalLayer(nn.Module):
    latent_size: int

    @nn.compact
    def __call__(self, x):
        mean_x = nn.Dense(self.latent_size, name="mean")(x)
        logvar_x = nn.Dense(self.latent_size, name="logvar")(x)

        return mean_x, logvar_x


class MLP(nn.Module):
    """MLP with Layer Norm"""

    layer_sizes: Sequence[int]
    activation: networks.ActivationFn = nn.relu
    kernel_init: networks.Initializer = jax.nn.initializers.lecun_uniform()
    activate_final: bool = False
    bias: bool = True

    @nn.compact
    def __call__(self, x: jnp.ndarray):
        for i, hidden_size in enumerate(self.layer_sizes):
            x = nn.Dense(
                hidden_size,
                name=f"hidden_{i}",
                kernel_init=self.kernel_init,
                use_bias=self.bias,
            )(x)
            if i != len(self.layer_sizes) - 1 or self.activate_final:
                x = self.activation(x)
                x = nn.LayerNorm()(x)
        return x


def reparameterize(rng, mean, logvar):
    std = jnp.exp(0.5 * logvar)
    eps = random.normal(rng, logvar.shape)
    return mean + eps * std


class IntentionNetwork(nn.Module):
    """Full VAE model, encode -> decode with sampled actions"""

    encoder_layers: Sequence[int]
    decoder_layers: Sequence[int]
    reference_obs_size: int
    latents: int = 60

    def setup(self):
        self.encoder = MLP(layer_sizes=self.encoder_layers, activate_final=True)
        self.latent = VariationalLayer(latent_size=self.latents)
        self.decoder = MLP(layer_sizes=self.decoder_layers)

    def __call__(self, obs, key):
        _, encoder_rng = jax.random.split(key)
        traj = obs[..., : self.reference_obs_size]
        latent_mean, latent_logvar = self.latent(self.encoder(traj))
        z = reparameterize(encoder_rng, latent_mean, latent_logvar)
        action = self.decoder(
            jnp.concatenate([z, obs[..., self.reference_obs_size :]], axis=-1)
        )

        return action, (latent_mean, latent_logvar)


class EncoderDecoderNetwork(nn.Module):
    """encoder and decoder, no sampling in bottleneck."""

    encoder_layers: Sequence[int]
    decoder_layers: Sequence[int]
    reference_obs_size: int
    latents: int = 60

    def setup(self):
        self.encoder = MLP(layer_sizes=self.encoder_layers, activate_final=True)
        self.decoder = MLP(layer_sizes=self.decoder_layers)

    def __call__(self, obs, key):
        traj = obs[..., : self.reference_obs_size]
        z = nn.Dense(self.latents, name="bottleneck")(self.encoder(traj))
        action = self.decoder(
            jnp.concatenate([z, obs[..., self.reference_obs_size :]], axis=-1)
        )

        return action, z


def make_intention_policy(
    param_size: int,
    latent_size: int,
    total_obs_size: int,
    reference_obs_size: int,
    preprocess_observations_fn: types.PreprocessObservationFn = types.identity_observation_preprocessor,
    encoder_hidden_layer_sizes: Sequence[int] = (1024, 1024),
    decoder_hidden_layer_sizes: Sequence[int] = (1024, 1024),
) -> IntentionNetwork:
    """Creates an intention policy network."""

    policy_module = IntentionNetwork(
        encoder_layers=list(encoder_hidden_layer_sizes),
        decoder_layers=list(decoder_hidden_layer_sizes) + [param_size],
        reference_obs_size=reference_obs_size,
        latents=latent_size,
    )

    def apply(processor_params, policy_params, obs, key):
        obs = preprocess_observations_fn(obs, processor_params)
        return policy_module.apply(policy_params, obs=obs, key=key)

    dummy_total_obs = jnp.zeros((1, total_obs_size))
    dummy_key = jax.random.PRNGKey(0)

    return networks.FeedForwardNetwork(
        init=lambda key: policy_module.init(key, dummy_total_obs, dummy_key),
        apply=apply,
    )


def make_encoderdecoder_policy(
    param_size: int,
    latent_size: int,
    total_obs_size: int,
    reference_obs_size: int,
    preprocess_observations_fn: types.PreprocessObservationFn = types.identity_observation_preprocessor,
    encoder_hidden_layer_sizes: Sequence[int] = (1024, 1024),
    decoder_hidden_layer_sizes: Sequence[int] = (1024, 1024),
) -> IntentionNetwork:
    """Creates an intention policy network."""

    policy_module = EncoderDecoderNetwork(
        encoder_layers=list(encoder_hidden_layer_sizes),
        decoder_layers=list(decoder_hidden_layer_sizes) + [param_size],
        reference_obs_size=reference_obs_size,
        latents=latent_size,
    )

    def apply(processor_params, policy_params, obs, key):
        obs = preprocess_observations_fn(obs, processor_params)
        return policy_module.apply(policy_params, obs=obs, key=key)

    dummy_total_obs = jnp.zeros((1, total_obs_size))
    dummy_key = jax.random.PRNGKey(0)

    return networks.FeedForwardNetwork(
        init=lambda key: policy_module.init(key, dummy_total_obs, dummy_key),
        apply=apply,
    )
