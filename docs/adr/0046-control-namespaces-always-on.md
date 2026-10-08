# 0046 — Every component's Kustomizations live in its own control namespace

- Status: Accepted; supersedes [0044](0044-control-namespaces-opt-in.md)
- Date: 2026-10-04
- Current answer: the generator always puts a component's Kustomizations in Namespace
  `gen-<group>-<name>`, created by its `gen-` release and labelled
  `flux-component-generator/control-namespace: "true"`. Its substitution Secrets and composer ref
  exports live there too; `flux-system` is refused for them. Reflector copies `sops-age` into every
  Namespace with that label. `dependsOn` always resolves to `gen-<owner>`.

## Context

Every component already lives in a control namespace through the opt-in switch
([0044](0044-control-namespaces-opt-in.md)), so the switch has nothing left to decide.

## Decision

- Remove `controlNamespaces.enabled`, the `flux-system` path and the move guard. The prune switch
  survives as `generate.pruneDisabled` for any risky change
  ([0012](0012-prune-off-around-risky-changes.md)), and the gate became `check-migration.py`.
- Refuse a component name ending in `-namespaces`, `-artifacts` or `-substitution-secrets`, which
  would make `dependsOn` resolution ambiguous.
- The root's own objects (ArtifactGenerators, `gen-` HelmReleases, `k0s-upgrade`) stay in
  `flux-system`.

## Consequences

- **The label hands out the decryption key.** Only the generator may set it, which
  `check-substitution.py` enforces; removing it, or Reflector's annotations on `sops-age`, deletes
  every copy and every component fails to decrypt.
- **A fresh cluster needs one manual copy**: Reflector's own Kustomization lives in
  `gen-infra-reflector` and must decrypt before Reflector exists (README,
  [Bootstrapping a cluster](../../README.md#bootstrapping-a-cluster)).
- Generated Kustomizations are spread over one namespace per component; use `-A`
  ([docs/operations.md](../operations.md#checking-a-deploy-landed)).
- Moving a Secret into a control namespace means re-encrypting it.

## References

- PR #584 (`e0b788ba`), `9f958e88`.
- [shared/infra/reflector](../../shared/infra/reflector/README.md#sops-age-in-control-namespaces);
  README: [Control namespaces and risky changes](../../README.md#control-namespaces-and-risky-changes).
