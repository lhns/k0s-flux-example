# kube-state-metrics

[kube-state-metrics](https://github.com/kubernetes/kube-state-metrics) (Helm chart, pinned in
`release.yaml`) exporting the state of **Flux objects only**, as `gotk_resource_info`
([ADR 0065](../../../docs/adr/0065-flux-state-metrics-from-kube-state-metrics.md)).

## Why custom resources only

Core objects (pods, deployments, nodes ...) already come from the OTel `k8s_cluster` receiver
([opentelemetry-monitoring](../opentelemetry-monitoring/README.md)). The chart runs with
`collectors: []` and `--custom-resource-state-only=true`, so nothing is exported twice. OTel has
no receiver that turns a custom resource's status into metrics; this is the gap KSM fills.

## What it exports

One series per Flux object and kind, value 1:

```
gotk_resource_info{customresource_group, customresource_kind, customresource_version,
                   name, exported_namespace, ready, reason, suspended}
```

- `ready`: status of the `Ready` condition (`True`, `False`, `Unknown`; absent if the object has none).
- `reason`: the `Ready` condition's reason (low cardinality).
- `suspended`: `spec.suspend`.
- Kinds: Kustomization, HelmRelease, GitRepository, OCIRepository, HelmRepository, HelmChart,
  Bucket, ExternalArtifact, ArtifactGenerator, Receiver. Alert and Provider have no status.
- Deliberately no `revision`, `url` or condition `message`: they change on every commit or
  attempt and would churn series and split alert instances. The upstream
  [flux2-monitoring-example](https://github.com/fluxcd/flux2-monitoring-example) config has them.

The config follows the CRD versions served by `flux-system/gotk-components.yaml`; update it with
a Flux bump that changes one (Alert and Provider move to v1 in Flux 2.10; not exported here).
RBAC is the chart's CRD list/watch rule plus list/watch on exactly the Flux resources above.

## Scraping

The pod carries `prometheus.io/scrape|port|path` and joins the `otel-scrape` vnet as ingress;
`kube-state-metrics` is in the cluster collector's `namespaces.names`
([opentelemetry-monitoring/release.yaml](../opentelemetry-monitoring/release.yaml)). Egress to the
API server needs no rule (kube-vnet isolates ingress only).

## Check

```
kubectl -n kube-state-metrics port-forward deploy/kube-state-metrics 8080
curl -s localhost:8080/metrics | grep gotk_resource_info
```
