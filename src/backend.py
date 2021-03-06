import typing

import jax
import jax._src.util as util
import numpy as np
from jax import lax, numpy as jnp, random

from .constants import ParallelAxes
from .context import Context

INT_OR_TUPLE = typing.Union[int, typing.Sequence[int]]


def is_main():
    return jax.process_index() == 0


def stable_rsqrt(inp: jnp.ndarray, eps: float) -> jnp.ndarray:
    return jnp.reciprocal(jnp.maximum(jnp.sqrt(jnp.maximum(inp, 0)), eps))


def pos_dim(inp: jnp.ndarray, dims: typing.Sequence[int]) -> typing.Sequence[int]:
    return tuple([d % inp.ndim for d in dims])


def tuple_int(obj: INT_OR_TUPLE) -> typing.Sequence[int]:
    if isinstance(obj, (tuple, list)):
        return tuple(obj)
    if isinstance(obj, int):
        return obj,
    raise ValueError


def sum_pool(inputs: jnp.ndarray, window_shape: typing.List[int],
             padding: typing.List[typing.Tuple[int, int]]) -> jnp.ndarray:
    strides = (1,) * (len(window_shape) + 2)
    dims = (1,) + tuple(window_shape) + (1,)
    padding = ((0, 0),) + tuple(padding) + ((0, 0),)
    return lax.reduce_window(inputs, 0, lax.add, dims, strides, padding)


def conv(inp: jnp.ndarray, weight: jnp.ndarray, padding: typing.List[typing.Tuple[int, int]], groups: int):
    ndim = weight.ndim
    dimension_numbers = (0, ndim - 1) + tuple(range(1, ndim - 1))
    dimension_numbers = lax.ConvDimensionNumbers(dimension_numbers, tuple(range(ndim)), dimension_numbers)
    return lax.conv_general_dilated(inp, weight, (1,) * (ndim - 2), padding=padding, feature_group_count=groups,
                                    dimension_numbers=dimension_numbers, precision='fastest')


def device_id(ctx: Context):
    return (lax.psum_scatter(jnp.arange(ctx.dims.heads), ParallelAxes.model) / ctx.dims.heads).astype(jnp.int32)


def dot(left: jnp.ndarray, right: jnp.ndarray, left_contract_dims: INT_OR_TUPLE, right_contract_dims: INT_OR_TUPLE,
        left_batch_dims: INT_OR_TUPLE = tuple(), right_batch_dims: INT_OR_TUPLE = tuple()) -> jnp.ndarray:
    dims = ((pos_dim(left, tuple_int(left_contract_dims)), pos_dim(right, tuple_int(right_contract_dims))),
            (pos_dim(left, tuple_int(left_batch_dims)), pos_dim(right, tuple_int(right_batch_dims))))
    return lax.dot_general(left, right, dims, "fastest")


def matmul(left: jnp.ndarray, right: jnp.ndarray, reduced_dims=1):
    return dot(left, right, tuple(range(-reduced_dims, 0)), tuple(range(reduced_dims)))


def prefixed_name(ctx: Context, name: str):
    return ctx.add_to_prefix(name, count=False).global_prefix


def assign(ctx: Context, name: str, inp: jnp.ndarray):
    name = prefixed_name(ctx, name)
    ctx.parameters[name] = inp


def normal(ctx: Context, shape: typing.Sequence[int]):
    ctx.prng_key, key = random.split(ctx.prng_key)
    return random.normal(key, shape, ctx.model.storage_dtype)


def orthogonal_init(ctx: Context, shape: typing.List[int], column_axes=(-1,)) -> jnp.ndarray:
    axes = tuple([shape[c] for c in column_axes])
    n_rows, n_cols = util.prod(shape) // util.prod(axes), util.prod(axes)
    matrix_shape = (n_rows, n_cols) if n_rows > n_cols else (n_cols, n_rows)
    out, r = jnp.linalg.qr(normal(ctx, matrix_shape))
    out *= lax.broadcast_to_rank(jnp.sign(jnp.diag(r)), rank=out.ndim)
    if n_rows < n_cols:
        out = out.T
    return jnp.reshape(out, tuple(np.delete(shape, column_axes)) + axes).astype(ctx.model.storage_dtype)


def get_param(ctx: Context, name: str, shape: typing.Optional[typing.List[int]] = None,
              std: typing.Optional[float] = None, mean: typing.Optional[float] = None, column_axes: int = 1,
              scale: float = 1., post_variance_scale: float = 1,
              lr_scale: float = 1, dtype: typing.Optional[jnp.float32] = None,
              init_val: typing.Optional[jnp.ndarray] = None) -> jnp.ndarray:
    prefix_name = prefixed_name(ctx, name)

    if dtype is None:
        computation_dtype = ctx.model.computation_dtype
        storage_dtype = ctx.model.storage_dtype
    else:
        computation_dtype = dtype
        storage_dtype = dtype

    if prefix_name not in ctx.parameters:
        if init_val is not None:
            param = init_val * scale * post_variance_scale
        elif std is None and mean is None:
            param = orthogonal_init(ctx, shape, range(len(shape) - column_axes, len(shape)))
            param *= scale * post_variance_scale
        else:
            param = normal(ctx, shape) * scale
            if std is not None:
                param *= std
            if mean is not None:
                param += mean
        ctx.parameter_variance[prefix_name] = lr_scale * scale
        param = param.astype(storage_dtype)
        assign(ctx, name, param)
    param = ctx.parameters[prefix_name]
    return param.astype(computation_dtype)


def zero_param(ctx: Context, name: str, shape: typing.List[int], dtype: typing.Optional[jnp.dtype]) -> jnp.ndarray:
    return get_param(ctx, name, shape, 0, 0, dtype=dtype)


def loop(fn: typing.Callable, fn_input: typing.Any, steps: int, unroll: int = 1):
    return lax.scan(lambda *x: (fn(*x[:-1]), None), fn_input, None, steps, unroll=unroll)[0]
