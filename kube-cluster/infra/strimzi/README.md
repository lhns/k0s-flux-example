# strimzi

The [Strimzi](https://strimzi.io) Kafka operator. **Operator only** — the `Kafka`,
`KafkaNodePool` and `KafkaTopic` CRs live with their consumer in
`infra/opentelemetry-gateway/`, the same split CNPG uses (operator in `infra/postgres`,
`Database`/`DatabaseRole` CRs in each app's own directory).

- `release.yaml` — Namespace + `OCIRepository`
  (`oci://quay.io/strimzi-helm/strimzi-kafka-operator`, tag = the operator version) +
  `HelmRelease`. Same shape as `infra/kube-vnet`; Renovate tracks `ref.tag` via the
  Flux manager.

## Values that matter

- **`watchAnyNamespace: true`** — cluster-wide watch, like CNPG and kube-vnet.
- **`generateNetworkPolicy: false`** — kube-vnet owns network policy here. Strimzi's
  generated NetworkPolicies are *additive*, so leaving them on would open paths through
  the default-deny baseline and create a second source of truth for the same edges.
  Every ingress edge to Kafka is therefore an explicit vnet membership in
  `infra/opentelemetry-gateway/vnet.yaml` (broker `:9092` clients, `:9091` replication,
  `:9090` controller, `:8443` kafka-agent).
  *If a `Kafka` CR ever hangs `NotReady` with the cluster operator logging connection
  timeouts to broker pods, set this `true` temporarily to confirm it is a policy problem,
  then fix the vnet — don't leave it on.*
- `createGlobalResources: true` — CRDs and ClusterRoles are chart templates, so Flux
  installs them with the release.

## Upgrading

**Check the [CHANGELOG](https://github.com/strimzi/strimzi-kafka-operator/blob/main/CHANGELOG.md)
before bumping `ref.tag`.** The supported Kafka versions move with the operator, and
dropping a Kafka minor is a breaking change for any `Kafka` CR pinned to it — 1.1.0
supports only Kafka **4.3.0 / 4.2.1** (4.1.x was dropped). Bump `spec.kafka.version` in
`infra/opentelemetry-gateway/kafka.yaml` in the same change when required.

Note the API group is `kafka.strimzi.io/v1` (the `v1alpha1`/`v1beta1`/`v1beta2` versions
were removed in 1.0.0). `scripts/validate.sh` may not find a kubeconform schema for it in
the datreeio CRDs catalog yet; the script runs with `-ignore-missing-schemas`, so that
degrades to *unchecked* rather than a lint failure.
