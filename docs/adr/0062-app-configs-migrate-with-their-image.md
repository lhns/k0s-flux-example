# 0062 — Apps that migrate their own config do it inside the update PR

- Status: Accepted
- Date: 2026-10-23, amended 2026-10-25
- Current answer: when an app can upgrade its own config file, Renovate runs that upgrade on the
  new image as a `postUpgradeTask` and commits the result into the same PR, and the lint checks
  that the git config matches what the image would write. The mautrix bridges are the first
  example (`scripts/bridge-config-sync.py`).

## Context

Configs live in git and are mounted read-only, so an app that would normally rewrite its config
on start (add new keys, drop or rename old ones) cannot. Each update would then need a hand-merge
against the app's own migration, or would run with silently ignored keys. Configs also hold
credentials as `${VAR}` placeholders that must survive any rewrite.

## Decision

- **The app's own migration does the work.** A sync script runs the new image's config upgrader
  (for the bridges, `-g`) over the git config and merges the result back: keys only the image has
  are added, keys only git has are removed, git's values and comments are kept.
- **Placeholders are protected.** Each `${VAR}` becomes a unique marker before the run and must
  appear exactly once afterwards; otherwise the script writes nothing and fails.
- **It runs in the update itself.** Renovate runs the script as a `postUpgradeTask`, so the
  migrated config is part of the image bump PR and reviewed with it. Renovate's container gets the
  runner's Docker socket for this. Automerge rules stay as they are.
- **The lint enforces it.** The same script in `--check` mode fails a PR whose config is out of
  step with its image, which also stops an automerge.
- **An app joins** by getting a sync mode in the script (or its own script on the same pattern),
  a `postUpgradeTask` rule for its image, and a lint entry. Apps that cannot migrate their config
  non-interactively stay hand-maintained.
- **Flux follows the same pattern** (2026-10-25): a Flux release bump regenerates its manifests
  with `gen-flux.sh` in the update PR, and the lint checks them ([0019](0019-flux-manifests-from-gen-flux-never-bootstrap.md)).

## Consequences

- Config migrations arrive with the update instead of being discovered after it.
- The Renovate container can start containers on the runner's daemon.
- Images Renovate does not track (e.g. our own forks) are synced by running the script by hand
  before the bump; the lint catches a forgotten run.

Details for the bridges: [matrix README](../../kube-cluster/apps/matrix/README.md), "Bridge configs
live in git".
