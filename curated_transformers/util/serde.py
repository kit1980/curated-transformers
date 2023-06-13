import re
from abc import ABC, abstractmethod
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Set, Union

import torch
from torch.nn import Module, Parameter

from .pytorch import ModuleIterator, apply_to_module

# Args: Parent module, module prefix, parameter name, tensor to convert, device.
# Returns the new paramater.
TensorToParameterConverterT = Callable[
    [Module, str, str, torch.Tensor, Optional[torch.device]], Parameter
]

# Args: State dict.
# Returns the converted state dict.
HFStateDictConverterT = Callable[
    [Mapping[str, torch.Tensor]], Mapping[str, torch.Tensor]
]


class DeserializationParamBucket(Dict, ABC):
    """Used to group parameters that need to be deserialized at the same time."""

    @abstractmethod
    def match(self, param_key: str) -> bool:
        """Returns True if the parameter key matches this bucket.

        :param param_key:
            Key extracted from a state dict.
        :returns:
            `True` if it's a match, `False` otherwise.
        """
        ...

    @abstractmethod
    def ready(self) -> bool:
        """Returns True if this bucket is ready for deserialization."""
        ...


class DefaultParamBucket(DeserializationParamBucket):
    """Default bucket that is always ready and matches all keys."""

    def match(self, param_key: str) -> bool:
        return True

    def ready(self) -> bool:
        return True


class RegExParameterBucket(DeserializationParamBucket):
    """Groups parameters whose keys match a given regular expression."""

    key_matcher: re.Pattern
    expected_keys: Set[str]

    def __init__(self, pattern: re.Pattern, expected_keys: Set[str]) -> None:
        super().__init__()

        self.key_matcher = pattern
        self.expected_keys = expected_keys

    def match(self, param_key: str) -> bool:
        return self.key_matcher.search(param_key) is not None

    def ready(self) -> bool:
        if len(self.expected_keys) != len(self):
            return False

        seen = 0
        for expected in self.expected_keys:
            for key in self.keys():
                if expected in key:
                    seen += 1
                    break
        return seen == len(self.expected_keys)


def load_model_from_checkpoints(
    model: Module,
    *,
    filepaths: Iterable[str],
    deserialization_buckets: List[DeserializationParamBucket],
    state_dict_converter: HFStateDictConverterT,
    tensor_to_param_converter: Optional[TensorToParameterConverterT] = None,
    device: Optional[torch.device] = None,
):
    """Load parameters from PyTorch checkpoints with minimal copies.

    :param model:
        PyTorch module into which the parameters are to be loaded.
    :param filepaths:
        Paths to PyTorch checkpoints.
    :param deserialization_buckets:
        Model-specific buckets into which parameters are to be sorted
        before deserialization.
    :param state_dict_converter:
        Callback to convert Hugging Face state dicts to the
        `curated-transformers` format.
    :param tensor_to_param_converter:
        Callback to perform custom conversions of the loaded parameters.
        Useful for loading quantized weights.
    :param device:
        Device in which to place the loaded parameters.
    """

    def convert_and_load_state_dict(
        state_dict: Mapping[str, torch.Tensor], seen_keys: Set[str]
    ):

        converted = state_dict_converter(state_dict)
        if len(converted) == 0:
            return
        seen_keys.update(converted.keys())

        # We have to walk the module tree for each state dict as there
        # are no guarantees on the ordering of the keys.
        _emplace_module_state_dict(
            model,
            converted,
            tensor_to_param_converter=tensor_to_param_converter,
            device=device,
        )

    state_dicts = _load_state_dicts_from_checkpoints(filepaths)
    # We need to cache the model's parameter keys before loading the state
    # dicts as the process could potentially change the structure of sub-modules,
    # e.g: when quantized layers rename their parameters.
    module_keys = set(model.state_dict().keys())
    seen_keys: Set[str] = set()

    # Ideally, we'd lazily load and convert the state dicts in the incoming
    # order, but some of the conversion operations induce dependencies between
    # parameters in different state dicts. So, we need to sort them into buckets
    # before performing the conversion ops, precluding us from "streaming" the
    # parameters into the model. Consequently, this requires us to hold the
    # incomplete buckets (and their parameters) in memory until all of their
    # parameters have been accumulated.
    default_bucket = DefaultParamBucket()
    for state_dict in state_dicts:
        # Sort into buckets.
        for name, param in state_dict.items():
            matching_buckets = [
                bucket for bucket in deserialization_buckets if bucket.match(name)
            ]
            num_matching_buckets = len(matching_buckets)
            if num_matching_buckets == 0:
                default_bucket[name] = param
            elif num_matching_buckets == 1:
                matching_buckets[0][name] = param
            else:
                raise ValueError(
                    f"Key `{name}` matched multiple ({num_matching_buckets}) deserialization buckets"
                )

        # Convert and load the default bucket first.
        convert_and_load_state_dict(default_bucket, seen_keys)
        default_bucket.clear()

        # Process other buckets that are full.
        for bucket in deserialization_buckets:
            if bucket.ready():
                convert_and_load_state_dict(bucket, seen_keys)
                bucket.clear()

    # Make sure that we didn't miss any keys.
    missing_keys = module_keys.difference(seen_keys)
    if len(missing_keys) != 0:
        raise ValueError(f"Some parameters were not updated/replaced: {missing_keys}")

    if any(len(bucket) for bucket in deserialization_buckets):
        raise ValueError("One or more deserialization buckets were not empty")


def default_tensor_to_parameter_converter(
    module: Module,
    module_prefix: str,
    parameter_name: str,
    tensor: torch.Tensor,
    device: Optional[torch.device] = None,
) -> Parameter:
    """Default tensor to parameter converter.

    :param module:
        Parent module of the parameter being converted/replaced.
    :param module_prefix:
        Prefix of the parent module.
    :param parameter_name:
        Name of the parameter being converted/replaced.
    :param tensor:
        Tensor to be converted.
    :param device:
        Device to which the converted parameter is moved.
    :returns:
        Converted parameter.
    """
    old_param = module._parameters[parameter_name]
    assert old_param is not None
    _validate_replacement(old_param, tensor, module_prefix)
    return Parameter(tensor, requires_grad=old_param.requires_grad).to(device=device)  # type: ignore


def _load_state_dicts_from_checkpoints(
    filepaths: Iterable[str],
) -> Iterable[Mapping[str, torch.Tensor]]:
    for path in filepaths:
        # Map to CPU first to support all devices.
        state_dict = torch.load(
            path, map_location=torch.device("cpu"), weights_only=True
        )
        yield state_dict


def _emplace_module_state_dict(
    module: Module,
    state_dict: Mapping[str, torch.Tensor],
    *,
    tensor_to_param_converter: Optional[TensorToParameterConverterT] = None,
    device: Optional[torch.device] = None,
):
    if tensor_to_param_converter is None:
        tensor_to_param_converter = default_tensor_to_parameter_converter

    def apply(itr: ModuleIterator):
        prefix_with_dot = f"{itr.prefix}."
        candidate_tensors = {
            k: v for k, v in state_dict.items() if k.startswith(prefix_with_dot)
        }
        if len(candidate_tensors) == 0:
            return

        local_params_and_buffers: Dict[
            str, Union[Optional[Parameter], Optional[torch.Tensor]]
        ] = dict(itr.module._parameters.items())
        for name, buf in itr.module._buffers.items():
            if name in local_params_and_buffers:
                raise KeyError(
                    f"Key `{name}` used in both learnable parameters and buffers in module `{itr.prefix}`"
                )
            elif name not in itr.module._non_persistent_buffers_set:
                local_params_and_buffers[name] = buf

        for name, param in local_params_and_buffers.items():
            key = f"{prefix_with_dot}{name}"
            if key not in candidate_tensors:
                continue
            elif param is None:
                raise ValueError(
                    f"Key `{name}` found in state dict but no data in module `{itr.prefix}`"
                )
            replacement = candidate_tensors[key]
            assert tensor_to_param_converter is not None
            _emplace_module_tensor(
                module=itr.module,
                module_prefix=itr.prefix,
                tensor_name=name,
                replacement_tensor=replacement,
                tensor_to_param_converter=tensor_to_param_converter,
                device=device,
            )

    apply_to_module(module, apply)


def _emplace_module_tensor(
    module: Module,
    module_prefix: str,
    tensor_name: str,
    replacement_tensor: torch.Tensor,
    tensor_to_param_converter: TensorToParameterConverterT,
    device: Optional[torch.device] = None,
):
    """Replaces a module's parameter or (persistent) buffer with the passed tensor and moves it
    to the given device. This is a zero-copy operation (excluding D2H/H2D transfers) where the
    input tensor is directly associated with the module. Unexpected behaviour can occur if the same
    tensor is associated with multiple modules.
    """
    is_parameter = tensor_name in module._parameters
    is_buffer = tensor_name in module._buffers
    assert is_parameter ^ is_buffer

    if is_parameter:
        new_param = tensor_to_param_converter(
            module, module_prefix, tensor_name, replacement_tensor, device
        )
        module._parameters[tensor_name] = new_param
    else:
        old_buffer = module._buffers[tensor_name]
        assert old_buffer is not None
        _validate_replacement(
            old_buffer, replacement_tensor, f"{module_prefix}.{tensor_name}"
        )
        module._buffers[tensor_name] = replacement_tensor


def _validate_replacement(
    replaced: Union[Parameter, torch.Tensor],
    replacement: torch.Tensor,
    name: str,
):
    if replaced.shape != replacement.shape:
        raise ValueError(
            f"Expected size of replacement for `{name}` to be {replaced.shape}, but got {replacement.shape}"
        )
    elif replaced.dtype != replacement.dtype:
        raise ValueError(
            f"Expected dtype of replacement for `{name}` to be {replaced.dtype}, but got {replacement.dtype}"
        )