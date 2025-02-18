# -*- coding: utf-8 -*-

# (C) Copyright 2020, 2021, 2022 IBM. All Rights Reserved.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""High level analog tiles (inference)."""

from copy import deepcopy
from typing import List, Optional, Union, TYPE_CHECKING

from torch import device as torch_device
from torch import ones, zeros, Tensor
from torch.autograd import no_grad

from aihwkit.exceptions import CudaError
from aihwkit.simulator.rpu_base import cuda
from aihwkit.simulator.tiles.analog import AnalogTile

if TYPE_CHECKING:
    from aihwkit.simulator.configs import InferenceRPUConfig
    from aihwkit.simulator.tiles import BaseTile

# pylint: disable=too-many-instance-attributes


class InferenceTile(AnalogTile):
    """Tile used for analog inference and hardware-aware training for inference.

    Args:
        out_size: output size
        in_size: input size
        rpu_config: resistive processing unit configuration.
        bias: whether to add a bias column to the tile.
        in_trans: Whether to assume an transposed input (batch first)
        out_trans: Whether to assume an transposed output (batch first)
        shared_weights: Whether to keep the weight in torch's memory space
    """

    def __init__(
            self,
            out_size: int,
            in_size: int,
            rpu_config: Optional['InferenceRPUConfig'] = None,
            bias: bool = False,
            in_trans: bool = False,
            out_trans: bool = False,
            shared_weights: bool = True,
    ):

        if not rpu_config:
            # Import `InferenceRPUConfig` dynamically to avoid import cycles.
            # pylint: disable=import-outside-toplevel
            from aihwkit.simulator.configs import InferenceRPUConfig
            rpu_config = InferenceRPUConfig()

        # Noise model.
        self.noise_model = deepcopy(rpu_config.noise_model)

        # Drift compensation.
        self.drift_compensation = None
        if rpu_config.drift_compensation:
            self.drift_compensation = deepcopy(rpu_config.drift_compensation)
        self.drift_baseline = None
        self.drift_readout_tensor = None  # type: Optional[Tensor]
        self.alpha = ones((1,))

        # Helpers.
        self.reference_combined_weights = None  # type: Optional[Tensor]
        self.programmed_weights = None  # type: Optional[Tensor]
        self.nu_drift_list = None  # type: Optional[List[Tensor]]

        super().__init__(out_size, in_size, rpu_config, bias, in_trans, out_trans)

        if shared_weights:
            self.shared_weights = zeros(out_size, in_size + int(bias),
                                        requires_grad=True)  # type: Tensor
            self.ensure_shared_weights()

    @no_grad()
    def _forward_drift_readout_tensor(self) -> Optional[Tensor]:
        """Perform a forward pass using the drift read-out tensor."""
        if self.drift_compensation is None:
            return None

        if self.drift_readout_tensor is None:
            self.drift_readout_tensor = self.drift_compensation.get_readout_tensor(
                self.tile.get_x_size()).detach().to(self.device)
            if self.in_trans:
                self.drift_readout_tensor = self.drift_readout_tensor.tranpose(0, 1).clone()

        # We need to take the bias as a common column here, also we do
        # not want to use indexed.
        return self.tile.forward(self.drift_readout_tensor, False,
                                 self.in_trans, self.out_trans, True)

    @no_grad()
    def program_weights(self, from_reference: bool = True) -> None:
        """Apply weights noise to the current tile weights and saves these for
        repeated drift experiments.

        This method also establishes the drift coefficients for each
        conductance slice.

        Args:
            from_reference: Whether to use weights from reference
        """
        if not from_reference or self.reference_combined_weights is None:
            self.reference_combined_weights = Tensor(self.tile.get_weights())

        self.programmed_weights, self.nu_drift_list = self.noise_model.apply_programming_noise(
            self.reference_combined_weights)

        if self.drift_compensation is not None:
            self.tile.set_weights(self.programmed_weights.numpy())
            forward_output = self._forward_drift_readout_tensor()

            self.drift_baseline = self.drift_compensation.init_baseline(forward_output)

    @no_grad()
    def drift_weights(
            self,
            t_inference: float = 0.0
    ) -> None:
        """Programs and drifts the current reference weights.

        The current weight reference is either the current weights or
        the ones at the time when :meth:`initialize_drift_reference`
        was called, which then would overwrite the current weights
        with the drifted ones.

        Args:
            t_inference: Time (in sec) of assumed inference
                time. Programming ends at t=0s.  The rest is waiting time,
                where the devices might drift and accumulate noise. See
                noise model used for details.
        """
        # pylint: disable=arguments-differ,arguments-renamed
        if self.programmed_weights is None:
            self.program_weights()

        drifted_weights = self.noise_model.apply_drift_noise(
            self.programmed_weights, self.nu_drift_list, t_inference)
        self.tile.set_weights(drifted_weights.detach().cpu().numpy())

        if self.drift_compensation is not None:
            forward_output = self._forward_drift_readout_tensor()
            self.alpha = self.drift_compensation.apply(forward_output,
                                                       self.drift_baseline).to(self.device)

    def forward(self, x_input: Tensor, is_test: bool = False) -> Tensor:
        """Forward pass with drift compensation.

        Note:
            The drift compensation scale will only be applied during
            testing, ie if ``is_test=True``.
        """
        # Import `aihwkit.simulator.configs` items dynamically to avoid import cycles.
        # pylint: disable=import-outside-toplevel
        from aihwkit.simulator.configs.helpers import parameters_to_bindings
        from aihwkit.simulator.configs.utils import WeightModifierType

        if not is_test and (self.rpu_config.modifier.type != WeightModifierType.COPY or
                            self.rpu_config.modifier.pdrop > 0.0):
            weight_modify_params = parameters_to_bindings(self.rpu_config.modifier)
            self.tile.modify_weights(weight_modify_params)

        if not is_test or self.drift_compensation is None:
            return super().forward(x_input, is_test)

        # only do drift compensation in eval mode
        return super().forward(x_input, True)*self.alpha

    @no_grad()
    def post_update_step(self) -> None:
        """Operators that need to be called once per mini-batch."""
        # Import `aihwkit.simulator.configs` items dynamically to avoid import cycles.
        # pylint: disable=import-outside-toplevel
        from aihwkit.simulator.configs.helpers import parameters_to_bindings
        from aihwkit.simulator.configs.utils import WeightClipType

        super().post_update_step()

        # TODO: make this a little nicer. Now each time bindings are generated.
        if self.rpu_config.clip.type != WeightClipType.NONE:
            weight_clip_params = parameters_to_bindings(self.rpu_config.clip)
            self.tile.clip_weights(weight_clip_params)

    def cuda(
            self,
            device: Optional[Union[torch_device, str, int]] = None
    ) -> 'BaseTile':
        """Return a copy of this tile in CUDA memory.

        Args:
            device: CUDA device

        Returns:
            Self with the underlying C++ tile moved to CUDA memory.

        Raises:
            CudaError: if the library has not been compiled with CUDA.
        """
        super().cuda(device)

        self.alpha = self.alpha.cuda(device)
        self.shared_weights.data = zeros(self.tile.get_x_size(),
                                         self.tile.get_d_size(),
                                         requires_grad=True).cuda(device)
        self.ensure_shared_weights()

        return self


class CudaInferenceTile(InferenceTile):
    """Analog inference tile (CUDA).

    Analog inference tile that uses GPU for its operation. The instantiation is based on
    an existing non-cuda tile: all the source attributes are copied except
    for the simulator tile, which is recreated using a GPU tile.

    Caution:
        Deprecated. Use ``InferenceTile(..).cuda()`` instead.

    Args:
        source_tile: tile to be used as the source of this tile
    """

    def __init__(self, source_tile: AnalogTile):
        if not cuda.is_compiled():
            raise CudaError('aihwkit has not been compiled with CUDA support')

        # Create a new instance of the rpu config.
        new_rpu_config = deepcopy(source_tile.rpu_config)

        # Create the tile, replacing the simulator tile.
        super().__init__(source_tile.out_size, source_tile.in_size, new_rpu_config,
                         source_tile.bias, source_tile.in_trans, source_tile.out_trans,
                         shared_weights=True)

        self.cuda(self.device)
