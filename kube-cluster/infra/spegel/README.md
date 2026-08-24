# spegel

[Spegel](https://spegel.dev) — a stateless peer-to-peer OCI registry mirror
(DaemonSet). Each node advertises the images it already has, so other nodes pull
from a peer instead of re-pulling upstream.

- `release.yaml` — `HelmRepository{type: oci}` (`oci://ghcr.io/spegel-org/helm-charts`)
  + `HelmRelease` `spegel` (chart `0.7.4`). `containerdRegistryConfigPath` **must
  match the second root** of the containerd drop-in's `config_path`.
- `vnet.yaml` — kube-vnet allowance so spegel pods can reach each other's libp2p
  router (`:5001`, pod-to-pod). Without it the cluster-isolation baseline denies
  P2P and Spegel silently falls back to upstream.

## Host prerequisite (not managed here)
Spegel needs two containerd settings on each **worker**, delivered by the k0s
drop-in `kube-cluster/k0s-files/registries.toml` (uploaded via `k0sctl.yaml` host
`files`): `discard_unpacked_layers = false` and `registry.config_path`. Apply
with `k0sctl apply`; it takes effect **on the next worker restart** — k0sctl only
restarts k0s when the binary or k0s config changed, and an uploaded file is
neither, so check `grep config_path /run/k0s/containerd-cri.toml` afterwards.

## Spegel OWNS its config path — do not put anything in it
Its `configuration` initContainer clears the whole directory on every pod start
and rewrites it (`pkg/oci/containerd/mirror.go`: `backupConfig()` snapshots only
on its FIRST ever run, then `clearConfig()` removes everything except `_backup`).
Anything added later is deleted with no warning and no backup — which is what
silently broke every composed-image pull on 17 Aug 2026.

That is documented behaviour rather than a bug, and `prependExisting` does not
help: it reads `_backup/<host>/`, and only for hosts listed in
`mirroredRegistries`, which is unset here.

So Spegel gets `/etc/k0s/registries/spegel` to own outright, and hand-written
registry config lives in a separate root it never touches. **Spegel's root must
stay SECOND in `config_path`**: it writes `_default`, which matches every
registry, and containerd stops at the first root that matches anything — put it
first and it answers for hand-written registries too. See
`kube-cluster/k0s-files/registries.toml`.

## Verify
```sh
kubectl -n spegel get pods -o wide                  # DaemonSet Ready on all workers
# after a worker restart, on a worker:
grep -iE 'discard_unpacked_layers|config_path' /run/k0s/containerd-cri.toml
ls /etc/k0s/registries/spegel/            # _default (+ _backup); Spegel rewrites this
ls /etc/k0s/registries/oci-composer/      # hand-written; must SURVIVE a spegel restart
```
P2P check: pull an image on worker A, schedule a pod using it on worker B, and
watch B's spegel logs resolve it from the peer rather than upstream. Spegel
falls back silently, so verify explicitly.
