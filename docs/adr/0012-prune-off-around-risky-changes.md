# 0012 — Prune is off around any change that can drop objects

- Status: Accepted
- Date: 2026-07-08, amended 2026-09-17, 2026-10-03 and 2026-10-04
- Current answer: a change that can make a Kustomization build fewer objects, or delete a
  Kustomization, runs with prune off: `generate.pruneDisabled` (names or `"*"`) in the root
  release's values, one PR per step, `scripts/check-migration.py` green between steps, revert
  while prune is still off on any deviation. A change to the root release itself also sets
  `generate.keepGenerated: true`. Objects whose loss is never acceptable carry
  `kustomize.toolkit.fluxcd.io/prune: disabled` permanently.

## Context

Kustomizations prune, and they are named after directories
([0004](0004-one-kustomization-per-component-from-a-root-chart.md)). Deleting one deletes what it
applied, PVCs included, and a deleted dynamic PV is reclaimed
([0008](0008-pv-reaper-split-reclaim.md)). Every generator change, rename and group move risks
exactly that. Renaming a Kustomization that installs a chart can also uninstall the chart's CRDs and
with them every custom resource of that kind.

## Decision

- **Adopt in place** (2026-07-04, documented 2026-07-08): mark the objects
  `prune: disabled`, let the old owner go, let the new owner adopt them under the same names,
  then remove the mark. A Kustomization deleted with `prune: false` leaves its objects, and one
  recreated under the same name adopts them with the same UID.
- **`pruneDisabled` as a generator value** (2026-10-03, PR #545), so a whole migration can turn
  prune off for all of a component's Kustomizations from one place.
- **A read-only gate** (`check-migration.py`): `snapshot`, `verify-prune-disabled`,
  `verify-moved` (same UIDs and owners, PVCs, PVs and namespaces unchanged, no pod restarted),
  `verify-done`. It has an offline test in `validate.sh`.
- **`keepGenerated`** (2026-10-04, PR #606): around a root-release change, Helm keeps every
  `gen-` release and ArtifactGenerator even if the root were reinstalled. Not permanent, since it
  also stops Helm deleting a removed component's.
- **Permanent opt-outs:** static PVs (2026-09-03) and everything in `namespaces/`
  ([0045](0045-namespaces-dir-never-pruned.md)).

## Consequences

- Big moves take three or more PRs, each on a deployed, verified state (control namespaces
  #547-#555; ExternalArtifacts #593-#599; the root chart #606-#608).
- A Kustomization with prune off orphans what leaves its build; restoring prune is a step of its
  own and must not be forgotten.

## References

- `b225f0a6`, `fee7f54f`, `7777b883`..`a1b0edf4`, `91e2eaa8`, `9059442f`, PR #545, #584, #606,
  #610.
- Conversation 2026-07-08 (the postgres move), 2026-07-30 (chart CRDs).
- README: [Control namespaces and risky changes](../../README.md#control-namespaces-and-risky-changes),
  [Moving a component between groups](../../README.md#moving-a-component-between-groups).
