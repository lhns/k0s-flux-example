# remote-docker

A Docker workspace for the whole cluster: `lhns/remote-docker-workspace`, one privileged pod
running dockerd and an SSH agent. Clients — laptops and CI runners alike — reach it over an
SSH-in-WebSocket tunnel and get their **own** directories really mounted into containers, over
NFS, rather than copied or synced.

Migrated from the Swarm compose service that served `docker.example.com`.

## Who uses it

- **People**, from machines that need not have Docker installed at all.
- **`apps/github-runner`**, whose runner pods are consequently unprivileged — the daemon they use
  is here.

`apps/gitea-runner` and `apps/forgejo-runner` deliberately still run their own dind sidecars.
Their `config.yaml` sets `valid_volumes: []` and `network: ""`, so each job gets a docker *volume*
and a daemon-created bridge — they never bind-mount a host path, so the bind rewriting that
justifies this design buys them nothing. Moving them here would also *reduce* their isolation,
since each has its own daemon today.

## Choices that are not obvious

**`perUserDind: true`** — a dockerd per enrolled account (ADR 0019). The Swarm service ran the
shared daemon (`WORKSPACE_PER_USER_DIND=false`), so this is a behaviour change at cutover: every
account now keeps its own copy of every image, and images built under the old shared daemon are
not visible to the new per-account ones. ADR 0012 records the shared layer cache as a real
benefit and says 0019 gives it up rather than pretending otherwise. Chosen anyway, because a third
party's CI is enrolled and should not casually see other work.

Say the rest of ADR 0019 out loud: **this is separation, not isolation.** Every per-account daemon
runs `--privileged`, so a determined account can still break out. What it prevents is the failure
that actually happens — `docker ps` showing somebody else's containers, `docker system prune`
removing them.

**`fuse-overlayfs`, not overlay2.** overlay2 refuses to start on Ceph- and NFS-backed volumes and
the failure is a daemon that never comes up rather than a warning. `ceph-rbd` is a *block* volume
with ext4 on top, so `dockerdArgs: ""` (overlay2, faster) should work here — treat it as a
follow-up once this is otherwise healthy, not as part of bringing it up.

**`ingress.enabled: false`.** The chart's Ingress carries ingress-nginx annotations; this cluster
routes with Traefik CRDs and the `lhns-de` wildcard from the `TLSStore`. `routing.yaml` does it in
the repo's own idiom. No Authelia — the client authenticates with SSH *inside* the tunnel, and a
ForwardAuth would challenge or strip the upgrade.

**Public keys in plain values.** They are public. Encrypting them would make enrolling somebody a
sops round-trip instead of a one-line reviewable diff. The chart renders them into its own Secret;
the object kind is a Secret only because `templates/statefulset.yaml` hardcodes `secretName:`.

**The graph volume holds every account's images.** Per-account daemons keep their `/var/lib/docker`
as named volumes on this one, so 10Gi is shared by humans and both runners. `ceph-rbd` has
`allowVolumeExpansion`, so growing it is a values bump with no migration — but a full graph stalls
builds, so notice before it bites.

## Prerequisite: the NFS client module

The workspace mounts NFS exported by each client, so the **node** kernel needs the `nfs` module.
It is present on the workers
(`/lib/modules/6.12.105+deb13-amd64/kernel/fs/nfs/nfs.ko.xz`) but was **not loaded** when this was
written. Without it every bind mount fails, which looks like an application bug rather than a
missing module.

## One replica, and what follows

A StatefulSet of one. Two pods must never hold the same graph directory, so `helm upgrade` stops
the old pod before starting the new one, and a node failure needs the RWO volume to detach before
the pod can reschedule. Neither is a bug; both follow from one writer owning the storage. On this
cluster that is worth weighing — a worker kernel-panicked five times in the week this was written.

Losing the **state** volume is not losing a cache: the SSH host keys change, so every client that
has connected before reports `REMOTE HOST IDENTIFICATION HAS CHANGED`, and each account's uid
moves, which moves its reverse-tunnel port.

## Verifying

```sh
kubectl exec -n remote-docker remote-docker-0 -- remote-dockerd healthcheck

# from a client
remote-docker remote status
docker run --rm -v .:/w alpine ls /w     # /w is the CLIENT's directory
```

The last one is the whole chain in one command: the tunnel, the NFS export, the bind rewriting and
the daemon in the pod.
