# 0038 — Configs stay plaintext; secrets are substituted from `substitution-secrets/`

- Status: Accepted
- Date: 2026-09-04, amended 2026-09-16, 2026-10-03 and 2026-10-16
- Current answer: a config file that holds a credential stays a real, readable file with
  `${VAR}` placeholders. The values are SOPS Secrets in the component's `substitution-secrets/`,
  which gets its own Kustomization; the component depends on it and substitutes from each Secret
  (`optional: false`). A substituted config that must end up in a Secret is a
  `configMapGenerator` marked `flux-component-generator/as-secret`, which the generator patches
  into a Secret. Substitution is strict: an undefined variable fails the reconcile, but only
  once the Kustomization has `postBuild` (see the 2026-10-16 amendment).

## Context

Configs with one password in them were encrypted whole, so nobody could read or review them.
Flux's `postBuild.substituteFrom` can fill placeholders, with traps:

- A Kustomization that creates and consumes its own substitution Secret reads the copy from
  before it applied: the first apply ships a literal `${VAR}`, and every change lands one
  reconcile late.
- A `secretGenerator` base64-encodes into `data:`, where envsubst cannot see placeholders.
- Without the feature gate, an undefined variable becomes an empty string, so a typo applies a
  password as `""`.

## Decision

- **A reserved subdirectory, not an annotation** (2026-09-04): its own Kustomization, so the
  value is never in the build that substitutes it; having the directory is the opt-in, and the
  Secret names are read from the manifests rather than listed. It holds no
  `kustomization.yaml`; kustomize-controller synthesises one.
- **`as-secret`**: a ConfigMap keeps the content plaintext; the patch changes the kind and moves
  `data` to `stringData` by path. `disableNameSuffixHash` is required, because a workload's
  Secret reference is not rewritten to the hashed name. A missing annotation would apply the
  credential as a ConfigMap; `check-substitution.py` fails both cases.
- **Checks** (2026-09-16): a `${VAR}` no source defines fails the lint.
- **`StrictPostBuildSubstitutions`** (2026-10-03, PR #546) makes the cluster fail the same way.
- **Strictness needs `postBuild`** (2026-10-16): a component's first `artifacts/` or
  `substitution-secrets/` must land before any `${VAR}` reference. Backstops: a
  ValidatingAdmissionPolicy denying `$` in image references, and the
  `CancelHealthCheckOnNewRevision` gate.
- The Secrets live in the control namespace, since Flux reads `substituteFrom` from the
  Kustomization's namespace ([0046](0046-control-namespaces-always-on.md)).

## Consequences

- **Substitution covers the whole component build**, not just the file meant. Every other `${…}`
  must be a source, be escaped as `$${…}`, or sit in an object with
  `kustomize.toolkit.fluxcd.io/substitute: disabled`; `$$` is applied as `$`.
- Better still is the app's own mechanism (an env var or a mounted Secret), so the config holds
  no credential at all.

## Alternatives considered

- **One shared `substitution-secrets` component** for every app : replaced so that apps stay
  self-contained.
- **A `substituteSecrets` annotation listing the Secrets**: a listed name can drift from the
  Secret and wedge the component on `secret not found`; names are now derived.

## References

- `b529f45b`, `638bc1be`, `9ed29455`, `b61c03cb`, `90eec0c2`, `212b2e1b`, PR #546.
- README: [Reserved subdirectories](../../README.md#reserved-subdirectories),
  [Substitution caveats](../../README.md#substitution-caveats),
  [Substituted config files: as-secret](../../README.md#substituted-config-files-as-secret).
