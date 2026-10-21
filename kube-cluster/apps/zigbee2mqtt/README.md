# zigbee2mqtt

`ghcr.io/koenkk/zigbee2mqtt` — Zigbee↔MQTT bridge. Frontend at
`zigbee2mqtt.example.com` (behind Authelia).

## Shape
- **Network coordinator** — `serial.port: tcp://10.20.62.170:6638` (adapter `ember`). No USB device, no
  node-pinning; the coordinator is reached over the LAN as external egress.
  That box is an **SLZB-MR5U with two independent SoCs**: this Zigbee radio on `:6638`, and a second
  radio serving Thread as a bare **RCP** on `:7638`, driven by the in-cluster border router (see
  `apps/otbr`). They run concurrently on separate channels —
  Thread does **not** contend for
  this coordinator. One physical caveat: it is still one box, so a firmware update or reboot takes
  Zigbee and Thread down together. Switching the Thread radio's mode restarts the device and this pod
  reconnects on its own (observed: one container restart, devices reporting again within a minute).
- **State** — `/app/data` (database.db + state.json = the paired network, configuration.yaml,
  coordinator_backup.json) on a **ceph-rbd RWO** volume.
  Single-writer (one coordinator owner) → `strategy: Recreate`, never two instances. Runs as root, so
  the RBD volume is writable directly (no fsGroup/initContainer).
- **MQTT** — in-cluster mosquitto
  (`mqtt://mosquitto.mosquitto.svc.cluster.local:1883`, set in `configuration.yaml`); the
  `mqtt_zigbee2mqtt` LDAP user authenticates against the in-cluster lldap. The password lives in the
  volume's `configuration.yaml`, not git — so no SOPS secret.
- **Auth** — the frontend is behind the reflected `authelia` forwardAuth (`group:admin`/`zigbee_admin`,
  `two_factor` — rule already in the Authelia config).
- **kube-vnet** — `net.traefik.traefik: ingress` (frontend) + `net.mosquitto.mosquitto: egress` (MQTT);
  coordinator + OTA are external egress.

The Zigbee radio accepts ONE client, so two instances never run at once.
