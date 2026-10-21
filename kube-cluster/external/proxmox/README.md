# proxmox

The Proxmox VE cluster (outside Kubernetes, [ADR 0056](../../../docs/adr/0056-external-group-for-systems-outside-the-cluster.md))
pushes its own metrics over OTLP with its built-in metric server to the OTel gateway, so there is no
collector to run here. This Flux component builds only the Grafana objects below.
The Ceph pools Proxmox mounts are covered by [external/ceph](../ceph/README.md).

Series carry `service.name=proxmox-ve`, `proxmox.cluster`, `node` (the same value as `proxmox.node`)
and, per family:

| family | extra labels | notes |
| --- | --- | --- |
| `proxmox_node_*` | `device` on `network_*` | `cpustat_cpu` is a 0-1 gauge; `cpustat_*_total` are jiffy counters the dashboard does not use; memory and `blockstat_*` (root filesystem) in bytes; `memory_memused` includes the ZFS ARC |
| `proxmox_storage_*` | `storage` | one series per node; `shared=1` storages repeat the same values on every node |
| `proxmox_vm_*` | `vmid`, `name`, `type` | stopped guests stay with `uptime=0`; `*_total` are byte counters; `proxmox_vm_disk` is always 0 (QEMU does not report it); `pressure*` are PSI percentages |

Samples arrive every 10s.

## Dashboards

`Proxmox VE` (`/d/proxmox-ve`) in folder *Proxmox*: cluster overview, nodes, storage (with a 7 day
time-to-full trend, the same calculation as the Ceph alerts) and guests (status table, top N).
Variables: cluster, node, guest type, guest (by name), top N. Every panel expression was run against
the live series. The dashboard uid must differ from the folder's (`proxmox`): folders and
dashboards share one uid namespace, and a clash makes the folder view recurse endlessly.

## Decisions

- The folder is a `GrafanaFolder` (`proxmox`, `prune: disabled`), as [ADR 0058](../../../docs/adr/0058-grafana-operator-manages-grafana-and-its-content.md)
  asks for new folders; it is not in `scripts/check-grafana.py`'s `FOLDERS`, which lists the uids Grafana generated.
- Queries use `node`, not `proxmox.node`, so selectors need no quoting; `proxmox.cluster` is quoted.
- Shared storages are deduplicated with `max by (storage)`, local ones keep `node`, since `local` exists on every node.
- Network panels read physical NICs only (`en*`, `eth*`): bridges, taps and `fw*` devices carry the same bytes again.
- Memory has two views, because Proxmox's "used" counts the ZFS ARC: *Memory used and total*, and *Memory available*.
