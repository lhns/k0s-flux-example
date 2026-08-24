# nodered

Node-RED (`nodered/node-red`, flow automation), migrated from Docker Swarm. Fronts four Traefik
hostnames plus a set of raw TCP/UDP LAN ports for Google Home local fulfillment.

## Shape
- **File-based, no database** — all state is in `/data` (flows, `flows_cred.json`, `settings.js`,
  `.config.*`, `node_modules`, `projects/`), on a **CephFS** volume (`nodered-data`), copied once from
  the old Swarm volume `fastappdata:/docker/nodered` (excluding its ~189M `log/`). The credentialSecret
  lives in `settings.js`/`.config`, so copying `/data` keeps `flows_cred.json` decryptable — no k8s
  secret needed. No OIDC client.
- **Single-instance** — flows, `node_modules`, and the LAN port bindings can't be shared, so
  `strategy: Recreate` (brief downtime on updates), never two writers.
- **HTTP via Traefik** (all hosts under the `*.example.com` wildcard, `tls: {}`):
  - `nodered.example.com` — the editor, behind Authelia (`group:admin`/`nodered`, rule already in the
    Authelia config). Public sub-path `/webhook/` (no auth). `/app/*` redirects to `app.example.com`.
  - `webhook.example.com` — public; `addPrefix /webhook` → `nodered:1880/webhook/*`.
  - `app.example.com` — the dashboard app, behind Authelia (`addPrefix /app`; `authelia` runs first so it
    sees the pre-prefix path the Authelia `app.example.com` rules match). Public.
  - `google-smarthome.example.com` — public; the `node-red-contrib-google-smarthome` cloud fulfillment
    endpoint on `:3001`.
- **Raw LAN ports** for Google Home **local** fulfillment (UDP discovery/command): a dedicated MetalLB
  LoadBalancer `nodered-lb` on **`10.20.2.16`** (`infra/metallb` `nodered-vip` pool), `externalTraffic
  Policy: Local`. Ports: UDP `6987`/`6988`/`6989`/`8882` + TCP `8882`. Traefik's VIP can't be shared
  (`Local` ETP) or carry UDP, hence the second IP. The Swarm `7021-7030` TCP range (unrelated flows)
  was dropped.
- **kube-vnet**: `net.traefik.traefik: ingress` for the HTTP ports; the LoadBalancer LAN ports are
  auto-allowed (`ext.svc`). Internet + old `10.20.2.x` hosts are external egress (unrestricted); add a
  `net.<ns>.<vnet>: egress` label if a flow needs an in-cluster service (e.g. mosquitto).

## One-time migration
1. Deploy with the Deployment at `replicas: 0` (namespace, PVC, both Services come up; `nodered-lb`
   gets `10.20.2.16`).
2. **Copy `/data`** from `10.20.2.10:/mnt/fastappdata/docker/nodered` (excluding `log/`) into the PVC
   (`chown -R 1000:1000`).
3. Flip the Deployment to `replicas: 1`.
4. Point `nodered.example.com`, `webhook.example.com`, `app.example.com`, `google-smarthome.example.com` DNS at the
   Traefik LB `10.20.2.15`; point the google-smarthome node's **local fulfillment/scan** at `10.20.2.16`.
