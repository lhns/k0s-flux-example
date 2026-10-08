# 0043 — CI runs on self-hosted runners in the cluster, each confined to its own pod

- Status: Accepted; the runner replaced by [0050](0050-arc-scale-sets-for-per-job-runners.md)
- Date: 2026-09-24, amended 2026-10-06
- Current answer: this repo's Lint and Renovate run on the `k0s-flux` ARC scale set in
  `apps/github-runner` ([0050](0050-arc-scale-sets-for-per-job-runners.md)): a pod per job,
  unprivileged, using Docker through a remote-docker workspace of its own (`apps/remote-docker-ci`)
  whose egress policy allows DNS and the public internet only. All pods share one remote-docker
  key; the account is ephemeral. GitHub-hosted runners remain a manual fallback
  (`workflow_dispatch` input `runner: github-hosted`). Below is the `runner-k0s-flux`
  StatefulSet design it replaces.

## Context

GitHub-hosted Actions minutes are a limited budget, and Lint and Renovate spend it. When it runs out
they stop, and with them Renovate's merge gate
([0015](0015-self-hosted-renovate-automerge-policy.md)). The cluster already ran Gitea and Forgejo
runners with a privileged dind sidecar and an egress policy.

## Decision

- **remote-docker, not a dind sidecar** (PR #415): no privileged sidecar, and bind mounts resolve
  on the runner's own filesystem. Per-account daemons are separation, not isolation. A runner is
  never registered on a public repository, where a fork PR would run attacker-controlled code.
- **A workspace of its own**: every account's daemon runs in the one shared workspace pod, so a
  NetworkPolicy cannot tell their job containers apart, and that pod has no egress policy because
  people use it. Only a separate pod can confine these jobs without constraining everyone else
  ([0031](0031-beyond-ingress-isolation.md)).
- **One key per replica, so a StatefulSet**: remote-docker identifies a client by its key, and two
  replicas sharing one would fight over one reverse-tunnel port.
- Renovate is pinned to an exact version and keeps its caches in the dedicated workspace.
- **Runtime key management** (2026-10-06): remote-docker 0.9.0's `remote key add|rm` and token
  enrolment are enabled in both workspaces with `lhns` as admin. Runner keys stay declarative in
  `authorizedKeys`; ephemeral client accounts come with
  [0050](0050-arc-scale-sets-for-per-job-runners.md).

## Alternatives considered

- A dind sidecar (PR #412): privileged, and dockerd resolves a bind mount in its own filesystem,
  so job containers see an empty workspace.
- An automatic picker job on a hosted runner: costs a minute per run and blocks when the budget is
  gone.
- Renovate without Docker: Flux bumps regenerate manifests with `flux install`, which would use
  whatever `flux` is on the runner, not the target version.
- Actions Runner Controller (2026-09-01): its Kubernetes mode has no Docker daemon, and its
  dind mode is still privileged, with a cold layer cache on every job. Revisited for Lint in
  [0050](0050-arc-scale-sets-for-per-job-runners.md).

## Consequences

- If the runner is down, neither Lint nor Renovate runs until someone dispatches the fallback,
  which spends Actions minutes. CI depends on the cluster it validates.
- Per-key identities do not fit per-job pods, which is why [0050](0050-arc-scale-sets-for-per-job-runners.md)
  keeps Renovate on this runner.

## References

- PR #412 (`05e887a8`), PR #415 (`7205e49a`), PR #451, PR #640, `10f50483` (remote-docker
  workspace).
- Conversation 2026-09-01 (first runner, ARC), 2026-09-24 (this repo's runner).
- [apps/github-runner](../../kube-cluster/apps/github-runner/README.md).
