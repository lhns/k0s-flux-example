# remote-docker-ci

A second `remote-docker-workspace`, used only by the `k0s-flux` ARC scale set (`apps/github-runner`). Same
chart and client as [`apps/remote-docker`](../remote-docker/README.md); read that first.

## Why not the shared workspace

**Egress.** Every account's daemon in `apps/remote-docker` runs inside that one pod, so every job
container leaves with the pod's IP and a NetworkPolicy cannot tell one account from another. That
pod has no egress policy, because people use it too. A workspace of its own lets
`networkpolicy.yaml` confine this runner's jobs to DNS and the public internet.

Doing it inside one workspace would need remote-docker to filter per account itself — e.g. put
each account's daemon on its own bridge and have the agent install `DOCKER-USER`/`FORWARD` drops for
private ranges from it, in the pod's netns where the account cannot undo them.

It also keeps Renovate's image cache off the graph the accounts of `apps/remote-docker` share.

## Shape

- `perUserDind: false`: one account (`k0s-flux`), so the workspace's own dockerd serves it.
- The account is **ephemeral** (`ephemeral.*` in `release.yaml`, remote-docker 0.9.0+): the
  scale set's pods all present one key, and each client process still gets its own tunnel port,
  exports and volumes, removed `grace` (2m) after it ends, together with its containers
  (`cleanupContainers`, fine for throwaway CI). `maxClients: 6` is the scale set's 4 runners, the warm
  pod's slot and runs in their grace period; the next client is refused. Older clients are refused too.
- **Two replicas**, each a workspace of its own on a different node: own host key, volumes,
  cache and ephemeral runs, so `maxClients` counts per replica. A run must stay on one replica.
  Runners use the one Service name, whose `sessionAffinity: ClientIP` pins a pod's IP to the
  replica kube-proxy picked first, for 24h after its last connection; a pod sent to the other
  replica refuses its host key.
- The graph is a cache: Renovate's image, kept between runs; `renovate.yml` removes superseded
  tags before each run.

## Residual risk

The pod is privileged, as the shared one is. A job reaches its daemon through `DOCKER_HOST`, so it
can break out to the node. What this workspace buys is that the ordinary case — a job's
containers — is confined to the internet, and that nothing else lives here.

## Metrics

`metrics.enabled` serves `remote_docker_*` on :9090, scraped by the otel `cluster` collector
(`vnet.yaml`; the namespace is listed in `infra/opentelemetry-monitoring`). Enabling it rolls the
StatefulSet, which interrupts running CI jobs.

## Which replica a run uses

```sh
kubectl exec -n remote-docker-ci remote-docker-ci-0 -- remote-dockerd ephemeral ls
kubectl exec -n remote-docker-ci remote-docker-ci-1 -- remote-dockerd ephemeral ls
```

Runs appear on the replica serving them while connected or in grace.

## Adding a key at runtime

Admin account `lhns` exists here (`admins` + `authorizedKeys.lhns`). There is no external route and
the vnet admits only `github-runner`, so `publicURL` is unset and nobody outside the cluster can
connect: run the admin commands in-cluster, or from a pod in `github-runner`.

```sh
kubectl exec -n remote-docker-ci remote-docker-ci-0 -- remote-dockerd token create --account <name>
kubectl exec -n remote-docker-ci remote-docker-ci-0 -- ls /etc/workspace/enrolled_keys.d
```

Tokens and runtime keys are per replica: run these in the replica you want. Runtime keys live in
`enrolled_keys.d` on that replica's `state` claim; `authorizedKeys` stays the declarative base.
Losing the claim revokes them.
