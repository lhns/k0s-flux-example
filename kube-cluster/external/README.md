# external

Components that describe systems outside the cluster: routes to external backends, and the Grafana
objects for their metrics. Routes are a selectorless Service plus an EndpointSlice (`services`).
Metrics are pushed or collected by the system itself (cephadm collector, Proxmox OTLP metric
server), so the cluster collector scrapes nothing here; a component may hold only Grafana objects
(`ceph`, `proxmox`). Why a group of its own:
[ADR 0056](../../docs/adr/0056-external-group-for-systems-outside-the-cluster.md).
