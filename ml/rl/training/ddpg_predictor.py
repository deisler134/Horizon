#!/usr/bin/env python3

from copy import deepcopy

import numpy as np
import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F
from torch.autograd import Variable


class DDPGPredictor(object):
    def __init__(self, trainer) -> None:
        self.action_range = trainer.env_details.action_range
        # Init actor network, actor target network, actor optimizer
        self.actor = ActorNet(
            trainer.actor_params.layers, trainer.actor_params.activations,
            trainer.final_layer_init
        )
        self.actor_target = deepcopy(self.actor)
        self.actor_optimizer = trainer.optimizer_func(
            self.actor.parameters(), lr=trainer.actor_params.learning_rate
        )
        self.noise = trainer.noise_generator

        # Init critic network, critic target network, critic optimizer
        self.critic = CriticNet(
            trainer.critic_params.layers, trainer.critic_params.activations,
            trainer.final_layer_init, trainer.env_details.action_dim
        )
        self.critic_target = deepcopy(self.critic)
        self.critic_optimizer = trainer.optimizer_func(
            self.critic.parameters(),
            lr=trainer.critic_params.learning_rate,
            weight_decay=trainer.critic_params.l2_decay
        )

    def predict_action(self, states, noisy=False) -> np.ndarray:
        """ Returns list of actions output from actor network
        :param states states as list of states to produce actions for
        """
        examples = []
        for state in states:
            example = np.zeros([len(state)], dtype=np.float32)
            for k, v in state.items():
                example[k] = v
            examples.append(example)

        self.actor.eval()
        with torch.no_grad():
            state_examples = Variable(torch.from_numpy(np.array(examples)))
            actions = self.actor(state_examples)
        self.actor.train()
        actions = actions.data.numpy()

        if noisy:
            actions = [x + (self.noise.get_noise()) for x in actions]

        # Continuous action space
        if self.action_range:
            return np.array(
                [
                    self.action_range[1] * np.clip(action, -1, 1)
                    for action in actions
                ],
                dtype=np.float32
            )
        # Discrete action space
        return np.array(actions, dtype=np.float32)

    @classmethod
    def export_actor(cls, trainer):
        return DDPGPredictor(trainer)


class ActorNet(nn.Module):
    def __init__(self, layers, activations, fl_init) -> None:
        super(ActorNet, self).__init__()
        self.layers: nn.ModuleList = nn.ModuleList()
        self.batch_norm_ops: nn.ModuleList = nn.ModuleList()
        self.activations = activations

        assert (len(layers) >=
                3), 'Invalid layer schema {} for actor network'.format(layers)

        for i, layer in enumerate(layers[1:]):
            self.layers.append(nn.Linear(layers[i], layer))
            self.batch_norm_ops.append(nn.BatchNorm1d(layers[i]))
            # If last layer use simple uniform init (as outlined in DDPG paper)
            if i + 1 == len(layers[1:]):
                init.uniform_(self.layers[i].weight, -fl_init, fl_init)
                init.uniform_(self.layers[i].bias, -fl_init, fl_init)
            # Else use fan in uniform init (as outlined in DDPG paper)
            else:
                fan_in_init(self.layers[i].weight)

    def forward(self, state) -> torch.FloatTensor:
        """ Forward pass for actor network. Assumes activation names are
        valid pytorch activation names.
        :param state state as list of state features
        """
        x = state
        for i, activation in enumerate(self.activations):
            x = self.batch_norm_ops[i](x)
            activation_func = getattr(F, activation)
            fc_func = self.layers[i]
            x = fc_func(x) if activation == 'linear' else activation_func(
                fc_func(x)
            )
        return x


class CriticNet(nn.Module):
    def __init__(self, layers, activations, fl_init, action_dim) -> None:
        super(CriticNet, self).__init__()
        self.layers: nn.ModuleList = nn.ModuleList()
        self.batch_norm_ops: nn.ModuleList = nn.ModuleList()
        self.activations = activations

        assert (len(layers) >=
                3), 'Invalid layer schema {} for actor network'.format(layers)

        for i, layer in enumerate(layers[1:]):
            # Batch norm only applied to pre-action layers
            if i == 0:
                self.layers.append(nn.Linear(layers[i], layer))
                self.batch_norm_ops.append(nn.BatchNorm1d(layers[i]))
            elif i == 1:
                self.layers.append(nn.Linear(layers[i] + action_dim, layer))
                self.batch_norm_ops.append(nn.BatchNorm1d(layers[i]))
            # Actions skip input layer
            else:
                self.layers.append(nn.Linear(layers[i], layer))

            # If last layer use simple uniform init (as outlined in DDPG paper)
            if i + 1 == len(layers[1:]):
                init.uniform_(self.layers[i].weight, -fl_init, fl_init)
                init.uniform_(self.layers[i].bias, -fl_init, fl_init)
            # Else use fan in uniform init (as outlined in DDPG paper)
            else:
                fan_in_init(self.layers[i].weight)

    def forward(self, state, action) -> torch.FloatTensor:
        """ Forward pass for critic network. Assumes activation names are
        valid pytorch activation names.
        :param state state as list of state features
        :param state action as list of action features
        """
        x = state
        for i, activation in enumerate(self.activations):
            if i == 0:
                x = self.batch_norm_ops[i](x)
            # Actions skip input layer
            elif i == 1:
                x = self.batch_norm_ops[i](x)
                x = torch.cat((x, action), dim=1)
            activation_func = getattr(F, activation)
            fc_func = self.layers[i]
            x = fc_func(x) if activation == 'linear' else activation_func(
                fc_func(x)
            )
        return x


def fan_in_init(tensor) -> None:
    """ Fan in initialization as described in DDPG paper."""
    val_range = 1. / np.sqrt(tensor.size(1))
    init.uniform_(tensor, -val_range, val_range)