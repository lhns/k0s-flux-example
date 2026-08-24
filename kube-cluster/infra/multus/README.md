# multus

A CNI *meta-plugin*: it lets selected pods have a **second network interface** alongside the
normal pod network. Nothing uses it yet — this component only makes the capability available.

## Why

Pods here are IPv4 single-stack (`podCIDR 172.18.0.0/16`) behind kube-router, and reachable
from outside only as **unicast** through MetalLB VIPs. Two things are impossible in that shape:

- **Matter is IPv6-only.** A normal pod has no IPv6 address at all, so a Matter controller
  cannot run in-cluster — before discovery is even considered.
- **mDNS/SSDP discovery never reaches a pod.** A MetalLB VIP is unicast and NAT'd, so
  multicast announcements from Chromecast/Hue/Sonos are not delivered. No MetalLB
  configuration can change this; it is the wrong layer.

`hostNetwork` is ruled out. Multus with a `macvlan` attachment gives a pod its own MAC and IP
**directly on the LAN** — SLAAC IPv6 from `fdb4:9bf5:7a48:17d0::/64`, and real multicast —
while keeping it isolated from the node's network stack, which is what `hostNetwork` would
not do.

## Multus vs MetalLB

They are not alternatives; they solve different problems.

| | MetalLB | Multus + macvlan |
|---|---|---|
| IP belongs to | a Service | one specific pod |
| Floating | yes — moves between nodes, follows the workload | no — tied to the pod's lifetime |
| Multiple replicas | one VIP across all of them | one IP per pod |
| NetworkPolicy | applies (kube-vnet `ext.svc`) | **bypassed** |
| Multicast / SLAAC | no | yes |

MetalLB stays responsible for every `LoadBalancer` Service. macvlan is only for pods that must
genuinely *be on the LAN*.

## What it does to the nodes

The DaemonSet installs the `multus` binary into `/opt/cni/bin` and writes
`/etc/cni/net.d/00-multus.conf` on every node it runs on. That name sorts ahead of
`10-kuberouter.conflist`, so **kubelet loads Multus as the primary CNI** and Multus delegates
to kube-router for the normal `eth0`. Every pod created on these nodes goes through Multus.

That is a large blast radius, so it was checked rather than assumed:

- kube-router's CNI is the **stock `bridge` + `portmap` conflist** — an ordinary delegate.
- `/etc/cni/net.d` holds exactly that one file, so auto-detection is unambiguous.
- kube-router's routing and firewall work is **controller-side** — it watches the API and is
  not part of the CNI ADD path, so putting Multus in front of the bridge plugin does not
  touch NetworkPolicy or BGP.
- The k0s **controllers are not cluster nodes**, so this DaemonSet never lands on the control
  plane.

## Removal is safe, and that is deliberate

`--cleanup-config-on-exit=true` makes each pod delete its own `00-multus.conf` on SIGTERM.
So `git revert` → Flux deletes the DaemonSet → every node returns to plain kube-router by
itself.

Without that flag, deleting the DaemonSet would leave a config pointing at a binary that is
no longer maintained on the node, and **you cannot schedule a pod to fix a node that cannot
schedule pods** — recovery would need SSH to each host. This single flag is why it is
acceptable to manage CNI with Flux at all.

The residual gap: cleanup runs on *graceful* termination. A hard node crash mid-rollout leaves
the file behind, and that node needs the file removed by hand.

## Using it

Define a `NetworkAttachmentDefinition` **in the consuming app's namespace** and reference it
from the pod:

```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata: {name: lan, namespace: <app>}
spec:
  config: |
    {
      "cniVersion": "0.3.1", "type": "macvlan",
      "master": "ens18", "mode": "bridge",
      "ipam": {"type": "static",
               "addresses": [{"address": "10.20.3.8/16", "gateway": "10.20.1.2"}]}
    }
```

```yaml
      annotations:
        k8s.v1.cni.cncf.io/networks: lan
```

The pod keeps `eth0` on the pod network — use it for in-cluster traffic and Services — and
gains `net1` on the LAN.

### Things that will bite

- **`net1` bypasses kube-vnet entirely.** NetworkPolicy does not see a secondary interface, so
  any pod given one is outside the default-deny baseline on that interface. Enforcing it would
  need MultiNetworkPolicy. This is the real cost of the capability.
- **A macvlan pod cannot reach its own node's IP.** Inherent to macvlan — anything node-local
  (hostPort, node agents) is unreachable over `net1`.
- **The hypervisor must allow multiple MACs per vNIC.** The nodes are Proxmox guests on
  `ens18`. If `net1` exists but nothing arrives, suspect this first.
- **IPAM is per-pod, not pooled.** `static` is deterministic and fine for singletons; anything
  replicated needs `whereabouts` or DHCP, neither of which is installed.
- **SLAAC works out of the box here** — verified: `net1` came up with a global
  `fdb4:9bf5:7a48:17d0:…` address marked `proto kernel_ra`, i.e. from a Router Advertisement,
  with no extra configuration. If that ever regresses to link-local only, chain the `tuning`
  plugin to set `net.ipv6.conf.net1.accept_ra=2`.
- **The LAN is a `/16`** (`10.20.0.0/16`, gateway `10.20.1.2`) — a `/24` mask will look like it
  works and then fail to reach half the estate.

## Upgrading

Re-vendor `deployments/multus-daemonset.yml` from the new tag and re-apply the four
deviations marked `DEVIATION` in `resources.yaml` — upstream's file is a self-declared
"quickstart" that "does not care about ... upgrade/update/uninstall scenario", so it will not
carry them for you. Renovate bumps the pinned image tag on its own; the manifest around it is
what needs a human.
