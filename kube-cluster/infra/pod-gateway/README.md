# pod-gateway — VPN-only egress for selected pods

Replaces the Swarm arrangement (`lhns/vpn-gateway` + `swarm-launcher` elevating gluetun as the
gateway for an `internal: true` overlay) for `apps/arr`. Those two pieces were Docker-socket
tricks with no Kubernetes equivalent; gluetun itself carries over unchanged, including
`SERVER_COUNTRIES=Netherlands` and `UPDATER_PERIOD=24h`.

## How it works

A mutating admission webhook intercepts pod CREATE in namespaces labelled `routed-gateway: "true"`.
For selected pods it injects an init container and a sidecar, and rewrites the pod's DNS config.
The init container adds routes for `NOT_ROUTED_TO_GATEWAY_CIDRS` via the kube-router gateway,
**deletes the default IPv4 and IPv6 routes**, verifies the internet is unreachable, builds a
`vxlan0` interface to the gateway pod, and installs a default route through it.

The route rewrite happens in an **init container**, so Kubernetes will not start the app containers
until it exits 0. That gating property is the reason this shape was chosen; do not weaken it.

The gateway pod runs the VXLAN endpoint, in-tunnel DHCP and DNS, SNAT, and gluetun.

## What this deployment does NOT give you

The design brief specifies four layers. This deploys layers 1 and 3 only. Read this before
assuming a pod here cannot leak.

1. **No nftables lock inside the pod netns.** Isolation rests entirely on the default route being
   deleted. Since Linux 5.7 an unprivileged process can call `SO_BINDTODEVICE` to bind a socket
   straight to `eth0` and bypass the routing table; dropping `CAP_NET_RAW` does not prevent this
   on any kernel we run. Closing it means patching `client_init.sh` to add a default-drop output
   ruleset permitting only `oif vxlan0` plus the local CIDRs. That patch is not applied here.
   `apps/arr/networkpolicy.yaml` is a partial, negatable substitute.
2. **The upstream leak check is ICMP-only to a hardcoded 8.8.8.8.** An upstream that filters ICMP
   while permitting TCP produces a false pass in the dangerous direction. A `verify`
   initContainer doing a real TCP test is designed but not yet written.
3. **No MikroTik backstop and no dedicated pod CIDR.** Nothing here survives our own bugs, and a
   leak would exit as `172.18.x.x`, indistinguishable from any other pod. Deliberately deferred.

Layer 3 is met for free: the chart leaves `failurePolicy` unset on the MutatingWebhookConfiguration,
which is the API-server default `Fail`. That rejects pod creation while the webhook is unreachable;
it does not touch running pods.

## Choices that are not obvious

**The chart's default NetworkPolicy is wrong for this cluster and is replaced.** Upstream allows
egress to UDP 1194 (OpenVPN) and `10.0.0.0/8` ("cluster IPs, default k3s"). We run WireGuard, and
`10.0.0.0/8` is our LAN while the cluster is `172.18.0.0/16` / `172.19.0.0/16`. Left as shipped it
blocks CoreDNS and the gateway never resolves a NordVPN endpoint. The replacement in `release.yaml`
permits DNS, UDP 51820, TCP 443 for gluetun's server-list updater, and the cluster CIDRs.

**`FIREWALL_OUTBOUND_SUBNETS` must contain the pod and service CIDRs, not just the VXLAN subnet.**
gluetun's firewall evaluates packets after kube-proxy has already DNAT'd a ClusterIP to a pod IP,
so it sees pod addresses.

**`NOT_ROUTED_TO_GATEWAY_CIDRS` must contain `172.18.0.0/16` and `172.19.0.0/16`.** Without them a
routed pod loses CoreDNS, the API server and every other pod the moment its default route is
deleted, and the init container's own gateway lookup fails.

**Opt-out, not opt-in.** `gatewayDefault: true` routes everything in the namespace; a pod leaves
the tunnel with the label `setGateway: "false"`. A forgotten label therefore means *more*
isolation, not less. Nothing in `arr` currently uses it — workloads that must not be tunnelled
get their own namespace instead, which is why seerr is not in there.

**This namespace must differ from the routed namespace** — a chart requirement, and why
`arr` is a separate namespace rather than running the apps here.

## The `arr` namespace is declared here

Deliberate, and explained at length in `namespace.yaml`. Short version: the chart renders its
settings ConfigMap *into* `arr`, so that namespace must exist before this HelmRelease
installs; and `app-arr` must not create pods before the webhook exists, because a missing
webhook does not block pod creation, it silently fails to mutate — the apps would come up with
their default route intact. Both constraints point the same way, so this component owns the
namespace and `app-arr` dependsOn it.

`routed_namespaces` in `release.yaml` and the `routed-gateway` label in `namespace.yaml` must
always name the same set.

## MTU

`client_init.sh` derives the vxlan MTU as `eth0 - 50` and clamps `VPN_INTERFACE_MTU` to it. The
comparison is written `[ ${VPN_INTERFACE_MTU} >= ${VXLAN0_INTERFACE_MAX_MTU} ]`, where `>` is
parsed as a redirection, so the test always errors and the `else` branch always wins.

On these nodes pod `eth0` is 1500, giving a 1450 ceiling, and WireGuard's 1420 sits below it — so
the bug is benign and the result is correct by accident. **If node MTU ever drops below 1470 the
init container will fail and pods will not start.** Fix is `-ge` with quoted operands; the patch
exists but is not applied here.

Verify MTU end to end with a large-payload transfer through the tunnel, not by reading `ip link`.

## Verifying

```sh
# gateway itself
kubectl -n pod-gateway logs deploy/pod-gateway -c gluetun     # WireGuard handshake
kubectl -n pod-gateway exec deploy/pod-gateway -c gluetun -- wget -qO- https://ifconfig.io

# a routed pod: exactly one default route, on vxlan0, and NordVPN's IP
kubectl -n arr exec deploy/sabnzbd -- ip route
kubectl -n arr exec deploy/sabnzbd -- wget -qO- https://ifconfig.io

# fail-closed: kill gluetun, routed pods must lose the internet rather than
# falling back to eth0
```
