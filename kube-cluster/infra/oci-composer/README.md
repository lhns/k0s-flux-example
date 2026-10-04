# oci-composer

[kube-oci-composer](https://github.com/lhns/kube-oci-composer) — assembles OCI artifacts from
content-addressed inputs and pushes them to its own registry, so a workload can get files its
upstream image does not ship without a custom image build, a static PV mounted over the path, or
an initContainer that clones something at runtime.

First consumer: [`../../apps/freshrss`](../../apps/freshrss/README.md).

## How a workload names an artifact

By the digest-pinned ref the artifact exports. Nothing names a tag.

- A component's `ImageComposition` and `ImageBuild` objects live in its `artifacts/` directory,
  in the component's own namespace, and each sets `push.writeRefTo` into the namespace of the
  component's Kustomizations, its control namespace `gen-<name>`, which `refExport.namespaces`
  must list. The objects' own
  Namespace goes in the component's `namespaces/`, so it exists before `artifacts/` applies.
  Namespace `oci-composer` is legacy and still accepted.
- After every publish the controller writes `<registry>/<namespace>/<name>@sha256:…` into
  ConfigMap `<kind>-<namespace>-<name>` there, labelled `reconcile.fluxcd.io/watch: Enabled`
  (`refExport` in `release.yaml`), so a new digest rolls its consumer at once.
- The component substitutes from each of those ConfigMaps (`optional: false`) and writes the image
  as `${KEY}`. A missing ConfigMap, or a key nobody exports, fails the reconcile: kustomize-controller
  substitutes strictly. Nothing is applied with an empty image.
- `artifacts/` is its own Flux Kustomization with `wait: true`, and the component depends on it.
  Both depend on `namespaces/`. The generator's comment in `templates/kustomizations.yaml` says
  why each of those is needed.

What that costs:

- **The deployed digest is state outside git.** A revert commit rebuilds rather than restores: a
  composition usually lands on its old digest, an `ImageBuild` on a new one. `operator.keepBuilds`
  keeps older builds pullable, but nothing points a workload back at them.
- **A failing build holds its component back**, because the component waits for `artifacts/` to be
  Ready. Pods keep the last digest they were given.
- **The whole component is substituted.** Every other `${…}` or `$$` in it has to be escaped or
  opted out; see `templates/kustomizations.yaml`.
- **Offline rendering cannot resolve the refs.** `scripts/check-substitution.py` counts every key an
  `artifacts/` object exports as defined, and fails an artifact that exports nothing, sits outside
  `artifacts/`, or is in a namespace that is neither `oci-composer` nor declared in the component's
  `namespaces/`.

## Build pods run in the object's namespace

An `ImageBuild`'s Job runs where the object is, and dials two things in `oci-composer`: the
builder's context endpoint (port 8090; source fetches are proxied through it) and the registry
(port 5000, push).

- **Network: nothing to add per namespace.** kube-vnet restricts ingress only, and the chart's
  own NetworkPolicies, `oci-composer-builder-context` and `oci-composer-registry`, admit every
  namespace on exactly those ports. `release.yaml` pins them on. A kube-vnet network would be
  broader: membership is not port-scoped. **Exception:** a namespace whose own Egress policy
  selects every pod (`apps/github-runner`) also needs one for
  `app.kubernetes.io/managed-by: kube-oci-builder` in `namespaces/`; only there, since anywhere
  else it would cut the build off the internet. `scripts/check-substitution.py` enforces both.
- **Pod security: the namespace must allow privileged pods.** Build pods run seccomp and
  AppArmor `Unconfined` with privilege escalation, so a namespace enforcing the `baseline` or
  `restricted` Pod Security level rejects them.

## Every `sourceRef` must pin `revision`

A pin bump moves the source and the composition in one commit, but the composition's watch can fire
before the `GitRepository`'s own update lands. The source is then self-consistent at the previous
generation, so nothing about it looks wrong: the composition builds the OLD content, exports it, and
the consumer rolls onto it until the source catches up.

`sourceRef.revision` closes it: the layer stays `Pending` until the artifact is at the named
revision. `operator.requirePinnedSources` (and `imageBuild.`) makes it mandatory.

The value is the revision source-controller publishes for that pin:

| pin | revision |
| --- | --- |
| `ref.commit: <sha>` | `sha1:<sha>` |
| `ref.tag: <tag>` | `<tag>` — the ref half matches whatever commit the tag resolves to |

A wrong value is `Pending` forever, so `scripts/check-source-pins.py` asserts the two agree — in
`validate.sh`, and as a Renovate `postUpgradeTasks` fixer, since Renovate sees only the pin.

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

Spegel owns its directory outright and deletes anything it did not write. Sharing one root means
this drop-in is destroyed on every Spegel restart — silently, since Spegel serves artifacts a node
already has and only a new digest fails.

**Order is load-bearing.** Spegel writes `_default`, which matches *every* registry, and containerd
stops at the first root that matches anything. This root must stay first, and must never gain a
`_default` of its own.

- **NodePort 30500, not a MetalLB VIP.** It spends no LAN address, and the drop-in is needed
  either way — containerd defaults to HTTPS for any `host:port` and has to be told otherwise. All three
  nodes are listed with **no `server` line**, so there is no primary: containerd tries them in
  order and falls through, and `server` would only have made one node a distinguished fallback.
- **Node IPs never appear in an image reference.** Every reference is
  `oci-composer.internal:30500/oci-composer/<name>@sha256:<digest>`; the addresses live only in the
  drop-in.
  That is what keeps references portable and reviewable.

## Availability

The controller does not serve; the registry Deployment does, and it is a single pod. Its NodePort
answers on every node, so no *node* is a single point of failure, but the registry pod restarting
or being drained stops pulls for as long as it takes to reschedule.

That is a real dependency, because pulls are **frequent, not rare**: every rebuild publishes a new
digest, and a new digest is a new pull on every node running the workload.

**Spegel does not mirror this.** It manages the `_default` directory, which containerd consults
only for registries with no directory of their own — and this one has one. So peer-to-peer
mirroring is not a fallback here.

## Verification

```sh
kubectl -n oci-composer get helmrelease,pod
kubectl get imagecomposition,imagebuild -A
kubectl -n flux-system get configmap -l app.kubernetes.io/managed-by=kube-oci-composer
# from a node that is NOT running the pod — proves the NodePort hop and that containerd
# never tried to resolve oci-composer.internal:
k0s ctr -n k8s.io images pull --hosts-dir /etc/k0s/registries/oci-composer   oci-composer.internal:30500/oci-composer/<name>@sha256:<digest>
# and the regression test that matters: restart that node's spegel pod, then confirm
# /etc/k0s/registries/oci-composer/oci-composer.internal_30500_/hosts.toml still exists
```
