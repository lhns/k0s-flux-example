# grafana-operator

Grafana Operator (Helm chart, version pinned in `release.yaml`). It deploys and manages the
Grafana instance described in [../../apps/grafana/README.md](../../apps/grafana/README.md).

## Scope and RBAC

Namespace-scoped: it watches only `grafana` (`WATCH_NAMESPACE`). Its Roles and
RoleBindings are manifests, not the chart's (`rbac.create: false`): `rbac.yaml` here for
`grafana-operator`, [`apps/grafana/rbac.yaml`](../../apps/grafana/rbac.yaml) for `grafana`. That keeps this
component from waiting for the `grafana` namespace and so for `infra-postgres`: it depends on
nothing, and only `app-grafana` depends on it (ADR 0060). Until `app-grafana` applies, the operator cannot watch `grafana` and
restarts. On a chart bump, `helm template` it and compare the Roles' rules. No ClusterRole. The CRDs are cluster-wide and
are installed by the chart.

## Authentication to Grafana

The operator calls Grafana's API with its projected ServiceAccount token (audience
`operator.grafana.com`), accepted by Grafana's `[auth.jwt]`. The `fullnameOverride` keeps the
ServiceAccount at `grafana-operator:grafana-operator`, which Grafana's `expect_claims` trusts.

## Network

VirtualNetwork `grafana-api` lets the operator pod (egress) reach Grafana's pod (ingress, joined
from namespace `grafana`) on :3000.

## Metrics

Scraped by pod annotation on :9090 (`/metrics`); no ServiceMonitor.

## Upgrading

Bump the OCIRepository tag in `release.yaml`. The CRDs upgrade with it (default `CreateReplace`).
