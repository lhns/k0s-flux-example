# flip7

A private Flip 7 helper (score calculator plus an online multiplayer game) at
**`flip7.example.com`**. Plain PHP and static files, no build step, no database. Public, no
Authelia: players join a room by shared link or QR code, without an account.

Runs on the official `php:*-apache` image, unmodified, as `www-data`.

## Storage

| PVC | class | mount | holds |
| --- | --- | --- | --- |
| `flip7-app` | `cephfs` | `/var/www/html`, read-only | the app's files |
| `flip7-data` | `ceph-rbd` | `/var/www/html/data` | room state |

The app keeps each room as a `flock()`ed file in `data/` (`room_*.php`, `seen_*`), deleted
after 48 h idle. That path is hardcoded, so the RBD volume is mounted over it; file locking on
CephFS is not reliable enough. The app writes the `.htaccess` that keeps `data/` unreachable
(the image allows overrides).

The app files are not in git or an image: they live only on `flip7-app`, by request, so they
are not covered by the usual [image volume / oci-composer](../../../README.md#getting-files-into-a-workload)
route.

## Updating the app

Scale to 0 in git if the change is not backwards compatible, then copy the new tree into the
volume with a helper pod mounting `flip7-app`, e.g.:

```sh
kubectl -n flip7 run flip7-copy --image=busybox:1.38.0 --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"a","persistentVolumeClaim":{"claimName":"flip7-app"}}],
  "containers":[{"name":"c","image":"busybox:1.38.0","command":["sleep","3600"],
  "volumeMounts":[{"name":"a","mountPath":"/app"}]}]}}'
tar -C flip7 -cf - . | kubectl -n flip7 exec -i flip7-copy -- tar -C /app -xf -
kubectl -n flip7 delete pod flip7-copy
```
