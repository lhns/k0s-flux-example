# forgejo-runner

Forgejo Actions runner — [`forgejo-runner`](https://code.forgejo.org/forgejo/runner) plus its own
Docker daemon. Serves `forgejo.example.com` (see [`../forgejo`](../forgejo/README.md)).

**This is a privileged workload.** Read *Containment* below before changing anything here.

## Shape

One Deployment, `replicas: 1`, `strategy: Recreate`, one Pod with two containers:

- **`dind`** — `docker:29.7.1-dind`, `privileged: true`, a **native sidecar**
  (`initContainers` entry with `restartPolicy: Always`) so the daemon is up before the runner
  starts and terminates after it exits, letting in-flight jobs drain.
- **`runner`** — `code.forgejo.org/forgejo/runner:13.0.0`, no privileges at all
  (`drop: [ALL]`, `RuntimeDefault`), reaching the daemon at `tcp://127.0.0.1:2375`.

## What changed from `../gitea-runner`

This started as a copy of `apps/gitea-runner` and stopped being one. `forgejo-runner` v13 is a fork
of `act_runner` that rewrote exactly the part that manifest is most opinionated about — identity.
The **containment carries over verbatim**; the identity apparatus is deleted.

| gitea-runner (act_runner) | here (v13) |
| --- | --- |
| `GITEA_INSTANCE_URL` / `_REGISTRATION_TOKEN` / `_RUNNER_NAME` env | **gone.** v13 ignores the deprecated `GITEA_*` variables outright, and there is *no* env path to configure a connection — it lives in the config file or the daemon exits |
| a registration token, redeemed once into `.runner` | `server.connections.<name>: {url, uuid, token_url}` |
| a 1 Gi `ceph-rbd` PVC holding that file | **no volume at all** — identity is config, not state |
| StatefulSet, purely so `metadata.name` was stable | **Deployment.** The display name is fixed server-side at registration, so pod-name stability buys nothing |
| labels baked into `.runner`, changeable only by re-registering | `runner.labels` re-sent on every start |
| runs as root | `USER 1000:1000` ⇒ the pod needs `fsGroup: 1000` |
| stock entrypoint | `CMD` is a bare `/bin/forgejo-runner`, which only prints help ⇒ the `daemon` subcommand is passed explicitly |
| — | `HOME=/data`, so the cache defaults to `$HOME/.cache/actcache`; `cache.dir: /cache` must be set or it lands on the wrong volume |

**The net effect is that this is simpler and harder to break.** The most common operational failure
in gitea-runner — *"registered, but the forge forgot it"*, requiring you to delete `.runner` and
bounce the pod — cannot happen here, because registration is idempotent and reproducible.

## Registration

Done **on the Forgejo side**, offline and non-interactively:

```sh
kubectl -n forgejo exec deploy/forgejo -- su git -c 'forgejo forgejo-cli actions generate-secret'
kubectl -n forgejo exec deploy/forgejo -- su git -c \
  'forgejo forgejo-cli actions register --name forgejo-runner --secret <40hex>'   # prints the UUID
```

The UUID goes in `config.yaml`; the 40-hex secret goes in `secret.yaml` (SOPS) and is read via
`token_url: file:/secrets/token`. **`register` is idempotent for a given secret** — re-running it
restores the registration, which is the entire recovery procedure.

**The UUID is derived from the secret**: it is the hex encoding of the ASCII of the secret's first
16 characters. Committing it in a plaintext ConfigMap therefore discloses 16 of the 40 hex
characters; the remaining 24 (96 bits) are still secret. That is an acceptable trade and it is
stated rather than hidden. If it is ever unwelcome, register through *Site Administration →
Actions → Runners* instead — there the UUID and token are independent.

## Containment

Be honest about what `privileged: true` means: **root on that worker node**, with unmasked `/proc`,
all capabilities, and seccomp/AppArmor unconfined regardless of what the manifest says. An
attacker there can reach the kubelet's credentials and every secret mounted into every pod on the
same node.

**The real perimeter is who can trigger a workflow.** The registration is *instance-scoped* (no
`--scope` was passed), so anyone who can push to a CI-enabled repo on this instance has root on
that node. Treat "push access to a repo with workflows" as equivalent to "root on a worker".

In descending order of value:

1. **No cluster credentials** — dedicated ServiceAccount with zero RoleBindings, and
   `automountServiceAccountToken: false`.
2. **Job containers are not privileged** (`container.privileged: false`) and cannot request host
   mounts (`container.valid_volumes: []`). Without the latter a workflow could mount the daemon's
   own socket and escape everything else.
3. **`container.docker_host` is a `tcp://` URL, which v13 defines as *not* shared into job
   containers.** The value that must never appear there is **`"automount"`**, which finds the
   daemon socket and mounts it into every job — handing away everything item 2 protects.
4. **Resource limits** on both containers, plus the `emptyDir` `sizeLimit`s. These do not stop a
   determined escape, but they do contain the overwhelmingly more likely event: a runaway build.
5. **Its own namespace**, labelled `pod-security.kubernetes.io/enforce: privileged` — separate from
   `forgejo` so the forge does not share a blast radius with the thing executing arbitrary code on
   its behalf.
6. **An egress NetworkPolicy** (`networkpolicy.yaml`), copied from the gitea runner where it is
   proven. Job traffic is SNAT'd to this pod's IP, so jobs inherit its reach. kube-vnet writes
   ingress-only policies by design, so a `policyTypes: [Egress]` policy is additive. Allowed: DNS,
   Forgejo via Traefik, and the public internet minus `10/8`, `172.16/12`, `192.168/16`,
   `169.254/16` — which removes the LAN, the Ceph mons, the API server, pod-to-pod and every
   ClusterIP.

   A workflow that legitimately needs something on the LAN will fail here. Widen the rule
   deliberately rather than dropping the policy.

   *Testing note:* `wget https://registry-1.docker.io/v2/` exits non-zero because that endpoint
   answers **401**, not because egress is blocked. Test with `nc -z`, a host that returns 2xx/3xx,
   or an actual `docker pull` — otherwise you will "prove" a break that is not there.

## Verify

```sh
# the security check: LOOPBACK only, never 0.0.0.0
# (netstat, not ss — the dind image ships busybox netstat and no iproute2)
kubectl -n forgejo-runner exec deploy/forgejo-runner -c dind -- sh -c 'netstat -ltn | grep 2375'

# the performance check: overlay2, never vfs
kubectl -n forgejo-runner exec deploy/forgejo-runner -c dind -- docker info | grep -i 'storage driver'

# the daemon started, rather than printing help or dying on config
kubectl -n forgejo-runner logs deploy/forgejo-runner -c runner
# must NOT contain "runner: 0 server connections configured, terminating"

# a restart must NOT create a second runner row
kubectl -n forgejo-runner delete pod -l app.kubernetes.io/name=forgejo-runner
```

Then run a workflow that checks out code, builds an image, and uses `actions/cache`.

## Operational notes

- **Actions must be enabled per repository** (repo settings → Advanced), same as Gitea. A runner
  showing *Idle* forever usually means no repo has enabled them, not that registration failed.
- **`uses: actions/checkout@v4` resolves against `data.forgejo.org`**, not GitHub — Forgejo's
  `DEFAULT_ACTIONS_URL` differs from Gitea's. Set in `apps/forgejo`.
- **v13 broke things for workflow authors**, not just operators: `::set-output`, `::set-env` and
  `::add-path` are removed (use `$FORGEJO_OUTPUT` / `$FORGEJO_ENV` / `$FORGEJO_PATH`), the
  `${{ gitea.* }}` context is gone, `container.network_mode` became `container.network`, expression
  and matrix-exclude errors now hard-fail instead of being ignored, and `DOCKER_USERNAME` /
  `DOCKER_PASSWORD` secrets are no longer applied automatically (use
  `jobs.<id>.container.credentials`).
- **If the cache is unreachable from jobs**, set `cache.host` to the dind bridge gateway
  (`172.17.0.1`) or use `actions_cache_url_override`. The v13 cache grew a proxy layer; the
  auto-detected pod IP is normally correct.
- Neither `emptyDir` prunes itself. The `sizeLimit` plus pod restarts serve that purpose. If it
  becomes a problem, add a third container running
  `while sleep 6h; do docker system prune -af --filter until=168h; done`.
