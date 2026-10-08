# descheduler

[Descheduler](https://github.com/kubernetes-sigs/descheduler) evicts pods so the scheduler
places them again, which re-spreads replicas after a drain or node loss. See
[ADR 0054](../../../docs/adr/0054-descheduler-respreads-replicas.md) for the policy and why.

A shared base: each cluster deploys it from `<cluster>/infra/descheduler/`, which holds the
Namespace (`namespaces/`) and the chart version (`version.yaml`, patched onto the base).

- `release.yaml` holds the HelmRepository (the chart is not published to OCI) and the
  HelmRelease with the policy. Only spread plugins run; the chart's default profile is replaced.
- No kube-vnet rule needed: it only talks to the API server.
- To review a policy change first, set `dry-run: true` in `cmdOptions`: it then only logs what it
  would evict (`kubectl -n descheduler logs deploy/descheduler`). The first cycle after start sees
  an incomplete snapshot in dry-run; judge from the second.
