"""Decode agibot_gdk BundleTensor payloads.

The pybind layer exposes every tensor's payload as raw bytes (a uint8 numpy
array); the element type is carried separately in ``tensor.dtype``
(BundleDType). Verified live 2026-07-16: state__joint__position arrives as
112 raw bytes that view() to 14 float64, string tensors are NUL-terminated
concatenated byte strings.
"""

from __future__ import annotations

import numpy as np

import agibot_gdk as gdk

_NP_BY_DTYPE = {
    gdk.BundleDType.FLOAT16: np.float16,
    gdk.BundleDType.FLOAT32: np.float32,
    gdk.BundleDType.FLOAT64: np.float64,
    gdk.BundleDType.INT8: np.int8,
    gdk.BundleDType.INT16: np.int16,
    gdk.BundleDType.INT32: np.int32,
    gdk.BundleDType.INT64: np.int64,
    gdk.BundleDType.UINT8: np.uint8,
    gdk.BundleDType.UINT16: np.uint16,
    gdk.BundleDType.UINT32: np.uint32,
    gdk.BundleDType.UINT64: np.uint64,
}


def decode_tensor(tensor) -> "np.ndarray | list[str]":
    """Return a typed numpy array (or list of str for STRING tensors)."""
    raw = np.asarray(tensor.data, dtype=np.uint8)
    if tensor.dtype == gdk.BundleDType.STRING:
        # NUL-terminated strings, one per shape element.
        parts = raw.tobytes().split(b"\x00")
        return [p.decode("utf-8", "replace") for p in parts if p]
    np_dtype = _NP_BY_DTYPE.get(tensor.dtype)
    if np_dtype is None:
        return raw.copy()
    arr = raw.view(np_dtype)
    shape = list(tensor.shape)
    if shape and int(np.prod(shape)) == arr.size:
        arr = arr.reshape(shape)
    return arr.copy()


def decode_scalar_u64(tensor) -> int:
    """Fast path for timestamp__camera__* tensors (single uint64)."""
    return int(np.asarray(tensor.data, dtype=np.uint8).view(np.uint64)[0])


def to_jsonable(value) -> object:
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value
