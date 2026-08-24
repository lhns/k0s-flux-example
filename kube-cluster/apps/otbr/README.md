# otbr — OpenThread Border Router

The Thread border router, in-cluster, replacing the **on-device OTBR in the SLZB-MR5U at
`10.20.1.50`** that SMLIGHT themselves label beta. On the LAN at **10.20.3.7** via macvlan, next to
[`../matter-server`](../matter-server/README.md) on `.8` and Home Assistant on `.9`.

The two are separate jobs and stay separate pods: OTBR is an IPv6 *router* between the 802.15.4
mesh and the LAN, matter-server speaks Matter over IP. Nothing about matter-server changes.

## Do this first: check the SLZB's Thread firmware

[openthread/openthread#12100](https://github.com/orgs/openthread/discussions/12100) describes an
**SLZB-MR4u** — the MR5U's sibling — whose OTBR would not talk to the radio at all: `Error decoding
hdlc frame: Parse`, `Wait for response timeout`, recovery loop, crash. It was fixed by a **SMLIGHT
firmware update**, not by any OTBR change, and the decisive detail is that the same failures
appeared over **direct serial** — so the transport was never the cause.

The RCP firmware stays SMLIGHT's whichever border router runs. If the current trouble is that bug,
this deployment fixes nothing. Check the Thread radio firmware in the SLZB UI, update it, re-test
the on-device OTBR, and only then continue. It costs minutes and may end the exercise.

## One radio, so this is either/or

The SLZB has a single Thread SoC. Putting it in **RCP mode** is what frees it for this pod, and
that *is* turning the on-device OTBR off — the two cannot run side by side, and comparing them
means flipping the device's mode back and forth. Switching modes reboots the device, which drops
Zigbee briefly; [`../zigbee2mqtt`](../zigbee2mqtt/README.md) records that as survivable ("one
container restart, devices reporting again within a minute").

## The dataset, and where it now lives

The **Active Operational Dataset** — network key, PAN ID, channel, network name — *is* the Thread
network. Carry it across and commissioned devices rejoin the same mesh; start fresh instead and
every Thread device is orphaned and must be re-commissioned by hand.

The obvious way to save it, `curl http://10.20.1.50:8080/node/dataset/active`, **only works while the
SLZB is in on-device OTBR mode**. Switching the radio to RCP takes that REST API away with it, so
the export has to happen first. It did not here — the mode was switched before the export.

That turned out not to matter, because **Home Assistant caches datasets it has seen**, in
`/config/.storage/thread.datasets`. One is stored, `source: otbr`, created 2026-07-31. So the
network identity survived the mode switch by luck rather than design. Check that file before
concluding a dataset is lost.

**This pod starts with no dataset**, so `/node/state` is `disabled` and `wpan0` stays DOWN until one
is applied — that is correct behaviour for an unconfigured node, not a fault. Apply it from Home
Assistant (Settings → Devices & Services → Thread), which pushes its preferred dataset to the new
border router and keeps HA's own records consistent. Doing it that way also keeps the network key
out of shell history.

## How it reaches the radio

The RCP is not a local device — the SLZB exposes it as **serial-over-IP on `:7638`**. OpenThread
implements exactly three radio transports (`spinel+spi://`, `spinel+hdlc+uart://`,
`spinel+hdlc+forkpty://`); there is deliberately **no TCP scheme**. So something bridges TCP to a
pty, and `forkpty` is the hook for it: `otbr-agent` forks the named program with a pty attached and
speaks HDLC over it.

```
RADIO_URL=spinel+hdlc+forkpty:///opt/socat/socat.sh?forkpty-arg=-&forkpty-arg=tcp:10.20.1.50:7638,nodelay
```

Home Assistant's own OTBR add-on bridges networked coordinators with socat the same way, so this is
the sanctioned pattern rather than a hack. It still deserves suspicion: the RCP split leaves the
timing-critical lower MAC on the radio and 802.15.4 has hard deadlines (ACK turnaround ~192 µs,
CSMA/CA backoff, CCA). Upstream recommends SPI over UART **for latency alone**. TCP adds jitter,
Nagle and retransmits on top — hence `nodelay`. It works on a quiet wired LAN and is out of spec.
**Treat mesh instability as a suspect here rather than a mystery.**

Two things about it are settled rather than assumed:

- **The pty does not corrupt binary spinel.** `otbr-agent` passes a `cfmakeraw` termios as
  `forkpty()`'s third argument, so the slave is raw from creation. Measured for contrast: pushed
  through a pty that is *not* raw, the 10-byte frame `7e0a0d010a7dff000a7e` comes back as 29 bytes
  — `0a`→`0d0a`, `01` echoed as literal `^A`, `00` dropped. Raw mode is doing real work; a sidecar
  socat with default settings would have silently mangled every frame.
- **A sidecar could not have worked anyway.** A pty belongs to the `/dev/pts` instance of the
  container that created it, so a shared `emptyDir` would carry the symlink but not the device.
  Same container, or nothing.

### Why socat arrives via an initContainer

`openthread/otbr` ships **no socat, no netcat, no curl**. It is also **Ubuntu 18.04, glibc 2.27** —
too old for any current prebuilt socat binary. (Confusingly, the source tree copied into the image
at `/app` contains a 2025 Dockerfile describing an Ubuntu 24.04 + s6-overlay image with
`ENTRYPOINT ["/init"]`. That is *not* what is published: `latest` is the legacy 18.04 image driven
by `/app/script/server`. Trust the running container, not `/app`.)

So the initContainer copies socat **plus the musl loader and its four libraries** out of
`alpine/socat` and drops in a two-line wrapper that runs them under that loader, bypassing the host
libc entirely. Verified in this exact image, including a binary round-trip through the relay.

socat is pinned to **1.8.0.3**, deliberately not 1.8.0.2 — [ot-br-posix#2719](https://github.com/openthread/ot-br-posix/issues/2719)
blames that version for border-router crashes after a few hours.

## Shape

- **macvlan on 10.20.3.7**, `master: ens18`, `mode: bridge`, static IPAM, **no gateway** (it would
  install a default route via `net1` and hijack egress off `eth0`). A border router must be on the
  LAN to send Router Advertisements; a ClusterIP cannot. `10.20.3.x` is the repo's macvlan range —
  `10.20.2.7` is free and would work technically on this flat `/16`, but a macvlan pod inside the
  MetalLB range is invisible to MetalLB, which would hand the same address to a Service later.
- **Sysctls come from the CNI `tuning` plugin**, not `securityContext.sysctls` and not the
  container. The container genuinely tries (`sudo sysctl --system`) and fails with "Read-only file
  system", because containerd mounts `/proc/sys` read-only for unprivileged pods.
  `net.ipv6.conf.all.forwarding` is not on kubelet's safe list either, so the pod route would need
  it allowlisted in `k0sctl.yaml`'s `workerProfiles` — a cluster-wide change for one pod. The
  tuning plugin sets it in the pod netns at CNI time, as matter-server already does for
  `accept_ra_rt_info_max_plen`.
  - `net.ipv6.conf.all.forwarding=1` is what makes this a router.
  - `net.ipv6.conf.net1.accept_ra=**2**`, not 1. Linux ignores RAs on an interface once forwarding
    is enabled unless `accept_ra` is explicitly 2. At 1 this pod would never learn the upstream
    prefix and border routing would not work.
- **`NET_ADMIN` + `NET_RAW`** and `/dev/net/tun` as a `hostPath` — OTBR creates the `wpan0` TUN
  interface, adds routes, and runs `ip6tables` for the Thread firewall. Not privileged.
- **`BACKBONE_INTERFACE=net1`.** The image defaults it to `eth0`, which would advertise the Thread
  prefix into the pod network and be invisible to every real device on the LAN. This is the single
  most important env var here.
- **REST API on `:8081`, bound explicitly.** Two easy mistakes, both avoided: the port is 8081 (the
  `8080` in upstream's `docker run` is the *host* side of a mapping to the **web UI** on port 80),
  and it binds `127.0.0.1` by default — unreachable from outside the pod unless `REST_HOST` is set.
- **The web UI stays on loopback** (`HTTP_HOST=127.0.0.1`). It is an unauthenticated admin
  interface that can erase the dataset, and `net1` has no NetworkPolicy in front of it. Reach it
  with `kubectl -n otbr port-forward deploy/otbr 8080:80`.
- **`NAT64=0`.** Not needed for Matter (the fabric is IPv6-only), and the entrypoint aborts the
  container outright if NAT64 fails to start. Flip to `1` if a Thread device ever needs IPv4.
- **`cephfs` RWX at `/var/lib/thread`** for the dataset, matching matter-server. This was
  `ceph-rbd` first, which was a mistake: the "embedded database file → block" rule is about
  SQLite-style access (mmap, byte-range locks), and `otbr-agent` keeps a single flat key-value
  file rewritten via tmp-file-plus-rename — 36K in practice. The decisive argument is backup
  rather than semantics. Backrest mounts the whole `fastappdata` CephFS filesystem and the
  `cephfs` StorageClass provisions subvolumes inside it, so dynamic cephfs volumes are at least
  reachable; dynamic RBD volumes are block images in pool `rbd.kube` with no mount and no path,
  so nothing can back them up. RBD put the one irreplaceable file here on the only storage in the
  repo that could not be recovered. (Coverage still needs the Backrest plan — task #89.)
  `strategy: Recreate` does not follow from the volume any more, but is still required by the
  fixed macvlan address and the single radio.
- **Digest-pinned image**: upstream publishes exactly one tag, `latest`, so a tag tracks nothing.
  Same call as [`../webtop`](../webtop/README.md).
- **Readiness is `tcpSocket`, not `httpGet /node/state`.** REST answers long before the node has
  attached to a mesh, and a probe failing while detached would restart the pod exactly when it is
  trying to re-attach.

## Security

**`net1` bypasses kube-vnet entirely** — NetworkPolicy is not applied to secondary interfaces.
matter-server carries the same caveat, but here it is bigger in kind rather than degree: this pod's
whole purpose is to *forward packets* between the Thread mesh and the LAN, so an entire routed path
exists outside the policy layer by design. The `VirtualNetwork` in `vnet.yaml` governs only the
ClusterIP REST path, which is the small one.

`/dev/net/tun` is the one piece of node access. It is a single character device and cannot be
narrowed further.

## Verification

1. `./scripts/validate.sh` (no `-strict` — it rejects SOPS blocks), then
   `flux get kustomizations | grep otbr`.
2. Pod `Running`, and `kubectl -n otbr logs deploy/otbr` free of `Error decoding hdlc frame` /
   `Wait for response timeout`. Those two are the firmware signature from the issue above.
3. Sysctls took: `kubectl -n otbr exec deploy/otbr -- sh -c 'cat /proc/sys/net/ipv6/conf/all/forwarding
   /proc/sys/net/ipv6/conf/net1/accept_ra'` → `1` and `2`. Check these rather than inferring them
   from addresses — `net1` legitimately has only a link-local address while no router is advertising
   a global prefix on the LAN, which is the state right after the SLZB's OTBR is switched off. A
   global address should appear on `net1` once this border router is advertising its own prefix.
4. `curl http://<clusterip>:8081/node/state` → `leader` or `router`, and
   `/node/dataset/active` matches the TLV exported above.
5. **Confirm the dataset actually persists**: `kubectl -n otbr exec deploy/otbr -- ls /var/lib/thread`
   should show `0_<extaddr>.data`. `otbr-agent`'s `--data-path` is not set explicitly by the
   entrypoint, so this relies on its default landing in the mounted volume — check it rather than
   assume it, since the failure mode is silent until a restart orphans every device. (Verified: the
   file is written there, and survived the RBD→cephfs volume migration with the network identity
   intact.)
6. **From matter-server**, `/proc/net/ipv6_route` shows the mesh prefix via `net1`. That route is
   the entire point; its absence is the documented signature of a broken setup.
7. In HA: remove the old border router, add `http://otbr.otbr.svc.cluster.local:8081`. Devices reachable without
   re-commissioning proves the dataset migration worked.
8. Confirm zigbee2mqtt reconnected after the device reboot.

## Rollback

Flip the SLZB's Thread radio back to on-device OTBR and delete this directory. The dataset is
unchanged, so devices rejoin.

## Risks

- **The radio firmware is still SMLIGHT's.** If the fault is in the RCP rather than the
  border-router software, moving OTBR into the cluster changes nothing. That is the main way this
  effort is wasted, and it will be evident quickly.
- **TCP to a radio is out of spec** — see above.
- **RA conflicts** if the SLZB's own OTBR is somehow left running. Single radio makes this unlikely;
  verify it is off after the mode switch.
- **The Thread firewall is off** (`FIREWALL=0`), because the workers have no `ip6table_filter`
  module loaded and the entrypoint `die`s rather than degrades — with it on, the container exited
  before ever contacting the radio. What is lost is `OTBR_FORWARD_INGRESS`, the Thread 1.2 rules
  filtering mesh-to-LAN traffic, so mesh devices reach the LAN unfiltered. Bounded by the fact that
  `net1` already sits outside NetworkPolicy entirely. Restore by loading `ip6table_filter` on the
  workers via `k0sctl.yaml` and dropping the variable.
- **`otbr-agent` still logs `Firewall - failed to update ipsets`** every so often, for the same
  reason. Harmless with `FIREWALL=0`, and it disappears if the module is ever loaded.
- **`master: ens18` hardcoded**, as elsewhere. Fails loudly in `ContainerCreating`.
- **One box, two radios.** Zigbee and Thread still share a device; a firmware update takes both
  down. Unchanged by this work.
