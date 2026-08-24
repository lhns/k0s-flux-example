# Home Assistant

The home-automation hub, at `homeassistant.example.com`.

It is **additive**: node-red keeps running untouched, including its Google Home local
fulfillment on `10.20.2.16` and the `app.example.com` dashboards. Whether HA eventually
subsumes any of that is a separate decision.

## Shape

- **Storage** — `/config` on `cephfs` (RWX, 10Gi), `strategy: Recreate`. cephfs rather
  than block because the recorder is on Postgres, so there is no SQLite here and the
  volume holds only files — the same reasoning grafana uses. `Recreate` is what enforces
  the single writer, *not* the access mode: two instances writing `.storage` corrupts it.
- **Database** — the recorder writes to the shared CNPG cluster
  (`postgres-rw.postgres:5432/homeassistant`), not the default SQLite file. Keeps a
  constant write stream off Ceph RBD and puts history in the existing Postgres backups,
  leaving `/config` holding only `.storage`, `custom_components/` and blueprints.
- **Auth** — HA's own login, **no Authelia forwardAuth**, plus SSO through the `auth_oidc`
  custom component against Authelia's OIDC provider (client `homeassistant`,
  `group:admin` / `group:homeassistant`, two_factor).
- **MQTT** — `mosquitto.mosquitto.svc.cluster.local:1883` as the `mqtt_homeassistant`
  LDAP user, added **through the UI**: Home Assistant removed broker settings from YAML,
  so this one cannot be declared in git. Zigbee devices arrive via zigbee2mqtt discovery.
- **kube-vnet** — `net.traefik.traefik: ingress`, `net.mosquitto.mosquitto: egress`,
  `net.postgres.postgres: egress`, `net.matter-server.matter-server: egress`. The macvlan
  interface (`net1`) is invisible to kube-vnet — see *Discovery* below.
- **Config** — split. `packages.yaml` in git becomes a hashed ConfigMap mounted at
  `/config/packages/gitops.yaml`; everything else in `/config` is writable and HA-owned.

## Why there is no Authelia middleware

Every other route in this repo that fronts a UI without native auth gets
`middlewares: [- name: authelia]`. HA must not, and a path bypass does not rescue it.

The companion app and every REST client authenticate with
`Authorization: Bearer <HA long-lived token>`. Authelia's forwardAuth — including the
`authelia-basicauth` variant — wants to own that same header, and a client cannot satisfy
both at once. This is why the FreshRSS `^/api/` bypass pattern does not transfer:
FreshRSS's API auth does not use `Authorization` at all.

So HA follows the grafana/immich/outline/sftpgo pattern instead — its own login, with
Authelia as an **OIDC provider**. `http.ip_ban_enabled` is the compensating control for
being internet-reachable behind only that login.

**Keep at least one native HA account.** `auth_oidc` is third-party and sits in the login
path; a broken component upgrade must not lock you out.

## One-time setup

1. **Create the MQTT user in lldap** — user `mqtt_homeassistant`, `memberOf=mqtt`, with
   the password stored in `secret.yaml` under `mqtt-password`. mosquitto authenticates
   against lldap, so this cannot be done from git.
2. **DNS** — nothing to do: `homeassistant.example.com` is already covered by the `*.example.com`
   wildcard, the same as every other app here.
3. **Onboarding** — open the UI, create the owner account. Then log out and use the
   *Authelia* button to sign in; `auth_oidc` creates the matching HA user on first use.
4. **Add yourself to the `homeassistant` lldap group** (or rely on `admin`), otherwise
   Authelia's `homeassistant` authorization policy denies the OIDC flow.
5. **Add the MQTT integration** — Settings → Devices & Services → Add Integration → MQTT,
   broker `mosquitto.mosquitto.svc.cluster.local`, port `1883`, username
   `mqtt_homeassistant`, password from `secret.yaml`. This is a config entry stored in
   `.storage`, not YAML — Home Assistant no longer accepts broker settings in
   `configuration.yaml`, so it cannot be declared in git.

Everything else — `configuration.yaml`, `secrets.yaml`, the packages include, and the
`auth_oidc` install — is seeded automatically by the initContainer, idempotently.

## Operational notes

- **Rotating the DB password**: update `db-role-secret.yaml`, and keep the new password
  **url-safe (alphanumeric)**. The initContainer interpolates it into a
  `postgresql://user:<pw>@host/db` DSN *without url-encoding*, so a `@` or `:` would
  silently produce a malformed connection string. Reloader restarts the pod, which
  re-renders `secrets.yaml`.
- **Rotating the OIDC secret** means regenerating both halves — the plaintext in
  `secret.yaml` and the pbkdf2 digest in `apps/authelia/configuration.yml`:
  ```
  authelia crypto hash generate pbkdf2 --variant sha512 --random --random.length 72 --random.charset rfc3986
  ```
- **Upgrading `auth_oidc`**: bump the tag in `resources.yaml` *and* delete
  `/config/custom_components/auth_oidc` in the volume — the initContainer skips the
  install when the directory already exists.
- **`!env_var` does not exist in Home Assistant.** It was removed from the YAML loader;
  only `!secret` works, which is why the initContainer renders `secrets.yaml`.
- **A `400: Bad Request` at login** almost always means `http.trusted_proxies` no longer
  matches. Traefik is a DaemonSet in the pod network, so the forwarded connection arrives
  from `172.18.0.0/16` — not a node address.

## Discovery, and why the LAN interface carries only that

Home Assistant has a **second interface, `net1` on `10.20.3.9`** (macvlan via `infra/multus`),
so mDNS and SSDP multicast reach it and Chromecast/Hue/Sonos discovery works.

It used to have a MetalLB VIP, `10.20.2.7`. That was **deleted**, not kept alongside: a VIP is
unicast and NAT'd, so it could never carry discovery, and everything else it served — the UI,
the API, the companion app, webhooks — is served by Traefik over TLS at
`homeassistant.example.com`. It was a plain-HTTP LAN address with no remaining purpose.

**The web server IS on `net1`, and should not be.** Attaching `net1` publishes HA's full UI and
API on the LAN, behind only HA's own login — no Authelia, and invisible to kube-vnet, which does
not police secondary interfaces.

`http.server_host` used to prevent this by binding the pod IP. It had to be dropped: **HA 2026.8**
keeps a *stable* and a *pending* http config, and a changed `server_host` reverts after 5 minutes
unless confirmed. The pod IP changes every restart, so the config was pending on every start,
never confirmed, and HA reverted to a stable entry naming a dead pod's IP — binding `127.0.0.1`
only and restart-looping. See `packages.yaml`, and TODO.md for the replacement (an iptables rule
dropping TCP 8123 on `net1`).

The pod IP changes on every restart and HA has no `!env_var`, so the initContainer renders it
into `secrets.yaml` from the downward API and the package references it with `!secret`.

### After a rebuild, do this

Pick **net1** in *Settings → System → Network*. HA auto-detects its discovery adapter from
the route to `224.0.0.251`, which resolves via `eth0` — so it will otherwise listen on the
wrong interface and discovery will simply appear not to work.

### Caveats

- `net1` is outside kube-vnet entirely; NetworkPolicy does not apply to secondary interfaces.
- A macvlan pod cannot reach its own node's IP.
- If you enable the **HomeKit bridge** later it binds all interfaces, including `net1` — which
  is what it needs, but it is exposure nothing currently constrains — see TODO.md.
- `10.20.3.9` must stay outside the DHCP pool.

## Matter and Thread

Both are working. The pieces, since they are easy to conflate:

- **matter-server** (`apps/matter-server`) — the Matter *controller*. Deployed in-cluster on
  its own macvlan address, reached here at
  `ws://matter-server.matter-server.svc.cluster.local:5580`.
- **OTBR** (`apps/otbr`) — the Thread *border router*, an IPv6 router between the 802.15.4
  mesh and the LAN. Deployed **in-cluster** on macvlan `10.20.3.7`, driving the SLZB-MR5U's
  Thread SoC in RCP mode over serial-over-IP. Add it in HA by URL
  (`http://otbr.otbr.svc.cluster.local:8081`); mDNS auto-discovery of border routers will not
  find it, because that discovery happens over IPv6 link-local multicast which `net1` receives
  but HA only scans on its selected adapter.

  It previously ran **on the SLZB itself** at `http://10.20.1.50:8080`. That mode is gone while
  the radio is an RCP — one Thread radio means the two are mutually exclusive. Note the port
  differs (`:8081`, not `:8080`), and that switching the radio's mode takes the on-device REST
  API away with it, so **export the dataset before flipping back**.
- **Matterbridge** — points the other way, exposing non-Matter devices *as* Matter to
  Apple/Google/Alexa. Redundant when HA is the hub, since zigbee2mqtt already reaches HA
  over MQTT.

The SLZB-MR5U has two independent SoCs — Zigbee on `:6638` for zigbee2mqtt, and the Thread
radio — so Thread does not contend with Zigbee. One box, though: a firmware update or reboot
takes both down together.

Note that `python-matter-server` is end-of-life; the deployed controller is its successor,
`matterjs-server`. See `apps/matter-server/README.md`.
