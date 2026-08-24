# zigbee2mqtt

`ghcr.io/koenkk/zigbee2mqtt` — Zigbee↔MQTT bridge, migrated from Docker Swarm. Frontend at
`zigbee2mqtt.example.com` (behind Authelia).

## Shape
- **Network coordinator** — `serial.port: tcp://10.20.1.50:6638` (adapter `ember`). No USB device, no
  node-pinning; the coordinator is reached over the LAN as external egress (confirmed routable from the
  nodes). The commented USB `swarm-launcher` in the old compose is vestigial.
  That box is an **SLZB-MR5U with two independent SoCs**: this Zigbee radio on `:6638`, and a second
  radio serving Thread as a bare **RCP** on `:7638`, driven by the in-cluster border router (see
  `apps/otbr`; it used to run OTBR on-device instead). They run concurrently on separate channels —
  Thread does **not** contend for
  this coordinator. One physical caveat: it is still one box, so a firmware update or reboot takes
  Zigbee and Thread down together. Switching the Thread radio's mode restarts the device and this pod
  reconnects on its own (observed: one container restart, devices reporting again within a minute).
- **State** — `/app/data` (database.db + state.json = the paired network, configuration.yaml,
  coordinator_backup.json) on a **ceph-rbd RWO** volume, copied once from the Swarm volume.
  Single-writer (one coordinator owner) → `strategy: Recreate`, never two instances. Runs as root, so
  the RBD volume is writable directly (no fsGroup/initContainer).
- **MQTT** — repointed from the old Swarm broker to the in-cluster mosquitto
  (`mqtt://mosquitto.mosquitto.svc.cluster.local:1883`, edited in the copied `configuration.yaml`); the
  `mqtt_zigbee2mqtt` LDAP user authenticates against the in-cluster lldap. The password lives in the
  volume's `configuration.yaml`, not git — so no SOPS secret.
- **Auth** — the frontend is behind the reflected `authelia` forwardAuth (`group:admin`/`zigbee_admin`,
  `two_factor` — rule already in the Authelia config).
- **kube-vnet** — `net.traefik.traefik: ingress` (frontend) + `net.mosquitto.mosquitto: egress` (MQTT);
  coordinator + OTA are external egress.

## One-time migration
1. Deploy with the Deployment at `replicas: 0` (namespace, PVC, Service, route come up).
2. **Copy `/app/data`** from `10.20.2.10:/mnt/fastappdata/docker/zigbee2mqtt` into the PVC, repointing
   MQTT in `configuration.yaml` (`10.20.2.10:1883` → `mosquitto.mosquitto.svc.cluster.local:1883`).
3. **Cutover** — the Zigbee radio accepts ONE client (true per radio; the Thread SoC is separate and
   unaffected): **stop the Swarm zigbee2mqtt first**, then flip the Deployment to `replicas: 1`.
4. Point `zigbee2mqtt.example.com` (+ `.kube`) DNS at the Traefik LB `10.20.2.15`.
