import copy
import math
import typing

import jax
from jax import lax, numpy as jnp

from src.backend import get_param, INT_OR_TUPLE, dot, matmul, conv, sum_pool, loop
from src.constants import ParallelAxes
from src.context import Context

REVERSIBLE_CTX = typing.Tuple[typing.Dict[str, jnp.ndarray], jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]


def activate(ctx, inp: jnp.ndarray) -> jnp.ndarray:
    return jax.nn.leaky_relu(inp, ctx.model.leaky_relu_slope)


def norm(ctx: Context, inp: jnp.ndarray, dims: INT_OR_TUPLE, keepdims=False) -> jnp.ndarray:
    square = jnp.square(inp).sum(dims, keepdims=keepdims)
    return lax.rsqrt(ctx.model.norm_eps + square)


def normalize(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("normalization")
    scale_param = get_param(ctx, "scale", [ctx.dims.heads, ctx.dims.one], mean=1, std=0, depth_indexing=True)
    if ctx.is_initializing:
        return inp

    @jax.custom_gradient
    def _fn(src: jnp.ndarray, param: jnp.ndarray):
        mean = src.mean(-1, keepdims=True)
        out = src - mean
        scale = norm(ctx, out, -1, True) * src.shape[-1] ** -0.5
        scaled = out * scale
        out = out * param  # no reshape needed as it's a single scalar per device)

        def _grad(dy: jnp.ndarray) -> typing.Tuple[jnp.ndarray, jnp.ndarray]:
            d_scale = (scaled * dy).sum().reshape(1, )
            dy = dy * (scale * param)
            dy -= (dy * out).mean(-1, keepdims=True) * out
            dy -= dy.mean(-1, keepdims=True)
            return dy, d_scale

        return out, _grad

    return _fn(inp, scale_param)


def pool_heads(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    return sum_pool(inp, [0, ctx.model.device_halo_size], [(0, 0), (ctx.model.device_halo_size // 2,) * 2])


def conv_weight(ctx: Context, inp: jnp.ndarray, depthwise: bool, conv_kernel: str, scale: float):
    weight = get_param(ctx, "weight", [ctx.dims.heads, ctx.dims.features_per_head,
                                       ctx.dims.one if depthwise else ctx.dims.features_per_head, conv_kernel],
                       column_axes=2, scale=scale, split_dims=[ctx.dims.depth, ctx.dims.heads], depth_indexing=True)
    if ctx.is_initializing:
        return inp
    return conv(inp, weight, [(weight.shape[-1] - 1, 0)], ctx.dims.sizes.features_per_head if depthwise else 1)


def full_conv(ctx: Context, inp: jnp.ndarray, scale: float) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("full_conv")
    return conv_weight(ctx, inp, False, ctx.dims.full_conv_kernel, scale)


def depthwise_conv(ctx: Context, inp: jnp.ndarray, scale: float) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("depthwise_conv")
    return conv_weight(ctx, inp, True, ctx.dims.depthwise_conv_kernel, scale)


def conv_block(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("group_convolution")
    inp = normalize(ctx, inp)
    mid = depthwise_conv(ctx, inp, 1 / ctx.model.activation_std)
    mid = activate(ctx, mid)
    return full_conv(ctx, mid, ctx.model.depth ** -0.5)


def feed_forward_features(ctx: Context, in_dim: str, out_dim: str) -> typing.Tuple[jnp.ndarray, jnp.ndarray]:
    inp_weight = get_param(ctx, "inp_weight", [ctx.dims.heads, in_dim, out_dim], scale=1 / ctx.model.activation_std,
                           depth_indexing=True)
    out_weight = get_param(ctx, "out_weight", [ctx.dims.heads, out_dim, in_dim], scale=ctx.model.depth ** -0.5,
                           depth_indexing=True)
    return inp_weight, out_weight


def group_feed_forward(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("group_feed_forward")
    inp_weight, out_weight = feed_forward_features(ctx, ctx.dims.features_per_head, ctx.dims.intermediate_parallel)
    inp = normalize(ctx, inp)

    if ctx.is_initializing:
        return inp

    mid = dot(inp, inp_weight, -1, 0, (), ())
    mid = activate(ctx, mid)
    out = dot(mid, out_weight, -1, 0, (), ())
    return out


def feed_forward(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("feed_forward")
    inp_weight, out_weight = feed_forward_features(ctx, ctx.dims.features_per_head, ctx.dims.intermediate_replicated)
    inp = normalize(ctx, inp)

    if ctx.is_initializing:
        return inp

    mid = dot(inp, inp_weight, -1, 0, (), ())
    mid = lax.psum(mid, ParallelAxes.model)
    mid = activate(ctx, mid)
    out = dot(mid, out_weight, -1, 0, (), ())
    return out


def one_hot(inp: jnp.ndarray, size: int) -> jnp.ndarray:
    return jnp.equal(jnp.reshape(inp, inp.shape + (1,)), jnp.reshape(jnp.arange(0, size), (1,) * inp.ndim + (size,)))


def input_embed(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("input_embed")
    inp_embd = get_param(ctx, "inp_embd", [ctx.dims.vocab, ctx.dims.heads, ctx.dims.features_per_head], column_axes=2)
    if ctx.is_initializing:
        return inp
    # TODO: Use lax.gather
    return matmul(one_hot(inp, ctx.data.vocab_size).astype(ctx.model.computation_dtype), inp_embd)


def output_embed_shard(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("output_embed")
    embd = get_param(ctx, "weight", [ctx.dims.heads, ctx.dims.features_per_head, ctx.dims.vocab])
    if ctx.is_initializing:
        return inp
    return matmul(inp, embd)


def reversible(ctx: Context, fn: typing.Callable[[Context, jnp.ndarray, jnp.ndarray], jnp.ndarray],
               src: REVERSIBLE_CTX) -> REVERSIBLE_CTX:
    if ctx.is_initializing:
        params, x00, x01, x10, x11 = src
        new_ctx = ctx.add_to_prefix("reversible")
        new_ctx.parameters = params
        out = fn(new_ctx, x10, new_ctx.depth_index)
        ctx.parameters = new_ctx.parameters
        ctx.parameter_dims = new_ctx.parameter_dims
        ctx.name_cache = new_ctx.name_cache
        ctx.prng_key = new_ctx.prng_key
        return new_ctx.parameters, x10, x11, out, x01

    name_cache = copy.deepcopy(ctx.name_cache)

    def base(params: typing.Dict[str, jnp.ndarray], inp: jnp.ndarray) -> jnp.ndarray:
        ctx.name_cache = copy.deepcopy(name_cache)
        new_ctx = ctx.add_to_prefix("reversible")
        new_ctx.parameters = params
        out = fn(new_ctx, inp, new_ctx.depth_index)
        ctx.name_cache = new_ctx.name_cache
        return out

    @jax.custom_gradient
    def _fn(params: typing.Dict[str, jnp.ndarray], x0: jnp.ndarray, back_x0: jnp.ndarray, x1: jnp.ndarray,
            back_x1: jnp.ndarray):
        def _grad(dy: REVERSIBLE_CTX) -> REVERSIBLE_CTX:
            d_params_old, dy0, y0, dy1, y1 = dy
            x0, grad_fn = jax.vjp(base, params, y0)
            d_params, dx0 = grad_fn(dy1)
            d_params = {k: d_params_old.get(k, 0) + d_params.get(k, 0) for k in d_params.keys()}
            return d_params, dy1, y1 - x0, dx0 + dy0, y0

        out = base(params, x1) + x0
        return (params, x1, x1, out, out), _grad

    return _fn(*src)


def psum_scatter(inp: jnp.ndarray) -> jnp.ndarray:
    @jax.custom_gradient
    def _fn(src: jnp.ndarray):
        def _grad(out: jnp.ndarray) -> jnp.ndarray:
            return lax.all_gather(out, ParallelAxes.model).reshape(inp.shape)

        return lax.psum_scatter(src, ParallelAxes.model), _grad

    return _fn(inp)


def cross_entropy_loss(ctx: Context, src: jnp.ndarray, tgt: jnp.ndarray) -> jnp.ndarray:
    src = src.reshape(ctx.dims.sizes.heads, -1, src.shape[-1])
    src = psum_scatter(src)  # model parallel -> data parallel for loss computation

    normalization = ctx.dims.sizes.batch / tgt.size
    tgt = one_hot(tgt.astype(src.dtype), src.shape[-1])
    shifted = src - lax.stop_gradient(src).max(-1, keepdims=True)
    exp_shifted = jnp.exp(shifted)
    sum_exp = exp_shifted.sum(-1, keepdims=True)
    out = ((jnp.log(sum_exp) - shifted) * tgt).sum(tuple(range(1, tgt.ndim)))
    return lax.pmean(out * normalization, ParallelAxes.model)


def momentumnet_main(ctx: Context, fn: typing.Callable[[Context, jnp.ndarray], jnp.ndarray]):
    def _fn(sub_ctx: Context, x: jnp.ndarray, idx: jnp.ndarray) -> jnp.ndarray:
        return fn(sub_ctx, x * (1 - ctx.model.momentumnet_beta) / ctx.model.momentumnet_beta ** (idx + 1))

    return _fn


def momentumnet_side(ctx: Context):
    def _fn(_ignored: Context, x: jnp.ndarray, idx: jnp.ndarray) -> jnp.ndarray:
        return x * ctx.model.momentumnet_beta ** idx

    return _fn


def revnet_out(src: typing.Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]) -> jnp.ndarray:
    @jax.custom_gradient
    def _fn(x0: jnp.ndarray, x0_back: jnp.ndarray, x1: jnp.ndarray, x1_back: jnp.ndarray):
        def _grad(dy) -> typing.Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
            return dy, x0, dy, x1

        return x0 + x1, _grad

    return _fn(*src)


def block(ctx: Context):
    def _fn(inp: typing.Tuple[REVERSIBLE_CTX, jnp.ndarray]) -> typing.Tuple[REVERSIBLE_CTX, jnp.ndarray]:
        src, ctx.depth_index = inp
        src = reversible(ctx, momentumnet_main(ctx, conv_block), src)
        src = reversible(ctx, momentumnet_side(ctx), src)
        ctx.depth_index += 1
        src = reversible(ctx, momentumnet_main(ctx, feed_forward), src)
        src = reversible(ctx, momentumnet_side(ctx), src)
        return src, ctx.depth_index + 1

    return _fn


def body_ctx(ctx: Context, src: jnp.ndarray) -> typing.Union[typing.Tuple[jnp.ndarray, jnp.ndarray], jnp.ndarray]:
    src = input_embed(ctx, src)
    zero = jnp.zeros_like(src)
    inp = ((ctx.parameters, src, zero, src, zero), jnp.zeros((1,), dtype=jnp.int32))
    if ctx.is_initializing:
        (ctx.parameters, _, _, _, _), _ = block(ctx)(inp)
    else:
        (ctx.parameters, _, _, _, _), _ = loop(block(ctx), inp, ctx.model.depth, ctx.model.depth_unroll)
    return output_embed_shard(ctx, revnet_out(src[1:]))


def compute(params: typing.Dict[str, jnp.ndarray], inp: jnp.ndarray) -> typing.Tuple[jnp.ndarray, jnp.ndarray]:
    ctx = Context()
    ctx.parameters = params
    src, tgt = inp
    unreduced_loss = cross_entropy_loss(ctx, body_ctx(ctx, src), tgt)
    top_loss = loss = unreduced_loss.sum() / ctx.dims.sizes.batch
    top_k = math.ceil(ctx.dims.sizes.batch * ctx.training.loss_top_p / ctx.training.loss_top_snap)
    top_k *= ctx.training.loss_top_snap
    if ctx.training.loss_top_p < 1 and top_k < ctx.dims.sizes.batch:
        top_loss, _ = lax.top_k(unreduced_loss, top_k)
        top_loss = top_loss.sum() / top_k
    return top_loss, loss
