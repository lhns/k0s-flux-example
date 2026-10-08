# 0016 — `validate.sh`: one offline gate for people and CI

- Status: Accepted
- Date: 2026-07-09, amended 2026-09-02, 2026-09-04, 2026-09-17, 2026-09-27 and 2026-09-30
- Current answer: `scripts/validate.sh` is the single entry point, run identically on a
  workstation and in the Lint workflow, with no cluster access. It runs yamllint, a
  `kustomize build` + `kubeconform -strict` sweep over every component of every cluster and the
  rendered generator output, golden tests of the generator, the repo-wide `check-*.py` scripts
  and opt-in per-component tests. An invariant that can be checked mechanically gets a check
  rather than a review comment.

## Context

A wrong manifest is deployed within seconds of a merge ([0006](0006-push-webhook-drives-reconciliation.md)),
and several failure modes here are silent: an empty substitution, a frozen CRD, a release over
the size cap, a Secret applied as a ConfigMap.

## Decision

- yamllint and kubeconform per component (2026-07-09), with a schema cache and the CRD catalog.
  Schemas and CI tools are cached on the runner, not vendored into the repo (PR #452).
- **A failed build fails the run** (2026-09-02), not swallowed by a pipe.
- **Golden tests** (2026-09-04): `tests/fixtures/` covers generator cases the live tree lacks,
  `tests/golden/` pins the comment-stripped render, and `tests/generator-guards.sh` the renders
  that must be refused. An intentional change updates the golden and the diff is the review.
- **Checks for invariants**, each added after a failure or ahead of a migration:
  `check-release-size.py` ([0039](0039-one-generator-release-per-component.md)),
  `check-substitution.py` ([0038](0038-plaintext-config-substituted-secrets.md)),
  `check-sops.py` ([0003](0003-sops-with-age-one-key-per-cluster.md)),
  `check-source-pins.py`, `check-fetch-digests.py` ([0027](0027-everything-pinned-and-checked.md)),
  `check-coverage.py` (snapshot classes, CNPG backups, [0017](0017-backups-stay-in-ceph.md)),
  `check-clusters.py`, `check-artifacts.py` ([0048](0048-several-clusters-one-repo.md)), and the
  offline test of `check-migration.py`. Exemptions need a written reason.
- **`kubeconform -strict`** (2026-09-30): a field in the wrong place passes non-strict
  validation. SOPS metadata is stripped first, line by line.
- **Per-component tests** in `<component>/tests/run.sh`: no cluster, deterministic, a missing tool
  fails unless `ALLOW_SKIP=1`, and every skip is listed at the end.

## Consequences

- The lint is also Renovate's merge gate ([0015](0015-self-hosted-renovate-automerge-policy.md)).
  For human PRs, merging only on green is a working rule ([0041](0041-change-workflow.md)); the
  repo has no branch protection.
- Text files are checked out as LF everywhere (`.gitattributes`, PR #336): CRLF made the golden
  diff and the tools manifests fail silently on Windows checkouts.

## References

- `632405d5`, `58c9ee0e`, `f9c37134`, `b5340870`, `0d1617ee`, PR #336, #449, #452, #512.
- Conversation 2026-07-09 (no vendored schemas).
- README: [Validation and tests](../../README.md#validation-and-tests);
  [kube-cluster/README.md](../../kube-cluster/README.md) (per-component tests).
