# teamspeak

TeamSpeak 3 voice server, migrated from Docker Swarm.

## Shape
- **Server:** `teamspeak:3.13.8`, raw ports only — voice UDP 9987, ServerQuery TCP 10011, file transfer TCP
  30033 — on a dedicated MetalLB VIP **`10.20.2.9`** (`teamspeak-lb`, `externalTrafficPolicy: Local`). No
  Traefik/HTTP. Runs as uid/gid 9987.
- **DB:** self-managed **MariaDB** `teamspeak-db` (`mariadb:11.8.8`) on `ceph-rbd` — TeamSpeak supports only
  SQLite or MariaDB. Root password in the SOPS secret (used as both `MARIADB_ROOT_PASSWORD` and
  `TS3SERVER_DB_PASSWORD`).
- **Files:** `/var/ts3server` (4.4G file-transfers + logs) **mounted in place** off the appdata CephFS via a
  static PV (`rootPath: /docker/teamspeak/data`) — not copied. Stays on the HDD tier.
- **kube-vnet:** `teamspeak-db` bare vnet; server `net.teamspeak-db: egress`, DB `net.teamspeak-db: ingress`.
  LB traffic is auto-allowed (no ingress label).

## One-time migration
The DB must be restored into the empty MariaDB *before* TeamSpeak first boots, or `create_mariadb` initialises
a FRESH server. And because `/var/ts3server` is mounted in place, the **Swarm TeamSpeak server must be stopped**
before the kube one scales up (no two writers). Hence it ships at `replicas: 0`.

1. Deploy (this dir + the `teamspeak-vip` pool in `infra/metallb/pool.yaml`); wait for `teamspeak-db` `1/1`.
2. Stop the Swarm `teamspeak` server (keep the Swarm DB up briefly for the dump).
3. Logical dump + restore (same DB, MariaDB → MariaDB, drops the stale core/redo):
   ```
   mariadb-dump -h 10.20.2.10 -P 3306 -u root -p<old> --add-drop-table --single-transaction teamspeak \
     | mariadb -h teamspeak-db -u root -p<new> teamspeak
   ```
   (one-shot Job on the `mariadb:11.8.8` image, labelled `kube-vnet/net.teamspeak-db: egress`).
4. Flip `teamspeak` to `replicas: 1`; it finds the restored DB (no fresh-init) and claims VIP `10.20.2.9`.
5. Forward the router / DNS for `9987/udp` (+ 10011, 30033 if used) → `10.20.2.9`; connect a client to verify
   the server, channels, permissions, and a preserved channel icon (proves the in-place file store).
6. Remove the Swarm `teamspeak` stack + its leftover DB dir.
