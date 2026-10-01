# remote-docker-ci

A second `remote-docker-workspace`, used only by `runner-k0s-flux` (`apps/github-runner`). Same
chart and client as [`apps/remote-docker`](../remote-docker/README.md); read that first.

## Why not the shared workspace

**Egress.** Every account's daemon in `apps/remote-docker` runs inside that one pod, so every job
container leaves with the pod's IP and a NetworkPolicy cannot tell one account from another. That
pod has no egress policy, because people use it too. A workspace of its own lets
`networkpolicy.yaml` confine this runner's jobs to DNS and the public internet.

Doing it inside one workspace would need remote-docker to filter per account itself — e.g. put
each account's daemon on its own bridge and have the agent install `DOCKER-USER`/`FORWARD` drops for
private ranges from it, in the pod's netns where the account cannot undo them.

It also keeps Renovate's image cache off the graph the humans and `runner-example-repo` share.

## Shape

- `perUserDind: false`: one account (`k0s-flux`), so the workspace's own dockerd serves it.
- One key per runner replica (ADR 0029: the client IS its key). Both replicas share the daemon,
  so neither may clean up containers at startup — it would kill the other's job.
- The graph is a cache: Renovate's image, kept between runs; `renovate.yml` removes superseded
  tags before each run.

## Residual risk

The pod is privileged, as the shared one is. A job reaches its daemon through `DOCKER_HOST`, so it
can break out to the node. What this workspace buys is that the ordinary case — a job's
containers — is confined to the internet, and that nothing else lives here.
