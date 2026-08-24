# matter-server

The Matter controller Home Assistant talks to. Runs **on the LAN** via a macvlan interface,
because Matter cannot work any other way in a pod.

## Shape

- **Image** — `ghcr.io/matter-js/matterjs-server:1.3.3`, the successor to
  python-matter-server (EOL at 8.1.2). See *Why matterjs-server* below.
- **Two interfaces** — `eth0` on the pod network for the control channel, `net1` on the LAN
  (macvlan, `10.20.3.8/16`) for device traffic.
- **Storage** — `cephfs` RWX 1Gi at `/data`, `strategy: Recreate`.
- **Home Assistant** connects to `ws://matter-server.matter-server.svc.cluster.local:5580`.
- **kube-vnet** — `net.matter-server: ingress`; HA carries
  `net.matter-server.matter-server: egress`.

## Why it needs a LAN interface

Matter is built on **IPv6 link-local multicast**. A normal pod here has no IPv6 address at
all (the pod network is IPv4 single-stack), and a MetalLB VIP is unicast and NAT'd, so
multicast never arrives. Neither is fixable at the Service layer.

`macvlan` gives the pod its own MAC and IP directly on the LAN — it picks up a global IPv6
address by SLAAC and receives multicast — while staying isolated from the node's network
stack, which `hostNetwork` would not do. `infra/multus` provides the mechanism.

Matter also requires the controller to be on the **same (V)LAN as the devices and any border
router**: link-local multicast does not cross VLANs. If devices are ever segregated, this
breaks and no amount of routing fixes it.

## Two details that will cost you an evening if changed

**No gateway in the IPAM block.** Adding one installs a default route via `net1` and hijacks
the pod's egress away from `eth0`, breaking cluster DNS and API access. The `/16` address
already makes the whole LAN on-link, and IPv6 routes arrive via Router Advertisements — a
default route is unnecessary.

**`accept_ra_rt_info_max_plen=64`**, set by the chained `tuning` plugin. This makes the
kernel honour Route Information Options in Router Advertisements, which is how a Thread
border router advertises the mesh prefix. Without it the controller has an address but no
route to Thread devices — everything looks configured and Thread simply does not work.

Per upstream's OS requirements, `net.ipv6.conf.all.forwarding` must stay **disabled** (it is,
in a pod netns by default); with forwarding on, `accept_ra` would need to be `2`.

## Why matterjs-server, given it is Beta

python-matter-server was rewritten and is end-of-life at 8.1.2 — "will no longer receive
updates or support", with an instruction to migrate as soon as possible. matterjs-server is
the designated successor, a drop-in replacement on the same WebSocket API, supporting Matter
1.4.2 (the SDK version HA's integration targets).

It is Beta and not yet CSA-recertified. Adopted now specifically **because there is no
commissioned fabric to migrate** — the alternative was deploying software that is already
dead and scheduling the migration for a moment when it would cost re-commissioning every
device.

## Thread

The border router is [**`../otbr`**](../otbr/README.md), a pod on macvlan `10.20.3.7`. The
SLZB-MR5U's Thread SoC now runs as a bare **RCP**, with `otbr-agent` in the cluster driving it
over serial-over-IP. It previously ran OTBR on-device; that was replaced because SMLIGHT label
it beta and it underperformed.

That device has **two independent SoCs**, which is what makes this painless:

| radio | endpoint | used by |
|---|---|---|
| Zigbee | `10.20.1.50:6638` | zigbee2mqtt |
| Thread | RCP on `10.20.1.50:7638` | `../otbr`, which serves the REST API on `:8081` |

Both run at once on separate channels, so Thread does not disturb Zigbee. (Earlier revisions
of this file claimed no radio existed and that the coordinator was single-client — true of a
single-radio coordinator, wrong for this one.)

The SLZB has **one** Thread radio, so on-device OTBR and `../otbr` are mutually exclusive:
comparing them means flipping the device's mode, not running both.

**Why matter-server is not the border router, and vice versa.** OTBR is an IPv6 *router*
between the 802.15.4 mesh and the LAN; it has no concept of Matter. matter-server speaks
Matter to devices over IP and has no radio. Commissioning a Thread device needs both: the
device must join the *Thread mesh* using the network dataset, and join the *Matter fabric*
via this server. The dataset flows OTBR REST API → HA Thread integration → HA Matter
integration → here.

**How the route gets here.** OTBR advertises the Thread prefix as a Route Information Option
in its Router Advertisements. `accept_ra_rt_info_max_plen=64`, set by the `tuning` plugin in
the NAD, is what makes this pod accept it. Verified again after the move to `../otbr`:
`/proc/net/ipv6_route` shows the OMR prefix `fdce:e161:4935:1::/64` and the on-link
`fd3e:1eec:520:582e::/64` via `net1`. If Thread devices are unreachable but present, check
that route first — its absence is the signature of the sysctl being lost.

Note the RA also installs an IPv6 **default route via `net1`**. Harmless — `eth0` only ever
had a link-local IPv6 — but it does mean IPv6 egress leaves via the unpoliced interface.
IPv4 egress is unaffected and still goes via `eth0`.

### OTBR as a pod — done, see [`../otbr`](../otbr/README.md)

This section used to describe the fallback as researched-but-unbuilt. It has been built: the
on-device OTBR disappointed, so `../otbr` now runs `otbr-agent` in the cluster against the
SLZB's RCP. The requirements sketched here all held (`NET_ADMIN`, `/dev/net/tun`, IPv6
forwarding enabled, a socat shim because OpenThread implements no TCP radio transport, a
digest pin because upstream publishes only `latest`). What the sketch got wrong is written up
there — chiefly that the image ships no socat at all, and that the REST API is `:8081` bound
to loopback rather than `:8080`.

## Operational notes

- **Back up `/data`.** It holds the fabric credentials identifying this controller to every
  commissioned device. Losing it means re-commissioning everything by hand. It lives under
  `fastappdata`, which Backrest already mounts — but confirm a plan actually covers it.
- **`10.20.3.8` must stay outside the DHCP pool.** A collision produces intermittent failures
  that look like Matter being flaky.
- **`master: ens18` is hardcoded in the NAD** and must exist on whichever node the pod lands.
  Verified: all three workers have exactly one NIC, `ens18`, carrying the default route —
  Proxmox virtio names deterministically from the PCI slot, so a rebuilt node keeps it. The
  exposure is a *new* node with different naming, and the macvlan plugin has no auto-detect
  (`master` is mandatory). Tolerable because it fails loudly: the pod sits in
  `ContainerCreating` with a CNI error naming the missing interface, rather than starting
  half-working. If it ever happens, fix the NAD rather than pinning the pod to a node.
- **Only one replica, ever.** Two controllers would share one fabric identity, and two pods
  cannot hold the same macvlan address.
- **`net1` bypasses kube-vnet entirely** — NetworkPolicy does not apply to secondary
  interfaces. Keep cluster-internal traffic on the ClusterIP.
