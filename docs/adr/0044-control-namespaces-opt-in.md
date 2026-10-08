# 0044 — Control namespaces, opt-in per component

- Status: Superseded by [0046](0046-control-namespaces-always-on.md)
- Date: 2026-10-03
- Current answer: superseded within a day. The generator gained the ability to put a component's
  Kustomizations, and their substitution sources, in a generated Namespace `gen-<group>-<name>`
  instead of `flux-system`, switched on per component by `generate.controlNamespaces.enabled`.

## Context

Every generated Kustomization lived in `flux-system`, together with every substitution Secret and
every composer ref export. For a later multi-user setup, a namespace per component is where a
tenant's service account, role bindings and SOPS key would go. A constraint follows from Flux: it
reads `substituteFrom` and `sops-age` from the Kustomization's own namespace, so both have to move
with it.

## Decision (as it was)

- The name `gen-<group>-<name>`, chosen over an `app-<name>-gen` suffix: the namespaces sort
  together, the name derives from the component, and it suggests no grouping by app.
- Kept apart from the `namespaces/` move ([0045](0045-namespaces-dir-never-pruned.md)): done in
  one step, a failure could not be traced to either.
- Two switches, both initially empty so nothing changed live: `enabled` and `pruneDisabled`
  (names or `"*"`). A move upgraded only the component and the releases depending on it.
- `dependsOn` resolved to `gen-<target>` or `flux-system` depending on whether the target had
  moved.
- A `lookup` guard (`guardMove`) refused a move while the old Kustomizations still pruned.
- A read-only gate script, `check-control-namespace-migration.py`, between steps.
- Reflector copies `sops-age` into every Namespace with the control-namespace label, after a
  one-time manual annotation; oci-composer's ref-export allow-list matches exact names, so each
  moved component with `artifacts/` was added to it.

## Why it was superseded

The opt-in existed only to migrate one component at a time. Every component moved the same day
(PR #547-#555), new components were defaulted in (PR #579), and the switch, the `flux-system`
path and `guardMove` were deleted.

## References

- PR #545 (`bc3c14e2`), #547-#555, #579.
- Conversation 2026-10-03 (motivation, naming).
