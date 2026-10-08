# 0045 — `namespaces/`: a component's Namespace has its own Kustomization and is never pruned

- Status: Accepted
- Date: 2026-10-03
- Current answer: a component's Namespace, and what must exist in it first (its LimitRange, a
  build NetworkPolicy), go in the reserved subdirectory `namespaces/`. It gets its own
  `<name>-namespaces` Kustomization; the component and its `-artifacts` wait for it. Everything in
  it carries `kustomize.toolkit.fluxcd.io/prune: disabled`, so removing a component leaves its
  Namespace; deleting it is a deliberate manual step.

## Context

- `artifacts/` objects should live in the component's own namespace, which therefore must exist
  before `artifacts/` applies.
- A pruned Namespace takes every PVC inside it, and a deleted dynamic PV is then reclaimed
  ([0008](0008-pv-reaper-split-reclaim.md)).

## Decision

- A third reserved subdirectory (PR #542), opt-in by existing, with no `kustomization.yaml`.
  Rejected: the generator creating the Namespace (awkward ownership), or the Namespace inside
  `artifacts/` (no use to components without one).
- Never pruned (PR #557), via the Kustomization's `commonMetadata`. The risks are unequal: a
  forgotten Namespace costs an empty namespace, a pruned one costs its data. The same protection
  for PVCs was deferred, since it changes every app's behaviour.
- `check-substitution.py` refuses a Namespace owned twice (in `namespaces/` and in the component
  build) and objects in `namespaces/` outside a Namespace declared there.
- Existing Namespaces move in with prune off ([0012](0012-prune-off-around-risky-changes.md)).

## Consequences

- Moving an existing Namespace in from the component build is still a prune risk, since the
  component prunes what leaves its build: follow [0012](0012-prune-off-around-risky-changes.md).
- Namespaces outlive their components; `docs/operations.md` lists how to find orphans.

## References

- PR #542 (`c2ef861f`), #557 (`4d2dafe2`), #558-#566, #571.
- Conversation 2026-10-03 (alternatives, never pruned).
- README: [Reserved subdirectories](../../README.md#reserved-subdirectories);
  [docs/operations.md](../operations.md#removing-a-component).
