# 0019 — Flux's own manifests come from `gen-flux.sh`, never `flux bootstrap`; controllers are tuned

- Status: Accepted
- Date: 2026-07-10, amended 2026-09-21, 2026-10-03 and 2026-10-04
- Current answer: `<cluster>/flux-system/gotk-components.yaml` and `gotk-sync.yaml` are generated
  by `scripts/gen-flux.sh` (`CLUSTER=`, `FLUX_VERSION=`), which includes the extra component
  source-watcher. A plain `flux bootstrap` would remove source-watcher and stop every component
  from deploying, so it is never used. Controller flags and limits are patches in
  `flux-system/kustomization.yaml`, sized from measurement.

## Context

The bootstrap manifests had been hand-edited, so bumping Flux or changing the controller set was
not reproducible.

## Decision

- **A script with pinned inputs** (2026-07-10) reproduces both files byte-for-byte and turns a
  Flux change into one command and a reviewed diff.
- **The version comes from the committed manifests' header** (2026-09-21), so a re-run cannot
  silently downgrade.
- **source-watcher is part of the set** (2026-10-04,
  [0047](0047-externalartifacts-per-component.md)), and the `ExternalArtifact` feature gate is on
  for kustomize- and helm-controller. A new cluster is bootstrapped by applying these files, not
  by `flux bootstrap` (README, [Bootstrapping a cluster](../../README.md#bootstrapping-a-cluster)).
- **`StrictPostBuildSubstitutions`** on kustomize-controller (2026-10-03,
  [0038](0038-plaintext-config-substituted-secrets.md)).
- **Tuning** (2026-10-03/04): the default `--concurrent=4` is too low for this many
  Kustomizations. Concurrency is 32 (kustomize), 10 (source), 8 (helm) and 24 (source-watcher),
  `--requeue-dependency` 5 s, and CPU limits the measured peaks, since more workers only wait for
  CPU at lower limits.

## Consequences

- Renovate bumps controller images inside `gotk-components.yaml`; a full regeneration at an
  older version would undo them.
- Each cluster sizes its own controllers ([0048](0048-several-clusters-one-repo.md)).

## References

- `6bb573cf`, PR #369, #546, #577, #581, #582, `cdef08e8`.
