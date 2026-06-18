# XProf 15-Minute Demo — GKE + DWS Flex-start on Ironwood (TPU7x)

A profiling walkthrough whose profile is captured on an **Ironwood (TPU7x)** slice provisioned through **GKE + Dynamic Workload Scheduler (flex-start)**. Covers: HLO Op Profile / Op Stats, Memory Viewer / Profile, Graph Viewer, Trace Viewer, DCN Collective Stats, and performance optimisation.

---

## 0. Decisions, identifiers, and constraints — read first

**The literal Ironwood identifiers you'll type** (this is the correction — `v7x` / `TPU7x` aren't valid tokens on their own):

| Thing | Value |
|---|---|
| Machine type | `tpu7x-standard-4t` (4 chips per VM, 2 NICs, 2 NUMA nodes) |
| Node label `cloud.google.com/gke-tpu-accelerator` | `tpu7x` |
| Topology (single host, 4 chips) | `2x2x1` |
| Chips to request (`google.com/tpu`) | `4` |
| Flex-start zone for Ironwood | `us-central1-c` |
| Min GKE version (Standard) | `1.34.0-gke.2201000` or later |

> **Verify the accelerator label on the live node.** Google's docs are briefly inconsistent — some pages show `tpu7x` for `gke-tpu-accelerator`, others show the machine type. After the node pool comes up, run `kubectl get nodes -o jsonpath='{.items[*].metadata.labels.cloud\.google\.com/gke-tpu-accelerator}'` and make the Pod's `nodeSelector` match exactly. If it's wrong the Pod sits `Pending` forever.

**Ironwood flex-start is GKE-only, and that's the point of this build.** There is no Cloud TPU VM (`queued-resources`) path for TPU7x — provisioning goes through a GKE node pool with `--flex-start`, and the workload runs as a Kubernetes Job/Pod.

**Never provision live.** A flex-start Pod sits `Pending` while DWS finds capacity — an *unbounded* wait. **Provision and capture the day before.** The 15 minutes in front of the audience is a walkthrough of a pre-captured profile loaded into XProf. Treat any live capture as optional garnish.

**The one tab a single slice can't fill: DCN Collective Stats** (a.k.a. *Megascale Stats*) only populates for a **multi-slice** workload (2+ slices over the Data Center Network). A single-host Ironwood slice produces *ICI* collectives, not DCN. Handle it by reusing a multi-slice profile you already have, or by showing the empty tab and explaining the mechanism via the Trace Viewer (script in §B6).

---

## Part A — Prep (do this the day before)

You'll run everything from **Cloud Shell** — it sidesteps the corporate-SSL/proxy issues that bite `gcloud`/`kubectl` on a managed laptop, and it has Web Preview for viewing XProf.

### A1. Cluster + Ironwood flex-start node pool

Skip the cluster step if you already have a Standard cluster (≥ 1.34.0-gke.2201000) that can place nodes in `us-central1-c`.

```bash
export PROJECT_ID=tpu-testing-2026
export REGION=us-central1
export ZONE=us-central1-c
export CLUSTER=xprof-demo
gcloud config set project ${PROJECT_ID}

# Standard cluster (needs at least one non-flex node pool to function — this default pool covers it)
gcloud container clusters create ${CLUSTER} \
  --location=${REGION} \
  --release-channel=rapid \
  --num-nodes=1 --machine-type=e2-standard-4 \
  --enable-ip-alias
# If the rapid default is below 1.34.0-gke.2201000, add: --cluster-version=1.34.1-gke.2541000
```

Add the **single-host Ironwood flex-start node pool** (autoscales 0→1; comes up only when DWS finds capacity):

```bash
export NODEPOOL=ironwood-flex
gcloud container node-pools create ${NODEPOOL} \
  --cluster=${CLUSTER} \
  --location=${REGION} \
  --node-locations=${ZONE} \
  --machine-type=tpu7x-standard-4t \
  --flex-start \
  --reservation-affinity=none \
  --enable-autoscaling --num-nodes=0 --min-nodes=0 --max-nodes=1

# confirm flex-start is on
gcloud container node-pools describe ${NODEPOOL} --cluster=${CLUSTER} \
  --location=${REGION} --format="get(config.flexStart)"     # -> True
```

> **If GKE rejects the Ironwood node pool without a placement policy** (Ironwood leans on workload policies for placement), create a single-host one and attach it:
> ```bash
> gcloud compute resource-policies create workload-policy ironwood-2x2x1 \
>   --type=HIGH_THROUGHPUT --accelerator-topology=2x2x1 --region=${REGION}
> # re-run the node-pool create above adding:  --placement-policy=ironwood-2x2x1
> ```
> and add `cloud.google.com/placement-policy-name: ironwood-2x2x1` to the Pod's `nodeSelector` in A2.
>
> **Scaling the slice / going multislice.** A bigger single slice (e.g. `2x2x2` = 8 chips = 2 VMs) means `--max-nodes=2` and `parallelism/completions: 2` in the JobSet; a true *multislice* capture — the thing that actually fills the DCN tab — means bumping the JobSet's `replicas` to the slice count. Single host keeps this demo simplest; §B6 covers the DCN story without it.

### A2. Capture the profile as a JobSet

DWS flex-start schedules **workloads**, and JobSet is the standard primitive for TPU workloads on GKE: each child Job maps to one TPU slice, so the *same* manifest scales straight from this single slice to a multislice capture by bumping `replicas`. So the capture runs as a JobSet, not a bare Pod.

Install the JobSet controller (once per cluster) and ship the script as a ConfigMap:

```bash
VERSION=v0.8.1   # or newer; see github.com/kubernetes-sigs/jobset/releases
kubectl apply --server-side \
  -f https://github.com/kubernetes-sigs/jobset/releases/download/$VERSION/manifests.yaml

kubectl create configmap capture-script --from-file=capture_profile.py
```

Apply `xprof-capture-jobset.yaml` (companion file). It runs `capture_profile.py` — a small sharded MLP fwd+bwd over a `(data×model)` mesh, on 4 Ironwood chips a `(2,2)` mesh, deliberately shaped to light up every tab (ICI collectives, big matmuls for the roofline, a real memory footprint) and intentionally **float32**, which becomes the optimisation punchline in §B7. The container captures to `/tmp`, then sleeps so you can copy the trace out. Shape of the manifest:

- `kind: JobSet` (`jobset.x-k8s.io/v1alpha2`); one `replicatedJob` named `slice`, `replicas: 1` (one slice)
- `parallelism: 1`, `completions: 1` — `2x2x1` = 4 chips = one `tpu7x` VM = one node
- same `nodeSelector` (`gke-flex-start`, `gke-tpu-accelerator: tpu7x`, `gke-tpu-topology: 2x2x1`) and `google.com/tpu: 4` as before, now inside the Job's pod template
- `alpha.jobset.sigs.k8s.io/exclusive-topology: cloud.google.com/gke-nodepool` for 1 job : 1 node pool placement

```bash
kubectl apply -f xprof-capture-jobset.yaml

# This is the unbounded DWS wait — start it EARLY.
kubectl get pods -l jobset.sigs.k8s.io/jobset-name=xprof-capture -w   # Pending (flex capacity) -> Running
kubectl get nodes -l cloud.google.com/gke-tpu-accelerator            # node appears when DWS provisions

# once Running, watch the capture finish:
POD=$(kubectl get pods -l jobset.sigs.k8s.io/jobset-name=xprof-capture -o jsonpath='{.items[0].metadata.name}')
kubectl logs -f "$POD"        # ends with "trace written to: /tmp/tb"
```

### A3. Pull the trace out

The JobSet's pod has a generated name, so select it by the JobSet label:

```bash
POD=$(kubectl get pods -l jobset.sigs.k8s.io/jobset-name=xprof-capture -o jsonpath='{.items[0].metadata.name}')
kubectl cp "$POD":/tmp/tb ./tb
ls -R ./tb     # expect plugins/profile/<timestamp>/*.xplane.pb  (+ *.trace.json.gz)
```

> **Stop billing as soon as you've got the trace:** `kubectl delete jobset xprof-capture` — the autoscaler then scales the Ironwood node back to zero. Don't leave the sleeping Job holding a TPU node overnight.
>
> **Cleaner, job-native variant — write straight to GCS** (`xprof-capture-jobset-gcs.yaml`): mount a bucket with the gcsfuse CSI driver and capture to `--logdir /data/tb`. The Job then runs to **Completion** and the profile persists (no sleep, no `kubectl cp`); view it with the `xprofiler` tool or `gcloud storage cp` it down. Needs Workload Identity + the gcsfuse driver on the cluster — the one-time setup commands are in that file's header. This is the better pattern, since a Job is meant to complete rather than sleep.

### A4. (Optional) a multi-slice profile for the DCN tab

Only if you want real DCN numbers rather than the explain-the-mechanism approach: reuse any multi-slice profile you already have, or capture one from a 2-slice run (with MaxText, just add `profiler=xplane`). As long as the run was multi-slice, the *Megascale / DCN Collective Stats* tab appears automatically.

### A5. Pre-flight the UI (don't skip)

In Cloud Shell:

```bash
pip install -U xprof
xprof --logdir ./tb --port 6006        # add --bind_all if Web Preview can't reach it
```

Click **Web Preview → Preview on port 6006**, then click through **all six** tools. Confirm charts actually render — XProf pulls the Google Charts library from `gstatic.com`, and corporate SSL inspection can blank them out. If they're blank, test on a clean network/hotspot **before** the demo. Note the exact profile-run timestamp in the top dropdown so you're not hunting for it live.

---

## Part B — The 15-minute run sheet (action → what you click → what you say)

Total ≈ 15:00. Times are cumulative targets; the quoted lines are the script. The tool walkthrough is identical no matter how the profile was captured.

### B0 · Open — 0:00–1:30
**Action:** XProf already loaded on the Overview Page (Cloud Shell Web Preview), profile-run selected.

> "This profile came off an Ironwood chip — Google's seventh-gen TPU — that I stood up on GKE through Dynamic Workload Scheduler. But the tool I'm about to show you, XProf, is the same whether you're on Ironwood, an older v5e, or even a GPU; only the numbers change. Collection overhead is under one percent because the TPU profiles itself in hardware, so this is the real workload, not a debug build. I captured a few steps of a small sharded model — let me walk you through how I'd actually find and fix a bottleneck with it."

### B1 · Overview Page — 1:30–2:45
**Action:** Stay on Overview. Point to step time, the compute-vs-input/idle breakdown, and device precision.

> "Overview is the triage page. Top line is my step time. This block shows where the time went — compute, communication, waiting on input. I can see immediately I'm compute-bound, not starved for data, so optimisation belongs on the device, not the input pipeline. And notice the precision figure: my compute is almost entirely 32-bit — hold that thought, it's the easy win at the end. Overview also gives recommendations and links straight into the deeper tools, which is where we're headed."

### B2 · HLO Op Profile + Op Stats — 2:45–5:00
**Action:** Open **HLO Op Profile**. Walk the sorted bars and the per-op efficiency column. Then switch to **HLO Op Stats** and sort by self-time.

> "Op Profile groups everything the compiler actually ran — fusions, the collectives — sorted by time. The bar is duration, but this efficiency column is the one that matters: how close each op got to the hardware's roofline. A fat bar that's *also* inefficient is exactly where my time should go. My big matmul fusions dominate and they're not saturating the MXU."

**Action:** switch to Op Stats table.

> "Op Stats is the same data as a sortable table. I sort by self-time to get the ranked offenders, each tied back to its HLO op and shape. This is my to-do list. And two of the top entries are my all-reduces — my cue to go look at communication."

### B3 · Memory Viewer + Memory Profile — 5:00–7:00
**Action:** Open **Memory Viewer**. Point to peak HBM, headroom, and the by-buffer breakdown at peak.

> "Memory Viewer shows HBM usage across the program's life and, crucially, the *contents* at peak — weights, activations, scratch. This is the first place I look for an out-of-memory, and how I decide what to shard harder or rematerialise. Ironwood has a large HBM budget per chip and I sharded weights across the model axis, so peak-per-chip leaves me plenty of room — I can grow the batch."

**Action:** switch to **Memory Profile**.

> "Memory Profile is the time dimension of the same story — allocations and frees as it runs. Sawtooth spikes are activation lifetimes; a staircase that never comes down is a leak. Clean here."

### B4 · Graph Viewer — 7:00–8:30
**Action:** Open **Graph Viewer**. Paste the op name of the top offender from B2 to centre the graph; expand a fusion.

> "Graph Viewer renders the actual HLO graph the compiler built — not my Python, the compiled reality. I don't browse it cold; I jump in from a specific op. I'll paste my worst op from Op Stats and it centres the graph there, so I can see what fused into it, what feeds it, and where the collectives sit in the dataflow. This is how I confirm *why* an op is shaped the way it is — and it's the bridge to the timeline."

### B5 · Trace Viewer — 8:30–11:00
**Action:** Open **Trace Viewer**. Zoom into one step. Click a matmul, then an all-reduce; point at gaps on the compute lane.

> "Trace Viewer is the one people live in — a real timeline, every lane a piece of the system, every block an op with a start and duration. I'll zoom to one step. The top lanes are the TPU cores doing compute; click any block and I get its exact duration and the framework op it came from. Now watch the gaps — white space on the compute lane is the TPU stalling. Here compute pauses while this all-reduce runs; that's communication not overlapping with math, and it's pure waste. The goal is to slide those collectives *underneath* the compute so the gaps close."

**Action (optional, +0:45):** if you launched the workload with `--server`, hit **Capture Profile** in the UI to grab a fresh trace live, just to show on-demand capture exists. Skip if behind schedule.

### B6 · DCN Collective Stats — 11:00–12:30

**If you loaded a multi-slice profile (§A4):** switch the dropdown to it and open **DCN Collective / Megascale Stats**.

> "When a model spans multiple slices, cross-slice traffic goes over the data-centre network, not the on-chip interconnect — a different cost model. This tab breaks down every cross-slice collective: how long it stalls and the bandwidth it actually needed. I find the one with the highest aggregated stall — that's my inter-slice bottleneck — and the fix is usually a sharding change so DCN transfers hide behind the backward-pass compute."

**If you only have the single-slice profile:** open the tab (it's empty), explain, then pivot to Trace Viewer.

> "This tab is for *multi-slice* jobs — traffic between slices over the data-centre network. My demo is a single Ironwood slice, so my collectives ran over the on-chip interconnect and this is empty by design. On a multi-slice run it lists each cross-slice collective by stall time, and the underlying send / send-done / recv / recv-done ops show up in the Trace Viewer — reading their durations there is exactly how you'd diagnose a DCN stall. Same muscle, different fabric."

### B7 · Performance optimisation — 12:30–14:30
**Action:** Open **Roofline Analysis**. Point to where the ops sit relative to the ridge point.

> "This is where it comes together. The roofline is the hardware's hard ceiling — the slope is bandwidth-limited, the flat part is compute-limited, the corner is the break-even arithmetic intensity. Every dot is an op. Dots under the slope are starved for bandwidth; dots under the flat line have the data but aren't using the compute. My matmuls sit *below* the flat ceiling — I have the data, I'm just not feeding the MXU efficiently."

**Action:** tie back to the three findings.

> "So the profile told one consistent story. One: Overview said I'm 32-bit — casting these matmuls to bf16 roughly doubles MXU throughput and is near-free on a TPU. Two: Trace Viewer showed all-reduces stalling compute — overlapping them or re-sharding closes those gaps. Three: Memory Viewer showed headroom — so I can grow the batch to raise arithmetic intensity and push these dots toward the ceiling. None of that was a guess; every number came from the hardware."

### B8 · Wrap — 14:30–15:00
> "That's the whole loop in one tool: Overview to triage, Op Profile and Op Stats to rank the offenders, Memory and Graph to understand them, Trace to see them on a timeline, the roofline to know how much room is left — and all of it from a real Ironwood run I provisioned on GKE with DWS. Happy to go deeper on any of these."

---

## Part C — Fallbacks & gotchas

- **Total safety net — the bundled demo profile.** If your cluster, capture, or copy is broken at showtime, XProf ships a sample profile. In Cloud Shell: `git clone https://github.com/openxla/xprof && xprof --logdir xprof/demo --port 6006`. Overview/Op/Memory/Trace/Graph all populate (it may lack DCN — handle that tab with the §B6 explain-the-mechanism script). Have it cloned before you walk in.
- **`no matches for kind "JobSet"`** → the JobSet controller isn't installed. Apply the release manifests (`v0.8.1` or newer) from §A2 first.
- **Pod stuck `Pending`** → the expected DWS wait *if* no node yet; provision early. If a node exists but the pod won't bind, the `nodeSelector` doesn't match the node's labels — re-check `gke-tpu-accelerator` against the live node (§0).
- **`Insufficient google.com/tpu` / can't schedule** → chip count vs topology mismatch. `2x2x1` = 4 chips, so request exactly `google.com/tpu: 4`.
- **JobSet shows `Completed` and your pod is gone before you cp'd** → with the `kubectl cp` variant the container must still be in its `sleep`; if it exited, just re-apply (capture takes seconds). Better: use the GCS variant so the artifact survives the pod.
- **`kubectl cp` fails** → the container needs `tar` on PATH (the JAX AI image has it); if not, `kubectl exec "$POD" -- tar -C /tmp/tb -cf - . | tar -xf - -C ./tb`.
- **Charts render blank** → XProf needs `gstatic.com` for Google Charts; corporate SSL inspection breaks it. Test on a clean network beforehand — this is the single most likely thing to bite you. Cloud Shell Web Preview usually avoids it.
- **`gcloud`/`kubectl` auth errors on your laptop** (incl. `Unauthorized`) → client-side credential/inspection issue, not RBAC. `gcloud container clusters get-credentials xprof-demo --location=us-central1`, and run everything from Cloud Shell to sidestep the proxy.
- **Sharding error on the `dot_general` matmul** → the script's explicit `out_sharding` is current-JAX (validated on 0.10.x, matching `jax-ai-image/tpu:latest`). If you pin an *older* image and hit this, pin a 2026 image tag instead, or ask me for the `shard_map` variant.
- **DCN tab empty** → expected on a single slice (ICI, not DCN). Not a bug; see §B6.

### Teardown
```bash
kubectl delete jobset xprof-capture --ignore-not-found
kubectl delete configmap capture-script --ignore-not-found
gcloud container node-pools delete ${NODEPOOL} --cluster=${CLUSTER} --location=${REGION} --quiet
gcloud container clusters delete ${CLUSTER} --location=${REGION} --quiet   # only if you created it for this
```

---

## Appendix — command cheat-sheet

```bash
# --- provision (Ironwood single-host, flex-start) ---
gcloud container node-pools create ironwood-flex --cluster=xprof-demo \
  --location=us-central1 --node-locations=us-central1-c \
  --machine-type=tpu7x-standard-4t --flex-start --reservation-affinity=none \
  --enable-autoscaling --num-nodes=0 --min-nodes=0 --max-nodes=1

# --- capture (JobSet) ---
VERSION=v0.8.1
kubectl apply --server-side -f https://github.com/kubernetes-sigs/jobset/releases/download/$VERSION/manifests.yaml
kubectl create configmap capture-script --from-file=capture_profile.py
kubectl apply -f xprof-capture-jobset.yaml
kubectl get pods -l jobset.sigs.k8s.io/jobset-name=xprof-capture -w   # Pending -> Running (DWS wait)
POD=$(kubectl get pods -l jobset.sigs.k8s.io/jobset-name=xprof-capture -o jsonpath='{.items[0].metadata.name}')
kubectl logs -f "$POD"                    # -> "trace written to: /tmp/tb"

# --- pull + view ---
kubectl cp "$POD":/tmp/tb ./tb
kubectl delete jobset xprof-capture       # release the TPU node
pip install -U xprof && xprof --logdir ./tb --port 6006   # Web Preview on 6006

# --- verify the accelerator label if the pod won't schedule ---
kubectl get nodes -o jsonpath='{.items[*].metadata.labels.cloud\.google\.com/gke-tpu-accelerator}'
```
