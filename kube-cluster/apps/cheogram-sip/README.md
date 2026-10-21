# cheogram-sip

XMPP to SIP gateway. An XEP-0114 component on one side and Asterisk on the other, so a
Conversations-family client places and takes real phone calls over Jingle.

Two images, because the two halves want different things. The XEP-0114 component is
`singpolyma/cheogram-sip`, from the JMP / Soprani.ca people. Asterisk is our own fork,
`ghcr.io/lhns/asterisk` — see [The Asterisk image](#the-asterisk-image).

Two registrar hosts. Inbound is attributed to the **line** whose DID was dialled and rings
that line's call plan (`lines.<name>.inbound_route`); outbound is permitted by the
line's own `callers` list.

| host | role |
| --- | --- |
| `sip.example.net` (ExampleISP fiber) | outbound PSTN, 110/112, and inbound on its numbers |
| `voip.example.org` | inbound, and its own short internal extensions. **Disabled** for now (`disabled: true`) |

Everything — both trunks and both in-cluster peers — is `res_pjsip`, in `pjsip.conf`.

`sip.example.net` carries three numbers, one carrier account each, on two lines — and so three
`res_pjsip` object sets, because a registration is the finest thing pjsip can attribute an
inbound call to. Which of them the call arrived on says which number was dialled; that
attributes it to a line, and the line's `inbound_route` says who it rings.

eventpbx cannot place outbound PSTN calls: the INVITE is correct and digest authenticated,
and it answers `100 Trying` then `200 OK` and plays a recorded refusal. That is their policy,
not a config fault, and it presents as a connected call rather than an error.

PSTN refuses outbound SMS: a `MESSAGE` to the PSTN trunk is answered `501 Sip Gw only`. Also
their policy, not a config fault. Unlike the eventpbx refusal it is a visible error, but it
arrives after the stack has already answered the sender, so nothing propagates back.

## Registrations

Every registration is an entry in the routing chart's `registrations` table
(`routing/values.yaml`), rendered into `registrations.tsv`. The `render-config` initContainer
loops that file and writes each entry's password into an `#include` — nothing else. The
object set that uses it is generated from the same table into `pjsip.conf`.

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

Every entry's object set is named after the **entry**, never after its host: `from_user` is
pinned per endpoint, so a name shared by three DIDs could present only one of them.
`routing.lua`'s `peer` field carries that name; it is what the dialplan dials and what an
inbound call is attributed by.

The whole set — auth, aor, registration, identify, endpoint — is generated from the fields
below, and only the password rendered, as pjsip's `password=`, into the file
`peer_secret_file` names. Every entry must carry one: the chart never sees a password, so
that `#include` is the only place one can land.

`contact_user` is the Contact user part the carrier echoes back, and one of the two things
inbound keys on, so the pair (`host`, `contact_user`) has to be unique — two entries sharing
one arrive indistinguishable, and `tests/check-routing.py` rejects that. The pair, not the
contact alone, because two trunks may well use the same one.

**It cannot contain an `@`.** pjsip parses the Contact URI it builds, so a JID in that slot
makes the entire `type=registration` object fail to load — one sorcery error at startup and
then silence. Measured on the built image, asserted twice in
`tests/check-routing.py`, and the reason inbound attribution is the **endpoint** rather than
the extension: see below.

`disabled: true` switches an entry off and keeps it, Secret key included, so turning it back on
is deleting that line. It renders no pjsip object and no `registrations.tsv` row, so its key is
never read and may be absent. A line whose `primary` is disabled is off: every outbound rule or
`#<line>` label reaching it is **refused** with `line '<name>' is disabled` in the log, never
passed on to the next rule, and nothing inbound is attributed to it. The flag is on the
registration only, because that is what holds the objects and the credentials; every other
member of such a line must be disabled too, and an emergency route or an `entrypoint` never may.

`entrypoint: true` marks the host's **default** registration. An inbound call that nothing
else identifies is attributed to it, which is what the carrier ceasing to echo anything would
look like: per-DID inbound degrades to one line rather than to nothing. It is therefore also
the only endpoint on that host allowed a `type=identify`, since a call matched by source
address can land nowhere else. At most one per host. Its `user` and `host` are also
`cheogram-sip.env`'s `SIP_USER` and `SIP_HOST` — the trunk the component appends to a bare
number — and `tests/check-routing.py` holds the two files against each other.

## Shape

```
both trunks -- SIP/UDP 5060 outbound REGISTER only, no inbound forward
    |           RTP UDP 10000-10049 both ways
    |
[pod: asterisk + cheogram-sip] --XEP-0114 x2, plaintext, pod network--> snikket pod, :5347
    |
    cheogram-sip-lb, MetalLB 10.20.176.150, externalTrafficPolicy: Local, 50 UDP ports
```

Two containers in one pod, from two different images: Asterisk from our fork, and the
Haskell `cheogram-sip` binary from upstream. They reach each other only over XMPP, through
prosody, so the split costs nothing and separates two different failure modes — the binary
exits whenever its component connection drops, while Asterisk holds live calls and reconnects
on its own. Neither runs the upstream image's `/usr/bin/entrypoint.sh`: it renders config and
then starts *both* processes, so running it in either container would put a second
`sip.xmpp.example.com` on the wire. `render-config` does that rendering instead, which is also
what keeps every credential out of both runtime containers' environments.

Two components, both defined in `apps/snikket/sip.cfg.lua`, neither in DNS and neither in
any cert: `sip.xmpp.example.com` (user facing, dial `+49XXXXXXXXX@sip.xmpp.example.com`) and
`asterisk.xmpp.example.com` (Asterisk's own `res_xmpp` connection).

Not a container in the snikket pod, though `CONNECT_IP`/`CONNECT_PORT` would allow either.
Snikket is `Recreate` on an RWO PVC, so every Asterisk iteration would be a cold restart of
the household chat server. This way it costs exactly one, ever.

## The Asterisk image

`ghcr.io/lhns/asterisk`, built by <https://github.com/lhns/asterisk> from branch
`22.11.0-cheogram-dtls`: Asterisk 22.11.0 plus commits giving `chan_motif` DTLS-SRTP
(XEP-0320), RTCP-MUX and the ICE-UDP interop they need, and `res_xmpp` the matching disco
feature. Without them nothing here completes an encrypted Jingle call. DTLS is opt-in per
endpoint, hence `dtlsenable=yes` in `motif.conf`. Another commit fixes upstream `chan_motif`
handing stack garbage to ao2 whenever it negotiates a Jingle description. The same changes,
based on upstream `master` for submission, are the fork's `chan_motif-dtls` and
`chan_motif-codecs-init` branches. That fork's CI
asserts the patches did what they claim, not merely that they applied — a startup check plus a
prosody/slixmpp call that the outgoing `session-initiate` really carries a well-formed
`<fingerprint hash='sha-256' setup='actpass'>` and `<rtcp-mux/>`, and an inbound call that must
complete without Asterisk crashing or logging `FRACK`, also on a pattern-initialized-stack build.

Digest-pinned, as everything here is; rebasing onto a newer Asterisk is work in the fork, then
a pin bump. The GHCR package is public, so nothing needs an `imagePullSecret` — making it
private would.

The image carries no `/usr/bin/entrypoint.sh` and no `/etc/asterisk`, which shapes two things:

- **`/etc/asterisk` is ours entirely**, not an overlay: the `cheogram-sip-config` ConfigMap is
  the whole of it, copied into a Memory-backed emptyDir (the trunk passwords are rendered in
  there) by `render-config`. A file Asterisk wants and does not find is a module quietly
  running on compiled-in defaults, so every one is explicit — `asterisk.conf`, `modules.conf`,
  `pjproject.conf` and a whole `confbridge.conf`.
- **Asterisk EXITS without `modules.conf`.** `loader.c` calls a missing one a failed module
  initialization and stops, with nothing else in the log. `modules.conf` also `noload`s every
  unused module that otherwise logs an ERROR per boot for a config file deliberately absent —
  ARI, CDR/CEL, `res_parking` and the rest. Each had already declined to load, so the list only
  reduces noise; prove that when extending it, by diffing the `Running` set from `module show`.
- **`pbx_config` is noloaded**, because there is no `extensions.conf` and none is wanted.
  So there is no `default` context: `public`'s `i` and `h` handlers end the call themselves
  rather than handing off to one.

Other standing traps here:

- **Every endpoint needs `context=public`.** `extensions.lua` defines no `default`, so an
  endpoint that omits it sends every inbound call into a context that does not exist.
- **Asterisk runs as `asterisk -f`.** Without it, it forks and the container's liveness stops
  tracking it; its console is detached either way, so `logger.conf`'s file, streamed by a
  `tail -F`, is the only place its log exists.
- **The `cheogram-sip` binary exits when it loses the component connection.** Every prosody
  restart costs it a crash-restart cycle, because `wait-prosody` only runs on pod start, not
  container restart. It does not take Asterisk with it.
- **Re-diff `extensions.lua`** against the upstream image after a digest bump: it began as that
  image's copy, and the component half still comes from there.
- **There is no `[general]`.** `context`, `message_context`, `force_rport`, `rtp_symmetric`,
  `direct_media`, `identify_by` and the codec list are per endpoint, and an endpoint missing one
  is silently wrong about it; `external_*_address`/`local_net` are per transport.
  `tests/check-routing.py` asserts each of them on the generated file.
- **`identify_by` must be stated.** Its default has moved between Asterisk versions, so an
  endpoint that leaves it out can quietly start matching inbound calls by source address.
- **`CHANNEL(peername)` and `SIP_HEADER()` read nothing here.** The dialplan uses
  `CHANNEL(endpoint)`, `CALLERID` and `PJSIP_HEADER(read,...)`; `SIP_HEADER` returning empty
  would drop every Matrix outbound call, which reads `X-Conference` off the bridge's INVITE.

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

`extensions.lua` is our copy of the image's, and nothing rewrites it at runtime, so the gate
above is the whole of what stands between the trunk and everyone.

The **negative** case has never been exercised on a real call: `dialplan_test.lua` asserts
refusal per path and the suite fails if the gate is disabled, but no live call has been
refused, and a silently open trunk looks exactly like a working one. A refusal logs
`Cannot dial '<to>': caller is not in the callers list of line '<line>'`.

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

## SIP_HOST is the PSTN trunk, not just "the one with the rendered peer"

The `cheogram-sip` binary is handed `SIP_HOST` as its PSTN gateway, and it appends that host
to any number dialled without one. So the dialplan never sees a bare number, and the routing table could never
apply, unless `SIP_HOST` already is the trunk you want. Hence PSTN is `SIP_HOST`.

The dialplan strips a host equal to `SIP_HOST` before routing, treating it as "no host was
given", so short extensions still reach their own trunk.

Outbound selection:

| dialled | goes |
| --- | --- |
| no label | the first matching rule in the routing table, below |
| label naming one of our trunk hosts | that host's default registration, with credentials |
| label naming a **line** | that line's primary registration, with credentials |
| anything else | **refused** |

Our trunk hosts are `SIP_HOST` and the `host` of every enabled registration, taken from the
routing table: no object is named after a host, so a label resolves through that host's default
registration. The last row is the fail-closed part — nothing falls through to an
uncredentialed `SIP/<user>@<label>` dial, which a provider may answer with a recorded refusal, i.e. a connected call rather than an error. Federated SIP
dialling would come back, if at all, as a deliberate allow list.

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
                       |          +-- registrations.tsv  --> render-config loops it and
                       |                                     printfs each entry's password
                       |
                       +-----> ConfigMap cheogram-sip-pjsip
                                  +-- pjsip.conf         --> copied into /etc/asterisk by
                                                             render-config, parsed once
```

Three renderings of the same values, because their readers are not the same: the dialplan
wants a Lua table, `render-config` is `/bin/sh`, which has no business parsing one, and
Asterisk wants its own config syntax. The TSV is the shell half — `name`, `user`,
`host`, `contact_user`, `peer_secret_file`, TAB separated, one `while read` away. `peer_secret_file` is last because TAB is IFS whitespace, so
`read` collapses a run of them and only the last field may be empty.

`routing-release.yaml` is the HelmRelease plus an ArtifactGenerator that packs `routing/` into
its own ExternalArtifact, the same arrangement as the generator charts (ADR 0047). Its revision
is a hash of `routing/`, so editing the values re-renders without a chart version bump, and a
commit elsewhere does not.
Three ConfigMaps because their readers reload differently. `pbx_lua` gives each channel its own
`lua_State` and re-executes `extensions.lua` per call, so `load_routing`'s `loadfile` re-reads
the mount every call: a routing edit is live once kubelet refreshes the volume, with no
restart, and a malformed table fails closed (no trunk) instead of at pod start.
`cheogram-sip-routing` therefore carries `reloader.stakater.com/ignore`. The other two are
read once at pod start — `registrations.tsv` by `render-config`, `pjsip.conf` by Asterisk —
so they must live in ConfigMaps that are **not** ignored, or an edited trunk would reach the
ConfigMap and never the running process. `tests/check-routing.py` asserts that.

Nothing per-trunk is written twice: every value in the generated `pjsip.conf` is an entry's
own field or a deployment fact (`transports`, `external_address`, `local_nets`). `entrypoint`
is what decides which endpoint carries the host's single `type=identify` and `identify_by=ip`,
so "exactly one per host" holds by construction rather than by review.

`tests/` holds this app's checks; the convention is documented in `../../README.md`. What the
schema cannot state lives in `tests/check-routing.py`, which runs against the **rendered**
output: every registration's `peer` must be the section that stack actually defines, and must
be the entry name; every `line` must exist and every `primary` must belong to the line that
presents it; every outbound rule must resolve, and a rule may pin a registration only as an
emergency route; `did` must be unique and (`host`, `contact_user`) must be unique; each host
needs exactly one default registration, so the inbound fallback is defined; a line name may
not also be a registration's host, or `#label` would be ambiguous; and a rule may present only a
registration whose endpoint pins `from_user` to it, because a carrier may reject or silently
rewrite a From the account does not own. Of each entry it also demands the whole object set,
`line=yes` with `endpoint=`, no `@` in any `contact_user`, and exactly one `type=identify` per host, on that
host's default registration: two would make which endpoint answers a source-address match
undefined, and one elsewhere would let such a call pass for a per-DID match. On the
registrations half: every entry name must resolve to a Secret key that exists, the config must
`#include` what the initContainer writes, the initContainer must read the TSV's fields in the
order the chart renders them — only the last may be empty, since TAB is IFS whitespace — and
the `entrypoint` row must agree with `cheogram-sip.env`. It also asserts that Helm still
rejects a bad table, because a schema that has quietly stopped being read looks exactly like
one that passes.

Two things there are not about the routing table at all, because this is the component's only
test harness and both are otherwise unchecked: the shape of `[livekit]` and `[matrixbridge]`
— transports on 5060, `identify_by=username`, an AOR for the bridge and none for livekit,
inbound `auth` for livekit and none for the bridge — and that both containers run the same
digest-pinned fork image. On a stock or older Asterisk the pod starts, answers, and gets
Jingle encryption wrong, with nothing to say so.

**`to_pattern` is a Lua pattern, not a regex.** Asterisk's Lua 5.1 has no regex engine, and a
regex here would not error — it would match nothing and the call would fall through to the
next rule. The schema rejects `{ } |` and backslash so that mistake fails loudly; repetition
counts are spelled out, `%d%d?%d?%d?%d?` for one to five digits. The renderer anchors each
pattern as `^(...)$` exactly once, so `11[02]` cannot also match `0112345`.

### Lines

A **line** is an identity namespace, and it is what the number in front of the separator
belongs to. Three DIDs share one host, so the host cannot say which of them rang; the line
can. A caller is rendered `<line>-<digits>` to Matrix (portal `office-4915112345678`,
conference `sip-office-+4915112345678`) and `+4915112345678#office` to XMPP — and that XMPP
form is also how a client dials back, so the two stay symmetric.

Membership is `registrations.<k>.line` and nothing else; a line has no member list that could
fall out of step. `primary` says only which member the line *presents* outbound — Ofcom's
Presentation Number, as against the Network Number the carrier transmits regardless.

`lines.<name>.members` (optional, literal `matrix:@user:server` only, each permitted by
`callers`) names Matrix users who should have the line's space and dial room in the bridge
without being rung; Asterisk ignores it. The chart derives the bridge's `line_members` — every
literal `matrix:` ring target plus `members`, per line, none for a line whose primary is
disabled — into the ConfigMap `sip-line-members`, which Reflector copies into `matrix`; the
bridge pod merges it into its config at start. `tests/check-routing.py` recomputes it.

There is **no default line**: every line prefixes, so `#<host>` is not a line name and fails
closed.

Split a portal ID back with `^(.*)-([0-9]+)$`, anchored on the trailing digit run, so a line
name may contain hyphens. `-` and not `_`, because mautrix escapes a literal underscore to
`=5f` in an MXID localpart; the schema rejects one in a line name.

The emergency rule pins a **registration** rather than a line, bypassing the presented number.
The presented number is what fixes the address the dispatcher sees, Sec. 164 TKG puts the duty
to transmit it on the carrier, and Sec. 120(2) permits presenting only a number a
Nutzungsrecht exists for — so the only meaningful choice is *which* registration, and a
free-text CID field here would be reassuring and inert.

Two traps. Per-DID inbound rests on the carrier handing something of ours back: the `;line=`
token pjsip puts in our Contact, or the Contact user part itself in the Request-URI. Both are
**observed or hoped-for behaviour, not a standard** — whether this carrier echoes `;line=` is
untested — and if both stop, inbound collapses to the host's default line, which is otherwise
indistinguishable from a correct match. Every call attributed that way logs `inbound
fallback:`; a call matched to a non-default endpoint cannot have been one, and does not. And a
line can only present its primary while that registration's own section pins the number;
`tests/check-routing.py` refuses a rule selecting a line whose primary does not.

### What of the table is live, and what is not

Live: outbound selection, first match wins, `emergency`, and the whole identity rendering —
inbound attribution, the XMPP host slot, and the Matrix conference name. A destination that
carries no label is routed only by this table; with the table missing or malformed the
dialplan finds no peer and **refuses** the call.

Partly live, and the limit:

- **`from_glob`** narrows a rule to certain callers, which is how a per-person default line is
  expressed; one rule uses it. It falls through: a caller it excludes is not refused, the next
  rule decides. Permission is the line's `callers`.
- **Matrix outbound uses the room's line**, taken from the conference name, so the call goes out
  on that line's primary registration and is gated by its `callers`; `to_pattern` is not
  consulted. Emergency numbers are excepted and keep their pinned registration. A conference
  without a line routes by `to_pattern`; a line that is not configured is refused.

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
| `10.21.27.11` | `hostAliases` in `resources.yaml` | ExampleISP's proxy, from their portal. Only needed because `sip.example.net` does not resolve. Also what `type=identify` and every AOR contact resolve to, so it is read at config load as well as per REGISTER. If it changes, or if they ever publish real DNS, the entry silently wins and registration stops with no error. |
| `203.0.113.10` | `routing/values.yaml` `external_address`, `rtp.conf` `ice_host_candidates`, `coturn/turnserver.conf`, and `matrix/livekit-config.yaml` `node_ip` | The static tunnel address, duplicated across three apps with no single source of truth. Wrong value means one-way audio in SIP, dead TURN relays and silent Element Call phone calls from off the LAN at once. |
| `10.20.0.0/16`, `172.18.0.0/16`, `172.19.0.0/16` | `routing/values.yaml` `local_nets` | LAN, podCIDR, serviceCIDR. Only change on a cluster rebuild. |
| `matrix-sip-bridge.matrix.svc.cluster.local` | the `[matrixbridge]` AOR contact and `extensions.lua`'s `bridge_host` | A MESSAGE's Request-URI is routed as written, so the dialplan needs the name too; `tests/check-routing.py` holds the two together. |
| `10.20.176.150` | `metallb/pool.yaml` and the Service | Ours. |

eventpbx's SBC is deliberately **not** pinned; it is resolved from `voip.example.org`.

Diagnostic when a trunk goes quiet, before touching anything (`403 not registered` is the
healthy pre-registration answer, and proves reachability plus that the source is accepted):

```bash
python3 -c '
import socket
m=("OPTIONS sip:sip.example.net SIP/2.0\r\nVia: SIP/2.0/UDP 10.20.150.84:5064;branch=z9hG4bKp;rport\r\n"
   "Max-Forwards: 70\r\nFrom: <sip:p@sip.example.net>;tag=p\r\nTo: <sip:sip.example.net>\r\n"
   "Call-ID: probe\r\nCSeq: 1 OPTIONS\r\nContact: <sip:p@10.20.150.84:5064>\r\nContent-Length: 0\r\n\r\n")
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(8); s.bind(("",5064))
s.sendto(m.encode(),("10.21.27.11",5060)); print(s.recvfrom(4096)[0].decode().split("\r\n")[0])'
```

**Do not diagnose this path with ping or traceroute.** ICMP is filtered on it, so both fail
while UDP 5060 works perfectly.

## Router

Forward to `10.20.176.150`:

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

The RouterOS SIP helper (`/ip firewall service-port sip`) is disabled: a SIP ALG rewriting
Contact and SDP conflicts with Asterisk setting its own `external_*_address`.

Separately, and unrelated to this app, **Snikket itself needs TCP 5222 and TCP 5269 forwarded
to `10.20.176.210`**. 5269 is what carries push notifications, and push is what wakes a
backgrounded phone for an incoming call, so without it a call does not ring even while the
gateway works.

## Addressing quirk

The advertised address and the egress address differ. `external_*_address`/`ice_host_candidates` say
`203.0.113.10`, a static address arriving through a tunnel, while outbound traffic egresses
from the DS-Lite CGNAT address. So a far end that latches onto our source (comedia, or ICE
peer-reflexive) and one that obeys our SDP send media to two different places. Both work in
practice, but it is a standing source of one-way audio if something changes.

Asterisk deliberately does **not** use the shared coturn: its `turnusername`/`turnpassword`
are static, while coturn runs `use-auth-secret` with time-bounded HMACs Asterisk cannot
derive. The client side already gets coturn from Snikket, and Asterisk is the end with a
static address and forwarded ports, so it is the side that least needs a relay.

## Verifying

```bash
# the trunks. -c asterisk: the binary is the OTHER container in this pod and has no CLI
kubectl -n cheogram-sip exec deploy/cheogram-sip -c asterisk -- asterisk -rx 'pjsip show registrations'
kubectl -n cheogram-sip exec deploy/cheogram-sip -c asterisk -- asterisk -rx 'pjsip show endpoints'
# a sorcery error is an object that did NOT load, logged once at startup and never again
kubectl -n cheogram-sip logs deploy/cheogram-sip -c asterisk | grep -i sorcery
# Asterisk's own component. The user-facing one is the cheogram-sip container's log
kubectl -n cheogram-sip exec deploy/cheogram-sip -c asterisk -- asterisk -rx 'xmpp show connections'
# what actually happened to a call, including the dialled JID and the disposition
kubectl -n cheogram-sip exec deploy/cheogram-sip -c asterisk -- sh -c 'tail -3 /var/log/asterisk/cdr-csv/Master.csv'
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

## The Matrix bridge endpoint

`matrix-sip-bridge` (`apps/matrix`) is an ordinary SIP peer, `[matrixbridge]` in `pjsip.conf`,
not an AMI client. It is **static**: it never REGISTERs, so its AOR contact names its Service in
the `matrix` namespace, with `;transport=tcp` in the URI because SIP MESSAGE over UDP fragments
and is silently dropped — and because pjsip takes the transport from the contact, not from the
endpoint's `transport=` alone.

The endpoint name must equal `sip.username` in the bridge's own config. The bridge digest-
authenticates `REGISTER` and nothing else, so its INVITEs and MESSAGEs arrive unauthenticated
from a pod address that is not the AOR contact; `identify_by=username` on the From user part is
the whole match, and the endpoint therefore carries no `auth` at all. What keeps anything else
in the cluster from claiming the name is `vnet.yaml`.

`message_context=messages-in` is on every endpoint, the trunks included, so *every* out-of-call
MESSAGE — carrier SMS as well as the bridge's — lands there. That is why the context is a
dispatcher rather than the bridge's own inbound path; see "SMS" below. An endpoint that left it
unset would send its MESSAGEs into `public` instead, whose Message branch is a deliberately dead
second copy.

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

A stage's Matrix targets are **one leg each**: every distinct literal `matrix:@user:server` becomes
`Local/<hex of the lowercase MXID>@matrix-leg/n`, so each person gets the call in their own bridge
room (hex, because an extension cannot hold `@` or `:`). A bare `matrix:` is one leg,
`Local/default@matrix-leg/n`, with no recipient: the bridge's default one. The `matrix-leg` context
decodes the extension and dials `PJSIP/<caller>@matrixbridge` with a `b()` pre-dial gosub,
`add-leg-headers`, which adds `X-Conference: sip-<line>-+<caller in E.164>` and
`X-Matrix-Recipient: <mxid>` to that channel. A pre-dial handler is the only place `PJSIP_HEADER(add)`
writes the outbound channel, which is why the PJSIP channel is dialled from inside the Local leg
rather than directly. The conference name, the caller and the recipient reach it as inherited
(`__`) channel variables. `check-routing.py` rejects the same MXID twice in one stage.

The conference header is what the bridge routes on: the portal room and the ghost are named from it,
and an INVITE naming a conference it does not recognise is declined, so a mismatch with
`calls.conference_prefix` fails closed and silently. The recipient header picks that person's own
login and room, and the bridge shows "Answered by <name>" in the others' rooms when one leg wins.
A caller on a line the dialplan could not attribute gets no Matrix leg.

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

The `matrix-answered` gosub runs on whichever leg answered, XMPP included, and recognises a Matrix
one by its channel name, `Local/…@matrix-leg-…`. **app_dial hangs the answered leg up if the gosub
fails** — so its body is wrapped in `pcall`. A Lua error there
would otherwise drop ordinary calls to the household number.

The two user profiles this uses are in `confbridge.conf`: livekit-sip is the marked user, the
caller is `end_marked`, and that pairing is what a Matrix hangup ends the caller with. The other
direction is the `h` extension calling `ConfKick()`: the profiles cannot express it, and neither
can a channel, because the leg that has to end the conference is by then hung up — or, on the
outbound path the bridge cancelled, was never in it.

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
signal there is for a second of earlier audio. A failure hangs the leg up with a cause the stack
turns into a response: a destination that is not a number or a bad conference header 404, no trunk
503, busy 486, no answer 480. The bridge fails the call on all of them alike.

Trunk selection is `resolve_destination`, shared with the XMPP path so the two cannot drift; the
room's line from the conference name is passed to it as a label (emergency numbers excepted). Caller
ID is not the dialplan's: every endpoint pins `from_user`, so the call presents that endpoint's own number
whatever `CALLERID` says. Which of our numbers a caller presents is selected by picking the peer,
which is the line's primary registration's — so a `from_glob` rule is what makes it per-person,
and only on the XMPP path, where the caller is known.

### SMS

`messages-in` is a **dispatcher**: it is where both trunks and the bridge land, so it classifies
each MESSAGE before doing anything with it. A message is outbound only when the `From` user part
is the bridge peer **and** the extension is a bare number. Both conditions are required — on the
`From` alone, a carrier-injected `From: sip:matrixbridge@…` would be an outbound-SMS primitive on
our trunk, billed to us. The trunk's own messages fail both: their `From` is a subscriber number
and their extension is the DID's own contact.

Inbound goes to the people the line's call plan rings, on both transports: one XMPP message per
distinct `xmpp:` ring target (none when the plan has none) and one bridge message per Matrix
recipient. A line with no call plan keeps the dialled contact on XMPP. The order is load-bearing. XMPP is sent first because the bridge's handler can block for 32s and a hung bridge
must not delay the phone; `MESSAGE_SEND_STATUS` is one channel variable overwritten by each send,
so it is captured between them.

The bridge leg is one MESSAGE per Matrix recipient of the line — the literal `matrix:` targets
of its `inbound_route`, the same people a call rings — each with `X-Matrix-Recipient` set through
`MESSAGE_DATA`, so the text lands in that person's room. A line with a bare `matrix:` or no Matrix
target sends one with no header (the bridge's default recipient). The bridge leg is sent only for a sender that normalises to `+E.164` — the same gate the call path
uses for the conference name, and both derive it from `matrix_conference_name`, which is what keeps
an SMS and a call from one number in **one** portal room. The sender's host is rewritten to the
line exactly as on the call path, so the JID resource is `+49…#home` rather than the carrier's SBC
address. The line comes from the dialled contact; a Message channel has no endpoint, so the pjsip
attribution the call path gets is not available here, and a carrier that does not echo our
Contact on a `MESSAGE` falls back to the host's default registration and logs
`how=[entrypoint]`. The `+` matters on the bridge leg: the bridge rejects a number without one
and then drops the message *while answering 200 OK*.

Outbound reuses `resolve_destination`, and sends with no `from`, so the endpoint's own
`from_user` applies — the same way the call path gets its caller ID. There is no allow-list and no rate
limit: the trust boundary is the `[matrixbridge]` peer plus the numeric-destination check, as for
outbound calls. Any Matrix user in a portal room can text any routable number a trunk accepts.

Today that is none of them: PSTN answers outbound `MESSAGE` with `501 Sip Gw only`, so the
outbound path is built, correct and dead. It stays because it costs nothing and works the day
a trunk that accepts `MESSAGE` is added. Inbound SMS is unaffected and does work.

The silent-failure surface here is large — Asterisk answers `202` before the dialplan runs, the
bridge answers `200` for a message it drops, and `MESSAGE_SEND_STATUS=SUCCESS` means "handed over",
not delivered. The `sms inbound`/`sms outbound`/`xmpp outbound` NOTICE lines are the whole
observability story.

The bridge caps a MESSAGE at 20480 serialised bytes with no segmentation and no retry, so a long
Matrix message fails outright.

## 110/112, and voicemail

110 and 112 are configured to behave sensibly, but they are not the emergency plan. This path is
VoIP behind DS-Lite CGNAT, with a documented history of one-way audio and of trunks that stop
registering when a route changes. A mobile stays the actual emergency plan.

Voicemail is a `record` stage that records nothing: `app_voicemail` declines to load with no
`voicemail.conf`, and the plan is to record a file and have the bridge post it as a Matrix voice
message. The design, and why it is Matrix only, is in `kube-cluster/TODO.md`.
