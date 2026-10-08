# 0042 — Composed images reach their consumers as exported digest refs, from `artifacts/`

- Status: Accepted; supersedes [0033](0033-spec-hash-tags-for-composed-images.md)
- Date: 2026-09-23, amended 2026-10-03
- Current answer: a component's `ImageComposition` and `ImageBuild` objects live in its
  `artifacts/` directory, in the component's own namespace. After every publish the composer writes
  the digest-pinned ref into a ConfigMap in the component's control namespace; the component
  substitutes it as `${<NAME>_REF}`. `artifacts/` is its own Kustomization with `wait: true`, and the
  component depends on it. Nothing names a tag.

## Context

Spec-hash tags ([0033](0033-spec-hash-tags-for-composed-images.md)) could wedge permanently when a
composition and its source pin moved in one commit, and made the generator parse every component's
YAML. kube-oci-composer gained `push.writeRefTo`. Its own ADRs had rejected exporting digests
when every artifact was a composition, whose digest follows from its spec; an `ImageBuild`'s
digest is an observation, which is what made exporting the right answer (conversation
2026-09-22).

## Decision

- **Export the digest, substitute it** (PR #392/#393): the ref the build actually produced, so
  consumer and artifact cannot disagree, and the generator stops hashing anything.
- **A reserved directory**: the generator reads only `artifacts/`, which keeps the chart small
  ([0039](0039-one-generator-release-per-component.md)), and `check-substitution.py` fails an
  artifact object anywhere else.
- **Wait for the build**: a consumer must not apply before its ref exists; strict substitution
  ([0038](0038-plaintext-config-substituted-secrets.md)) fails the reconcile rather than applying an
  empty image.
- **In the component's own namespace** (2026-10-03, PR #567-#575): with `namespaces/`
  ([0045](0045-namespaces-dir-never-pruned.md)) the objects live next to their consumer, not in
  `oci-composer`. Build pods then run there, which needs the namespace to allow privileged pods
  and, under a namespace-wide egress policy, a build policy in `namespaces/`.

## Consequences

- **The deployed digest is state outside git.** A revert rebuilds rather than restores: an
  `ImageBuild` lands on a new digest.
- **A failing build holds its component back**; pods keep the last digest they were given.
- **The whole component is substituted**, with the caveats of
  [0038](0038-plaintext-config-substituted-secrets.md).

## References

- `049fe078`, `75ee72eb`, PR #392, #393, #395, #551, #567-#575, #571.
- [infra/oci-composer](../../kube-cluster/infra/oci-composer/README.md#how-a-workload-names-an-artifact).
