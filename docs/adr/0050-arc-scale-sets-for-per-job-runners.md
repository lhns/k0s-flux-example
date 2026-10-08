# 0050 — Actions Runner Controller scale sets for per-job runners

- Status: Accepted
- Date: 2026-10-05, amended 2026-10-07
- Current answer: GitHub runners for Lint and Renovate become
  ARC scale sets: a fresh pod per job, deleted after it, unprivileged, internet-only egress, and
  only a one-time registration in the job's reach. `k0s-flux` (Lint and Renovate) has Docker through
  ephemeral remote-docker clients on `apps/remote-docker-ci`. The controller is an
  app (`apps/arc-controller`) watching only the `github-runner` namespace. The `runner-k0s-flux`
  StatefulSet of [0043](0043-ci-on-self-hosted-runners.md) is gone.

## Context

The runners of [0043](0043-ci-on-self-hosted-runners.md) are long-lived pods that re-register
after each job: state can leak between jobs through the pod, and a job can read the PAT the runner
registers with. For this repo's own jobs that is accepted; for a repository whose jobs run
third-party code it is not, which is what prompted the change.

## Decision

- Scale sets per runner label, with `minRunners` warm pods.
- **Lint and Renovate share one `k0s-flux` scale set with Docker** through remote-docker's ephemeral
  accounts (remote-docker 0.9.0, its ADR 0050): every client process gets its own port and volumes,
  cleaned up after a grace period, so pods sharing one enrolled key do not collide. The pods start
  the client in the runner container through a wrapper (`artifacts/arc-runner-k0s-flux.yaml`); a
  sidecar cannot serve the pod's filesystem. Lint therefore depends on `remote-docker-ci`.
  `maxClients` is 6: `maxRunners` 4, the warm pod's slot and runs in grace.
- **Two `remote-docker-ci` replicas** (2026-10-07, remote-docker 0.10.0); cache and `maxClients`
  are per replica. The Service's `sessionAffinity: ClientIP` pins each runner pod to one replica,
  which a run must keep; runners do not select a replica themselves.
- **The controller is in `apps/`**, with a Role in one namespace rather than a ClusterRole: the
  cluster does not need it to function, which is the sharpened definition of `infra/`
  ([0005](0005-groups-infra-apps-rbac.md)).
- The StatefulSet of 0043 and its PAT Secret are removed here.
- **One runner base image.** The scale sets' images are stages of one Dockerfile
  (`artifacts/arc-runner-base.yaml`). The base carries the tool set GitHub's `ubuntu-latest`
  preinstalls, so a workflow runs unchanged on both; versions may be newer (Renovate keeps them
  current). `k0s-flux` adds a stage with the remote-docker client. oci-composer cannot build one
  ImageBuild `FROM` another (the digest is not in git) or read a Dockerfile from a ConfigMap, hence
  stages of one Dockerfile kept identical in both ImageBuilds (checked by
  `scripts/check-arc-dockerfile.py`) rather than chained builds.
- Rollback is a revert while the controller still runs (the scale sets carry finalizers).

## References

- PR #652 (replacing the stacked #647-#650, [0041](0041-change-workflow.md)), `b82ca85e` (two
  replicas).
- Conversation 2026-10-05.
