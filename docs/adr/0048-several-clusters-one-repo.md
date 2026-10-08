# 0048 — Several clusters in one repo: a directory per cluster, shared bases

- Status: Accepted
- Date: 2026-10-04, amended 2026-10-05
- Current answer: a cluster is any top-level directory with `flux-system/kustomization.yaml`;
  scripts and CI find clusters by that. Each cluster has its own `flux-system/`, root release and
  root ArtifactGenerator copying only its own directory, `k0sctl.yaml`, and age key. Components
  more than one cluster deploys are plain kustomize bases under `shared/`, pulled in by a
  per-cluster overlay with `flux-component-generator/bases`. Versions live in the overlays, never
  in a base. `kube-cluster/` is prod; `kube-test-cluster/` is scaffolded, not yet bootstrapped.

## Context

The generator was never tied to the directory name: groups are `base` paths in the root
release's values. The second cluster is where a version runs before prod's reviewed PR.

One repo keeps it simple: it is self-contained, with the whole setup in one place. Monorepos are
not preferred for code, but the cluster is one coherent system; with several clusters this may be
revisited.

## Decision

- **Per-cluster pieces** that cannot be shared: the sync path, the root copy rules (naming only
  `<cluster>/` paths, or every root release would carry every cluster's files), groups and
  `k0sUpgrade`, Flux and its tuning, the cluster itself, an age key and `.sops.yaml` rule.
  `check-clusters.py` enforces them.
- **`shared/` holds plain bases**: no generator annotations, no reserved directories, no SOPS
  files, since a base reaches every cluster and each cluster decrypts only its own secrets.
- **`bases`** copies the listed `shared/` directories into the component's artifact at their repo
  paths, so `../` resolves there; only `shared/` is accepted. Two components generating one name
  fail the render.
- **Versions in overlays**, as a patch shaped like a whole HelmRelease or OCIRepository (which
  Renovate's flux manager reads) or kustomize `images:`, because an edit to a base reaches every
  cluster in the same push.
- **What becomes a base**: a component that runs, or will run, in both clusters with mostly the
  same content and differences that fit small overlay patches. Traefik is not one yet (the shared
  part would be little more than the chart reference); storage, Postgres, NATS, backups,
  telemetry and the composer stay per cluster.
- **Promotion by Renovate rule**: minor and patch updates automerge under `kube-test-cluster/`,
  so a version runs there before prod's reviewed PR.
- reloader, k0s-upgrade, reflector, kube-vnet, cert-manager and metallb are bases, with prod's
  builds byte-identical to before.

## Alternatives considered

- One Flux managing both clusters: prod would become a single point of failure and hold every
  cluster's key.
- A repo per cluster: declined for now. It costs a release pipeline, a versioned chart and
  duplicated CI, worth it only if the generator becomes a product of its own or stronger
  isolation is needed.

## Consequences

- **Never `flux bootstrap`**; a cluster is bootstrapped by applying its generated manifests (README,
  [Bootstrapping a cluster](../../README.md#bootstrapping-a-cluster)).
- On a fresh cluster an operator and its custom resources must be separate components: one
  Kustomization holding both fails its dry-run before the CRDs exist (prod's operators were
  installed by k0s first).
- Every check and the deploy check run per cluster, with that cluster's kubeconfig and key.

## References

- PR #626-#630, #643, #646, `494d5fbd`.
- Conversation 2026-10-04 (design, base candidates), 2026-10-05 (why one repo).
- README: [Multiple clusters](../../README.md#multiple-clusters);
  [kube-test-cluster/README.md](../../kube-test-cluster/README.md).
