# 0003 — Secrets are SOPS-encrypted with age, one key per cluster

- Status: Accepted
- Date: 2026-07-04, amended 2026-07-08, 2026-09-16, 2026-09-24 and 2026-10-04
- Current answer: every Secret in git is SOPS-encrypted to its cluster's age public key; only
  that cluster holds the private key (`flux-system/sops-age`), and kustomize-controller
  decrypts. `.sops.yaml` has one rule per cluster directory, and `shared/` holds no encrypted
  file. Secrets reach the repo through `scripts/secret-from-clipboard.py`, never through a chat
  or a terminal.

## Context

Credentials had to live next to the manifests that use them, readable by Flux and by nobody who
merely reads the repo.

## Decision

- **SOPS + age, decrypted by Flux.** The generator adds `decryption` to every component
  Kustomization. Flux reads `sops-age` from the Kustomization's own namespace, which since
  [0046](0046-control-namespaces-always-on.md) means Reflector copies it into each control
  namespace.
- **One key per cluster** (2026-07-08): the creation rule was narrowed from every `*.yaml` to
  `kube-cluster/`, so a second cluster is a second rule with its own recipient. Enforced since
  2026-10-04 by `scripts/check-clusters.py`.
- **Checked without the key** (2026-09-16): `scripts/check-sops.py` asserts every value is
  `ENC[...]`, the recipients and `encrypted_regex` match `.sops.yaml`, and the file is still a
  Secret. A broken MAC needs the private key, so `scripts/check-sops-mac.sh` runs on a host
  that has it, outside CI.
- **Handover by clipboard** (2026-09-24): the user copies the value and runs
  `secret-from-clipboard.py`, which shows it masked, pipes the Secret to `sops` on stdin and
  verifies the output contains no plaintext. Agents must never ask for a secret in chat
  (AGENTS.md, "Receiving secrets").

## Consequences

- Only the cluster can decrypt, so a key cannot be added to an existing encrypted file without
  the cluster's key: one Secret per file.
- Moving a Secret to another namespace means re-encrypting it, since `metadata.namespace` is
  under the SOPS MAC.
- A label that grants Reflector's copy of `sops-age` hands out the decryption key; only the
  generator may set it ([0046](0046-control-namespaces-always-on.md)).

## References

- `50057287` (SOPS enabled), `f31e02bf` (per-cluster rule), `c9825cc8` (check-sops), PR #413
  (`c413daa2`, clipboard script), `d9854ef0` (AGENTS.md "Receiving secrets"), PR #626, PR #629
  (per-cluster check, second key).
- README: [Secrets](../../README.md#secrets), [Handing over a secret](../../README.md#handing-over-a-secret).
