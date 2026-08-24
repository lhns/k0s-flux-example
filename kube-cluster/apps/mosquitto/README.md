# mosquitto

Eclipse Mosquitto MQTT broker with the `mosquitto-go-auth` plugin, migrated from Docker Swarm.

## Shape
- **Two listeners**: raw **MQTT/TCP :1883** (native clients) and **MQTT-over-WebSockets :9001**.
- **TCP exposure**: via a dedicated **`mqtt` Traefik entrypoint on :1883** (`infra/traefik`) + an
  `IngressRouteTCP` (`HostSNI(*)`, plain-TCP passthrough) → `mosquitto:1883`. Native clients connect to
  **10.20.2.15:1883** (Traefik's VIP) or `mqtt.example.com:1883`. (MetalLB IP-sharing was not usable because
  Traefik runs `externalTrafficPolicy: Local`, so the entrypoint reuses Traefik's own VIP instead.)
- **WS exposure**: Traefik `IngressRoute` on `mqtt.example.com` (addPrefix `/mqtt`, replicating the Swarm
  router) → `mosquitto:9001`. `mqtt.example.com` stays the canonical broker host (not switched to `.kube`
  — clients are configured with it). `mqtt.example.net` was dropped (not in the `*.example.com` cert).
- **Auth**: every client is authenticated against **LDAP (lldap)** via `mosquitto-go-auth`
  (`ldap://10.20.2.10:3890`, users in `memberOf=mqtt`, superusers `mqtt_superuser`). Auth is at the MQTT
  protocol layer, so there is no HTTP auth middleware on the WS route.
- **Config** (`config-secret.yaml`, SOPS): the whole `mosquitto.conf` — it carries the LDAP bind
  password — mounted read-only at `/etc/mosquitto`. The only change from the Swarm file is
  `auth_opt_ldap_url` (was the overlay `auth_lldap:3890`).
- **State**: persistence file `mosquitto.db` on a dedicated Ceph RBD volume (`/var/lib/mosquitto`),
  copied once from `fastappdata:/docker/mosquitto/data`. No external database.
- Single replica, `strategy: Recreate` (RWO). OTEL traces to the in-cluster gateway
  (`opentelemetry-gateway.opentelemetry-gateway:4317`), which is a ClusterIP and so needs the
  `net.opentelemetry-gateway.otel-gateway` egress label — the Swarm collector it replaced was
  external and needed none.

## One-time migration
1. Deploy at `replicas: 0` (namespace, PVC, config secret, services come up).
2. Copy `fastappdata:/docker/mosquitto/data` (mosquitto.db) into the `mosquitto-data` RBD PVC.
3. Flip to `replicas: 1`.
4. Point `mqtt.example.com` DNS at the Traefik VIP `10.20.2.15` and repoint native clients to
   `10.20.2.15:1883`. Stop the Swarm broker.

## Note
Raw MQTT/TCP required adding a `mqtt` entrypoint (:1883) to the Traefik chart (`infra/traefik`), since
MetalLB can't share Traefik's VIP with a second LoadBalancer while Traefik uses
`externalTrafficPolicy: Local`.
