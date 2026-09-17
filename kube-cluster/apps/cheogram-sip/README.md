# cheogram-sip

XMPP to SIP gateway. An XEP-0114 component on one side and Asterisk on the other, so a
Conversations-family client places and takes real phone calls over Jingle. Image
`singpolyma/cheogram-sip`, from the JMP / Soprani.ca people.

Two registrar hosts. Inbound is attributed to the **line** whose DID was dialled and rings
that line's call plan (`lines.<name>.inbound_route`); outbound is permitted by the
line's own `callers` list.

| host | role |
| --- | --- |
| `sip.example.net` (ExampleISP fiber) | outbound PSTN, and inbound on its numbers |
| `voip.example.org` | inbound, and its own short internal extensions |

`sip.example.net` carries three numbers, one carrier account each, on two lines. Each registers its
own contact, `<number>@xmpp.example.com`, so an inbound call's extension says which number was
dialled; that attributes the call to a line, and the line's `inbound_route` says who it rings.

eventpbx cannot place outbound PSTN calls: the INVITE is correct and digest authenticated,
and it answers `100 Trying` then `200 OK` and plays a recorded refusal. That is their policy,
not a config fault, and it presents as a connected call rather than an error.

PSTN refuses outbound SMS: a `MESSAGE` to the PSTN trunk is answered `501 Sip Gw only`. Also
their policy, not a config fault. Unlike the eventpbx refusal it is a visible error, but it
arrives after chan_sip has already answered the sender, so nothing propagates back.

## Registrations

Every registration except one is an entry in the routing chart's `registrations` table
(`routing/values.yaml`), rendered into `registrations.tsv`. The `render-config`
initContainer loops that file and `printf`s one `register =>` line per entry
into `registrations.conf`, which `sip.conf` `#include`s — because a register line names its
host after an at-sign, and any such occurrence in `sip.conf` itself makes the entrypoint skip
its whole setup.

**The entry name is the name of the Secret key holding that entry's password.** Nothing names
a password in the values: they render into a plaintext ConfigMap, which
`scripts/check-substitution.py` exists to keep credential-free. The passwords are instead
mounted as files: one Secret, `cheogram-sip-registrations`, mounted whole with no `items` at
`/sip-secrets`, so the entry name alone resolves to `/sip-secrets/<name>`. Adding a number --
or a whole new provider -- is therefore an entry plus a Secret key of the same name and **no
manifest edit**. The provider lives in the key name, never in the Secret name.

Entry names are `<provider>-<account>` and constrained to `[a-z0-9-]`, which is a legal
Kubernetes Secret key verbatim — nothing is normalised on the way, so there is nothing to
mistype and nothing to drift. The cost of the convention is that a rename breaks the link
implicitly, so the initContainer refuses to start the pod when an entry's key is missing or
empty, and `tests/check-routing.py` catches it one step earlier by reading the key names
straight out of the SOPS files (only the values there are encrypted).

An entry whose trunk also has a **peer** in `sip.conf` names a `peer_secret_file`, and the
same password is written there as a `secret=` line. Every other entry gets a peer of its own
rendered into `peers.conf` by the same initContainer, named after the **entry** rather than
after its host: `fromuser` is pinned per peer, so a peer named after a host shared by three
DIDs could present only one of them. `routing.lua`'s `peer` field names that section, and it
is what the dialplan dials — see the comment on that `#include` in `sip.conf`.

`contact_user` is PJSIP's name for the same field and is what inbound keys on, so the pair
(`host`, `contact_user`) has to be unique — two entries sharing one arrive on the same
extension and nothing downstream can tell them apart, and `tests/check-routing.py` rejects
that. The pair, not the contact alone: the household and eventpbx registrations both
register `SIP_JID` and are told apart by the peer the call arrived on.

The household number's own registration is the one the **image's entrypoint** builds, from
`SIP_USER`/`SIP_HOST`/`SIP_JID`, and it cannot be stopped without fighting the image. It is
declared in the table anyway, marked `entrypoint: true`, because without an entry it has no
line and therefore no identity. That flag keeps it out of `registrations.tsv` — a second
REGISTER for the same account would fight the entrypoint's. Its key follows the convention like
every other; it is simply the one key the main container reads too, as `SIP_PASSWORD`.
`tests/check-routing.py` holds that row against `cheogram-sip.env`, because it is the one row
nothing here renders and therefore nothing here can correct.

It is also the **fallback** for its host: an inbound call whose contact matches no entry is
attributed to it. That is what the carrier no longer echoing our registered Contact would look
like, and it degrades per-DID inbound back to one line rather than to nothing.

## Shape

```
eventpbx ---- SIP/UDP 5060 outbound REGISTER only, no inbound forward
    |           RTP UDP 10000-10049 both ways
    |
[cheogram-sip] --XEP-0114, plaintext, pod network--> snikket pod, prosody :5347
    |
    cheogram-sip-lb, MetalLB 10.20.2.81, externalTrafficPolicy: Local, 50 UDP ports
```

Two components, both defined in `apps/snikket/sip.cfg.lua`, neither in DNS and neither in
any cert: `sip.xmpp.example.com` (user facing, dial `+49XXXXXXXXX@sip.xmpp.example.com`) and
`asterisk.xmpp.example.com` (Asterisk's own chan_xmpp connection).

Not a container in the snikket pod, though `CONNECT_IP`/`CONNECT_PORT` would allow either.
Snikket is `Recreate` on an RWO PVC, so every Asterisk iteration would be a cold restart of
the household chat server. This way it costs exactly one, ever.

## The image fights you

It is `latest`-only, amd64-only, last pushed 2024-02-21, and uses `chan_sip`, which upstream
Asterisk removed in 21. The digest is pinned, so the digest is the version. Re-diff
`extensions.lua` against the image after any bump.

- **`/etc/asterisk` must be writable.** The entrypoint seds the component secret and the
  trunk password into `xmpp.conf` and `sip.conf`. A read-only ConfigMap mount there makes it
  fail, so `render-config` copies the image's own `/etc/asterisk` into a Memory-backed
  emptyDir and overlays ours. Memory-backed because the password lands in that file.
- **`sip.conf` must keep a line starting `;register => 1234`.** The entrypoint inserts the
  real register line after it. Delete the sentinel and registration never happens, silently.
  The init container asserts it.
- **`sip.conf` must not mention the trunk host after an at-sign, not even in a comment.** The
  entrypoint guards its whole setup with `if ! grep "@$SIP_HOST" sip.conf`, so any occurrence
  makes it skip registration, the peer and the dialplan edit. A comment warning about this
  trap once triggered it. The init container asserts this too.
- **`sip.conf [general]` needs `context=public`.** `extensions.lua` defines only `public`,
  `jingle` and `xmpp`; the Debian default `context=default` does not exist there, so every
  inbound call would die in a missing context.
- **The entrypoint prints the register line, password included,** whenever it finds sip.conf
  already configured, which is every container restart. Its stdout is filtered for that.
- **The peer block is written by the entrypoint**, not by us. Only `[general]` is ours.
- **The binary exits when it loses the component connection.** Every prosody restart costs it
  a crash-restart cycle, because `wait-prosody` only runs on pod start, not container restart.

## Outbound gate

**`lines.<name>.callers` is the whole of outbound permission.** A destination resolves to a
line — by routing rule, by `#<line>` label, or by a bare trunk label's default registration —
and `line_permits` asks that line's list whether this caller may present it. It **refuses**
(`kind = "forbidden"`, no peer); it does not fall through to another line, so revoking someone
is one edit and cannot leak.

Fails closed everywhere: no `callers` key, an empty list, an unknown line, or a caller the
dialplan could not identify all refuse. Every path names its caller — XMPP calls from the
Jingle CALLERID and XMPP messages from the MESSAGE from-JID, Matrix from `X-Matrix-Caller` on
the INVITE, SMS from the From display name.

`from_glob` on an outbound rule is **not** this. It narrows which rule applies and falls
through when it does not match; permission is only ever the line.

An `emergency` rule pins a registration rather than a line, so no `callers` list is consulted
and 110/112 stay reachable from an account permitted nothing else. `tests/check-routing.py`
holds its `to_pattern` to digits and classes, because it is the one ungated path.

`extensions.lua` is our copy of the image's. The entrypoint hardcodes a single caller by
rewriting the comment `-- outbound calls` into `if from ~= "$SIP_JID" then return end`; our
file omits that comment, so the sed is a no-op. **A sed that matches nothing exits 0**, so the
gate above is what stands between the trunk and everyone.

Where the outer boundary sits: the components have `modules_disabled = { "s2s" }`, so only
local accounts reach them, and `apps/snikket/ldap.cfg.lua` only admits `memberOf=xmpp` or
`memberOf=admin` with self-registration disabled. Adding someone to the lldap `xmpp` group
gets them as far as the gate; the line's `callers` decides the rest.

## prosody side

`apps/snikket/sip.cfg.lua`, loaded through `SNIKKET_TWEAK_EXTRA_CONFIG`. Two settings there
are load-bearing and both fail silently:

- **`component_interfaces = { "0.0.0.0" }`** — prosody binds 5347 to localhost otherwise, and
  it must sit above the `Component` lines, since a Component opens a section that runs to end
  of file.
- **`validate_from_addresses = false`** — Asterisk's `res_xmpp` sends some stanzas with no
  `from`, and prosody kills the stream over it (`<invalid-from/>`). The gateway then
  reconnects, works for one call, and dies again, so the symptom is "the first call arrives,
  later ones cannot be established". This is a global relaxation; it is acceptable only
  because both components are ours, reachable solely from this pod.

## SIP_HOST is the PSTN trunk, not just "the one the image registers"

The entrypoint builds a peer and register line from `SIP_HOST`, but the `cheogram-sip` binary
is *also* handed it as its PSTN gateway, and it appends that host to any number dialled
without one. So the dialplan never sees a bare number, and the routing table could never
apply, unless `SIP_HOST` already is the trunk you want. Hence PSTN is `SIP_HOST` and
eventpbx is defined by hand in `sip.conf`, which is the reverse of how this was first built.

The dialplan strips a host equal to `SIP_HOST` before routing, treating it as "no host was
given", so short extensions still reach their own trunk.

Outbound selection:

| dialled | goes |
| --- | --- |
| no label | the first matching rule in the routing table, below |
| label in `SIP_TRUNKS` | that peer, with credentials |
| label naming a **line** | that line's primary registration's peer, with credentials |
| anything else | **refused** |

`SIP_TRUNKS` entries MUST equal peer names in `sip.conf`. The last row is the fail-closed
part: an unrecognised label used to fall through to `SIP/<user>@<label>`, a dial with no
credentials that resolved the label as a DNS name — which a provider may answer with a
recorded refusal, i.e. a connected call rather than an error. Federated SIP dialling goes with
it, and comes back, if at all, as a deliberate allow list.

## The routing table

`routing/` is a Helm chart whose **values are the table**. That is the whole reason it is a
chart: Helm checks its values against `values.schema.json` before it renders anything, so a
malformed table is rejected by helm-controller at reconcile time instead of by Asterisk at
call time. Reading the same YAML with `.Files.Get` would render it unvalidated.

```
routing/values.yaml  --helm--> ConfigMap cheogram-sip-routing
                       |          +-- routing.lua        --> mounted at /routing, read
                       |                                     per call via SIP_ROUTING_FILE
                       |
                       +-----> ConfigMap cheogram-sip-registration-table
                                  +-- registrations.tsv  --> render-config loops it and
                                                             printfs registrations.conf
                                                             and peers.conf
```

Two renderings of the same values, because their readers are not the same: the dialplan wants
a Lua table, and `render-config` is `/bin/sh`, which has no business parsing one. The TSV is
that half — `name`, `user`, `host`, `contact_user`, `peer_secret_file`, TAB separated, one
`while read` away. An `entrypoint` entry appears only in the Lua half.

`routing-release.yaml` is the HelmRelease, pointing at a path in this repo against the
`flux-system` GitRepository — the same arrangement as `flux-system/generators.yaml`, and
`reconcileStrategy: Revision` for the same reason: editing the values bumps no chart version.
Two ConfigMaps because their readers reload differently. `pbx_lua` gives each channel its own
`lua_State` and re-executes `extensions.lua` per call, so `load_routing`'s `loadfile` re-reads
the mount every call: a routing edit is live once kubelet refreshes the volume, with no
restart, and a malformed table fails closed (no trunk) instead of at pod start.
`cheogram-sip-routing` therefore carries `reloader.stakater.com/ignore`. The registration table
is read once by `render-config`, so `reloader` still rolls the pod when that half changes.

`tests/` holds this app's checks; the convention is documented in `../../README.md`. What the
schema cannot state lives in `tests/check-routing.py`, which runs against the **rendered**
output: every registration's `host` must be a peer in `sip.conf`; every `line` must exist and
every `primary` must belong to the line that presents it; every registration's `peer` must be
the section the initContainer actually writes; every outbound rule must resolve,
and a rule may pin a registration only as an emergency route; `did` must be unique and
(`host`, `contact_user`) must be unique; each host needs exactly one default registration, so
the inbound fallback is defined; a line name may not also be a `SIP_TRUNKS` host, or `#label`
would be ambiguous; and a rule may present only a registration whose peer pins `fromuser` to
it — in `sip.conf` or in the rendered `peers.conf`, whose format string it reads out of
`resources.yaml` rather than restating — because a carrier may reject or silently rewrite a From
the account does not own. On the
registrations half: every entry name must resolve to a Secret key that exists, `sip.conf` must
`#include` what the initContainer writes and still mention no `@SIP_HOST`, and the
`entrypoint` row must agree with `cheogram-sip.env`. It also asserts that Helm still rejects a
bad table, because a schema that has quietly stopped being read looks exactly like one that
passes.

**`to_pattern` is a Lua pattern, not a regex.** Asterisk's Lua 5.1 has no regex engine, and a
regex here would not error — it would match nothing and the call would fall through to the
next rule. The schema rejects `{ } |` and backslash so that mistake fails loudly; repetition
counts are spelled out, `%d%d?%d?%d?%d?` for one to five digits. The renderer anchors each
pattern as `^(...)$` exactly once, so `11[02]` cannot also match `0112345`.

### Lines

A **line** is an identity namespace, and it is what the number in front of the separator
belongs to. Three DIDs share one host, so the host cannot say which of them rang; the line
can. A caller is rendered `<line>-<digits>` to Matrix (portal `office-4915112345678`,
conference `sip-office-4915112345678`) and `+4915112345678#office` to XMPP — and that XMPP
form is also how a client dials back, so the two stay symmetric.

Membership is `registrations.<k>.line` and nothing else; a line has no member list that could
fall out of step. `primary` says only which member the line *presents* outbound — Ofcom's
Presentation Number, as against the Network Number the carrier transmits regardless.

There is **no default line**: every line prefixes, so `#<host>` is not a line name and fails
closed. The cost is a one-time break — every existing Matrix portal and every saved XMPP
contact is re-keyed, rooms are re-created and history stays in the old ones.

Split a portal ID back with `^(.*)-([0-9]+)$`, anchored on the trailing digit run, so a line
name may contain hyphens. `-` and not `_`, because mautrix escapes a literal underscore to
`=5f` in an MXID localpart; the schema rejects one in a line name.

The emergency rule pins a **registration** rather than a line, bypassing the presented number.
The presented number is what fixes the address the dispatcher sees, Sec. 164 TKG puts the duty
to transmit it on the carrier, and Sec. 120(2) permits presenting only a number a
Nutzungsrecht exists for — so the only meaningful choice is *which* registration, and a
free-text CID field here would be reassuring and inert.

Two traps. The per-DID discriminator is the carrier echoing our registered Contact in the
inbound Request-URI — **observed behaviour, not a standard**; if it stops, inbound collapses
to the host's default line, which is otherwise indistinguishable from a correct match. Every
call attributed that way logs `inbound fallback:`. And a line can only really present its primary once that
registration has a peer of its own pinning `fromuser`, which is what `peers.conf` now gives
every non-entrypoint registration; `tests/check-routing.py` still refuses a rule selecting a
line whose primary has none.

### What of the table is live, and what is not

Live: outbound selection, first match wins, `emergency`, and the whole identity rendering —
inbound attribution, the XMPP host slot, and the Matrix conference name. A destination that
carries no label is routed only by this table; with the table missing or malformed the
dialplan finds no peer and **refuses** the call.

Partly live, and the limit:

- **`from_glob`** narrows a rule to certain callers, which is how a per-person default line is
  expressed; one rule uses it. It falls through: a caller it excludes is not refused, the next
  rule decides. Permission is the line's `callers`.
- **The Matrix outbound path ignores the line in the conference name** and routes by
  `to_pattern` like every other path. The bridge cannot produce a line-prefixed conference for
  a call a Matrix user starts, so honouring one would only work for call-backs.

## Caller ID is normalised to E.164

Carriers name the same human as `+49...`, `0049...`, `0...` or bare, and nothing downstream
reconciles them, so one caller arrives as several XMPP JIDs and several Matrix portal rooms.
`extensions.lua` collapses the caller's number on both inbound paths, calls and SMS, driven by
`SIP_E164_COUNTRY`.

It sits in the dialplan rather than in a `normalize-cid` gosub because that recipe does not apply
here: this dialplan keys off `SIP_HEADER("From")`, not `CALLERID(num)`, so rewriting `CALLERID`
would change nothing. There is no later repair either, because for a call the conference name is
the only channel that carries caller identity onward.

Two ordering traps. Anything at or below `SIP_SHORT_MAXLEN` is left alone, or 110, 112 and
eventpbx's own extensions become `+49110`; and `00` is tested before `0`, or `0049...` becomes
`+4949...`.

`SIP_E164_AREA` is deliberately empty, and empty is the safe value. A bare number is either a
national number with the trunk `0` omitted, needing only the country code, or a subscriber number
with no area code at all, needing one prepended. Nothing in the call distinguishes the two, so
setting it would corrupt every number of the first kind. Bare numbers pass through untouched
until a week of `normalize_e164:` NOTICE lines shows which the carrier actually sends.

## Caller JIDs use `#`, not `\40`

A SIP address has to fit in a JID localpart, which cannot contain `@`. XEP-0106 escapes it to
`\40`, giving `5551\40voip.example.org@sip.xmpp.example.com`. We use `#` instead:
`5551#voip.example.org@sip.xmpp.example.com`.

`#` is legal in a JID localpart and is one of the few characters RFC 3261 does **not** allow
unescaped in a SIP user part, so splitting back to `user@host` is unambiguous. A dot would
not be: a SIP user part may contain dots, so `john.doe.example.com` cannot be split reliably.

Cost: `#` starts a fragment in URI syntax, so a JID rendered into an `xmpp:` URI must
percent-encode it. `sip_uri_to_local` and `local_to_sip_uri` are the two ends of the
conversion; change one and calling back breaks.

The host in that JID is the **line name**, not whatever the carrier put in `From` (an SBC IP
address, in eventpbx's case). That says which of our numbers rang, survives the carrier
renumbering, and makes the JID dialable back through the credentialed peer rather than
anonymously to an IP. Not a reverse DNS lookup: a PTR is whatever they chose to publish, need
not route anywhere, and would put a resolver in the call path.

## Hardcoded addresses, and what happens when they change

| address | where | risk |
| --- | --- | --- |
| `10.21.27.11` | `hostAliases` in `resources.yaml` | ExampleISP's proxy, from their portal. Only needed because `sip.example.net` does not resolve. If it changes, or if they ever publish real DNS, the entry silently wins and registration stops with no error. |
| `203.0.113.10` | `sip.conf` `externip`, `rtp.conf` `ice_host_candidates`, and `coturn/turnserver.conf` | The static tunnel address, duplicated across two apps with no single source of truth. Wrong value means one-way audio in SIP and dead TURN relays at once. |
| `10.20.0.0/16`, `172.18.0.0/16`, `172.19.0.0/16` | `sip.conf` `localnet` | LAN, podCIDR, serviceCIDR. Only change on a cluster rebuild. |
| `10.20.2.81` | `metallb/pool.yaml` and the Service | Ours. |

eventpbx's SBC is deliberately **not** pinned; it is resolved from `voip.example.org`.

Diagnostic when a trunk goes quiet, before touching anything (`403 not registered` is the
healthy pre-registration answer, and proves reachability plus that the source is accepted):

```bash
python3 -c '
import socket
m=("OPTIONS sip:sip.example.net SIP/2.0\r\nVia: SIP/2.0/UDP 10.20.5.15:5064;branch=z9hG4bKp;rport\r\n"
   "Max-Forwards: 70\r\nFrom: <sip:p@sip.example.net>;tag=p\r\nTo: <sip:sip.example.net>\r\n"
   "Call-ID: probe\r\nCSeq: 1 OPTIONS\r\nContact: <sip:p@10.20.5.15:5064>\r\nContent-Length: 0\r\n\r\n")
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(8); s.bind(("",5064))
s.sendto(m.encode(),("10.21.27.11",5060)); print(s.recvfrom(4096)[0].decode().split("\r\n")[0])'
```

**Do not diagnose this path with ping or traceroute.** ICMP is filtered on it, so both fail
while UDP 5060 works perfectly. That mistake cost an evening.

## Router

Forward to `10.20.2.81`:

| port | protocol | why |
| --- | --- | --- |
| 10000-10049 | UDP | RTP, both the trunk leg and the Jingle leg |

**Not 5060.** Registration auth means Asterisk registers outbound and inbound INVITEs return
down that pinhole. A forwarded 5060 is a scanner magnet and buys nothing here.

### The PSTN path needs a route as well as a srcnat

ExampleISP carries telephony on its own VLAN (232; internet is 132), and `10.21.27.11` lives inside
the carrier network, five hops away over the WAN. Reaching it correctly needs **both**:

1. a route sending `10.21.0.0/16` out the telephony VLAN interface
2. a srcnat to that interface's DHCP address (`10.100.x`)

A srcnat alone rewrites the source but does not change routing, so the packet still leaves via
the WAN carrying a VLAN address and the ISP drops it as spoofed. The symptom is total silence
with conntrack still counting replies. Confirm success by the `received=` value in the
registrar's answer: it should be the `10.100.x` address, not the CGNAT one.

The RouterOS SIP helper (`/ip firewall service-port sip`) is disabled. It was suspected during
that outage and was not the cause, but a SIP ALG rewriting Contact and SDP while Asterisk sets
its own `externip` is a known conflict, so leaving it off is the right posture.

Separately, and unrelated to this app, **Snikket itself needs TCP 5222 and TCP 5269 forwarded
to `10.20.2.15`**. Both were missing until 2026-09-12, which meant clients could not connect
from outside the LAN and no inbound s2s had ever succeeded. The second one breaks push
notifications, and push is what wakes a backgrounded phone for an incoming call, so calls did
not ring even though the gateway was working.

## Addressing quirk

The advertised address and the egress address differ. `externip`/`ice_host_candidates` say
`203.0.113.10`, a static address arriving through a tunnel, while outbound traffic egresses
from the DS-Lite CGNAT address. So a far end that latches onto our source (comedia, or ICE
peer-reflexive) and one that obeys our SDP send media to two different places. Both work in
practice, and ICE is what rescued the first working call, but it is a standing source of
one-way audio if something changes.

Asterisk deliberately does **not** use the shared coturn: its `turnusername`/`turnpassword`
are static, while coturn runs `use-auth-secret` with time-bounded HMACs Asterisk cannot
derive. The client side already gets coturn from Snikket, and Asterisk is the end with a
static address and forwarded ports, so it is the side that least needs a relay.

## Verifying

```bash
# trunk
kubectl -n cheogram-sip exec deploy/cheogram-sip -c cheogram-sip -- asterisk -rx 'sip show registry'
kubectl -n cheogram-sip exec deploy/cheogram-sip -c cheogram-sip -- asterisk -rx 'sip show peers'
# components
kubectl -n cheogram-sip exec deploy/cheogram-sip -c cheogram-sip -- asterisk -rx 'xmpp show connections'
# what actually happened to a call, including the dialled JID and the disposition
kubectl -n cheogram-sip exec deploy/cheogram-sip -c cheogram-sip -- sh -c 'tail -3 /var/log/asterisk/cdr-csv/Master.csv'
```

The CDR is the useful instrument. Asterisk logs almost nothing at default verbosity, and
`asterisk -rx "core set verbose"` only raises it for that short-lived remote console, so it
looks like silence. `channel originate` in particular returns 0 and prints nothing whether it
works or not.

**Jingle needs a full JID.** `Motif/jingle-endpoint/<bare jid>` creates no channel at all, with
no error anywhere. Add the resource (`prosodyctl shell "c2s:show()"` lists them) to ring a
client directly, which is a useful way to test the media path without involving the trunk:

```bash
asterisk -rx 'channel originate Motif/jingle-endpoint/user@xmpp.example.com/Resource application Echo'
```

## The Matrix bridge peer

`matrix-sip-bridge` (`apps/matrix`) is an ordinary SIP peer, `[matrixbridge]` in `sip.conf`, not
an AMI client. It is **static**: it never REGISTERs, so `host=` names its Service in the `matrix`
namespace, and `transport=tcp` because SIP MESSAGE over UDP fragments and is silently dropped.
That needs `tcpenable=yes` in `[general]`, or chan_sip refuses the peer without using it.

The peer name must equal `sip.username` in the bridge's own config. The bridge digest-
authenticates `REGISTER` and nothing else, so its INVITEs and MESSAGEs arrive unauthenticated
from a pod address that is not `host=`; the From user part is all chan_sip can match them on,
which is also why `insecure=invite,port` is set.

`accept_outofcall_message=yes` plus an `outofcall_message_context` is required globally *and* on
the peer, or an inbound MESSAGE is refused before any dialplan sees it. Setting it in `[general]`
is a global default neither trunk overrides, so *every* out-of-call MESSAGE — carrier SMS included
— lands in `messages-in`. That is why the context is a dispatcher rather than the bridge's own
inbound path; see "SMS" below.

### Inbound calls into Matrix

An inbound trunk call rings the call plan of the line whose DID was dialled:
`lines.<name>.inbound_route`, a list of stages tried in order until one answers. A stage's
`ring` targets go into **one** `Dial()`, so they ring together; the stages are sequential, and
because Asterisk times a whole `Dial()` and never a single target, **total ring time is the sum
of the stages**. A `record` stage is terminal and, with no voicemail in this image, ends the
call. The bridge's `calls.ring_timeout` must stay above any total the plan can reach: it is a
backstop against a wedged dialplan, not the ring policy.

A `tel:` target — forwarding a stage to an external number — is **refused, not pending**. It
originates an outbound trunk call from inside an inbound one, and none of that is safe here:
the leg carries no caller identity for `callers` to gate, eventpbx answers outbound with a
recorded refusal that `Dial()` reads as an answer and cancels every other leg with, a mobile's
voicemail answers before a human does, nothing caps concurrent trunk channels or the 50-port
RTP pool, and a number that forwards back to a DID loops with nothing to bound it.

A stage that rings both transports is XMPP and Matrix as legs of one `Dial()`, with
`SIPAddHeader(X-Conference: sip-<line>-<caller in E.164 without the plus>)` in front of it. That
header is the only thing the bridge routes on: the portal room and the ghost are named from it and
from nothing else, and an INVITE naming a conference it does not recognise is declined, so a
mismatch with `calls.conference_prefix` fails closed and silently. A caller on a line the dialplan
could not attribute gets no header and no Matrix leg.

Three Asterisk constraints shape the rest, all of them verified rather than assumed:

- **A channel inside `ConfBridge()` cannot also `Dial()`.** Parking the caller and then ringing
  the legs is impossible, so the order is Dial first, ConfBridge after.
- **`Dial()` hangs every other leg up with `ANSWERED_ELSEWHERE` the instant one answers.** The
  bridge answering is therefore what cancels the XMPP legs — intended, and the reason the bridge
  will not answer until a Matrix user has actually joined the RTC session. That logic lives in
  the bridge; do not defeat it from here.
- **`U()` plus `GOSUB_RESULT=CONTINUE` is mandatory.** The bridge's leg carries no media, so
  without it the caller is bridged to silence. CONTINUE hangs that leg up and drops the caller
  through to `ConfBridge()` at the next priority.

The `matrix-answered` gosub runs on whichever leg answered, XMPP included, and **app_dial hangs
the answered leg up if the gosub fails** — so its body is wrapped in `pcall`. A Lua error there
would otherwise drop ordinary calls to the household number.

`confbridge-matrix.conf` is appended to the image's `confbridge.conf` and adds only the two user
profiles: livekit-sip is the marked user, the caller is `end_marked`. The bridge ends a call by
removing livekit-sip from the LiveKit room, and that pairing is the only thing that then hangs
the caller up.

### Outbound calls from Matrix

The bridge INVITEs `sip:<E.164>@cheogram-sip-internal...` from the `[matrixbridge]` peer, carrying
the same `X-Conference` header. Reaching `context=matrixbridge` at all requires that peer, which is
the only authorisation on this path.

The far end has to end up in the **conference**, not bridged to the bridge's control leg, which
carries no media — so the dialplan uses `Originate(...,exten,matrix-outbound,<conference>,1)` rather
than `Dial()`. `Originate` blocks until the call is answered or fails and sets `ORIGINATE_STATUS`,
which is the only way this path can answer the bridge with a real SIP status instead of hanging.

The 200 is deliberately withheld until the far end answers. The bridge only asks livekit-sip to
join the conference *after* its INVITE is answered, so answering early would trade the one failure
signal there is for a second of earlier audio. A failure hangs the leg up with a cause chan_sip
turns into a response: a destination that is not a number or a bad conference header 404, no trunk
503, busy 486, no answer 480. The bridge fails the call on all of them alike.

Trunk selection is `resolve_destination`, shared with the XMPP path so the two cannot drift. Caller
ID is not the dialplan's: every peer pins `fromuser`, so the call presents that peer's own number
whatever `CALLERID` says. Which of our numbers a caller presents is selected by picking the peer,
which is the line's primary registration's — so a `from_glob` rule is what makes it per-person,
and only on the XMPP path, where the caller is known.

### SMS

`messages-in` is a **dispatcher**: it is where both trunks and the bridge land, so it classifies
each MESSAGE before doing anything with it. A message is outbound only when the `From` user part
is the bridge peer **and** the extension is a bare number. Both conditions are required — on the
`From` alone, a carrier-injected `From: sip:matrixbridge@…` would be an outbound-SMS primitive on
our trunk, billed to us. The trunk's own messages fail both: their `From` is a subscriber number
and their extension is the JID from the register line's quoted contact.

Inbound goes to XMPP **and** Matrix, mirroring how an inbound call rings both, and the order is
load-bearing. XMPP is sent first because the bridge's handler can block for 32s and a hung bridge
must not delay the phone; `MESSAGE_SEND_STATUS` is one channel variable overwritten by each send,
so it is captured between them.

The bridge leg is sent only for a sender that normalises to `+E.164` — the same gate the call path
uses for the conference name, and both derive it from `matrix_conference_name`, which is what keeps
an SMS and a call from one number in **one** portal room. The sender's host is rewritten to the
line exactly as on the call path, so the JID resource is `+49…#home` rather than the carrier's SBC
address. The line comes from the dialled contact, as on the call path; whether a carrier echoes
our Contact on a `MESSAGE` is untested, and one that does not falls back to the host's default
registration and logs `how=[entrypoint]`. The `+` matters on the bridge leg: the bridge rejects a number without one
and then drops the message *while answering 200 OK*.

Outbound reuses `resolve_destination`, and sends with no `from`, so chan_sip applies the peer's
`fromuser` — the same way the call path gets its caller ID. There is no allow-list and no rate
limit: the trust boundary is the `[matrixbridge]` peer plus the numeric-destination check, as for
outbound calls. Any Matrix user in a portal room can text any routable number a trunk accepts.

Today that is none of them: PSTN answers outbound `MESSAGE` with `501 Sip Gw only`, so the
outbound path is built, correct and dead. It stays because it costs nothing and works the day
a trunk that accepts `MESSAGE` is added. Inbound SMS is unaffected and does work.

The silent-failure surface here is large — chan_sip answers `202` before the dialplan runs, the
bridge answers `200` for a message it drops, and `MESSAGE_SEND_STATUS=SUCCESS` means "handed over",
not delivered. The `sms inbound`/`sms outbound`/`xmpp outbound` NOTICE lines are the whole
observability story.

The bridge caps a MESSAGE at 20480 serialised bytes with no segmentation and no retry, so a long
Matrix message fails outright.

## Decided, not yet built

Design settled, no code. Nothing in this section describes current behaviour.

110 and 112 are configured to behave sensibly, but they are not the emergency plan. This path is
VoIP behind DS-Lite CGNAT, with a documented history of one-way audio and of trunks that stop
registering when a route changes. A mobile stays the actual emergency plan.

- **Voicemail means recording a file and delivering it**, because `app_voicemail` is not in the
  image. `app_record`, `app_confbridge`, `app_queue`, `app_followme` and `app_mixmonitor` are.
  The bridge already has media upload and Matrix API access, so a recording becomes an `m.audio`
  event with the MSC3245 voice marker and renders as a normal voice message.
- **Voicemail is Matrix only by design, and that is not a defect.** Snikket's prosody has
  `mod_http_file_share` enabled and Conversations-family clients render voice messages fine, so
  the gap is delivery, not support: Asterisk cannot perform an XEP-0363 upload, and the component
  relays only text, since `MessageSend` carries a body and not a file. The XMPP side still gets
  the call, just not the recording. Parity, if ever wanted, means teaching the bridge XEP-0363
  against Snikket's `http_file_share` and sending an OOB message. Asterisk cannot help either way.

## State

Working, both directions, with real audio. From the CDR:

```
public  5551 -> admin@xmpp.example.com   billsec=3    ANSWERED   inbound
jingle  lhns -> jingle-endpoint     billsec=3    ANSWERED   outbound
public  7669 -> admin@xmpp.example.com   billsec=12   ANSWERED   inbound
jingle  lhns -> jingle-endpoint     billsec=10   ANSWERED   outbound
```

`context=public` is an inbound call from the trunk, `context=jingle` an outbound one from a
client. `billsec` above zero is the bit that matters: media flowed, so ICE, DTLS-SRTP and the
RTP forwards all work in both directions. Also proven: DTLS needs no certificate config, the
client's fingerprint simply arrives.

Not yet proven live: the gate's **negative** case. Dial out from an account in no line's
`callers` and confirm refusal, because a silently open trunk looks exactly like a working one.
It is covered offline — `dialplan_test.lua` asserts refusal per path and the suite fails if the
gate is disabled — but a real call has not been refused yet. A refusal logs
`Cannot dial '<to>': caller is not in the callers list of line '<line>'`.
