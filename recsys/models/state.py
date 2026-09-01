from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from typing import Any

import torch

DTYPES: dict[str, torch.dtype] = {
    "bool": torch.bool,
    "float16": torch.float16,
    "float32": torch.float32,
    "float64": torch.float64,
    "int8": torch.int8,
    "int16": torch.int16,
    "int32": torch.int32,
    "int64": torch.int64,
    "uint8": torch.uint8,
}


def encode_tensor(tensor: torch.Tensor) -> dict[str, Any]:
    value = tensor.detach().cpu().contiguous()
    dtype_name = str(value.dtype).removeprefix("torch.")
    if dtype_name not in DTYPES:
        raise TypeError(f"unsupported tensor dtype: {value.dtype}")
    raw = bytes(value.reshape(-1).view(torch.uint8).tolist())
    return {
        "dtype": dtype_name,
        "shape": list(value.shape),
        "data_base64": base64.b64encode(raw).decode("ascii"),
    }


def decode_tensor(value: Mapping[str, Any]) -> torch.Tensor:
    if set(value) != {"dtype", "shape", "data_base64"}:
        raise ValueError("tensor document has unknown or missing fields")
    dtype_name = value["dtype"]
    shape = value["shape"]
    payload = value["data_base64"]
    if not isinstance(dtype_name, str) or dtype_name not in DTYPES:
        raise ValueError("unsupported tensor dtype")
    if not isinstance(shape, list) or any(
        isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 0
        for dimension in shape
    ):
        raise ValueError("invalid tensor shape")
    if not isinstance(payload, str):
        raise ValueError("tensor payload must be base64 text")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid tensor base64") from exc
    dtype = DTYPES[dtype_name]
    item_size = torch.empty((), dtype=dtype).element_size()
    expected = item_size
    for dimension in shape:
        expected *= dimension
    if len(raw) != expected:
        raise ValueError("tensor byte length does not match shape/dtype")
    tensor = torch.frombuffer(bytearray(raw), dtype=dtype).clone()
    return tensor.reshape(shape)


def encode_state_dict(state: Mapping[str, torch.Tensor]) -> dict[str, dict[str, Any]]:
    return {name: encode_tensor(state[name]) for name in sorted(state)}


def decode_state_dict(value: Mapping[str, Any]) -> dict[str, torch.Tensor]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("state dict must be a non-empty object")
    return {str(name): decode_tensor(document) for name, document in value.items()}


def _json_optimizer_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, tuple | list):
        return [_json_optimizer_value(row) for row in value]
    raise TypeError(f"unsupported optimizer metadata type: {type(value).__name__}")


def encode_optimizer_state(optimizer: torch.optim.Optimizer) -> dict[str, Any]:
    state = optimizer.state_dict()
    encoded_state = []
    for parameter_id in sorted(state["state"]):
        values = state["state"][parameter_id]
        encoded_state.append(
            {
                "parameter_id": int(parameter_id),
                "values": {
                    str(name): (
                        {"kind": "tensor", "value": encode_tensor(value)}
                        if isinstance(value, torch.Tensor)
                        else {"kind": "scalar", "value": _json_optimizer_value(value)}
                    )
                    for name, value in sorted(values.items())
                },
            }
        )
    groups = [
        {str(name): _json_optimizer_value(value) for name, value in sorted(group.items())}
        for group in state["param_groups"]
    ]
    return {"schema_version": "1.0", "state": encoded_state, "param_groups": groups}


def decode_optimizer_state(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {"schema_version", "state", "param_groups"}:
        raise ValueError("optimizer state has unknown or missing fields")
    if value["schema_version"] != "1.0":
        raise ValueError("unsupported optimizer state schema")
    raw_state = value["state"]
    groups = value["param_groups"]
    if not isinstance(raw_state, list) or not isinstance(groups, list):
        raise ValueError("optimizer state/groups must be arrays")
    decoded: dict[int, dict[str, Any]] = {}
    for row in raw_state:
        if not isinstance(row, dict) or set(row) != {"parameter_id", "values"}:
            raise ValueError("invalid optimizer parameter state")
        parameter_id = row["parameter_id"]
        values = row["values"]
        if isinstance(parameter_id, bool) or not isinstance(parameter_id, int) or parameter_id < 0:
            raise ValueError("invalid optimizer parameter ID")
        if parameter_id in decoded or not isinstance(values, dict):
            raise ValueError("duplicate or invalid optimizer parameter state")
        decoded_values: dict[str, Any] = {}
        for name, document in values.items():
            if not isinstance(document, dict) or set(document) != {"kind", "value"}:
                raise ValueError("invalid optimizer state value")
            if document["kind"] == "tensor":
                decoded_values[str(name)] = decode_tensor(document["value"])
            elif document["kind"] == "scalar":
                decoded_values[str(name)] = document["value"]
            else:
                raise ValueError("invalid optimizer state value kind")
        decoded[parameter_id] = decoded_values
    if any(not isinstance(group, dict) for group in groups):
        raise ValueError("optimizer param group must be an object")
    return {"state": decoded, "param_groups": groups}
