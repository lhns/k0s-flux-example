# Migrating Docker Swarm services to Kubernetes (k0s + Flux)

A self-contained runbook for moving a service off the legacy Docker Swarm host into **this** cluster.
It is written so that a person — or an AI handed a `docker-compose.yml` / stack file — can do a migration
end to end without prior knowledge of the cluster. Read Chapter 3 (the workflow) first, then dip into the
deep chapters and the per-service appendix as needed.

> **Footgun status tags** appear throughout: `[FIXED: …]` (already resolved, kept for history),
> `[CURRENT]` (still bites today), `[CONVENTION]` (a rule of this repo), `[ENV-SPECIFIC]` (depends on our
> hardware). A later pass can prune `[FIXED]` items.

## Table of contents
1. [Target stack at a glance](#1-target-stack-at-a-glance)
2. [Ground rules (safety)](#2-ground-rules-safety)
3. [The migration workflow](#3-the-migration-workflow)
4. [Databases: dump & migrate](#4-databases-dump--migrate)
5. [Volumes: mount, peek, migrate](#5-volumes-mount-peek-migrate)
6. [Networking (kube-vnet + Traefik + MetalLB)](#6-networking)
7. [Secrets & the GitOps generator](#7-secrets--the-gitops-generator)
8. [Cross-cutting footguns](#8-cross-cutting-footguns)
9. [Per-service appendix](#9-per-service-appendix)
10. [Reference tables](#10-reference-tables)

---

## 1. Target stack at a glance

| Concern | What we run | Key facts |
|---|---|---|
| Orchestrator | **k0s** + **Flux** GitOps | repo = `k0s-flux`; a generator HelmRelease renders one Kustomization per component dir |
| Ingress | **Traefik** DaemonSet via **MetalLB** | HTTPS VIP **`10.20.2.15`** — the address all `*.example.com` DNS points at; `websecure` (443) default entrypoint |
| TLS | **cert-manager** wildcard | `*.example.com` / `*.kube.example.com` cert + `TLSStore/default` → routes just write `tls: {}` |
| Network policy | **kube-vnet `0.7.1`** | default-**deny** ingress everywhere (even same-namespace); membership by pod label |
| SSO | **Authelia** forwardAuth | reflected `Middleware` `authelia`; attach per-route, or skip for apps that own auth |
| Block storage | Ceph **RBD** → SC `ceph-rbd` | RWO, pool `rbd.kube`; **databases, SQLite** |
| Shared FS | Ceph **CephFS** → SC `cephfs` | RWX, filesystems `fastappdata` (SSD) / `appdata` (HDD); media/config/**mount-in-place** |
| Object store | Ceph **RGW** S3 | `https://s3.example.com` (path-style, region placeholder `us-east-1`); admin via `radosgw-admin` on the mgr nodes |
| Shared Postgres | **CloudNativePG** | `infra/postgres` Cluster, service `postgres-rw.postgres.svc.cluster.local:5432`, PG **18.4** |
| Secrets | **SOPS + age** | single recipient; encrypt on the jumphost |
| Jumphost | `lhns@10.20.5.15` (a.k.a. `debian-01`) | has `sops 3.13.2` + `age`, `kubectl`, `flux`; SSH to hosts |
| Swarm host (source) | `10.20.2.10` | **read-only**; dumped over the network into the cluster |
| Ceph mgr / RGW nodes | `10.20.2.101/102/103` | `ceph`, `radosgw-admin` here; **read-only, never destructive** |

Windows note: this repo is edited from a Windows checkout. `kubectl`/`flux`/`sops` are driven **from the
jumphost** over SSH, not locally.

---

## 2. Ground rules (safety)

These are hard constraints — they never relax:

- **Never touch the Swarm.** Do not modify anything or any folder on the Swarm host; you may only *read* it
  (dump DBs, inspect volumes) and only *remove stale resources from kube*. The Swarm stays running as the
  rollback target until cutover is verified.
- **Ceph & Proxmox are read-only.** You may SSH to Ceph nodes to run `ceph -s` / `radosgw-admin` **reads**,
  but never destructive operations, and **ask before changing any config**. Don't use the Proxmox host for
  tests — use the jumphost.
- **Secrets:** only read the DB secrets you actually need for a migration; don't go spelunking others.
- **Outward/irreversible actions** (deleting data, cutting DNS, stopping a source service) happen only after
  the kube side is verified, and the user performs the Swarm-side stop.

---

## 3. The migration workflow

The spine. Each step links to its deep chapter.

**1. Read the compose/stack file.** Inventory, per service: image (+ tag), command/env, published ports,
named/bind volumes, networks, and secrets. Note which services are databases.

**2. Classify each service** into one of four data patterns — this decides everything downstream:
| Pattern | When | Where it goes |
|---|---|---|
| **stateless** | no volume, or volume is disposable cache | Deployment only |
| **shared-CNPG** | Postgres-backed app | fold into `infra/postgres` (Ch. 4) |
| **self-managed DB** | Mongo / MySQL / Redis it needs | StatefulSet in the app ns (Ch. 4) |
| **mount-in-place** | large existing data dir (media, metrics, mail blobs) | static CephFS PV over the Swarm dir (Ch. 5) |

**3. Scaffold `kube-cluster/apps/<name>/`** with the standard file set (Ch. 7):
`resources.yaml`, `routing.yaml`, `vnet.yaml`, `secret.yaml`, `kustomization.yaml`
(+ `database.yaml` & `db-role-secret.yaml` for CNPG apps).

**4. Translate services → workloads.** `Deployment` (stateless) or `StatefulSet` (owns a PVC/identity) + a
`Service`. **Every object sets its own `metadata.namespace`. Never add a top-level `namespace:` transformer**
to a `kustomization.yaml` (Ch. 7 explains why). Compose `depends_on` → readiness ordering (initContainers or
just let it retry); compose `networks` are irrelevant (kube-vnet replaces them).

**5. Storage** → Chapter 5. **6. Databases** → Chapter 4.

**7. Networking** → Chapter 6: add kube-vnet edges (labels + `vnet.yaml`), a Traefik `IngressRoute`, decide
Authelia yes/no, and a MetalLB VIP if it needs raw TCP ports.

**8. Secrets** → Chapter 7: SOPS-encrypt on the jumphost.

**9. Images:** prefer a real version tag; if the image only publishes `:latest`, **digest-pin** it
(`image: repo:latest@sha256:…`). Renovate tracks both.

**10. Commit & push.** Flux applies automatically (a GitHub webhook reconciles within seconds; the generator
uses `reconcileStrategy: Revision` so a **new directory** is picked up without a chart-version bump). Two
manual nudges you will occasionally need:
```bash
# a NEW app/infra dir was added — re-render the generator so its Kustomization exists:
flux reconcile helmrelease generators -n flux-system --with-source
# force one component, or clear a stale "failed" health after slow image pulls tripped progressDeadline:
kubectl -n flux-system annotate kustomization app-<name> \
  reconcile.fluxcd.io/requestedAt=$(date +%s%N) --overwrite
```

**11. Verify, then cut over.** Pods `1/1`; `/health` (or equivalent) 200; TLS valid; kube-vnet path works
(`nc -zv -w3 host port`, **not** `nc -z` — see Ch. 8). Then the **user stops the Swarm service** and you
point DNS at `10.20.2.15` (or the app's MetalLB VIP for raw-port services).

---

## 4. Databases: dump & migrate

### 4.0 Decision tree
- **Postgres app →** reuse the shared **CNPG** cluster (§4.1). This is the default and the best-trodden path.
- **App needs Mongo/MySQL/Redis specifically →** self-managed StatefulSet in the app ns (§4.3–4.5).
- **Cross-engine (MariaDB/MySQL → PG) →** schema fresh + selective data copy, not a homogeneous dump (§4.6).

Governing rule: **dump the source over the network while the Swarm DB stays running** — stop only the *app*
(so nothing writes), never the source `db` container (if it's down you can't dump it).

### 4.1 Postgres → shared CNPG (the canonical path)

**Declare the DB + role** (both live in the **`postgres`** namespace, not the app ns):
```yaml
# database.yaml
apiVersion: postgresql.cnpg.io/v1
kind: DatabaseRole
metadata: {name: <app>, namespace: postgres}
spec:
  cluster: {name: postgres}
  name: <app>
  ensure: present
  login: true
  passwordSecret: {name: <app>-db}
  databaseRoleReclaimPolicy: retain
---
apiVersion: postgresql.cnpg.io/v1
kind: Database
metadata: {name: <app>, namespace: postgres}
spec:
  cluster: {name: postgres}
  name: <app>
  owner: <app>
  databaseReclaimPolicy: retain
```

**Credentials** — a `basic-auth` SOPS secret in `postgres`, **reflected** into the app ns by emberstack
Reflector (so the app can `secretKeyRef` it locally):
```yaml
# db-role-secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: <app>-db
  namespace: postgres
  annotations:
    reflector.v1.k8s.emberstack.com/reflection-allowed: "true"
    reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces: <app>
    reflector.v1.k8s.emberstack.com/reflection-auto-enabled: "true"
    reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: <app>
type: kubernetes.io/basic-auth
stringData:               # SOPS-encrypted (Ch. 7)
  username: <app>
  password: <generated — fresh, NOT the old Swarm password>
```
The CNPG password is **new**; the old Swarm password is used only to *read* the source dump.

**Dump → restore** with a one-shot Job (label it `kube-vnet/net.postgres.postgres: egress` so it may reach
CNPG). Use the CNPG image so client-major matches server-major (**same PG major is required for a clean
`pg_dump`/`pg_restore`** — we run **18.4**):
```bash
# inside a Job pod: SRC_PW = old Swarm pw (env), TGT_PW = <app>-db/password
PGPASSWORD="$SRC_PW" pg_dump -Fc --no-owner --no-acl \
  -h 10.20.2.10 -p <srcport> -U postgres -d <srcdb> -f /tmp/x.dump
PGPASSWORD="$TGT_PW" pg_restore --no-owner --no-acl \
  -h postgres-rw.postgres.svc.cluster.local -p 5432 -U <app> -d <app> /tmp/x.dump \
  || echo "pg_restore exit $? (benign object errors possible)"
# verify:
PGPASSWORD="$TGT_PW" psql -h postgres-rw.postgres.svc.cluster.local -U <app> -d <app> -tAc \
  "select count(*) from information_schema.tables where table_schema='public'"
```
Streaming variant (wipe target first, no temp file) — the Grafana precedent:
```bash
PGPASSWORD="$TGT_PW" psql "$T" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
PGPASSWORD="$SRC_PW" pg_dump "$S" --no-owner --no-acl -Fc | PGPASSWORD="$TGT_PW" pg_restore --no-owner --no-acl -d "$T"
# S="host=10.20.2.10 port=<p> user=postgres dbname=postgres"
# T="host=postgres-rw.postgres.svc.cluster.local user=<app> dbname=<app> sslmode=require"
```
Notes / footguns:
- **`--no-owner --no-acl`** always (roles differ between source and CNPG).
- **`sslmode` is per driver** [CURRENT]: CNPG uses a self-signed CA. `libpq`/OpenSSL clients that fail the
  handshake can use **`sslmode=disable`** (traffic stays in-cluster, CNPG `host` pg_hba allows it);
  `pgjdbc`/`pgx` apps use **`sslmode=require`** (encrypt without cert validation).
- Transient `connection refused` on the target right after deploy = CNPG primary still electing; retry.
- If the restore errors on `CREATE EXTENSION`, the extension needs superuser — create it out-of-band first.
- **PG major bump (e.g. 14→16, immich):** restore into a **fresh `ceph-rbd` PVC** and set the container's
  **`PGDATA` to a subdirectory** of the mount, so the new ext4 volume's `lost+found` doesn't make `initdb`
  refuse the "non-empty data dir" [CURRENT]. Extensions (pgvector/vchord) recreate at matching versions on
  first app start.

### 4.3 Self-managed MongoDB (single-member replica set)
Notesnook needs multi-document transactions → a replica set (even of one). **FerretDB cannot help** — it
presents as a *standalone* and doesn't support `rs.initiate()`/replSet semantics. So run real Mongo:
```yaml
# headless Service (clusterIP: None) → stable DNS <name>-0.<name>.<ns>.svc.cluster.local
# StatefulSet: image mongo:7.0.12 ; args ["--replSet","rs0","--bind_ip_all"] ; ceph-rbd volumeClaimTemplate
readinessProbe:
  exec:
    command:
      - bash
      - -c
      - >
        mongosh --quiet --eval 'try { rs.status().ok } catch (e) {
          rs.initiate({_id:"rs0",members:[{_id:0,host:"<name>-0.<name>.<ns>.svc.cluster.local:27017"}]}) };
          db.runCommand({ping:1}).ok' | tail -1 | grep -q 1
```
- The RS is initiated **inside the readiness probe** (idempotent). It **must** advertise the stable FQDN — a
  bare `rs.initiate()` registers the member under the random pod name and clients can't reach it.
- Clients connect with `?replSet=rs0`, e.g. `mongodb://<name>-0.<name>.<ns>.svc.cluster.local:27017/<db>?replSet=rs0`.
- Created **fresh** (no dump in our case). Back up separately later (a `mongodump` CronJob → RGW).
- **AVX requirement** [ENV-SPECIFIC] — see Ch. 8 §Proxmox. If a worker shows `AVX-NO`, mongo crash-loops;
  the fix is the VM CPU type, not the image. `mongo:4.4` is the last release without the AVX requirement.

### 4.4 Self-managed MySQL
`standardnotes` precedent: StatefulSet, `mysql:8.4`, ceph-rbd PVC at `/var/lib/mysql`,
`readinessProbe: mysqladmin ping -h 127.0.0.1 -u root -p"$MYSQL_ROOT_PASSWORD" --silent`. Created fresh.

### 4.5 Redis / LocalStack sidecars
- **Redis** = ephemeral cache/sessions → `emptyDir`, `readinessProbe: redis-cli ping`.
- **LocalStack** (SNS/SQS event bus, standardnotes) → the Service **must be named `localstack`** (apps
  default their endpoint to `http://localstack:4566`); bootstrap topics/queues via a ConfigMap mounted at
  `/etc/localstack/init/ready.d`; **readiness via HTTP `/_localstack/health`, not `awslocal`** (Ch. 8).

### 4.6 Cross-engine (MariaDB/MySQL → Postgres)
Not a homogeneous dump. Two done here:
- **Guacamole** (MariaDB → CNPG PG): started **fresh** — the `guacamole/guacamole` image ships Postgres
  `initdb` SQL (`--postgresql`); OIDC handles login and auto-creates users, so only connection definitions
  were worth moving. Load the schema with a one-time init job.
- **Firefox Sync / syncstorage-rs** (MySQL → PG): schemas differ, so `SELECT | COPY FROM STDIN` with unit
  conversion — MySQL stores ms as bigint, PG wants `timestamptz`, so `to_timestamp(col/1000.0)`. Quoting the
  `WITH (NULL 'NULL')` clause over ssh needed `'"'"'NULL'"'"'` escaping.

### 4.7 Getting the dump off the Swarm and running SQL safely
- There is **no reusable copy Job in the repo** — write a one-shot **labelled** pod (every pod needs ≥1
  label, Ch. 8) that reaches `10.20.2.10:<port>` and CNPG.
- **Quoting footgun** [CURRENT]: single quotes in inline SQL collide with the outer `ssh '…'` wrapper and get
  mangled; nested `ssh → kubectl exec → sh -c → mysql -e "…"` triple-quoting is worse. **Write the SQL to a
  file and pipe it via stdin**, or use a single-quoted heredoc:
  ```bash
  ssh lhns@10.20.5.15 'kubectl -n <ns> exec -i <pod> -- sh -c "mysql -uroot -p\"$MYSQL_ROOT_PASSWORD\""' < grant.sql
  # or:  ssh lhns@10.20.5.15 'bash -s' <<'EOF'  … script with quotes/$()…  EOF
  ```

---

## 5. Volumes: mount, peek, migrate

### 5.1 Choose the storage class
| SC | Access | Backing | Use for |
|---|---|---|---|
| `ceph-rbd` | **RWO** block | pool `rbd.kube` (ext4) | databases, SQLite, anything wanting real block semantics |
| `cephfs` | **RWX** file | fs `fastappdata` (SSD) / `appdata` (HDD), `Retain` | media, config, and **mounting existing data in place** |
| `local-path` | RWO | node-local disk | throwaway/node-pinned only |

CNPG explicitly advises **against** a shared FS for PGDATA → databases go on `ceph-rbd`. RWO ⇒ single-node
attach (see Multi-Attach, §5.5).

### 5.2 Large volumes → MOUNT IN PLACE, never copy
For big existing data dirs (metrics 744G, logs 36G, media, mail blobs) do **not** copy — a copy would be huge
and would land on the near-full, `HEALTH_ERR` HDD tier. Bind a **static CephFS PV** straight onto the Swarm
data dir:
```yaml
apiVersion: v1
kind: PersistentVolume
metadata: {name: <app>-data}
spec:
  accessModes: [ReadWriteMany]
  capacity: {storage: 1Gi}          # DUMMY — ignored for static cephfs; binding is by volumeName
  storageClassName: ""
  persistentVolumeReclaimPolicy: Retain
  csi:
    driver: cephfs.csi.ceph.com
    volumeHandle: <app>-data
    nodeStageSecretRef: {name: csi-cephfs-secret, namespace: ceph-csi-cephfs}
    volumeAttributes:
      clusterID: ceph-cluster
      fsName: appdata                # or fastappdata
      staticVolume: "true"
      rootPath: /docker/observability/<app>     # the existing dir on the Ceph FS
---
# a PVC with storageClassName: "" and volumeName: <app>-data binds it; the pod mounts that PVC.
```
Pod hygiene for a mounted-in-place store:
- Run as the **data's uid/gid** (often `runAsUser: 0 / runAsGroup: 0`) and set **no `fsGroup`** — an `fsGroup`
  triggers a recursive `chown` of the whole tree (catastrophic on 744G) [CURRENT].
- `strategy: Recreate` (single writer).
- **Generous `readinessProbe.failureThreshold`, no liveness probe** — these stores open slowly off the HDD
  tier (36G took ~25 min to open); a liveness probe would crash-loop it mid-open.

### 5.3 Small volumes → copy into a fresh PVC
```bash
# source app scaled to 0 first (RWO can't be shared). busybox/alpine Job mounting src(RO)+dst(PVC):
sh -c 'set -e; ls -la /old; du -sh /old; cp -a /old/. /new/; sync; du -sh /new'
# verify file counts / sizes match, then delete the Job/pod.
```

### 5.4 Peek at a volume's contents
```bash
# throwaway LABELLED busybox pod mounting the PVC (labels required — Ch. 8):
kubectl -n <ns> run peek --image=busybox:1.36 --restart=Never \
  --overrides='{"metadata":{"labels":{"app":"peek"}},"spec":{ … mount the PVC at /src … }}'
kubectl -n <ns> exec peek -- sh -c 'du -sh /src; ls -la /src; find /src -type f | wc -l'
kubectl -n <ns> delete pod peek
# who owned a Released PV?
kubectl get pv <pv> -o jsonpath='{.spec.claimRef.namespace}/{.spec.claimRef.name}'
```

### 5.5 Volume footguns
- **RWO Multi-Attach** [CURRENT]: a copy pod holding the app's RWO PVC blocks the app pod
  (`Multi-Attach error … Volume is already used by pod(s) …`); delete the copy pod first. Same error hits
  every RWO volume on **abrupt node death**.
- **Free volumes after an abrupt node death** [CURRENT]:
  ```bash
  kubectl taint node <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute --overwrite
  # (remove the taint once the node is back)
  ```
  This is the exact k8s-defined taint that force-detaches volumes immediately (otherwise ~6 min wait). There
  is no shorter alias.
- **Planned reboot — avoid the whole mess:**
  ```bash
  kubectl drain <node> --ignore-daemonsets --delete-emptydir-data   # cordons + cleanly unmounts
  # power-cycle …
  kubectl uncordon <node>
  ```
- **Static PV sizing** is a dummy `1Gi` (ignored). **StorageClass/PV params are immutable** — to change class,
  mount options, or reclaim policy you delete+recreate the SC and re-provision. `cephfs` is `Retain`, so a
  deleted PVC leaves a `Released` PV + orphaned subvolume (`pv-reaper` reclaims those).
- **Swarm-host → PVC:** no NFS/hostPath/Job precedent exists — write your own `rsync`/`cp -a` pod from a
  Swarm node into a pod mounting the target PVC.

---

## 6. Networking

### 6.1 kube-vnet (default-deny ingress)
`ingressIsolationLevel: pod` means **all ingress is denied by default, even pod-to-pod in the same
namespace**. A pod receives traffic only through explicit vnet membership (plus auto-allows: LoadBalancer/
NodePort Services, hostPorts, apiserver-dialed webhooks). Egress is unrestricted.

Membership is by **pod label**, value = direction (`ingress` = server/accepts, `egress` = client/initiates):
- **Same namespace:** `kube-vnet/net.<vnet>: ingress` on the server, `kube-vnet/net.<vnet>: egress` on the
  client. Declare the vnet in `vnet.yaml` with only a `description`:
  ```yaml
  apiVersion: kube-vnet.lhns.de/v1alpha1
  kind: VirtualNetwork
  metadata: {name: <vnet>, namespace: <ns>}
  spec: {description: "<client> -> <server> (:<port>), intra-namespace."}
  ```
- **Cross namespace:** client label `kube-vnet/net.<targetNs>.<vnet>: egress`, and the target vnet lists the
  **client's** namespace:
  ```yaml
  spec:
    description: "…"
    allowedNamespaces:
      names: [<clientNs>]     # cross-ns peers ONLY
  ```

Rules:
- **A vnet never lists its own namespace** in `allowedNamespaces` — the home namespace is always implicit
  [CONVENTION]. (Common early mistake: adding the self-entry "to be safe" — it's redundant.)
- **Every pod must carry ≥1 label** or kube-vnet's ValidatingAdmissionPolicy (`failurePolicy: Fail`) **denies
  it cluster-wide** [CURRENT]. This bites every stripped `kubectl run` / debug pod — always add e.g.
  `labels: {app: test}`.
- Traefik reaches any web backend via the backend carrying `kube-vnet/net.traefik.traefik: ingress` (the
  `traefik` vnet is exposed cluster-wide with `allowedNamespaces.all: true`).
- With `externalTrafficPolicy: Local`, the source is the **real client IP**, not the SNAT'd node IP —
  account for that in any policy that filters by source.
- **CNI programming race** [FIXED: kube-vnet 0.7.1]: pre-0.7.1, a brand-new pod that fired a request
  *instantly with no retry* (e.g. `vmctl`) raced the CNI programming its source IP → `connection refused`.
  Old workaround was `initContainers: [{name: wait-netpol, image: alpine, command: ["sh","-c","sleep 25"]}]`.
  0.7.1 fixed it; the sleep is no longer needed (kept only as a harmless buffer in the vmctl TODO).

### 6.2 Traefik routing
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata: {name: <app>, namespace: <ns>}
spec:
  entryPoints: [websecure]
  routes:
    - kind: Rule
      match: Host(`<host>.example.com`)
      services: [{name: <svc>, port: <port>}]
  tls: {}          # empty → the *.example.com wildcard default TLSStore
```
- A host **outside** `*.example.com` needs its own cert and `tls: {secretName: <cert>}`.
- **Authelia** (SSO): attach the reflected forwardAuth middleware to the route —
  `middlewares: [{name: authelia}]` (it's reflected into every namespace, so reference it locally). **Skip it**
  for apps that own their auth (E2E apps like Notesnook / Standard Notes — the account password derives the
  encryption key, so auth can't be delegated). `authelia-basicauth` exists for API/CLI clients.
- **Raw TCP ports** (SMTP/IMAP/MQTT/LDAP) don't go through `IngressRoute` — give the Service a **MetalLB VIP**
  (e.g. mail on `10.20.2.8`) with `externalTrafficPolicy: Local` for real source IPs, and add the entrypoint
  to Traefik if it multiplexes the shared VIP.

---

## 7. Secrets & the GitOps generator

### 7.1 SOPS
`.sops.yaml` (repo root) encrypts only the secret *values*:
```yaml
creation_rules:
  - path_regex: kube-cluster/.*\.yaml$
    encrypted_regex: ^(data|stringData)$
    age: age1f6skv3ec96vjelz9nvg9jyply7rp88xef6mf6wmc8f3ljgty2vdqhkms26
```
Encrypt on the jumphost (has `sops 3.13.2` + `age`; the private key is in `flux-system/sops-age` in-cluster,
and `~/.config/sops/age/keys.txt` on the jumphost — **back it up or the repo secrets are unrecoverable**):
```bash
# write the plaintext Secret, then encrypt in place / to the repo path, deleting plaintext immediately:
ssh lhns@10.20.5.15 "sops --encrypt --age age1f6skv3ec96vjelz9nvg9jyply7rp88xef6mf6wmc8f3ljgty2vdqhkms26 \
  --encrypted-regex '^(data|stringData)$' --input-type yaml --output-type yaml /dev/stdin" \
  < plain-secret.yaml > kube-cluster/apps/<app>/secret.yaml
```
Reference from a Deployment with `secretKeyRef` per env var (or a projected volume of files for config that
reads `{{ secret "/secrets/…" }}`).

### 7.2 The generator & the app file set
`templates/kustomizations.yaml` globs `kube-cluster/{apps,infra}/*/kustomization.yaml` and emits one Flux
`Kustomization` per dir, named **`app-<dir>`** / **`infra-<dir>`**. So the standard app dir is:
```
resources.yaml       # Namespace + Deployments/StatefulSets/Services/PVCs
routing.yaml         # Traefik IngressRoutes (web-fronted apps)
vnet.yaml            # kube-vnet VirtualNetworks (where isolation edges exist)
secret.yaml          # SOPS-encrypted
kustomization.yaml   # the manifest below
database.yaml        # CNPG apps only (Database + DatabaseRole, in postgres ns)
db-role-secret.yaml  # CNPG apps only (reflected basic-auth secret)
```
```yaml
# kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
metadata:
  annotations:
    dependsOn: infra-postgres,infra-reflector,infra-ceph-csi-rbd   # comma-separated Flux Kustomization names
resources: [resources.yaml, routing.yaml, vnet.yaml, secret.yaml]
configMapGenerator:                 # optional — mounts config files
  - name: <app>-config
    namespace: <ns>                 # generator entries MUST set namespace (Ch.7 rule)
    files: [config.yml]
```
Conventions & footguns:
- **No top-level `namespace:` transformer** — it force-overrides the namespace on *every* object, silently
  breaking multi-namespace apps (e.g. a CNPG `Database` pinned to `postgres`). Each object sets its own
  `metadata.namespace`; each generator entry sets `namespace:`.
- **`dependsOn`** is a bespoke comma-separated annotation read by the generator (not Flux's native
  `spec.dependsOn`). Declare deps so a component doesn't error against a not-yet-created CRD/vnet on cold boot.
  Common targets: `infra-postgres`, `infra-reflector`, `infra-ceph-csi-rbd`, `infra-ceph-csi-cephfs`,
  `infra-traefik`, `infra-metallb`, `infra-cert-manager`.
- **`configMapGenerator`/`secretGenerator`** append a content-hash suffix and rewrite references, so a config
  change **auto-rolls** the pods — reference the generator *name*, not a hardcoded name.
- **Reconcile:** push → webhook applies within seconds. A **new dir** needs
  `flux reconcile helmrelease generators -n flux-system --with-source`. Clear a **stale** failed health (after
  slow image pulls trip `progressDeadlineSeconds`) by re-annotating the kustomization with `requestedAt`.
- **Moving a component between `apps/` and `infra/`** is dangerous — the generator names by dir, so a naive
  `git mv` is *delete (prune everything) + create*. Use the **`prune: disabled` dance** documented in the
  repo `README.md` ("Moving a component between groups") — do not improvise.
- **`.sh` files must be committed LF** [CURRENT]: there is no `.gitattributes`, and this repo is edited on
  Windows, so `git` warns "LF will be replaced by CRLF". A `.sh` with CRLF breaks the container's `bash`.
  Add `*.sh text eol=lf` (and verify `git show HEAD:path | grep -c $'\r'` is 0) for any script a container execs.

---

## 8. Cross-cutting footguns

| # | Footgun | Status | Fix / rule |
|---|---|---|---|
| 1 | kube-vnet CNI race: no-retry client gets `connection refused` on first request | `[FIXED: 0.7.1]` | upgrade ≥0.7.1; old workaround `initContainer sleep 25` |
| 2 | Label-less pod denied cluster-wide (VAP `failurePolicy: Fail`) | `[CURRENT]` | every pod (incl. `kubectl run`) needs ≥1 label |
| 3 | Own-ns listed in `allowedNamespaces` | `[CONVENTION]` | list cross-ns peers only; home ns is implicit |
| 4 | Exec probe times out at default **1s** on a cold-starting sidecar (LocalStack `awslocal`) | `[CURRENT]` | use an HTTP probe (`/_localstack/health`) or big `timeoutSeconds`/`failureThreshold` |
| 5 | Not-ready pod → removed from Service endpoints → dependents hang | `[CURRENT]` | generous readiness for slow-opening stores; wait-for-deps initContainer |
| 6 | `busybox nc -z` false BLOCKED | `[CURRENT]` | verify paths with `nc -zv -w3 host port` or `wget`, not `nc -z` |
| 7 | Image publishes only `:latest` | `[CONVENTION]` | digest-pin `:latest@sha256:…`; Renovate tracks the digest |
| 8 | `configMapGenerator` hash-suffix breaks a hardcoded ref | `[CONVENTION]` | reference the generator name; it auto-rolls on change |
| 9 | `.sh` committed CRLF breaks container bash | `[CURRENT]` | force LF (`.gitattributes: *.sh text eol=lf`) |
| 10 | StorageClass/PV params immutable | `[CURRENT]` | delete+recreate SC, re-provision PVC |
| 11 | MongoDB aborts without **AVX** | `[ENV-SPECIFIC]` | set worker VM CPU type `IvyBridge` + full power-cycle (below) |
| 12 | DB/store stuck "opening storage"; heavy queries 422 | `[ENV-SPECIFIC]` | check `ceph -s` **first** — `HEALTH_ERR`/`backfill_toofull`/`nearfull` HDD tier stalls it, not k8s |
| 13 | RGW **CORS** for browser presigned uploads | `[CURRENT]` | `radosgw-admin` can't set it; use `s3api put-bucket-cors`/boto3 `PutBucketCORS` (S3-API only) |
| 14 | Single quotes in inline SQL mangled over `ssh '…'` | `[CURRENT]` | pipe via stdin / `'bash -s' <<'EOF'` |
| 15 | Stale Kustomization "failed" after slow image pulls | `[CURRENT]` | pods healthy → re-annotate `requestedAt` to clear it |

**Proxmox / AVX (footgun 11) [ENV-SPECIFIC]:** worker VMs ran CPU type `x86-64-v2-AES`, which **masks AVX**
(an AVX/v3 feature). The oldest host (pve-04, Xeon E5-2680 **v2** = Ivy Bridge) has AVX but **not** AVX2, so
`x86-64-v3` won't run there. Fix — set every worker VM to the oldest host's microarch so it's migration-safe:
```bash
qm set <vmid> --cpu IvyBridge     # then STOP/START the VM (CPU-type change needs a full power-cycle, not reboot)
# verify in guest: grep -o avx /proc/cpuinfo | head -1   → avx
```
Never use `host` passthrough (pins the VM to one CPU, breaks live-migration). Fix the CPU type, not the image.

**Ceph triage (footgun 12):** `ssh root@10.20.2.101 ceph -s`. On this cluster the `appdata`/`archive` pools are
the slow HDD tier (`crush_rule 3`); `ceph-rbd` (pool `rbd.kube`) and RGW metadata are the fast tier. A
`HEALTH_ERR` HDD tier silently stalls any DB/backfill that opens storage there — that's why large observability
data is mounted in place (never copied onto a near-full HDD pool) and why the `vmctl` backfill is blocked.

---

## 9. Per-service appendix

Concrete "what broke → fix" notes. Many are one-offs but illustrate general classes.

**stalwart (mail).** Config lives in Postgres (`pg_dump` → CNPG); mail ports on MetalLB VIP `10.20.2.8`
(ETP-Local). Footguns: **ghost "Cluster Nodes"** — stalwart's node-id is a lease keyed on the OS hostname,
which in k8s is the random pod name, so every roll piles up a ghost → pin `STALWART_HOSTNAME=stalwart`
(safe only with `replicas:1` + `strategy: Recreate`). **S3 `SignatureDoesNotMatch`** on blob purge = wrong
S3 creds in the DB blob-store config (compounded by the slow HDD RGW tier). **LDAP login "no results"** →
see lldap.

**lldap.** `[bug]` A login `OR` filter referencing **two custom attributes** silently returns zero rows.
Root cause: an equality filter on a `is_list=true` custom attribute; commit `dbc85d9` made it return
`UnwillingToPerform`, and the filter converter aborts the whole `AND`/`OR` on one failing term (swallowed to
an empty result in a global search). Rule: **lldap can't evaluate an `OR` over ≥2 custom attributes.** Fix
(no lldap change): drop one custom attr from stalwart's login filter →
`(|(uid=%s)(stalwartmail=%s))` (loses alias login as a tradeoff). Upstream issue #858, "not planned".

**umleiter (Gmail→stalwart IMAP mirror).** `dial mail.example.com:993: tls: no application protocol` for 27h —
the **public edge** answered `:993` with an HTTP/TLS reverse proxy (wrong cert + ALPN) instead of passing TCP
through to the stalwart VIP `10.20.2.8`. Fix: make the edge **TCP-passthrough** `:993/:465/:25/:4190` → the VIP;
restart umleiter to clear its backoff. Also: its GHCR image was private → 403 (make public or add a pull secret).

**grafana → victoriatraces/logs/metrics.** Datasource host-IP `http://10.20.2.10:10428` →
`http://victoriatraces.victoriatraces.svc.cluster.local:10428`. Cross-ns default-deny: add `grafana` to the
target vnet's `allowedNamespaces.names` **and** label grafana `kube-vnet/net.victoriatraces.victoriatraces:
egress` (mirrors its existing postgres egress).

**dashy.** An old comment claimed "no version tags" and pinned a digest, blocking Renovate — but `4.4.0` is a
real published tag. Fix the pin + comment so Renovate tracks it.

**notesnook (E2E notes).** Mongo replSet + stateless identity/sync/sse + S3/RGW attachments; **no Authelia**
(owns auth); `SELF_HOSTED=1` grants Believer/Pro server-side. Web-client footguns (multi-layer): no official
web image exists. `dyw770/notesnook-web` is stale and reads config from **`urls.json`** (its `NN_*` env vars
are inert) — override `/urls.json` via ConfigMap. Switched to **`heyfworld/notesnook-web`** (digest-pinned) —
but it **ignores `urls.json`** and reads **`config.js` generated at startup by `envsubst` from `NN_*` env
vars**; with those empty it silently fell back to `api.notesnook.com` and made a Free account on the *public*
instance. Fix: set `NN_API_HOST`/`NN_AUTH_HOST`/`NN_SSE_HOST`/`NN_MONOGRAPH_HOST`, drop the dead `urls.json`.
Attachments need a **bucket CORS** policy set via `PutBucketCORS` (boto3/aws-cli) — `radosgw-admin` can't.

**standardnotes (E2E notes).** Monolith + MySQL + Redis + LocalStack; files on a local ceph-rbd PVC; **port
3104 must be publicly reachable** (clients upload encrypted chunks directly via `PUBLIC_FILES_SERVER_URL`).
Footguns, in order hit: (a) LocalStack readiness used `awslocal` (cold-start > 1s) → never Ready → Service had
no endpoints → init-wait hung → use `/_localstack/health`. (b) cookie sessions: `Set-Cookie` defaulted
`Domain=standardnotes.com`, which the browser rejects on our host → set
`AUTH_SERVER_COOKIE_DOMAIN=standardnotes-api.example.com`. (c) **Pro is not auto-granted** (opposite of Notesnook)
— manual SQL grant (`PRO_USER` role + active `PRO_PLAN` row); the role lives in a 60s access token → **fully
sign out/in**, not just reload. (d) `ends_at` set to a year-3000 **17-digit** value white-screened the client
(it detects the timestamp unit by digit count; **16 digits = microseconds**) → use `4102444800000000`
(2100-01-01). (e) **first-party host allowlist**: even with the role, native features stayed locked because
the client only honors roles when the sync host is in a hardcoded `app.js` allowlist
(`api.standardnotes.com`, `sync.standardnotes.org`, `localhost:3123`). Fix: an init container `sed`s our hosts
into `APPLICATION_DEFAULT_HOSTS`/`FILES_DEFAULT_HOSTS` on a writable copy nginx serves — image digest-pinned so
the `sed` targets can't drift, with `grep` guards that fail the pod if a target is missing.

**Others (one-liners).**
- *navidrome:* SQLite copy while Swarm stopped; Authelia forwardAuth **+** subsonic basic-auth on `/rest`.
- *spliit:* CNPG app; its RGW bucket needs **both** CORS (presigned PUT) **and** a public-read policy (plain
  `<img>` URLs) — Notesnook needs only the CORS.
- *guacamole:* image supports Postgres (`initdb.sh --postgresql`); MariaDB schema differs → start fresh.
- *mosquitto:* raw TCP 1883 (+ WS 9001) via LB; auth = LDAP (mosquitto-go-auth → lldap `:3890`).
- *msmtpd:* internal relay on `:2500` → Gmail; keeps the app-password in one SOPS secret so Authelia's config
  is secret-free.
- *barman-cloud/velero:* both need an S3 target — created a `kube-barman` RGW bucket (Velero reused it under a
  `velero/` prefix).
- *authelia:* forwardAuth `Middleware` reflected into every namespace (kube-traefik-reflector), so routes
  reference it locally instead of cross-namespace.
- *immich-album-federation:* fresh CNPG DB (self-migrates); **do not** set a `namespace:` transformer — its
  `database.yaml`/`db-role-secret.yaml` live in `postgres`.

---

## 10. Reference tables

### Cluster facts
| Thing | Value |
|---|---|
| Traefik HTTPS VIP (DNS target) | `10.20.2.15` |
| Mail VIP (SMTP/IMAP/managesieve) | `10.20.2.8` |
| CNPG service | `postgres-rw.postgres.svc.cluster.local:5432` (PG 18.4) |
| RGW S3 endpoint | `https://s3.example.com` (path-style, region `us-east-1`) |
| Workers | `10.20.2.72` / `.73` / `.74` |
| Ceph mgr / RGW nodes | `10.20.2.101` / `.102` / `.103` (`ceph`, `radosgw-admin`) |
| Swarm host (source, read-only) | `10.20.2.10` |
| Jumphost | `lhns@10.20.5.15` (`sops` 3.13.2, `age`, `kubectl`, `flux`) |
| age recipient | `age1f6skv3ec96vjelz9nvg9jyply7rp88xef6mf6wmc8f3ljgty2vdqhkms26` |
| StorageClasses | `ceph-rbd` (RWO), `cephfs` (RWX), `local-path` |
| kube-vnet | chart `oci://ghcr.io/lhns/charts/kube-vnet` tag `0.7.1`, `ingressIsolationLevel: pod` |

### `infra/` modules — when to `dependsOn`
| Need | dependsOn |
|---|---|
| Postgres DB | `infra-postgres` (+ `infra-reflector` for the reflected secret) |
| Block PVC (DB/SQLite) | `infra-ceph-csi-rbd` |
| Shared-FS PVC / mount-in-place | `infra-ceph-csi-cephfs` |
| Web ingress | `infra-traefik` |
| Raw-port LoadBalancer | `infra-metallb` |
| Non-wildcard TLS cert | `infra-cert-manager` |
| Roll pods on config/secret change | `infra-reloader` (or rely on generator hash) |
Other infra: `cloudnative-pg`, `barman-cloud-plugin`, `snapshot-controller`, `velero`, `spegel`,
`pv-reaper`, `flux-webhook`, `traefik-reflector`.

### App scaffold checklist
- [ ] `resources.yaml` — Namespace + workloads + Services (+ PVCs); every object has `metadata.namespace`
- [ ] `vnet.yaml` — one VirtualNetwork per internal edge; pods labelled `kube-vnet/net.*`
- [ ] `routing.yaml` — IngressRoute(s), `tls: {}`, Authelia middleware or not
- [ ] `secret.yaml` — SOPS-encrypted on the jumphost
- [ ] `database.yaml` + `db-role-secret.yaml` — CNPG apps only (in `postgres` ns)
- [ ] `kustomization.yaml` — `dependsOn` annotation; `configMapGenerator` entries with `namespace:`
- [ ] image: real tag, or digest-pinned `:latest`
- [ ] push → `flux reconcile helmrelease generators … --with-source` (new dir) → verify `1/1` + `/health` + TLS
