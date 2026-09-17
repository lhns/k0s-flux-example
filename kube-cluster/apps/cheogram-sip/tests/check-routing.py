#!/usr/bin/env python3
"""Check the SIP routing chart: render it, then assert what a schema cannot.

Everything checked here runs against the RENDERED Lua, never against the values, so a
template that drops or mangles an entry is caught as well as a bad table.

--write-lua also hands that rendered table to the dialplan suite, which is what keeps the
suite from drifting from the chart.
"""
import argparse
import pathlib
import re
import subprocess
import sys
import tempfile

APP = pathlib.Path(__file__).resolve().parent.parent
CHART = APP / "routing"
SIP_CONF = APP / "sip.conf"
RESOURCES = APP / "resources.yaml"
ENV = APP / "cheogram-sip.env"


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


def env_value(key):
    m = re.search(r"(?m)^%s=(.*)$" % re.escape(key), ENV.read_text(encoding="utf-8"))
    return m.group(1).strip() if m else None


def sip_sections():
    """Every [name] section in sip.conf, mapped to its body. The image's entrypoint appends
    one more peer named after SIP_HOST at startup, which this cannot see -- callers add it."""
    text = SIP_CONF.read_text(encoding="utf-8")
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^\[([^\]]+)\]$", line.strip())
        if m:
            cur = m.group(1)
            out[cur] = []
            continue
        if cur:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


# registrations.tsv field each $var in the peer printf stands for. rpw is the password, which
# is read from /sip-secrets at runtime and exists nowhere in git -- it is substituted with a
# placeholder here so that the rest of the section can still be checked.
PEER_PRINTF_FIELDS = {"rname": "name", "ruser": "user", "rhost": "host",
                      "rcontact": "contact_user"}

# The ring-address forms inbound_legs understands, asserted on the RENDERED table.
RING_ADDRESS = re.compile(r"^(xmpp:|matrix:)")

# definitions/glob in values.schema.json, asserted again on the RENDERED table.
GLOB = re.compile(r"^(\*|xmpp:\*|matrix:\*|xmpp:(\*|[^@]+)@[^@]+|matrix:@(\*|[^:]+):[^:]+)$")

# What an emergency to_pattern may contain, once anchored: digits and character classes.
# It is the one rule that bypasses the caller gate, so ".*" there is an open trunk.
EMERGENCY_PATTERN = re.compile(r"^\^\((?:[0-9]|\[[0-9]+\])+\)\$$")

# The peer-writing printf in resources.yaml: its single-quoted format string, then the
# line-continued argument list, then the file it appends to.
PEER_PRINTF_RE = re.compile(
    r"""(?m)^\s*printf '(\[%s\][^']*)' \\\n\s*((?:"\$\w+" ?)+)>> /render/peers\.conf$""")


def generated_peer_sections(rows):
    """The peer sections the render-config initContainer writes into peers.conf.

    A registration that names a peer_secret_file already has a hand-written peer in sip.conf,
    named after its host; every other one gets a peer named after the ENTRY, because fromuser
    is pinned per peer and several registrations share one host.

    The format string is READ OUT of resources.yaml rather than restated here. Restating it
    would let this file keep asserting a fromuser pin that the initContainer had stopped
    writing -- the exact shape of failure the pin exists to prevent.
    """
    m = PEER_PRINTF_RE.search(RESOURCES.read_text(encoding="utf-8"))
    if not m:
        fail("resources.yaml has no recognisable peer printf writing /render/peers.conf; "
             "either it stopped rendering peers or this check can no longer read it")
    fmt, args = m.group(1), re.findall(r'"\$(\w+)"', m.group(2))
    out = {}
    for r in rows:
        if r["peer"]:
            continue
        vals = []
        for a in args:
            if a == "rpw":
                vals.append("<secret>")
            elif a in PEER_PRINTF_FIELDS:
                vals.append(r[PEER_PRINTF_FIELDS[a]])
            else:
                fail("the peer printf passes $%s, which registrations.tsv does not carry" % a)
        rendered = fmt.replace(chr(92) + "n", "\n")
        if rendered.count("%s") != len(vals):
            fail("the peer printf has %d conversions but %d arguments"
                 % (rendered.count("%s"), len(vals)))
        for v in vals:
            rendered = rendered.replace("%s", v, 1)
        head, body = rendered.split("\n", 1)
        head = head.strip()
        if not (head.startswith("[") and head.endswith("]")):
            fail("the peer printf does not open with a section header: %r" % head)
        out[head[1:-1]] = body
    for name, body in sorted(out.items()):
        # Which peer chan_sip attributes an unauthenticated inbound INVITE to is not
        # predictable once several share one address, so a peer here that is not in `public`
        # would send an inbound call into a context that rings nobody.
        if not re.search(r"(?m)^context=public$", body):
            fail("rendered peer %r is not context=public; an inbound call attributed to it "
                 "would land in a context that rings nobody" % name)
        if not re.search(r"(?m)^type=peer$", body):
            fail("rendered peer %r is not type=peer" % name)
    return out


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


def parse_registrations_tsv(tsv):
    """The register table as /bin/sh reads it: TAB separated, five fields, the first of
    which is also the name of the Secret key holding that entry's password."""
    rows = []
    for n, line in enumerate(tsv.splitlines(), 1):
        if not line.strip():
            continue
        f = line.split("\t")
        if not 4 <= len(f) <= 5:
            fail("registrations.tsv line %d has %d fields, not 4 or 5: %r" % (n, len(f), line))
        name, user, host, contact = f[:4]
        peer = f[4] if len(f) == 5 else ""
        for label, v in (("name", name), ("user", user), ("host", host), ("contact_user", contact)):
            if not v or v.split() != [v]:
                fail("registrations.tsv line %d has an empty or whitespace %s: %r"
                     % (n, label, line))
        rows.append({"name": name, "user": user, "host": host,
                     "contact_user": contact, "peer": peer})
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
    sip = SIP_CONF.read_text(encoding="utf-8")
    keys = sops_keys()

    for r in rows:
        # The entry name IS the Secret key. It must therefore be a legal one, and it must
        # exist: a missing key already refuses to start the pod, and catching it here stops
        # the rollout one step earlier.
        if not re.match(r"^[a-z0-9-]+$", r["name"]):
            fail("registration %r is not usable as a Kubernetes Secret key" % r["name"])
        if r["name"] not in keys:
            fail("registration %r has no Secret key of that name in this component's "
                 "secret*.yaml; the initContainer would refuse to start the pod" % r["name"])

        # A peer's secret= line has to land under the exact filename sip.conf #includes, or
        # the peer registers with no secret and fails auth silently.
        if r["peer"] and ("#include " + r["peer"]) not in sip:
            fail("registration %r writes %s, which sip.conf does not #include"
                 % (r["name"], r["peer"]))

    # The entrypoint builds its own register line; a second one for the same account would
    # fight it, and the entry has no Secret key of its own to render one from.
    rendered = {r["name"] for r in rows}
    for name, reg in registrations.items():
        if reg["entrypoint"] and name in rendered:
            fail("registration %r is the entrypoint's own and must not be rendered into "
                 "registrations.tsv" % name)
        if not reg["entrypoint"] and name not in rendered:
            fail("registration %r rendered no register line, so it never registers" % name)

    if "#include registrations.conf" not in sip:
        fail("sip.conf does not #include registrations.conf, so nothing here registers")

    host = env_value("SIP_HOST")
    if host:
        # The inverse of the initContainer's own assertion, and the trap that costs the
        # most: the entrypoint guards its whole setup with `grep "@$SIP_HOST" sip.conf`, so
        # a single occurrence -- a comment is enough -- skips the register line, the peer
        # and the dialplan edit, silently.
        if "@" + host in sip:
            fail("sip.conf mentions @%s; the image entrypoint would skip its whole setup"
                 % host)
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


def check(registrations, lines, outbound, rows):
    """Invariants a JSON Schema cannot express, against the rendered table."""
    sections = sip_sections()
    generated = generated_peer_sections(rows)
    clash = sorted(set(sections) & set(generated))
    if clash:
        fail("peers.conf would redefine %s, which sip.conf already defines"
             % ", ".join(clash))
    # Every peer by name, whichever file writes it, for the fromuser check further down.
    bodies = dict(sections)
    bodies.update(generated)
    # A registration that renders no register line renders no peer either, so its peer is the
    # host-named one: the entrypoint's own, or a hand-written section.
    has_own = {r["name"]: bool(r["peer"]) for r in rows}
    # The image's entrypoint appends one peer named after SIP_HOST at startup, so that host
    # is a legitimate peer even though sip.conf never mentions it. See the app README.
    sip_host, sip_user = env_value("SIP_HOST"), env_value("SIP_USER")
    sip_jid = env_value("SIP_JID")
    peers = set(sections) | ({sip_host} if sip_host else set())
    real = sorted(p for p in peers if p != "general")

    for name, reg in registrations.items():
        # `peer` is the section an outbound call on this registration leaves by. It is not
        # always the host: fromuser is pinned per peer, so each DID a line may present needs
        # one of its own. The two renderers derive it from the same values and must agree, or
        # the dialplan dials a peer nothing writes and the call is refused.
        want_peer = reg["host"] if (reg.get("entrypoint") or has_own.get(name, True)) else name
        if reg.get("peer") != want_peer:
            fail("registration %r rendered peer %r into routing.lua, but the initContainer "
                 "writes its peer as %r" % (name, reg.get("peer"), want_peer))
        for field in ("user", "host", "peer", "contact_user", "did", "line"):
            if not reg.get(field):
                fail("registration %r rendered without a %s" % (name, field))
        # A host that matches no peer would be dialled with no credentials, which a carrier
        # may answer with a recorded refusal -- a connected call, not an error.
        if reg["host"] not in peers:
            fail("registration %r is at host %r, which is not a peer in sip.conf (have: %s)"
                 % (name, reg["host"], ", ".join(real)))
        if reg["line"] not in lines:
            fail("registration %r belongs to line %r, which the table does not define"
                 % (name, reg["line"]))
        # The entrypoint's row describes a register line built by the IMAGE, so it is the
        # one row nothing here renders and nothing here can correct. If it disagrees with
        # the env the image reads, every identity derived from it is wrong.
        if reg["entrypoint"]:
            for field, want, key in (("user", sip_user, "SIP_USER"),
                                     ("host", sip_host, "SIP_HOST"),
                                     ("contact_user", sip_jid, "SIP_JID")):
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
    for host in sorted({r["host"] for r in registrations.values()}):
        on_host = sorted(n for n, r in registrations.items() if r["host"] == host)
        entry = [n for n in on_host if registrations[n]["entrypoint"]]
        if len(entry) > 1:
            fail("host %r has more than one entrypoint registration (%s); the inbound "
                 "fallback would be undefined" % (host, ", ".join(entry)))
        if len(on_host) > 1 and not entry:
            fail("host %r has %d registrations and none marked entrypoint, so an inbound "
                 "call whose contact matches nothing has no defined line"
                 % (host, len(on_host)))

    # A line name and a trunk host both appear after "#" in an XMPP destination, so one that
    # is both is ambiguous in resolve_destination.
    trunk_hosts = {t.strip() for t in (env_value("SIP_TRUNKS") or "").split(",") if t.strip()}
    for name in lines:
        if name in trunk_hosts:
            fail("line %r has the same name as a SIP_TRUNKS host; '#%s' would be ambiguous"
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
            presented = r["registration"]
        else:
            if r.get("line") not in lines:
                fail("outbound rule %d selects unknown line %r" % (i, r.get("line")))
            presented = lines[r["line"]]["primary"]
        # Presenting a number means the peer the call leaves by pins fromuser to it. The
        # entrypoint's peer is written by the image and pins SIP_USER, which the entrypoint
        # row above is already held to; every other one is a section here.
        reg = registrations[presented]
        if not reg["entrypoint"]:
            peer = reg.get("peer") or reg["host"]
            body = bodies.get(peer)
            if body is None:
                fail("outbound rule %d presents registration %r, whose peer %r is neither a "
                     "section in sip.conf nor rendered into peers.conf"
                     % (i, presented, peer))
            if not re.search(r"(?m)^fromuser=%s$" % re.escape(reg["user"]), body):
                fail("outbound rule %d presents registration %r, but the peer %r does not pin "
                     "fromuser=%s -- the call would present another number"
                     % (i, presented, peer, reg["user"]))


def reject(values, what, expect):
    """Assert Helm refuses these values, and refuses them for the stated reason."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write(values)
        bad = fh.name
    try:
        rc, _, err = helm_template(bad)
    finally:
        pathlib.Path(bad).unlink(missing_ok=True)
    if rc == 0:
        fail("helm accepted %s; values.schema.json is not being enforced" % what)
    if expect not in err:
        fail("helm rejected %s but not on %r, so the schema may not be the thing that "
             "rejected it:\n%s" % (what, expect, err.strip()))


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
    # The line half. Each of these is a shape the old `numbers` model could not express, so
    # a schema that silently stopped being read would let all three through.
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
    # from_glob is a list now; a bare string would silently narrow nothing.
    reject("outbound:\n  - {to_pattern: '.*', from_glob: 'xmpp:a@b.example', line: home}\n",
           "a from_glob that is still a bare string", "from_glob")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write-lua", metavar="PATH",
                    help="also write the rendered routing.lua here, for the dialplan suite")
    args = ap.parse_args()

    rc, out, err = helm_template()
    if rc != 0:
        fail("helm template failed:\n" + err.strip())
    lua = extract_block(out, "routing.lua")
    tsv = parse_registrations_tsv(extract_block(out, "registrations.tsv"))
    registrations, lines, outbound = parse_lua(lua)
    check(registrations, lines, outbound, tsv)
    check_registrations_tsv(tsv, registrations)
    check_schema_is_enforced()
    if args.write_lua:
        with open(args.write_lua, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(lua + "\n")
    print("routing chart OK: %d registration(s), %d line(s), %d outbound rule(s)"
          % (len(registrations), len(lines), len(outbound)))


if __name__ == "__main__":
    main()
