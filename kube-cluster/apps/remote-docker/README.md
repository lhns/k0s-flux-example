# remote-docker

A Docker workspace for the whole cluster: `lhns/remote-docker-workspace`, one privileged pod
running dockerd and an SSH agent. Clients — laptops and CI runners alike — reach it over an
SSH-in-WebSocket tunnel and get their **own** directories really mounted into containers, over
NFS, rather than copied or synced.

Migrated from the Swarm compose service that served `docker.example.com`.

## Who uses it

- **People**, from machines that need not have Docker installed at all.
- Not `apps/github-runner`: its `k0s-flux` scale set uses `apps/remote-docker-ci`, a second workspace, so a
  pod-level egress policy can confine its jobs without confining everyone here.

`apps/gitea-runner` and `apps/forgejo-runner` deliberately still run their own dind sidecars.
Their `config.yaml` sets `valid_volumes: []` and `network: ""`, so each job gets a docker *volume*
and a daemon-created bridge — they never bind-mount a host path, so the bind rewriting that
justifies this design buys them nothing. Moving them here would also *reduce* their isolation,
since each has its own daemon today.

## Choices that are not obvious

**`perUserDind: true`** — a dockerd per enrolled account (ADR 0019 of the
remote-docker repo). Every account keeps its own copy of every image. ADR 0012 there records the shared layer cache as a real
benefit and says 0019 gives it up rather than pretending otherwise. Chosen anyway, because a third
party's CI is enrolled and should not casually see other work.

Note the rest of that ADR: **this is separation, not isolation.** Every per-account daemon
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
as named volumes on this one, so it is shared by every account. Grow it by
patching the PVC (`ceph-rbd` has `allowVolumeExpansion`), not the values: see `release.yaml`. A
full graph stalls builds, so notice before it bites. The agent prunes unused images and build
cache (unused for 7 days, or oldest first above 80% usage), never volumes, and only in a running
daemon: an account's daemon starts when it connects. `remote user rm <account> --purge` removes a
departed account's daemon and volume; deleting its key file by hand leaves them behind.

## Prerequisite: the NFS client module

The workspace mounts NFS exported by each client, so the **node** kernel needs the `nfs` module.
Without it every bind mount fails, which looks like an application bug rather than a
missing module.

## One replica, and what follows

A StatefulSet of one. Two pods must never hold the same graph directory, so `helm upgrade` stops
the old pod before starting the new one, and a node failure needs the RWO volume to detach before
the pod can reschedule. Neither is a bug; both follow from one writer owning the storage. 

Losing the **state** volume is not losing a cache: the SSH host keys change, so every client that
has connected before reports `REMOTE HOST IDENTIFICATION HAS CHANGED`, and each account's uid
moves, which moves its reverse-tunnel port.

## Adding a key at runtime

`lhns` is an admin (`admins` in `release.yaml`). Runtime keys are stored in `enrolled_keys.d` on the
`state` claim; `authorizedKeys` stays the declarative base. Losing the claim revokes them.

```sh
remote-docker remote key add <file|->        # own account; --account <name> as admin
remote-docker remote key rm SHA256:...
remote-docker remote key ls
remote-docker remote token create --account <name>   # prints the line the new device runs
```

Token links use `publicURL` (`wss://docker.example.com/`). Server side:
`kubectl exec -n remote-docker remote-docker-0 -- ls /etc/workspace/enrolled_keys.d`.

## Verifying

```sh
kubectl exec -n remote-docker remote-docker-0 -- remote-dockerd healthcheck

# from a client
remote-docker remote status
docker run --rm -v .:/w alpine ls /w     # /w is the CLIENT's directory
```

The last one is the whole chain in one command: the tunnel, the NFS export, the bind rewriting and
the daemon in the pod.
