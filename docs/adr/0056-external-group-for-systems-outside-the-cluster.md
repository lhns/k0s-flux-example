# 0056 — A fourth group, `external`, for systems outside the cluster

- Status: Accepted
- Date: 2026-10-20, amended 2026-10-20 and 2026-10-21
- Current answer: a component that describes a system outside the cluster (scrape targets, routes
  to external backends) lives in `kube-cluster/external/<name>/`, Flux name `ext-<name>`. It holds
  selectorless Services, static EndpointSlices and what points at them; its only workload is a
  collector, when scraping it needs system-specific rules and the system cannot run one itself.

## Context

[0005](0005-groups-infra-apps-rbac.md) splits components by what the cluster needs to function.
Components that only model hosts outside the cluster fit neither `infra/` nor `apps/`: they run
nothing, and their content is the owner's LAN topology (addresses, hostnames), which changes when
that network changes, not when the cluster does.

## Decision

- New group `{ base: kube-cluster/external, prefix: ext }` in the root release's values.
- An external system is a selectorless Service plus an EndpointSlice with a custom
  `endpointslice.kubernetes.io/managed-by`, so the EndpointSlice controller leaves it alone.
  Consumers (Traefik, the cluster collector's `kubernetes-endpointslices` scrape job) discover it
  like any in-cluster Service.
- The cluster collector scrapes any Service in an opted-in namespace annotated
  `prometheus.io/scrape: "true"`, honouring `prometheus.io/port`, `path` and `scheme`.
- Amended 2026-10-20: an external system with system-specific scrape rules brings its own
  collector in its component (static targets, no Service annotations); the cluster collector keeps
  only generic jobs.
- Amended 2026-10-21: a system that can run a collector runs it there, under its own orchestrator
  (Ceph: a cephadm custom container). Its spec stays in `external/<name>/` and is applied by hand,
  so the component may build no objects.
- Amended 2026-10-21: the cluster collector has only a `kubernetes-pods` job, so annotating a
  Service scrapes nothing. An external system is scraped by a collector it or its component runs;
  Service and EndpointSlice are for routes.

## Consequences

- Moving an existing component here renames its Kustomization: README,
  [Moving a component between groups](../../README.md#moving-a-component-between-groups).
- Adding a host is an edit to an EndpointSlice; nothing discovers hosts on its own.
- Everything in the group is LAN topology: `scripts/make-public.py` rewrites the addresses, or
  excludes a component that also holds credentials (`delete_paths`).

## References

- PR #950 (this record, first component `kube-cluster/external/ceph`).
