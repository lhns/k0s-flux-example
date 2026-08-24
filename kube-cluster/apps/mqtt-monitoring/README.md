# mqtt-monitoring

`ghcr.io/lhns/mqtt-monitoring` — subscribes to MQTT topics and exports them as OpenTelemetry metrics.
Migrated from Docker Swarm.

## Shape
- **Stateless** — no volume, no database, and **no inbound ports** (no Service, no Traefik routing). It
  only makes outbound connections: MQTT to mosquitto, and OTLP to the collector. Single Deployment,
  start-first rolling updates (matches the Swarm `order:start-first`).
- **MQTT** — repointed from the old Swarm broker (`10.20.2.10:1883`) to the **in-cluster mosquitto**
  (`mosquitto.mosquitto.svc.cluster.local:1883`). Auth is the `mqtt_monitoring` LDAP user (mosquitto
  authenticates against the in-cluster lldap). Reachability: the pod carries
  `kube-vnet/net.mosquitto.mosquitto: egress` and joins the new `mosquitto` VirtualNetwork
  (`apps/mosquitto/vnet.yaml`).
- **Config** — the full HOCON config (server, credentials, metric filters) holds the MQTT password, so
  the whole `CONFIG` is a **SOPS secret** (`config-secret.yaml`), injected as the `CONFIG` env var. Edit
  it with `sops`. (Only the broker address was changed from the Swarm config.)
- **OTLP** — exports to the in-cluster gateway
  (`opentelemetry-gateway.opentelemetry-gateway.svc.cluster.local:4317`). It was the Swarm
  collector at `10.20.2.10:4317`.
- **kube-vnet**: egress-only membership in **two** vnets — `mosquitto` (the broker) and
  `opentelemetry-gateway.otel-gateway` (the OTLP endpoint). The latter is needed because a
  ClusterIP is isolated where the old external address was not: drop that label and telemetry
  stops silently. No ingress (nothing connects to it).

## Migration
Stateless, no data or DB to migrate — deploying the manifests + repointing the broker is the whole
migration. Stop the Swarm `mqtt-monitoring` stack once the in-cluster one is exporting.
