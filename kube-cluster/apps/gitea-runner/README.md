# gitea-runner

Gitea Actions runner — [`act_runner`](https://gitea.com/gitea/act_runner) plus its own Docker
daemon, migrated from Docker Swarm. Serves `gitea.example.com` (see [`../gitea`](../gitea/README.md)).

**This is the cluster's first privileged application workload.** Read *Containment* below before
changing anything here.

## Shape

One StatefulSet, `replicas: 1`, one Pod with two containers:

- **`dind`** — `docker:29.7.1-dind`, `privileged: true`, a **native sidecar**
  (`initContainers` entry with `restartPolicy: Always`) so the daemon is up before `act_runner`
  starts and terminates after it exits, letting in-flight jobs drain.
- **`act-runner`** — `gitea/act_runner:0.6.1`, no privileges at all
  (`drop: [ALL]`, `RuntimeDefault`), reaching the daemon at `tcp://127.0.0.1:2375`.

## What Kubernetes deleted

The Swarm compose was mostly scaffolding around limitations this cluster does not have:

| Swarm artifact | why it existed | here |
| --- | --- | --- |
| `swarm-launcher` sidecar | Swarm cannot run privileged containers | gone — `privileged: true` is a field |
| `socat UNIX-LISTEN … TCP:` shim | the daemon lived in another netns | gone — a Pod shares one netns |
| `LAUNCH_NETWORK_MODE: container:` | Swarm's netns sharing | gone — implicit |
| custom `entrypoint` + `tini` | only existed to start socat | gone — stock entrypoint |
| `--storage-driver=fuse-overlayfs` | `/var/lib/docker` was on **NFS** | gone — **overlay2**, nothing here is NFS |
| `{{.Task.Slot}}` × 3 services | per-slot identity and data dirs | one StatefulSet ordinal |
| `ports: ["8088:8088"]` | vestigial | gone — no Service |

## Decisions worth knowing

**Concurrency is `runner.capacity: 4`, not replicas.** Swarm ran three slots, each with its own
dockerd and its own complete image cache. One pod running four jobs against a single daemon shares
one layer cache and is strictly cheaper on a 20-CPU worker. Scale replicas only if a single node
genuinely cannot hold the load — each ordinal then gets its own PVC, `.runner` and name.

**StatefulSet for a stable *name*, not for storage.** `GITEA_RUNNER_NAME` comes from
`metadata.name`. Under a Deployment every roll would produce a new random name and register a
*new* runner, leaving the old ones as permanently-offline rows in the admin UI.

**`/var/lib/docker` is an `emptyDir` with `sizeLimit: 40Gi`.** `ceph-rbd` would work — ext4 on
block supports overlay2 natively — but it is the wrong trade: the data is 100 % disposable, 40 Gi
becomes ~120 Gi raw at 3× replication on a cluster whose OSDs are ~95 % full, image pulls are the
worst possible workload for network-replicated storage, and dynamic RBD is not backed up anyway.
The `sizeLimit` is also the containment that matters — a runaway build evicts *this pod* instead
of filling the node and taking its neighbours with it.

*Cost:* a cold cache after every restart. Spegel does not help — it mirrors containerd, and this
is a separate image store. If that becomes painful, move to `local-path` (node-local, no Ceph),
never to `ceph-rbd`.

**`/data` is a 1 Gi `ceph-rbd` PVC** holding only the `.runner` registration identity (~1 KB).
Without it, every pod recreation would register a new runner. The Actions cache is deliberately
pointed at a *separate* emptyDir (`cache.dir: /cache`) so it cannot silently fill that 1 Gi.

**`dockerd` binds loopback only.** The stock entrypoint binds `0.0.0.0:2375` when
`DOCKER_TLS_CERTDIR` is empty — an unauthenticated, root-equivalent Docker API reachable by any
pod that can route here. The shared netns makes `127.0.0.1` sufficient. **Verify this after any
image bump** (see below); Docker 29 changed firewall handling.

**`GITEA_INSTANCE_URL` is the public hostname, resolved locally.** It has to be public because it
propagates into every job as the clone URL and `GITHUB_SERVER_URL`; an in-cluster Service name
would produce URLs that only resolve inside the pod network. A `hostAliases` entry maps
`gitea.example.com` → `10.20.2.15` (Traefik's VIP) so the traffic never leaves the cluster, and TLS
still validates against the `*.example.com` wildcard. It also lets the runner register *before* public
DNS is moved. Note that containers started by dind do **not** inherit `hostAliases`.

## Containment

Be honest about what `privileged: true` means: **root on that worker node**, with unmasked `/proc`,
all capabilities, and seccomp/AppArmor unconfined regardless of what the manifest says. An
attacker there can reach the kubelet's credentials and every secret mounted into every pod on the
same node.

**The real perimeter is who can trigger a workflow.** The registration token is *instance-scoped*,
so anyone who can push to a CI-enabled repo on this instance has root on that node. Treat "push
access to a repo with workflows" as equivalent to "root on a worker".

What is actually done here, in descending order of value:

1. **No cluster credentials** — dedicated ServiceAccount with zero RoleBindings, and
   `automountServiceAccountToken: false`. An attacker with root in the container still has no
   token for the API server.
2. **Job containers are not privileged** (`container.privileged: false`) and cannot request host
   mounts (`container.valid_volumes: []`). Without the latter a workflow could mount the daemon's
   own socket and escape everything else.
3. **Resource limits** on both containers, plus the `emptyDir` `sizeLimit`. These do not stop a
   determined escape, but they do contain the overwhelmingly more likely event: a runaway build.
4. **Its own namespace**, labelled `pod-security.kubernetes.io/enforce: privileged`. Nothing
   enforces PodSecurity today, so this changes no behaviour — it states the exception at the
   boundary so every *other* namespace can be tightened later without touching this one.

5. **An egress NetworkPolicy** (`networkpolicy.yaml`). Job traffic is SNAT'd to this pod's IP, so
   jobs inherit its reach. kube-vnet writes ingress-only policies by design, so a
   `policyTypes: [Egress]` policy is additive and does not conflict with it. Allowed: DNS, Gitea
   via Traefik, and the public internet minus `10/8`, `172.16/12`, `192.168/16`, `169.254/16` —
   which removes the LAN, the Ceph mons, the API server, pod-to-pod and every ClusterIP. Verified
   in place:

   | check | result |
   | --- | --- |
   | DNS | resolves |
   | `https://gitea.example.com/api/v1/version` | `{"version":"1.27.1"}` |
   | `docker pull alpine:3.22` | succeeds |
   | Ceph mon `10.20.2.101:6789` | blocked |
   | API server `10.20.2.50:6443` | blocked |
   | the gitea pod IP `:3000` | blocked |

   A workflow that legitimately needs something on the LAN will fail here. Widen the rule
   deliberately rather than dropping the policy.

   *Testing note:* `wget https://registry-1.docker.io/v2/` exits non-zero because that endpoint
   answers **401**, not because egress is blocked. Test reachability with `nc -z`, a host that
   returns 2xx/3xx, or an actual `docker pull` — otherwise you will "prove" a break that is not
   there.

## Verify

```sh
# the security check: LOOPBACK only, never 0.0.0.0
kubectl -n gitea-runner exec gitea-runner-0 -c dind -- sh -c 'ss -ltnp | grep 2375'

# the performance check: overlay2, never vfs
kubectl -n gitea-runner exec gitea-runner-0 -c dind -- docker info | grep -i 'storage driver'

# act_runner reaches the daemon without socat
kubectl -n gitea-runner exec gitea-runner-0 -c act-runner -- wget -qO- http://127.0.0.1:2375/version

# registered, and the identity persisted
kubectl -n gitea-runner exec gitea-runner-0 -c act-runner -- ls -la /data/.runner
# → the Gitea admin UI shows gitea-runner-0 as Idle

# a restart must NOT create a second runner row
kubectl -n gitea-runner delete pod gitea-runner-0
```

Then run a workflow that checks out code, builds an image, and uses `actions/cache`.

## Operational notes

- **"Registered but Gitea forgot it"** is the most likely surprise — restoring Gitea from a backup
  predating registration, or deleting the runner in the UI, leaves a `.runner` whose credentials
  are rejected. act_runner fails auth and does **not** re-register on its own. Recovery:
  ```sh
  kubectl -n gitea-runner exec gitea-runner-0 -c act-runner -- rm -f /data/.runner
  kubectl -n gitea-runner delete pod gitea-runner-0
  ```
- **Labels are sticky.** `runner.labels` are written into `.runner` at registration; changing them
  in `config.yaml` may not take effect until re-registration (same recovery as above).
- **Three stale runner rows** (`runner-1`, versions 0.2.13/0.4.1) carried over from the Swarm slots
  and show as offline. Delete them in *Site Administration → Actions → Runners*.
- Neither `emptyDir` prunes itself. The Swarm stack ran a 6-hourly `docker prune` loop; here the
  `sizeLimit` plus pod restarts serve the same purpose. If it becomes a problem, add a third
  container running `while sleep 6h; do docker system prune -af --filter until=168h; done`.
