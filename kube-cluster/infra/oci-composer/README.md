# oci-composer

[kube-oci-composer](https://github.com/lhns/kube-oci-composer) — assembles OCI artifacts from
content-addressed inputs and serves them from its own read-only registry, so a workload can get
files its upstream image does not ship without a custom image build, a static PV mounted over the
path, or an initContainer that clones something at runtime.

First consumer: [`../../apps/freshrss`](../../apps/freshrss/README.md).

## When to use it

**Only when the inputs are not already images** — git repositories (including `subpath` selection
out of a monorepo), jars fetched by URL, ConfigMaps. Something has to turn those into OCI content,
and nothing else here will.

If the content is *already* a published image, mount it directly as an image volume instead;
`subPath` covers layout, and wrapping it in a composition only re-wraps an artifact that already
exists. See **Getting files into a workload** in the [root README](../../../README.md) for the
rule and both worked examples.

## The thing to understand first: who pulls

**Image pulls are performed by the kubelet on each node, never by the pod that mounts the result.**
Everything awkward about this deployment follows from that:

- **kube-vnet does not apply.** NetworkPolicy governs pod traffic; this is the node talking.
- **Cluster DNS is unusable.** Nodes resolve with the host resolver, which knows nothing of
  `*.svc.cluster.local`. A ClusterIP *name* could never work here.
- **`*.kube.example.com` is worse than useless.** It resolves to `203.0.113.10`, the public edge — so a
  hostname there would send every internal image pull out of the LAN and back again, through a box
  that has nothing to do with this.

## How it is actually reached

`registry.host: oci-composer.internal:30500`, which **resolves nowhere**. Each node's containerd
is told where it really is, by a registry drop-in shipped in
[`../../k0s-files/`](../../k0s-files/) via `k0sctl.yaml`:

```toml
[host."http://10.20.2.72:30500"]
  capabilities = ["pull", "resolve"]
# ...and .73, .74 — no `server` line, so no node is a distinguished fallback
```

That mechanism is not new here: `k0s-files/registries.toml` sets `registry.config_path`, which
containerd is already reading for Spegel.

**It lives in its own root, and that is not tidiness.** `config_path` lists two directories,
searched left to right:

```
/etc/k0s/registries/oci-composer   <- this drop-in; hand-written; ours
/etc/k0s/registries/spegel         <- Spegel clears and rewrites this on every pod start
```

Spegel owns its directory outright and deletes anything it did not write. When the two shared one,
this drop-in was destroyed on every Spegel restart — silently, since Spegel's peer-to-peer
mirroring served artifacts a node already had and only a brand-new tag ever failed. It broke
composed pulls on 17 Aug 2026 and again during testing.

**Order is load-bearing.** Spegel writes `_default`, which matches *every* registry, and containerd
stops at the first root that matches anything. This root must stay first, and must never gain a
`_default` of its own.

- **NodePort 30500, not a MetalLB VIP.** It spends no LAN address, and the drop-in is needed
  either way — containerd defaults to HTTPS for any `host:port` and has to be told otherwise. All three
  nodes are listed with **no `server` line**, so there is no primary: containerd tries them in
  order and falls through, and `server` would only have made one node a distinguished fallback.
- **Node IPs never appear in an image reference.** Every reference is
  `oci-composer.internal:30500/<namespace>/<name>:<tag>`; the addresses live only in the drop-in.
  That is what keeps references portable and reviewable.

## Availability

**Both replicas serve.** `replicaCount: 2` with `operator.storage.shared: true`, so the registry is
not a single point of failure: losing a pod, or draining the node under it, leaves the other
answering pulls. Publishing, garbage collection and status writes stay leader-only — serving is
read-only, so several replicas answering is safe.

This matters more than it first looks. Pulls are **frequent, not rare**: with spec-hash tags every
change to a composition produces a new tag, and a new tag is a new pull on every node running the
workload. A registry that stops answering when one pod moves is a poor dependency for something
like Kafka, where a broker restarting mid-outage could not fetch its plugins and would fail to
start.

Two things make it work, and the second is easy to miss:

- **Shared blobs** — the cephfs RWX PVC. Asserted with `storage.shared`, because a process handed a
  directory cannot tell whether it is node-local or a shared mount.
- **Shared manifests** — these live in the registry's *in-memory* map rather than the store, so
  shared blobs alone would leave a standby answering 404 for content sitting right there. Every
  replica refills that map from `status.history` on an interval.

The chart **fails** on `replicaCount > 1` without shared storage, rather than handing over a
standby that serves nothing.

**Spegel still does not mirror this.** It manages the `_default` directory, which containerd
consults only for registries with no directory of their own — and this one has one. So peer-to-peer
mirroring is not a fallback here; the replicas are.

## Verification

```sh
kubectl -n oci-composer get helmrelease,pod
# from a node that is NOT running the pod — proves the NodePort hop and that containerd
# never tried to resolve oci-composer.internal:
k0s ctr -n k8s.io images pull --hosts-dir /etc/k0s/registries/oci-composer   oci-composer.internal:30500/<namespace>/<name>:<tag>
# and the regression test that matters: restart that node's spegel pod, then confirm
# /etc/k0s/registries/oci-composer/oci-composer.internal_30500_/hosts.toml still exists
```
