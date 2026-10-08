# 0015 — Self-hosted Renovate; automerge only what the lint can vouch for

- Status: Accepted
- Date: 2026-07-09, amended 2026-07-23, 2026-07-29, 2026-08-06, 2026-09-02, 2026-09-15,
  2026-09-18 and 2026-10-04
- Current answer: Renovate runs from this repo's own workflow (every 3 hours) with a
  fine-grained PAT. It automerges patch updates, Docker digest updates, Renovate's own image,
  and minor updates under `kube-test-cluster/`, each only once the `lint` commit status is
  green. Minor and major updates are review PRs. A list of packages never automerges (k0s,
  ceph-csi, Multus, VictoriaMetrics, Kafka's classpath plugin, own forks) or waits on the
  dependency dashboard (MariaDB non-patch, otbr).

## Context

Every chart, image and pinned download is a version in git ([0027](0027-everything-pinned-and-checked.md)),
so keeping them current is a constant stream of small PRs, and a merged one is deployed within
seconds.

## Decision

- **Self-hosted, scheduled** (2026-07-09), with the flux and kubernetes managers over every YAML
  file and custom managers for what they cannot see (k0s in `k0sctl.yaml`, image volumes,
  `fetch.url` pins, workflow `env:`). Self-hosted because the repo should be self-contained and
  self-sustaining; it runs on the cluster's own runners ([0043](0043-ci-on-self-hosted-runners.md)).
- **`platformAutomerge: false`**: Renovate merges through the API during its own run; the repo
  has no branch protection to delegate to.
- **Automerge only patch** (2026-07-09): with no CI gate, merging minor was not safe.
- **The gate is a commit status** (2026-07-23): the PAT cannot read the Checks API, so
  Renovate saw every branch as pending. `lint.yml` mirrors its result as a `lint` status.
- **Majors of stateful services are checked against the maintained line** (2026-07-29): a
  higher number can be a rapid or rolling release (MongoDB, MariaDB).
- **Firmware-coupled images wait for the dashboard** (2026-08-06): an otbr digest is one half of
  a contract with radio firmware outside the cluster, which no CI can check.
- **Digests automerge** (2026-09-02): for images that publish no versions, a digest is the only
  update, and reviewing each says nothing. Docker only; a Flux commit bump is also a "digest"
  but is new source.
- **Groups only for one release train** (2026-09-02): images built from one commit, or
  components that must not run mixed versions (VictoriaMetrics). Unrelated PRs are not grouped
  because they arrive together. A version cap belongs to the file it is for (`matchFileNames`),
  never repo-wide.
- **`rebaseWhen: conflicted`** (2026-09-15): with merges only during its own run, rebasing on
  every merge cost each PR another cycle.
- **Reviewed PRs are judged by rendering** (2026-09-18): for a chart bump, both versions are
  rendered against the live values and diffed, rather than read from release notes; the default
  is to hold. Majors with an irreversible on-disk migration and the control plane are held
  categorically.
- **Promotion** (2026-10-04): a version runs on the test cluster before prod's reviewed PR
  ([0048](0048-several-clusters-one-repo.md)).

## Consequences

- A package that must move with another (a GitRepository pin and its `revision:`s, synapse and
  its pymods image, remote-docker's client and chart) needs a group or a `postUpgradeTasks`
  fixer, and a lint check that fails a half-bump.
- A bump that restarts something everyone depends on gets an explicit never-automerge rule; a
  missing rule means it automerges on patch.
- A held major stays an open PR with its reason written down; notesnook's MongoDB 9 is
  [0051](0051-notesnook-mongodb-held-at-8.md).

## References

- `eef54095`, `0432efc4`, `0f7bd578`, `4b5012c2`, `2908226e`, `ba928ab1`, `f749fa92`,
  `113aa0c9`, PR #352, #356, #396, #419, #630.
- Conversation 2026-07-29 (maintained lines), 2026-08-06 (otbr), 2026-09-02 (grouping),
  2026-09-18 (render-diff triage), 2026-10-05 (why self-hosted).
- [renovate.json](../../renovate.json) carries a `description` on every rule.
