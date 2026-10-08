# 0041 — Change workflow: plan, verify each step, PRs at deploy boundaries, merge on green

- Status: Accepted
- Date: 2026-09-17, amended 2026-09-18, 2026-09-27 and 2026-10-05
- Current answer: a risky change starts from a written plan with a verification gate per phase.
  It ships in as many PRs as it has deploy boundaries, each verified live before the next, and no
  more; a change whose parts only work together is one PR. A PR is merged only once CI has
  passed. A deploy counts as landed when its Flux Kustomization is Ready. Substantial work goes to
  subagents, never two in the same files at once, and their reports are checked before acting on
  them.

## Context

A merged commit deploys within seconds ([0006](0006-push-webhook-drives-reconciliation.md)), and
structural changes here can prune live objects ([0012](0012-prune-off-around-risky-changes.md)).
Most changes are made with AI agents working in parallel.

## Decision

- **Plan first** (2026-09-17): write the plan before acting and verify at each step, since a
  structural change can prune live objects (e.g. the generator split,
  [0039](0039-one-generator-release-per-component.md)).
- **Push only what was meant** (2026-09-14): check `origin/main..HEAD` before pushing; an
  unchecked push can ship unreviewed commits, and a merged commit deploys within seconds.
- **Landed means Ready** (2026-09-16): a failed reconcile leaves the old Deployment healthy, so a
  rollout status passes either way ([docs/operations.md](../operations.md#checking-a-deploy-landed)).
- **PR boundaries are deploy boundaries** (2026-09-18): a split costs review and rebase work and
  is worth it only where it is a point of no return.
- **Merge on green** (2026-09-27): merge only once CI has passed, not merely finished.
- **One reviewable PR over a dependent stack** (2026-10-05): PRs that cannot be merged alone are
  hard to review, so they are one PR. Stacks stay for staged live rollouts, where each step is
  verified before the next touches production.
- **Subagents** (2026-09-17): one agent per set of files, queued rather than run concurrently on
  the same repo; a completion report is a claim, re-checked before merging or deploying.

## Consequences

- The repo enforces only the lint ([0016](0016-validate-sh-one-offline-gate.md)); everything
  else here is a working rule. `main` has no branch protection; adding it is the owner's call and
  still open.
- Structural changes add or amend a record here in the same PR (AGENTS.md, "Decisions").

## References

- PR #333, #334, #450, #545-#555, #593-#599, #647-#650, #652.
- Conversation 2026-09-14 (push), 2026-09-16 (Ready), 2026-09-17 (plan, subagents), 2026-09-18
  (deploy boundaries), 2026-09-27 (merge on green), 2026-10-05 (one PR).
