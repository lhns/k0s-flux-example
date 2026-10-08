# 0027 — Every upstream input is pinned, Renovate-trackable, and checked

- Status: Accepted
- Date: 2026-07-27, amended 2026-08-05, 2026-09-03, 2026-09-20 and 2026-09-30
- Current answer: nothing floats. Images carry a real version tag, plus the digest where the tag
  could hide a different version; an image with no version tags is pinned by digest. Charts,
  git sources, downloaded files and vendored manifests are pinned to an exact version and, where
  bytes are fetched, a digest. Every pin is in a form Renovate can update, and where two pins must
  agree, a lint check fails if they do not.

## Context

A floating tag turns a pod restart into an unreviewed upgrade, and a bare digest turns a version
jump into a PR titled "update digest".

## Decision

- **Tags, digests where needed** (runbook, 2026-07-27): prefer a version tag; digest-pin an
  image that only publishes `latest`.
- **Tag and digest** (2026-08-05): where a tag could hide a different version, pin both; a
  digest alone hides a version jump, and the tag makes the PR say what it is.
- **No unpinned leftovers** (2026-09-03): bare `:latest` images are pinned to the digest already
  running, so nothing changes and Renovate can track them.
- **Source pins agree** (2026-09-20, PR #352): every oci-composer `sourceRef` carries the
  `revision:` of the GitRepository pin it names, or a build can publish old content under a new
  tag. `check-source-pins.py` fails a mismatch, and a Renovate post-upgrade task rewrites them.
- **Downloads by digest** (2026-09-30, PR #494/#495): `fetch.url` pins carry a digest checked by
  `check-fetch-digests.py` (and fixed by Renovate's post-upgrade task); upstream manifests are
  vendored unmodified with the local changes as kustomize patches, so a bump re-downloads the file
  and re-applies the patches.

## Consequences

- Sources poll hourly, because a pinned source never changes between commits
  ([0006](0006-push-webhook-drives-reconciliation.md)).
- An image-volume reference is invisible to Renovate's kubernetes manager; it needs the
  `# renovate:` comment (README, [Getting files into a workload](../../README.md#getting-files-into-a-workload)).
- The one deliberate exception to tracking: CI's Helm is pinned but not Renovate-tracked, because
  the golden test compares rendered Helm output (2026-09-30).

## References

- `0b621503`, `cd185a75`, `8b576c4d`, `e720310b`, `c96555b7`, PR #352, #494, #495.
- [docs/migrating-swarm-to-kubernetes.md, 3](../migrating-swarm-to-kubernetes.md#3-the-migration-workflow) (step 9).
