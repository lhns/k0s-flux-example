# teamspeak

TeamSpeak 3 voice server.

## Shape
- **Server:** `teamspeak:3.13.8`, raw ports only — voice UDP 9987, ServerQuery TCP 10011, file transfer TCP
  30033 — on a dedicated MetalLB VIP **`10.20.176.236`** (`teamspeak-lb`, `externalTrafficPolicy: Local`). No
  Traefik/HTTP. Runs as uid/gid 9987.
- **DB:** self-managed **MariaDB** `teamspeak-db` on `ceph-rbd` — TeamSpeak supports only
  SQLite or MariaDB. Root password in the SOPS secret (used as both `MARIADB_ROOT_PASSWORD` and
  `TS3SERVER_DB_PASSWORD`).
- **Files:** `/var/ts3server` (4.4G file-transfers + logs) **mounted in place** off the appdata CephFS via a
  static PV (`rootPath: /docker/teamspeak/data`) — not copied. Stays on the HDD tier.
- **kube-vnet:** `teamspeak-db` bare vnet; server `net.teamspeak-db: egress`, DB `net.teamspeak-db: ingress`.
  LB traffic is auto-allowed (no ingress label).

The DB must exist before TeamSpeak first boots, or `create_mariadb` initialises a FRESH server.
