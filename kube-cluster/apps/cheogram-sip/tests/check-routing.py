#!/usr/bin/env python3
"""Check the SIP routing chart: render it, then assert what a schema cannot.

Everything checked here runs against the RENDERED Lua, never against the values, so a
template that drops or mangles an entry is caught as well as a bad table.

--write-lua also hands that rendered table to the dialplan suite, which is what keeps the
suite from drifting from the chart.
"""
import argparse
import contextlib
import io
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

APP = pathlib.Path(__file__).resolve().parent.parent
CHART = APP / "routing"
RESOURCES = APP / "resources.yaml"
KUSTOMIZATION = APP / "kustomization.yaml"
DIALPLAN = APP / "extensions.lua"
ENV = APP / "cheogram-sip.env"

# The ConfigMap key the chart generates pjsip.conf into. Asterisk reads the file once at
# start, so where it lands is checked as well as what is in it.
PJSIP_KEY = "pjsip.conf"

# The two hand-written in-cluster endpoints. Neither registers, so neither is in the routing
# table and the per-registration checks below skip them -- these are their own invariants.
# `aor` is None for one that must NOT have an AOR contact.
IN_CLUSTER = {
    "livekit": {
        "context": "livekit",
        # Only ever the caller, and nothing dials it, so an AOR would be an address for a
        # call that is never placed -- and one more thing to keep right.
        "aor": None,
        # We challenge its INVITE. auth=, not outbound_auth=: we never authenticate to it.
        "auth": True,
    },
    "matrixbridge": {
        "context": "matrixbridge",
        # STATIC: it never REGISTERs, so its address has to be the Service, over TCP -- a
        # multi-part MESSAGE over UDP overflows the datagram and is dropped silently.
        "aor": "sip:matrix-sip-bridge.matrix.svc.cluster.local:5060;transport=tcp",
        # Nothing to challenge: the bridge digest-authenticates only REGISTER, which it
        # never sends. identify_by=username on the From user part is the whole match.
        "auth": False,
    },
}


def fail(msg):
    print("check-routing: " + msg, file=sys.stderr)
    sys.exit(1)


def helm_template(values=None):
    """Render the chart. Helm validates values.schema.json here, so a bad table never
    reaches the parsing below -- it comes back as a non-zero exit and its message."""
    cmd = ["helm", "template", "routing", str(CHART), "--namespace", "cheogram-sip"]
    if values:
        cmd += ["-f", values]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def helm_template_text(values):
    """helm_template with values given as YAML text rather than a file."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write(values)
        path = fh.name
    try:
        return helm_template(path)
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def env_value(key):
    m = re.search(r"(?m)^%s=(.*)$" % re.escape(key), ENV.read_text(encoding="utf-8"))
    return m.group(1).strip() if m else None


def pjsip_objects(text):
    """The RENDERED pjsip.conf as {(name, type): body}. Section names REPEAT there -- one per
    object type for the same trunk is the canonical pjsip layout -- so the type is half the
    key."""
    out = []
    cur, body = None, []
    for line in text.splitlines():
        m = re.match(r"^\[([^\]]+)\]$", line.strip())
        if m:
            if cur:
                out.append((cur, "\n".join(body)))
            cur, body = m.group(1), []
            continue
        if cur is not None:
            body.append(line)
    if cur:
        out.append((cur, "\n".join(body)))
    typed = {}
    for name, b in out:
        m = re.search(r"(?m)^type=(\w+)$", b)
        if not m:
            fail("pjsip.conf section %r declares no type=; sorcery would skip it silently"
                 % name)
        if (name, m.group(1)) in typed:
            fail("pjsip.conf declares %r as type=%s twice" % (name, m.group(1)))
        typed[(name, m.group(1))] = b
    return typed


# Per-endpoint settings with no global default under pjsip: there is no [general], so an
# endpoint that omits one fails quietly rather than loudly. from_user/from_domain are checked
# against the registration instead, further down.
PJSIP_ENDPOINT_SETTINGS = {
    "context": "public",
    "message_context": "messages-in",
    "force_rport": "yes",
    "rtp_symmetric": "yes",
    "direct_media": "no",
    "disallow": "all",
    # pjsip rejects the old rfc2833 spelling, leaving the endpoint's DTMF at the default.
    "dtmf_mode": "rfc4733",
}

# The same settings on an in-cluster endpoint, minus the two it sets differently: `context`
# is its own, and it is not in the messages-in dispatcher unless it sends MESSAGEs.
IN_CLUSTER_SETTINGS = {k: v for k, v in PJSIP_ENDPOINT_SETTINGS.items()
                       if k not in ("context", "message_context")}


def check_asterisk_image():
    """Both containers run the same digest-pinned image from our Asterisk fork.

    Asterisk 22 is what makes every pjsip object here loadable at all, and the fork is what
    can do DTLS-SRTP over Jingle. On a stock or older image the pod starts, answers, and
    gets Jingle encryption wrong; nothing else would say so. render-config must match, so
    what renders the config is what parses it.
    """
    res = RESOURCES.read_text(encoding="utf-8")
    found = {}
    for container in ("render-config", "asterisk"):
        m = re.search(r"(?m)^(\s*)- name: %s$\n(?:\1  .*\n|\1  \n)*?\1  image: (\S+)$"
                      % re.escape(container), res)
        if not m:
            fail("resources.yaml has no container %r with an image:" % container)
        found[container] = m.group(2)
    for container, image in found.items():
        if not re.fullmatch(r"ghcr\.io/lhns/asterisk:\S+@sha256:[0-9a-f]{64}", image):
            fail("container %r runs %r; it has to be our asterisk fork, pinned by digest "
                 "(ghcr.io/lhns/asterisk:<tag>@sha256:...)" % (container, image))
    if found["render-config"] != found["asterisk"]:
        fail("render-config runs %r and asterisk runs %r; the config has to be rendered by "
             "the binary that parses it" % (found["render-config"], found["asterisk"]))
    if "singpolyma/cheogram-sip" not in res:
        fail("resources.yaml no longer references the upstream image at all; the "
             "cheogram-sip container is the XEP-0114 component and stays on it")


def check_transport(objects, peer, ep):
    """The transport an endpoint names, and the port it binds.

    5060, on both protocols: that is what everything pointing at this Asterisk expects --
    apps/matrix's sip-bridge-config and livekit-sip, and the cheogram-sip-internal Service
    in resources.yaml. A transport on another port is reachable from nothing.
    """
    m = re.search(r"(?m)^transport=(\S+)$", ep)
    if not m:
        fail("pjsip endpoint %r names no transport" % peer)
    tr = objects.get((m.group(1), "transport"))
    if tr is None:
        fail("pjsip endpoint %r names transport %r, which pjsip.conf does not define"
             % (peer, m.group(1)))
    bind = re.search(r"(?m)^bind=\S+:(\d+)$", tr)
    if not bind:
        fail("pjsip transport %r has no bind=<address>:<port>" % m.group(1))
    if bind.group(1) != "5060":
        fail("pjsip transport %r binds %s, but everything that reaches this Asterisk is "
             "pointed at 5060 -- apps/matrix and the cheogram-sip-internal Service"
             % (m.group(1), bind.group(1)))


def check_in_cluster(objects):
    """livekit and matrixbridge: hand-written, in no routing table, and each wrong in its own
    way if a line goes missing."""
    for peer, want in sorted(IN_CLUSTER.items()):
        ep = objects.get((peer, "endpoint"))
        if ep is None:
            fail("pjsip.conf has no type=endpoint %r; the in-cluster peer it stands for "
                 "would be an unidentified caller and rejected" % peer)
        check_transport(objects, peer, ep)
        for opt, val in sorted(IN_CLUSTER_SETTINGS.items()):
            if not re.search(r"(?m)^%s=%s$" % (opt, re.escape(val)), ep):
                fail("pjsip endpoint %r does not set %s=%s; pjsip has no global default for "
                     "it and the endpoint is wrong about it silently" % (peer, opt, val))
        if not re.search(r"(?m)^context=%s$" % re.escape(want["context"]), ep):
            fail("pjsip endpoint %r is not context=%s; its calls would land in a context "
                 "that is not its own" % (peer, want["context"]))
        # THE WHOLE MATCH. Neither peer registers and neither is at the AOR address it sends
        # from, so the From user part against the endpoint name is all there is. Anything
        # else -- ip, or a version-dependent default -- makes every call from it anonymous.
        if not re.search(r"(?m)^identify_by=username$", ep):
            fail("pjsip endpoint %r is not identify_by=username; it sends from a pod "
                 "address that matches nothing, so its calls would be anonymous" % peer)
        aor = objects.get((peer, "aor"))
        if want["aor"] is None:
            if aor is not None:
                fail("pjsip.conf gives %r a type=aor; nothing dials it, so that is an "
                     "address for a call never placed and one more thing to keep right"
                     % peer)
            if re.search(r"(?m)^aors=", ep):
                fail("pjsip endpoint %r names an aor it does not have" % peer)
        else:
            if aor is None or not re.search(
                    r"(?m)^contact=%s$" % re.escape(want["aor"]), aor):
                fail("pjsip aor %r does not have contact=%s; a typo here leaves it "
                     "UNREACHABLE with the far end perfectly healthy" % (peer, want["aor"]))
            if not re.search(r"(?m)^aors=%s$" % re.escape(peer), ep):
                fail("pjsip endpoint %r does not name its aor, so dialling it reaches "
                     "nothing" % peer)
        auth = objects.get((peer, "auth"))
        if want["auth"]:
            if auth is None or not re.search(r"(?m)^auth=%s$" % re.escape(peer), ep):
                fail("pjsip endpoint %r has no INBOUND auth=; its INVITE would be accepted "
                     "on the From user part alone" % peer)
            # outbound_auth is what a trunk uses to authenticate TO a carrier. Here it would
            # be silently inert, and reads as if the INVITE were challenged when it is not.
            if re.search(r"(?m)^outbound_auth=", ep):
                fail("pjsip endpoint %r sets outbound_auth; nothing here authenticates "
                     "outbound, and it does not challenge an inbound INVITE" % peer)
        elif auth is not None or re.search(r"(?m)^auth=", ep):
            fail("pjsip endpoint %r has an auth; it never sends credentials, so the "
                 "challenge would refuse every call it makes" % peer)

    # The dialplan addresses a MESSAGE to the bridge by Request-URI, which is routed as
    # written, so its host is spelled in extensions.lua as well as in the AOR here.
    host = IN_CLUSTER["matrixbridge"]["aor"].split(":")[1].split(";")[0]
    if ('or "%s"' % host) not in DIALPLAN.read_text(encoding="utf-8"):
        fail("extensions.lua's bridge_host default is not %s, the [matrixbridge] aor "
             "contact host; an outbound SMS would be addressed to a name that does not "
             "resolve" % host)


def check_pjsip(registrations, rows, pjsip_conf):
    """The res_pjsip object set behind every registration, and the two in-cluster endpoints.

    pjsip has no [general], so every line below is one an endpoint that omitted it would be
    silently wrong about. It runs on the GENERATED file, so a template that drops a line is
    caught as well as a bad table.
    """
    objects = pjsip_objects(pjsip_conf)
    peer_file = {r["name"]: r["peer"] for r in rows}

    # A disabled entry is no object at all: one left behind would REGISTER, or answer, for a
    # line the dialplan treats as off.
    for regname, reg in sorted(registrations.items()):
        if reg["disabled"]:
            left = sorted(ty for (n, ty) in objects if n == reg["peer"])
            if left:
                fail("registration %r is disabled but pjsip.conf still defines %r as type=%s"
                     % (regname, reg["peer"], ", type=".join(left)))
    registrations = {n: r for n, r in registrations.items() if not r["disabled"]}

    for regname, reg in sorted(registrations.items()):
        peer = reg["peer"]
        # A pjsip trunk is FOUR objects under one name and a missing one fails differently
        # every time: no auth is a trunk with no password, no aor is an outbound dial with
        # no address, no registration is a trunk that never registers, no endpoint is an
        # inbound call matched to nothing. Named together so a copied block that lost one is
        # one failure rather than four unrelated ones.
        missing = [ty for ty in ("auth", "aor", "registration", "endpoint")
                   if (peer, ty) not in objects]
        if missing:
            fail("registration %r: %r has no type=%s in "
                 "pjsip.conf; the object set is incomplete"
                 % (regname, peer, ", type=".join(missing)))
        ep = objects.get((peer, "endpoint"))
        if ep is None:
            fail("registration %r: %r is no type=endpoint in "
                 "pjsip.conf" % (regname, peer))
        for opt, want in sorted(PJSIP_ENDPOINT_SETTINGS.items()):
            if not re.search(r"(?m)^%s=%s$" % (opt, re.escape(want)), ep):
                fail("pjsip endpoint %r does not set %s=%s; pjsip has no global default for "
                     "it and the endpoint is wrong about it silently" % (peer, opt, want))
        # The per-endpoint default has moved between Asterisk versions, so an endpoint that
        # leaves it implicit can start matching inbound calls by IP without anything changing
        # here.
        if not re.search(r"(?m)^identify_by=\S+$", ep):
            fail("pjsip endpoint %r does not set identify_by; the default is version "
                 "dependent and decides what an inbound INVITE is matched on" % peer)
        # Whatever identifies the endpoint has to actually exist, or every inbound call is
        # anonymous. identify_by=ip means a type=identify, and nothing else does.
        if re.search(r"(?m)^identify_by=ip$", ep):
            ident = objects.get((peer, "identify"))
            if ident is None or not re.search(r"(?m)^endpoint=%s$" % re.escape(peer), ident):
                fail("pjsip endpoint %r is identify_by=ip with no type=identify naming it; "
                     "inbound calls would be anonymous" % peer)
        # A codec list, and only ones the RTP path already carries.
        if not re.search(r"(?m)^allow=[a-z0-9,]+$", ep):
            fail("pjsip endpoint %r sets no allow= codec list" % peer)
        # Caller ID. Nothing else pins the presented From: the dialplan never sets it.
        for opt, want in (("from_user", reg["user"]), ("from_domain", reg["host"])):
            if not re.search(r"(?m)^%s=%s$" % (opt, re.escape(want)), ep):
                fail("pjsip endpoint %r does not pin %s=%s, so a call on registration %r "
                     "would present another number" % (peer, opt, want, regname))
        # An endpoint with no AOR contact has no address, so an outbound dial fails.
        aor = objects.get((peer, "aor"))
        if aor is None or not re.search(r"(?m)^contact=sip:", aor):
            fail("pjsip endpoint %r has no type=aor with a contact=, so PJSIP/<exten>@%s "
                 "reaches nothing" % (peer, peer))
        if not re.search(r"(?m)^aors=%s$" % re.escape(peer), ep):
            fail("pjsip endpoint %r does not name its aor" % peer)
        check_transport(objects, peer, ep)
        # The registration, which REPLACES the rendered register line. Both would be one
        # account REGISTERing twice.
        rg = objects.get((peer, "registration"))
        if rg is None:
            fail("registration %r: pjsip.conf has no "
                 "type=registration for %r, so it never registers" % (regname, peer))
        # line=yes tags our Contact with a generated ;line= and matches a call echoing it
        # back to THIS registration. With several DIDs on one host it is the only per-DID
        # discriminator pjsip has of its own, and endpoint= is what it matches them to.
        for opt, want in (("client_uri", "sip:%s@%s" % (reg["user"], reg["host"])),
                          ("server_uri", "sip:%s" % reg["host"]),
                          ("contact_user", reg["contact_user"]),
                          ("line", "yes"),
                          ("endpoint", peer)):
            if not re.search(r"(?m)^%s=%s$" % (opt, re.escape(want)), rg):
                fail("pjsip registration %r does not set %s=%s, which registration %r in "
                     "the routing table says it is" % (peer, opt, want, regname))
        # MEASURED on the pinned image: pjsip parses the Contact URI it builds, so an "@"
        # here makes the whole registration object fail to load -- one sorcery error at
        # startup and then silence.
        if "@" in reg["contact_user"]:
            fail("registration %r has contact_user %r; an at-sign there "
                 "is not a legal SIP user part and the registration object would not load"
                 % (regname, reg["contact_user"]))
        # pjsip treats a 401/403 as permanent by default, so a credential problem would
        # register once and then go quiet.
        if not re.search(r"(?m)^auth_rejection_permanent=no$", rg):
            fail("pjsip registration %r does not set auth_rejection_permanent=no; an auth "
                 "failure would stop it retrying with nothing to say why" % peer)
        # The password, rendered by the initContainer as `password=` into the file this
        # entry's peer_secret_file names.
        auth = objects.get((peer, "auth"))
        if auth is None or not re.search(r"(?m)^username=%s$" % re.escape(reg["user"]), auth):
            fail("pjsip endpoint %r has no type=auth with username=%s"
                 % (peer, reg["user"]))
        if not re.search(r"(?m)^outbound_auth=%s$" % re.escape(peer), ep + "\n" + rg):
            fail("pjsip endpoint or registration %r does not name its outbound_auth" % peer)
        inc = peer_file.get(regname)
        if not inc:
            fail("registration %r names no peer_secret_file, so its password is written "
                 "nowhere" % regname)
        if ("#include " + inc) not in pjsip_conf:
            fail("registration %r writes %s, which pjsip.conf does not #include; the trunk "
                 "would authenticate with no password" % (regname, inc))

    # MEASURED on the pinned image: an "@" in a contact_user makes the whole registration
    # object fail to load, and the check above only sees the ones the table describes. This
    # one sees every contact_user in the file, hand-written objects included.
    for bad in re.findall(r"(?m)^contact_user=(.*@.*)$", pjsip_conf):
        fail("pjsip.conf has contact_user=%s; an '@' is not a legal SIP user part and the "
             "registration object would not load" % bad)

    # THE INBOUND FALLBACK, and the one place two objects can silently disagree. A call the
    # carrier does not tag with our ;line= is matched by source address, so every host needs
    # exactly ONE type=identify -- on its default registration, which is what the dialplan
    # reports as the fallback. Two on one address and which endpoint answers is undefined;
    # none and such a call is anonymous.
    pj = dict(registrations)
    for host in sorted({r["host"] for r in pj.values()}):
        on_host = sorted(n for n, r in pj.items() if r["host"] == host)
        default = [n for n in on_host if pj[n]["entrypoint"]] or (
            on_host if len(on_host) == 1 else [])
        if not default:
            fail("pjsip host %r has %d registrations and no entrypoint, so an inbound call "
                 "the carrier does not tag has no defined endpoint" % (host, len(on_host)))
        identified = [n for n in on_host if (pj[n]["peer"], "identify") in objects]
        if identified != default:
            fail("pjsip host %r has its type=identify on %s, but its default registration "
                 "is %s; exactly the default may carry one, or a call matched by source "
                 "address lands on an endpoint the dialplan calls a per-DID match"
                 % (host, ", ".join(identified) or "nothing", ", ".join(default)))
        # identify_by=ip without an identify is checked above; this is the other half, and
        # the one that makes a DID endpoint reachable ONLY by its own ;line=.
        for n in on_host:
            if n in default:
                continue
            if re.search(r"(?m)^identify_by=ip$", objects[(pj[n]["peer"], "endpoint")]):
                fail("pjsip endpoint %r is identify_by=ip but is not %s's default "
                     "registration; it would match inbound calls meant for another DID"
                     % (pj[n]["peer"], host))

    # An object under a name nothing claims answers calls, or registers, with nothing to
    # attribute it by -- the shape a half-finished rename leaves. The transports and the two
    # in-cluster endpoints are claimed by being named here; everything else is a trunk.
    claimed = ({r["peer"] for r in pj.values()} | set(IN_CLUSTER)
               | {n for (n, ty) in objects if ty == "transport"})
    for name in sorted({n for n, _ in objects} - claimed):
        fail("pjsip.conf defines %r, which is neither a transport, an in-cluster peer, nor a "
             "registration's peer in the routing table; an inbound call matched to it could "
             "not be attributed to a line" % name)


# The ring-address forms inbound_legs understands, asserted on the RENDERED table.
RING_ADDRESS = re.compile(r"^(xmpp:|matrix:)")

# definitions/glob in values.schema.json, asserted again on the RENDERED table.
GLOB = re.compile(r"^(\*|xmpp:\*|matrix:\*|xmpp:(\*|[^@]+)@[^@]+|matrix:@(\*|[^:]+):[^:]+)$")

# What an emergency to_pattern may contain, once anchored: digits and character classes.
# It is the one rule that bypasses the caller gate, so ".*" there is an open trunk.
EMERGENCY_PATTERN = re.compile(r"^\^\((?:[0-9]|\[[0-9]+\])+\)\$$")


def extract_block(manifest, key):
    """One of the ConfigMap's data keys, un-indented out of its block scalar."""
    try:
        body = manifest.split(key + ": |\n", 1)[1]
    except IndexError:
        fail("the rendered ConfigMap has no %s key" % key)
    out = []
    for line in body.splitlines():
        if line.strip() and not line.startswith("    "):
            break                      # dedented: end of the block scalar
        out.append(line[4:])
    return "\n".join(out)


def check_pjsip_is_generated(manifest):
    """Find the rendered pjsip.conf, and assert it lands where a trunk change can apply.

    Asterisk parses pjsip.conf ONCE, at start. In cheogram-sip-routing -- which is annotated
    reloader.stakater.com/ignore because pbx_lua re-reads routing.lua per call -- an edited
    trunk would reach the ConfigMap, never the running Asterisk, and nothing would say so.
    """
    docs = [d for d in manifest.split("\n---") if ("\n  %s: |\n" % PJSIP_KEY) in d]
    if len(docs) != 1:
        fail("the chart renders %s into %d ConfigMaps; exactly one owns it"
             % (PJSIP_KEY, len(docs)))
    doc = docs[0]
    m = re.search(r"(?m)^  name: (\S+)$", doc)
    if not m:
        fail("the ConfigMap holding %s has no metadata.name" % PJSIP_KEY)
    name = m.group(1)
    if re.search(r'(?m)^\s*reloader\.stakater\.com/ignore: "true"$', doc):
        fail("%s is generated into ConfigMap %r, which is reloader-ignored; Asterisk reads it "
             "once at start, so a trunk change would never reach a running pod"
             % (PJSIP_KEY, name))

    # The other half: a ConfigMap nothing mounts rolls nothing either.
    res = RESOURCES.read_text(encoding="utf-8")
    m = re.search(r"(?m)^\s*- name: (\S+)\n\s*configMap:\n\s*name: %s$" % re.escape(name), res)
    if not m:
        fail("resources.yaml mounts no ConfigMap %r, so the generated %s reaches no pod -- "
             "and `reloader.stakater.com/auto` only rolls for what the pod mounts"
             % (name, PJSIP_KEY))
    volume = m.group(1)
    if not re.search(r"(?m)^\s*- \{name: %s, mountPath: (\S+?), readOnly: true\}$"
                     % re.escape(volume), res):
        fail("no container mounts the %r volume; the ConfigMap would be declared and unread"
             % volume)
    if not re.search(r"(?m)^\s*cp \S*/%s /render/$" % re.escape(PJSIP_KEY), res):
        fail("render-config no longer copies %s into /render; Asterisk would read the "
             "image's own, or none" % PJSIP_KEY)

    # A stale hand-written copy would win: render-config overlays /config last.
    if (APP / PJSIP_KEY).exists():
        fail("%s is both generated by the chart and present in the component directory; the "
             "hand-written one is what the pod would get" % PJSIP_KEY)
    if re.search(r"(?m)^\s*- %s$" % re.escape(PJSIP_KEY), KUSTOMIZATION.read_text("utf-8")):
        fail("kustomization.yaml still ships %s in cheogram-sip-config" % PJSIP_KEY)
    return extract_block(doc, PJSIP_KEY)


# The initContainer's `while IFS="$tab" read -r ...` variable list. Read out of
# resources.yaml rather than restated: a field added to the TSV without a variable here is
# glued onto the last one, silently.
TSV_READ_RE = re.compile(r'(?m)^\s*while IFS="\$tab" read -r ((?:\w+ )+)\|\|')

TSV_FIELDS = ("name", "user", "host", "contact_user", "peer")
# The `read` variable each field is named by, which is not always the field name.
TSV_VARS = {"name": "name", "user": "user", "host": "host", "contact_user": "contact",
            "peer": "peer"}


def parse_registrations_tsv(tsv):
    """The register table as /bin/sh reads it: TAB separated, one field per TSV_FIELDS, the
    first of which is also the name of the Secret key holding that entry's password."""
    m = TSV_READ_RE.search(RESOURCES.read_text(encoding="utf-8"))
    if not m:
        fail("resources.yaml has no recognisable `while read` over registrations.tsv; "
             "either it stopped reading the table or this check can no longer find it")
    read_vars = m.group(1).split()
    if len(read_vars) != len(TSV_FIELDS):
        fail("registrations.tsv carries %d fields but the initContainer reads %d variables "
             "(%s); `read` puts every field past the last variable into it"
             % (len(TSV_FIELDS), len(read_vars), " ".join(read_vars)))
    # ORDER, not just count. TAB is IFS whitespace, so `read` collapses a run of them: a
    # swapped pair puts every value in the wrong variable, and the loop would then write a
    # trunk's password into a file named after its contact.
    want_vars = ["r" + TSV_VARS[f] for f in TSV_FIELDS]
    if read_vars != want_vars:
        fail("the initContainer reads registrations.tsv as (%s), but the chart renders it as "
             "(%s); the fields would be read into the wrong variables"
             % (" ".join(read_vars), " ".join(want_vars)))
    rows = []
    for n, line in enumerate(tsv.splitlines(), 1):
        if not line.strip():
            continue
        f = line.split("\t")
        # An empty LAST field may arrive as a trailing tab or as nothing at all -- the
        # renderer drops it on the final line -- and `read` leaves its variable empty either
        # way. Anything shorter than that is a field the loop would misread.
        if len(f) == len(TSV_FIELDS) - 1:
            f.append("")
        if len(f) != len(TSV_FIELDS):
            fail("registrations.tsv line %d has %d fields, not %d: %r"
                 % (n, len(f), len(TSV_FIELDS), line))
        row = dict(zip(TSV_FIELDS, f))
        # TAB is IFS whitespace, so `read` collapses a run of them: an empty field anywhere
        # but the last shifts every later one into the wrong variable.
        for label in TSV_FIELDS[:-1]:
            if not row[label] or row[label].split() != [row[label]]:
                fail("registrations.tsv line %d has an empty or whitespace %s, and only the "
                     "last field may be empty: %r" % (n, label, line))
        rows.append(row)
    if not rows:
        fail("could not read any registration out of the rendered registrations.tsv")
    return rows


def sops_keys():
    """stringData key names of every SOPS Secret in the component. Only the VALUES are
    encrypted, so the names are readable from git and can be checked against the table."""
    keys = {}
    for f in sorted(APP.glob("secret*.yaml")):
        instringdata = False
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line[:1].isspace():
                instringdata = line.rstrip() == "stringData:"
                continue
            m = re.match(r"^\s+([A-Za-z0-9_.-]+):", line)
            if instringdata and m:
                keys[m.group(1)] = f.name
    return keys


def check_registrations_tsv(rows, registrations):
    """The register lines: everything about one that can be wrong silently."""
    keys = sops_keys()

    for name in sorted(registrations):
        # The entry name IS the Secret key. It must therefore be a legal one, and it must
        # exist: a missing key already refuses to start the pod, and catching it here stops
        # the rollout one step earlier. Demanded of a disabled entry too, so that turning it
        # back on stays a one-line change.
        if not re.match(r"^[a-z0-9-]+$", name):
            fail("registration %r is not usable as a Kubernetes Secret key" % name)
        if name not in keys:
            fail("registration %r has no Secret key of that name in this component's "
                 "secret*.yaml; the initContainer would refuse to start the pod" % name)

    # A trunk's REGISTER is its type=registration object, so all the initContainer writes is
    # the password -- and it has to land under the exact filename pjsip.conf #includes, or
    # the account authenticates with nothing and fails auth silently.
    res = RESOURCES.read_text(encoding="utf-8")
    if not re.search(r"(?m)^\s*printf 'password=%s\\n'", res):
        fail("the initContainer no longer renders a password= line, so a trunk would "
             "authenticate with nothing")

    # Every registration is rendered, the entrypoint one included: a row missing here is an
    # account whose password is written nowhere, and which therefore never registers.
    rendered = {r["name"] for r in rows}
    for name, reg in sorted(registrations.items()):
        if reg["disabled"]:
            # A row makes the initContainer demand the key and write a secret nothing reads.
            if name in rendered:
                fail("registration %r is disabled but still has a registrations.tsv row"
                     % name)
        elif name not in rendered:
            fail("registration %r is in no registrations.tsv row, so its password is written "
                 "nowhere and it never registers" % name)

    host = env_value("SIP_HOST")
    if host:
        if not any(r["host"] == host for r in rows):
            fail("no registration is at %s, the trunk the extra DIDs live on" % host)


def parse_lua(lua):
    """A deliberately shallow reader for the shape templates/configmap.yaml emits.

    Not a Lua parser: it is coupled to that template's line layout, which is the point --
    a template change that moves a field shows up here as a missing one rather than as a
    route nobody checked. Anything it cannot read is a failure, never a skip.
    """
    registrations, lines, outbound = {}, {}, []
    section, cur = None, None
    for line in lua.splitlines():
        s = line.strip()
        if s in ("registrations = {", "lines = {", "outbound = {"):
            section = s.split()[0]
            cur = None
            continue
        if section == "registrations":
            m = re.match(r'^\["(.+?)"\] = \{ (.*) \},$', s)
            if m:
                fields = dict(re.findall(r'(\w+) = "((?:[^"\\]|\\.)*)"', m.group(2)))
                fields["entrypoint"] = " entrypoint = true" in (" " + m.group(2))
                fields["disabled"] = " disabled = true" in (" " + m.group(2))
                registrations[m.group(1)] = fields
                continue
        if section == "lines":
            m = re.match(r'^\["(.+?)"\] = \{$', s)
            if m:
                cur = m.group(1)
                lines[cur] = {"primary": None, "callers": None, "inbound_route": []}
                continue
            m = re.match(r"^callers = \{ (.*) \},$", s)
            if m and cur:
                lines[cur]["callers"] = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
                continue
            m = re.match(r'^primary = "(.*)",$', s)
            if m and cur:
                lines[cur]["primary"] = m.group(1)
                continue
            if s == "{ record = true }," and cur:
                lines[cur]["inbound_route"].append({"record": True})
                continue
            m = re.match(r"^\{ timeout = (\d+), ring = \{ (.*) \} \},$", s)
            if m and cur:
                ring = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(2))
                lines[cur]["inbound_route"].append(
                    {"timeout": int(m.group(1)), "ring": ring})
                continue
        if section == "outbound":
            m = re.match(r'^\{ to_pattern = "(.*?)"(.*) \},$', s)
            if m:
                rest = m.group(2)
                rule = dict(re.findall(r'(\w+) = "((?:[^"\\]|\\.)*)"', rest))
                rule["to_pattern"] = m.group(1)
                rule["emergency"] = ", emergency = true" in rest
                for key, body in re.findall(r"(\w+) = \{ ([^}]*) \}", rest):
                    rule[key] = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
                # A field this loop cannot read parses as ABSENT, and an assertion over an
                # absent field passes vacuously. Fail rather than assume.
                seen = set(rule) | {"to_pattern", "emergency"}
                for key in re.findall(r"(\w+) =", rest):
                    if key not in seen:
                        fail("outbound rule %d has a field %r this parser cannot read; an "
                             "assertion over it would pass vacuously" % (len(outbound), key))
                outbound.append(rule)
                continue
    if not registrations:
        fail("could not read any registration out of the rendered routing.lua")
    if not lines:
        fail("could not read any line out of the rendered routing.lua")
    if not outbound:
        fail("could not read any outbound rule out of the rendered routing.lua")
    return registrations, lines, outbound


def check(registrations, lines, outbound, rows, pjsip_conf):
    """Invariants a JSON Schema cannot express, against the rendered table."""
    sip_host, sip_user = env_value("SIP_HOST"), env_value("SIP_USER")
    # The endpoint is the peer for every purpose below: the dialplan dials it, an inbound
    # call is attributed by it, and check_pjsip holds its own shape.
    pjsip_bodies = {n: b for (n, t), b in pjsip_objects(pjsip_conf).items()
                    if t == "endpoint"}
    peers = set(pjsip_bodies)
    real = sorted(peers)

    # FIRST, because `entrypoint` is what the generator gives the host's single
    # type=identify: a host with none, or with two, has no defined inbound fallback and
    # everything below would be reading a file built on that.
    enabled = {n: r for n, r in registrations.items() if not r["disabled"]}
    for name, reg in sorted(registrations.items()):
        if reg["disabled"] and reg["entrypoint"]:
            fail("registration %r is both disabled and the entrypoint; the host's inbound "
                 "fallback and the PSTN trunk would be gone" % name)
    for host in sorted({r["host"] for r in enabled.values()}):
        on_host = sorted(n for n, r in enabled.items() if r["host"] == host)
        entry = [n for n in on_host if enabled[n]["entrypoint"]]
        if len(entry) > 1:
            fail("host %r has more than one entrypoint registration (%s); the inbound "
                 "fallback would be undefined" % (host, ", ".join(entry)))
        if len(on_host) > 1 and not entry:
            fail("host %r has %d registrations and none marked entrypoint, so an inbound "
                 "call whose contact matches nothing has no defined line"
                 % (host, len(on_host)))

    for name, reg in registrations.items():
        # `peer` is the section an outbound call on this registration leaves by, and what an
        # inbound one is attributed by. ALWAYS the entry name: fromuser -- from_user on a
        # pjsip endpoint -- is pinned per section, so each DID a line may present needs one of
        # its own, and a section named after the host three of them share could not.
        if reg.get("peer") != name:
            fail("registration %r rendered peer %r into routing.lua; every registration's "
                 "section is named after the entry" % (name, reg.get("peer")))
        for field in ("user", "host", "peer", "contact_user", "did", "line"):
            if not reg.get(field):
                fail("registration %r rendered without a %s" % (name, field))
        # The endpoint the call actually leaves by has to exist, or it would be dialled with
        # no credentials -- which a carrier may answer with a recorded refusal, i.e. a
        # connected call rather than an error. check_pjsip holds the rest of its shape.
        if reg["disabled"]:
            if reg["peer"] in peers:
                fail("registration %r is disabled but %r is still an endpoint in pjsip.conf"
                     % (name, reg["peer"]))
        elif reg["peer"] not in peers:
            fail("registration %r leaves by %r, which is no endpoint in pjsip.conf "
                 "(have: %s)" % (name, reg["peer"], ", ".join(real)))
        if reg["line"] not in lines:
            fail("registration %r belongs to line %r, which the table does not define"
                 % (name, reg["line"]))
        # The `entrypoint` row is also cheogram-sip.env's SIP_USER at SIP_HOST -- the trunk
        # the component appends to a bare number -- and the two are written in different
        # files. If they disagree the number the table routes by is not the one that can be
        # reached, and nothing downstream could correct it.
        if reg["entrypoint"]:
            for field, want, key in (("user", sip_user, "SIP_USER"),
                                     ("host", sip_host, "SIP_HOST")):
                if want is not None and reg[field] != want:
                    fail("registration %r is the entrypoint's own but its %s is %r, while "
                         "%s is %r" % (name, field, reg[field], key, want))

    # Inbound attribution is (host, contact_user) -> registration, then the host's default
    # registration when the carrier's Request-URI matches no contact. Both halves have to be
    # unambiguous or an inbound call is attributed to whichever entry Lua's pairs() reached
    # first, which is not a defined order.
    seen = {}
    for name, reg in sorted(registrations.items()):
        key = (reg["host"].lower(), reg["contact_user"].lower())
        if key in seen:
            fail("registrations %r and %r register the same contact %r at %r, so an inbound "
                 "call cannot say which number rang"
                 % (seen[key], name, reg["contact_user"], reg["host"]))
        seen[key] = name
    dids = {}
    for name, reg in sorted(registrations.items()):
        if reg["did"] in dids:
            fail("registrations %r and %r both claim DID %r"
                 % (dids[reg["did"]], name, reg["did"]))
        dids[reg["did"]] = name
    # A line name and a trunk host both appear after "#" in an XMPP destination, so one that
    # is both is ambiguous in resolve_destination. Every host, disabled ones included, so
    # re-enabling one cannot introduce the clash.
    trunk_hosts = {r["host"].lower() for r in registrations.values()} | {(sip_host or "").lower()}
    for name in lines:
        if name.lower() in trunk_hosts:
            fail("line %r has the same name as a trunk host; '#%s' would be ambiguous"
                 % (name, name))

    for name, line in sorted(lines.items()):
        if not line["primary"]:
            fail("line %r rendered without a primary" % name)
        reg = registrations.get(line["primary"])
        if reg is None:
            fail("line %r presents registration %r, which the table does not define"
                 % (name, line["primary"]))
        # A carrier may reject or silently rewrite a From the account does not own, so the
        # presented number has to be one of this line's own.
        if reg["line"] != name:
            fail("line %r presents registration %r, which belongs to line %r"
                 % (name, line["primary"], reg["line"]))
        # A disabled primary switches the whole line off, so a member still enabled would
        # ring in on a line nobody can call back on.
        if reg["disabled"]:
            live = sorted(n for n, r in enabled.items() if r["line"] == name)
            if live:
                fail("line %r has a disabled primary but enabled member(s) %s; disable them "
                     "too, or present one of them" % (name, ", ".join(live)))
        # The gate. A line with no callers list refuses everyone, which is the safe
        # direction but still a line nobody can use, so it never ships.
        if not line["callers"]:
            fail("line %r rendered with no callers; nobody could present it" % name)
        for c in line["callers"]:
            if not GLOB.match(c):
                fail("line %r: caller %r is not an address pattern" % (name, c))
        stages = line["inbound_route"]
        if not stages:
            fail("line %r rendered with no inbound stages" % name)
        for j, s in enumerate(stages):
            if "record" in s and j != len(stages) - 1:
                fail("line %r: a record stage must be last" % name)
            # An address inbound_legs cannot parse contributes no leg, so a typo is a
            # stage that silently rings nobody.
            for addr in s.get("ring", []):
                if not RING_ADDRESS.match(addr):
                    fail("line %r stage %d: ring address %r is neither xmpp: nor matrix:"
                         % (name, j + 1, addr))
            # One leg per person: the same MXID twice rings them twice. The dialplan
            # lowercases, so compare that way.
            if has_duplicate_mxid(s.get("ring", [])):
                fail("line %r stage %d: a duplicate matrix: target in one ring stage"
                     % (name, j + 1))
        if not any("ring" in s for s in stages):
            fail("line %r has no ring stage" % name)

    for i, r in enumerate(outbound):
        # The renderer anchors exactly once. Without it "11[02]" would also match
        # "0112345" and an emergency rule would swallow ordinary numbers.
        if not (r["to_pattern"].startswith("^(") and r["to_pattern"].endswith(")$")):
            fail("outbound rule %d was rendered unanchored: %r" % (i, r["to_pattern"]))
        # Asterisk's Lua 5.1 has no regex engine. A regex here does not error, it matches
        # nothing and the call falls through to the next rule.
        bad = set("{}|\\") & set(r["to_pattern"])
        if bad:
            fail("outbound rule %d has regex-only characters %s in a Lua pattern: %r"
                 % (i, "".join(sorted(bad)), r["to_pattern"]))
        # An emergency rule is the only thing that reaches a trunk ungated, so its pattern
        # must not be able to match an ordinary number. Digits and classes only.
        if r["emergency"]:
            if not EMERGENCY_PATTERN.match(r["to_pattern"]):
                fail("outbound rule %d is an emergency route whose pattern %r is not a plain "
                     "digit pattern; it would be an ungated trunk" % (i, r["to_pattern"]))
        # First match wins, so an ordinary rule above an emergency one can swallow 110.
        if not r["emergency"] and any(x["emergency"] for x in outbound[i + 1:]):
            fail("outbound rule %d precedes an emergency route; first match wins, so it "
                 "could swallow the emergency number" % i)
        for g in r.get("from_glob", []):
            if not GLOB.match(g):
                fail("outbound rule %d: from_glob %r is not an address pattern" % (i, g))
        if r["emergency"] and not r.get("registration"):
            fail("outbound rule %d is an emergency route but pins no registration; the "
                 "presented number is what fixes the address the dispatcher sees" % i)
        if r.get("registration"):
            if not r["emergency"]:
                fail("outbound rule %d pins a registration without being an emergency "
                     "route; ordinary routes select a line" % i)
            if r["registration"] not in registrations:
                fail("outbound rule %d pins unknown registration %r"
                     % (i, r["registration"]))
            if registrations[r["registration"]]["disabled"]:
                fail("outbound rule %d is an emergency route pinning disabled registration %r"
                     % (i, r["registration"]))
            presented = r["registration"]
        else:
            if r.get("line") not in lines:
                fail("outbound rule %d selects unknown line %r" % (i, r.get("line")))
            presented = lines[r["line"]]["primary"]
            # Allowed: the dialplan refuses it as "disabled" and never tries the next rule.
            if registrations[presented]["disabled"]:
                continue
        # Presenting a number means the endpoint the call leaves by pins from_user to it,
        # which is why every endpoint carries the same values as its registration.
        reg = registrations[presented]
        peer = reg.get("peer") or reg["host"]
        body = pjsip_bodies.get(peer)
        if body is None:
            fail("outbound rule %d presents registration %r, whose peer %r is no endpoint "
                 "in pjsip.conf" % (i, presented, peer))
        if not re.search(r"(?m)^from_user=%s$" % re.escape(reg["user"]), body):
            fail("outbound rule %d presents registration %r, but the endpoint %r does not "
                 "pin from_user=%s -- the call would present another number"
                 % (i, presented, peer, reg["user"]))


def reject(values, what, expect):
    """Assert Helm refuses these values, and refuses them for the stated reason."""
    rc, _, err = helm_template_text(values)
    if rc == 0:
        fail("helm accepted %s; values.schema.json is not being enforced" % what)
    if expect not in err:
        fail("helm rejected %s but not on %r, so the schema may not be the thing that "
             "rejected it:\n%s" % (what, expect, err.strip()))


def glob_permits(glob, addr):
    """routing_glob_match in extensions.lua, which is what `callers` is checked with."""
    glob, addr = glob.lower(), addr.lower()
    if glob == "*":
        return True
    if glob == "xmpp:*":
        return addr.startswith("xmpp:")
    if glob == "matrix:*":
        return addr.startswith("matrix:")
    m = re.match(r"^xmpp:\*@(.+)$", glob)
    if m:
        n = re.match(r"^xmpp:[^@]+@(.+)$", addr)
        return bool(n) and n.group(1) == m.group(1)
    m = re.match(r"^matrix:@\*:(.+)$", glob)
    if m:
        n = re.match(r"^matrix:@[^:]+:(.+)$", addr)
        return bool(n) and n.group(1) == m.group(1)
    return glob == addr


MEMBER = re.compile(r"^matrix:@[^:@*]+:[^:@*]+$")


def has_duplicate_mxid(ring):
    mxids = [a.lower() for a in ring if MEMBER.match(a)]
    return len(mxids) != len(set(mxids))


def derive_line_members(lines, registrations=None):
    """network.line_members for apps/matrix, from the routing table's `lines`: per line the
    union of its literal matrix: ring targets and its `members`. Bare `matrix:` rings the
    bridge's default recipient and names nobody. A line with no one is omitted, and so is
    one whose primary registration is disabled."""
    out = {}
    for name, line in lines.items():
        if (registrations or {}).get(line.get("primary"), {}).get("disabled"):
            continue
        found = set()
        seen = set()
        for m in line.get("members") or []:
            if not MEMBER.match(m):
                fail("line %r: member %r is not a literal matrix:@localpart:server" % (name, m))
            if m.lower() in seen:
                fail("line %r: member %r is listed twice" % (name, m))
            seen.add(m.lower())
            if not any(glob_permits(g, m) for g in line.get("callers") or []):
                fail("line %r: member %r is not permitted by the line's callers; the bridge "
                     "would give them a dial room they cannot dial from" % (name, m))
            found.add(m)
        for stage in line.get("inbound_route") or []:
            found.update(a for a in stage.get("ring") or [] if MEMBER.match(a))
        if found:
            out[name] = sorted(a[len("matrix:"):] for a in found)
    return out


def check_line_members(rendered):
    """The chart's sip-line-members ConfigMap against derive_line_members, which also holds
    `members` to `callers`; then the derivation's own rejections."""
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    expected = {"network": {"line_members": derive_line_members(values["lines"],
                                                                values["registrations"])}}
    got = yaml.safe_load(extract_block(rendered, "line-members.yaml"))
    if got != expected:
        fail("sip-line-members renders %r, but the routing table derives %r" % (got, expected))

    # The derivation's own rejections, on synthetic lines.
    base = {"callers": ["matrix:@*:a.de"], "inbound_route": [{"ring": ["matrix:"]}]}
    for what, bad in (
            ("a member callers does not permit", {"members": ["matrix:@x:b.de"]}),
            ("a duplicate member", {"members": ["matrix:@x:a.de", "matrix:@X:a.de"]}),
            ("a pattern member", {"members": ["matrix:@*:a.de"]})):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                derive_line_members({"l": {**base, **bad}})
        except SystemExit:
            continue
        fail("derive_line_members accepted " + what)
    if not has_duplicate_mxid(["matrix:@x:a.de", "matrix:@X:a.de"])             or has_duplicate_mxid(["matrix:@x:a.de", "matrix:@y:a.de", "matrix:"]):
        fail("has_duplicate_mxid does not tell a repeated MXID from distinct ones")
    ring = ["matrix:@x:a.de", "matrix:", "xmpp:y@z"]
    got = derive_line_members({"l": {**base, "members": ["matrix:@x:a.de"],
                                     "inbound_route": [{"ring": ring}]}})
    off = derive_line_members({"l": {**base, "primary": "p", "members": ["matrix:@x:a.de"]}},
                              {"p": {"disabled": True}})
    if got != {"l": ["@x:a.de"]} or derive_line_members({"l": base}) != {} or off != {}:
        fail("derive_line_members returned %r" % (got,))


def check_schema_is_enforced():
    """The negative cases. values.schema.json only protects anything while Helm actually
    reads it -- a wrong $schema dialect, or a renamed file, disables it silently."""
    reject("outbound:\n  - {to_pattern: '[0-9]{1,5}', line: home}\n",
           "a routing table with a regex to_pattern", "to_pattern")
    # The registration half. A field missing here is a trunk that never registers, and a
    # field carrying whitespace splits one TAB-separated line into two entries.
    # A NEW entry, not an override of one in values.yaml: -f merges maps, so a field
    # "removed" from an existing entry is still supplied by the default values.
    reject("registrations:\n  broken: {user: '1234', contact_user: 'x@y', did: '1234',"
           " line: home}\n",
           "a registration with no host", "host")
    reject("registrations:\n  pstn-02012345671: {contact_user: 'a b'}\n",
           "a registration whose contact_user contains a space", "contact_user")
    # The object set is hand-written, so peer_secret_file is the only place a password can
    # land: an entry without one authenticates with nothing.
    reject("registrations:\n  broken: {user: '1234', host: h, contact_user: 'x', did: '1234',"
           " line: home}\n",
           "a registration with no peer_secret_file", "peer_secret_file")
    # The line half. A schema that silently stopped being read would let all three through.
    reject("lines:\n  back_office: {primary: pstn-02012345671, inbound_route:"
           " [{ring: ['xmpp:a@b.example'], timeout: 25}]}\n",
           "a line name with an underscore", "lines")
    reject("outbound:\n  - {to_pattern: '.*', line: home, registration: pstn-02012345678,"
           " emergency: true}\n",
           "an outbound rule that selects both a line and a registration", "outbound")
    reject("outbound:\n  - {to_pattern: '11[02]', registration: pstn-02012345678}\n",
           "a registration-pinning rule that is not an emergency route", "outbound")
    # A ring address the dialplan cannot parse is a stage that rings nobody, silently.
    reject("lines:\n  home: {primary: pstn-02012345678, inbound_route:"
           " [{ring: ['tel:+4915112345678'], timeout: 25}]}\n",
           "a ring address that is neither xmpp: nor matrix:", "ring")
    # The gate. A line with no callers, or an empty list, would present to anyone the
    # dialplan let through; the dialplan refuses it, and the schema must never ship it.
    reject("lines:\n  spare: {primary: pstn-02012345672, inbound_route:"
           " [{ring: ['xmpp:a@b.example'], timeout: 25}]}\n",
           "a line with no callers list", "callers")
    reject("lines:\n  home: {callers: [], primary: pstn-02012345678, inbound_route:"
           " [{ring: ['xmpp:a@b.example'], timeout: 25}]}\n",
           "a line whose callers list is empty", "callers")
    reject("lines:\n  home: {callers: ['not-an-address'], primary: pstn-02012345678,"
           " inbound_route: [{ring: ['xmpp:a@b.example'], timeout: 25}]}\n",
           "a caller that is not an address pattern", "callers")
    # Disabling the entrypoint would take the PSTN trunk and the inbound fallback with it.
    reject("registrations:\n  pstn-02012345678: {disabled: true}\n",
           "a disabled entrypoint", "pstn-02012345678")
    # from_glob is a list; a bare string would silently narrow nothing.
    reject("outbound:\n  - {to_pattern: '.*', from_glob: 'xmpp:a@b.example', line: home}\n",
           "a from_glob that is still a bare string", "from_glob")


def check_reenable(registrations, out, write_lua):
    """Re-enabling is deleting the flag and nothing else. Rendered with every `disabled`
    cleared, the table passes every check above, and the live render is exactly that one
    minus the disabled entries: their pjsip objects, their TSV rows, and the flag itself."""
    disabled = sorted(n for n, r in registrations.items() if r["disabled"])
    override = "registrations:\n" + "".join(
        "  %s:\n    disabled: null\n" % n for n in disabled)
    rc, full, err = helm_template_text(override) if disabled else (0, out, "")
    if rc != 0:
        fail("the table does not render with its disabled entries enabled:\n" + err.strip())
    lua = extract_block(full, "routing.lua")
    rows = parse_registrations_tsv(extract_block(full, "registrations.tsv"))
    regs, lines, outbound = parse_lua(lua)
    pjsip_conf = check_pjsip_is_generated(full)
    if any(r["disabled"] for r in regs.values()):
        fail("clearing `disabled` left an entry disabled")
    check(regs, lines, outbound, rows, pjsip_conf)
    check_registrations_tsv(rows, regs)
    check_pjsip(regs, rows, pjsip_conf)

    if extract_block(out, "routing.lua").replace(", disabled = true }", " }") != lua:
        fail("routing.lua differs from the re-enabled render by more than the disabled flag")
    if extract_block(out, "registrations.tsv").splitlines() != [
            l for l in extract_block(full, "registrations.tsv").splitlines()
            if l.split("\t")[0] not in disabled]:
        fail("registrations.tsv differs from the re-enabled render by more than the "
             "disabled rows")
    heads = tuple("[%s]\n" % registrations[n]["peer"] for n in disabled)
    if check_pjsip_is_generated(out) != "\n\n".join(
            c for c in pjsip_conf.split("\n\n") if not c.startswith(heads)):
        fail("pjsip.conf differs from the re-enabled render by more than the disabled "
             "entries' objects")
    if write_lua:
        with open(write_lua, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(lua + "\n")
    return disabled


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write-lua", metavar="PATH",
                    help="also write the rendered routing.lua here, for the dialplan suite")
    ap.add_argument("--write-lua-enabled", metavar="PATH",
                    help="and the one rendered with every disabled entry enabled")
    args = ap.parse_args()

    rc, out, err = helm_template()
    if rc != 0:
        fail("helm template failed:\n" + err.strip())
    lua = extract_block(out, "routing.lua")
    tsv = parse_registrations_tsv(extract_block(out, "registrations.tsv"))
    registrations, lines, outbound = parse_lua(lua)
    pjsip_conf = check_pjsip_is_generated(out)
    check(registrations, lines, outbound, tsv, pjsip_conf)
    check_registrations_tsv(tsv, registrations)
    check_pjsip(registrations, tsv, pjsip_conf)
    check_in_cluster(pjsip_objects(pjsip_conf))
    check_asterisk_image()
    check_schema_is_enforced()
    check_line_members(out)
    disabled = check_reenable(registrations, out, args.write_lua_enabled)
    if args.write_lua:
        with open(args.write_lua, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(lua + "\n")
    print("routing chart OK: %d registration(s) (%d disabled), %d line(s), "
          "%d outbound rule(s)" % (len(registrations), len(disabled), len(lines), len(outbound)))


if __name__ == "__main__":
    main()
