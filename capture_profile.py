#!/usr/bin/env python3
"""
capture_profile.py  —  Generate a rich XProf profile on a single-host TPU slice.

Runs a small, sharded MLP training step in pure JAX (no Flax, to avoid version
churn during a live demo) and captures a profiler trace. The workload is
deliberately shaped to light up every XProf tool you care about:

  * 2D mesh (data x model)            -> ICI collectives in the Trace Viewer
                                         and HLO Op Profile (all-reduce / -gather)
  * several large matmuls + GELU      -> compute-bound ops for the Roofline + Op
                                         Profile / Op Stats
  * sizeable weights & activations    -> something to look at in Memory Viewer
  * a real fwd+bwd over N steps        -> step time + a graph for the Graph Viewer

NOTE on DCN / Megascale (DCN Collective) Stats: that tab ONLY appears for a
*multi-slice* workload (2+ slices talking over the Data Center Network). A
single-host slice produces ICI collectives, not DCN, so the DCN Collective Stats
tab will be empty here. See the runbook for how to handle that segment.

Usage (inside the GKE TPU pod, via the ConfigMap mount in the runbook):
    python capture_profile.py --logdir /tmp/tb --steps 8
    # then pull the trace out of the pod to view locally:
    #   kubectl cp xprof-capture:/tmp/tb ./tb
    # (or mount a GCS bucket and write straight to gs:// for sharing)

Local sanity check on CPU (no TPU needed):
    XLA_FLAGS=--xla_force_host_platform_device_count=8 \
        python capture_profile.py --smoke --logdir /tmp/tb_smoke
"""

import argparse
import time

import jax
import jax.numpy as jnp
import numpy as np
from jax.sharding import NamedSharding, PartitionSpec as P

MESH = None  # set in main(); used by loss_fn for explicit output shardings


def _mm(a, b, out_spec):
    """matmul a@b with an explicit output sharding.

    Needed because contracting over a dimension that is sharded on BOTH operands
    (the 'model' axis here) is ambiguous to the partitioner; we tell it the
    result is data-sharded, which is what forces the all-reduce collective.
    """
    dn = (((a.ndim - 1,), (0,)), ((), ()))
    return jax.lax.dot_general(a, b, dn, out_sharding=NamedSharding(MESH, out_spec))


def build_mesh():
    """(data, model) mesh over all local devices.

    Ironwood single host (tpu7x-standard-4t) -> 4 chips -> (2, 2).
    v5e-8 -> 8 chips -> (4, 2).
    Falls back to (n, 1) for any other device count (e.g. CPU smoke test).
    """
    n = jax.device_count()
    if n % 2 == 0 and n >= 2:
        data, model = n // 2, 2
    else:
        data, model = n, 1
    mesh = jax.make_mesh((data, model), ("data", "model"))
    return mesh, data, model


def make_params(key, n_layers, d_model, d_ff, mesh):
    """Weights sharded on the 'model' axis (the d_ff dimension)."""
    repl = NamedSharding(mesh, P())              # replicated
    w1_sh = NamedSharding(mesh, P(None, "model"))  # (d_model, d_ff) -> shard d_ff
    w2_sh = NamedSharding(mesh, P("model", None))  # (d_ff, d_model) -> shard d_ff

    params = []
    for _ in range(n_layers):
        key, k1, k2 = jax.random.split(key, 3)
        scale1 = (2.0 / d_model) ** 0.5
        scale2 = (2.0 / d_ff) ** 0.5
        w1 = jax.device_put(jax.random.normal(k1, (d_model, d_ff), jnp.float32) * scale1, w1_sh)
        w2 = jax.device_put(jax.random.normal(k2, (d_ff, d_model), jnp.float32) * scale2, w2_sh)
        params.append((w1, w2))
    # one extra replicated scalar just so the pytree has a replicated leaf too
    _ = repl
    return params, key


def loss_fn(params, x):
    """Stack of residual MLP blocks; the second matmul in each block contracts
    over the sharded 'model' axis, forcing a model-axis collective."""
    h = x
    for (w1, w2) in params:
        z = jax.nn.gelu(h @ w1)              # (batch, d_ff), sharded on model
        h = h + _mm(z, w2, P("data", None))  # all-reduce over model
    return jnp.mean(h * h)


@jax.jit
def train_step(params, x):
    loss, grads = jax.value_and_grad(loss_fn)(params, x)
    # plain SGD; gradients all-reduce over the data axis (batch is data-sharded)
    lr = 1e-3
    new_params = [(w1 - lr * g1, w2 - lr * g2)
                  for (w1, w2), (g1, g2) in zip(params, grads)]
    return new_params, loss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logdir", default="/tmp/tb", help="trace output dir (local or gs://)")
    ap.add_argument("--steps", type=int, default=8, help="total steps to run")
    ap.add_argument("--warmup", type=int, default=2, help="steps to run before tracing")
    ap.add_argument("--layers", type=int, default=4)
    ap.add_argument("--d-model", type=int, default=8192)
    ap.add_argument("--d-ff", type=int, default=16384)
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--server", action="store_true",
                    help="also start the on-demand profiler server on :9012")
    ap.add_argument("--smoke", action="store_true",
                    help="tiny sizes for a CPU correctness check")
    args = ap.parse_args()

    if args.smoke:
        args.layers, args.d_model, args.d_ff, args.batch = 2, 256, 512, 64
        args.steps, args.warmup = 3, 1

    print(f"JAX {jax.__version__} | devices={jax.device_count()} "
          f"({jax.devices()[0].platform})")
    global MESH
    MESH, data, model = build_mesh()
    mesh = MESH
    print(f"mesh: data={data} x model={model}")

    if args.server:
        # lets you trigger on-demand capture from the XProf/TensorBoard UI
        jax.profiler.start_server(9012)
        print("on-demand profiler server listening on :9012")

    key = jax.random.PRNGKey(0)
    params, key = make_params(key, args.layers, args.d_model, args.d_ff, mesh)

    x_sh = NamedSharding(mesh, P("data", None))
    key, kx = jax.random.split(key)
    x = jax.device_put(
        jax.random.normal(kx, (args.batch, args.d_model), jnp.float32), x_sh)

    # warmup: triggers compilation so the captured steps are steady-state
    for _ in range(args.warmup):
        params, loss = train_step(params, x)
    jax.block_until_ready(loss)
    print(f"warmup done (loss={float(loss):.4f}); tracing {args.steps} steps...")

    # ---- the captured region ----
    jax.profiler.start_trace(args.logdir)
    t0 = time.time()
    for i in range(args.steps):
        params, loss = train_step(params, x)
    jax.block_until_ready(loss)   # ensure on-device work is captured
    dt = time.time() - t0
    jax.profiler.stop_trace()
    # ------------------------------

    print(f"captured {args.steps} steps in {dt*1e3:.1f} ms "
          f"({dt/args.steps*1e3:.2f} ms/step), final loss={float(loss):.4f}")
    print(f"trace written to: {args.logdir}")
    print("view it:  xprof --logdir %s --port 6006" % args.logdir)


if __name__ == "__main__":
    main()
