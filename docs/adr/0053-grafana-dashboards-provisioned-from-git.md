# 0053 — Grafana dashboards provisioned from git

- Status: Superseded by [0058](0058-grafana-operator-manages-grafana-and-its-content.md)
- Date: 2026-10-08, amended 2026-10-20
- Current answer: no longer used. Dashboards kept in git were file-provisioned from
  `apps/grafana/dashboards/`, one provider and ConfigMap per Grafana folder (`CI`, `Cluster`,
  `Postgres`), and were read-only in the UI; the JSON was edited in the repo.

## Context

Grafana keeps its dashboards in Postgres, so none are reviewable, reproducible or restored with
the cluster. Dashboards should not drift when someone edits them in the UI, and the metrics they
need are enabled in the same change.

## Decision

- Dashboards that live in git are file-provisioned into the folder `CI`: JSON under
  `apps/grafana/dashboards/`, shipped by a `configMapGenerator` (a content change renames the
  ConfigMap and restarts Grafana, [0025](0025-manifest-rules-namespaces-and-config-rollout.md)).
- The provider sets `allowUiUpdates: false` and `disableDeletion: true`: the UI cannot save over
  them. To change one, edit the JSON in git; to experiment, "Save as" a copy, which lands in the
  database, and export it back to git when it is worth keeping.
- Dashboards already in the database stay there; moving them is not part of this decision.
- Dashboards pick the datasource through a `datasource` variable of type
  `victoriametrics-metrics-datasource` instead of a hard-coded uid.
- **One provider and ConfigMap per folder** (2026-10-20): `dashboards/<folder>/` feeds folder
  `<Folder>` (`CI` from the top level, `Cluster`, `Postgres`). App metrics reach Grafana through the
  OTel collector's annotation scrape, never a `PodMonitor`.

## Consequences

- A dashboard edit is a restart of Grafana (brief; its state is in Postgres).
- ConfigMaps cap at 1 MiB, so a few hundred KiB of dashboards fit; past that, split the generator.
- Metrics a dashboard needs are enabled in the same PR as the dashboard (see
  [`apps/arc-controller`](../../kube-cluster/apps/arc-controller/README.md)).

## References

- `24589638`; superseded by [0058](0058-grafana-operator-manages-grafana-and-its-content.md).
