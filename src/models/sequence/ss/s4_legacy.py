if __name__ == "__main__":
    import sys
    import pathlib

    p = pathlib.Path().absolute()
    print("Adding path: ", p)
    sys.path.append(str(p))

import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.utils as U
from einops import rearrange, repeat
from omegaconf import DictConfig
import opt_einsum as oe
import numpy as np

optimized = True

if optimized:
    contract = oe.contract
else:
    contract = torch.einsum

from src.models.sequence.ss.kernel import HippoSSKernel, _conj
from src.models.nn import LinearActivation, Activation


class S4(nn.Module):
    requires_length = True

    def __init__(
        self,
        d_model,
        d_state=64,
        l_max=1,  # Maximum length of sequence. Fine if not provided: the kernel will keep doubling in length until longer than sequence. However, this can be marginally slower if the true length is not a power of 2
        channels=1,  # maps 1-dim to C-dim
        bidirectional=False,
        # Arguments for FF
        activation="gelu",  # activation in between SS and FF
        postact=None,  # activation after FF. Setting to 'glu' usually improves performance
        hyper_act=None,  # Use a "hypernetwork" multiplication
        dropout=0.0,
        transposed=True,  # axis ordering (B, L, D) or (B, D, L)
        verbose=False,
        poly=False,
        # SSM Kernel arguments
        **kernel_args,
    ):
        """
        d_state: the dimension of the state, also denoted by N
        l_max: the maximum sequence length, also denoted by L
          if this is not known at model creation, set l_max=1
        channels: can be interpreted as a number of "heads"
        bidirectional: bidirectional
        dropout: standard dropout argument
        transposed: choose backbone axis ordering of (B, L, H) or (B, H, L) [B=batch size, L=sequence length, H=hidden dimension]

        Other options are all experimental and should not need to be configured
        """

        super().__init__()
        if verbose:
            import src.utils.train

            log = src.utils.train.get_logger(__name__)
            log.info(f"Constructing S4 (H, N, L) = ({d_model}, {d_state}, {l_max})")

        self.h = d_model
        self.n = d_state
        self.bidirectional = bidirectional
        self.channels = channels
        self.transposed = transposed

        # optional multiplicative modulation GLU-style
        # https://arxiv.org/abs/2002.05202
        self.hyper = hyper_act is not None
        if self.hyper:
            channels *= 2
            self.hyper_activation = Activation(hyper_act)

        self.D = nn.Parameter(torch.randn(channels, self.h))

        if self.bidirectional:
            channels *= 2

        self.poly = poly
        print(f"Poly={poly}")
        # SSM Kernel
        # kernel_args["mode"] = "slow"
        self.kernel = HippoSSKernel(
            self.h, N=self.n, L=l_max, channels=channels, verbose=verbose, **kernel_args
        )
        # Pointwise
        self.activation = Activation(activation)
        dropout_fn = nn.Dropout2d if self.transposed else nn.Dropout
        self.dropout = dropout_fn(dropout) if dropout > 0.0 else nn.Identity()

        # position-wise output transform to mix features
        self.output_linear = LinearActivation(
            self.h * self.channels,
            self.h,
            transposed=self.transposed,
            activation=postact,
            activate=True,
        )

    def forward(
        self, u, state=None, **kwargs
    ):  # absorbs return_output and transformer src mask
        """
        u: (B H L) if self.transposed else (B L H)
        state: (H N) never needed unless you know what you're doing

        Returns: same shape as u
        """
        if not self.transposed:
            u = u.transpose(-1, -2)
        L = u.size(-1)

        # Compute SS Kernel
        k, k_state = self.kernel(L=L, state=state)  # (C H L) (B C H L)

        # Convolution
        if self.bidirectional:
            k0, k1 = rearrange(k, "(s c) h l -> s c h l", s=2)
            k = F.pad(k0, (0, L)) + F.pad(k1.flip(-1), (L, 0))
        k_f = torch.fft.rfft(k, n=2 * L)  # (C H L)
        u_f = torch.fft.rfft(u, n=2 * L)  # (B H L)
        y_f = contract(
            "bhl,chl->bchl", u_f, k_f
        )  # k_f.unsqueeze(-4) * u_f.unsqueeze(-3) # (B C H L)
        y = torch.fft.irfft(y_f, n=2 * L)[..., :L]  # (B C H L)

        # Compute D term in state space equation - essentially a skip connection
        y = y + contract(
            "bhl,ch->bchl", u, self.D
        )  # u.unsqueeze(-3) * self.D.unsqueeze(-1)

        # print(f"kernel.log_dt: {self.kernel.log_dt.size()}")  # [32]
        # print(f"kernel.w: {self.kernel.w.size()}")  # [32]
        # print(f"kernel.B: {self.kernel.B.size()}")  # [32]
        # print(f"kernel.C: {self.kernel.C.size()}")  # [1, 256, 32]
        # print(f"u: {u.size()}")  # [50, 256, 784]
        # print(f"y: {y.size()}")  # [50, 1, 256, 784]
        # print(f"y_f: {y_f.size()}")  # [50, 1, 256, 785]
        # print(f"u_f: {u_f.size()}")  # [50, 256, 785]
        # print(f"k: {k.size()}")  # [1, 256, 784]
        # print(f"k_f: {k_f.size()}")  # [1, 256, 785]
        # cb = contract("d,bcd->bcd", self.kernel.B, self.kernel.C)
        if self.poly:
            us = torch.nn.functional.pad(u[..., :-1], (1, 0), "constant", 0)
            us = us * u
            dt = torch.exp(self.kernel.log_dt.to(u.device))
            B = _conj(self.kernel.B).to(u.device)
            dC = _conj(self.kernel.C).to(u.device)
            w = _conj(self.kernel.w).to(u.device)
            dB = torch.diag_embed(1.0 / (1.0 - 0.5 * dt[:, None] * w))  #  (256,64,64)
            dB = dt[:, None] * contract("dab,b->da", dB, B)
            dB1 = dB.unsqueeze(2)
            dB2 = dB.unsqueeze(1)
            dB = (dB1 * dB2).sum(2)
            dCB = contract("abc,bc->ab", dC, dB).unsqueeze(2)
            if self.bidirectional:
                fwd, bwd = dCB.unbind(0)
                fwd, bwd = fwd.unsqueeze(0), bwd.unsqueeze(0)
                y = (
                    y
                    + (us * fwd).unsqueeze(1).float()
                    + (us.flip(2) * bwd).unsqueeze(1).float()
                )
            else:
                y = y + (us * dCB).unsqueeze(1).float()

        # d_state (self.n) = 64
        # d_model (self.h) = 256

        # Compute state update
        if state is not None:
            assert (
                not self.bidirectional
            ), "Bidirectional not supported with state forwarding"
            y = y + k_state
            next_state = self.kernel.forward_state(u, state)
        else:
            next_state = None

        # Optional hyper-network multiplication
        if self.hyper:
            y, yh = rearrange(y, "b (s c) h l -> s b c h l", s=2)
            y = self.hyper_activation(yh) * y

        # Reshape to flatten channels
        y = rearrange(y, "... c h l -> ... (c h) l")

        y = self.dropout(self.activation(y))

        if not self.transposed:
            y = y.transpose(-1, -2)

        y = self.output_linear(y)

        return y, next_state

    def step(self, u, state):
        """Step one time step as a recurrent model. Intended to be used during validation.

        u: (B H)
        state: (B H N)
        Returns: output (B H), state (B H N)
        """
        assert not self.training

        y, next_state = self.kernel.step(u, state)  # (B C H)
        y = y + u.unsqueeze(-2) * self.D
        y = rearrange(y, "... c h -> ... (c h)")
        y = self.activation(y)
        raise ValueError("Not implemented")
        if self.transposed:
            y = self.output_linear(y.unsqueeze(-1)).squeeze(-1)
        else:
            y = self.output_linear(y)
        return y, next_state

    def default_state(self, *batch_shape, device=None):
        return self.kernel.default_state(*batch_shape)

    @property
    def d_state(self):
        return self.h * self.n

    @property
    def d_output(self):
        return self.h

    @property
    def state_to_tensor(self):
        return lambda state: rearrange("... h n -> ... (h n)", state)