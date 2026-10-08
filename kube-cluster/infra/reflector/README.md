# reflector (kube-cluster)

The shared base [shared/infra/reflector](../../../shared/infra/reflector/README.md) with this
cluster's version.

`apps/grafana` uses it: the `grafana-db` credential lives once in the `postgres` namespace (for
the CNPG `DatabaseRole`) and is reflected into `grafana` (for `GF_DATABASE_PASSWORD`).
