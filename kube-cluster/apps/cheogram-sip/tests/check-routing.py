#!/usr/bin/env python3
"""Check the SIP routing chart: render it, then assert what a schema cannot.

Everything runs against the RENDERED output, so a template that drops or mangles an entry is
caught as well as a bad table. --write-lua hands the rendered routing.lua to the dialplan
suite, which keeps that suite from drifting from the chart.
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
ENV = APP / "cheogram-sip.env"

VALUES = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
# Names for the negative cases, taken from the live table so no number is spelled out here.
ENTRY = next(n for n, r in VALUES["registrations"].items() if r.get("entrypoint"))
OTHER = next(n for n in VALUES["registrations"] if n != ENTRY)

# The hand-written in-cluster peers: endpoint context, and whether they challenge inbound.
IN_CLUSTER = {"livekit": ("livekit", True), "matrixbridge": ("matrixbridge", False)}


def fail(msg):
    print("check-routing: " + msg, file=sys.stderr)
    sys.exit(1)


def helm_template(values=None):
    """Render the chart. Helm validates values.schema.json here, so a bad table comes back
    as a non-zero exit and its message."""
    cmd = ["helm", "template", "routing", str(CHART), "--namespace", "cheogram-sip"]
    if values:
        cmd += ["-f", values]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def helm_template_text(values):
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
    """The rendered pjsip.conf as {(name, type): body}. Section names repeat there (one per
    object type for the same trunk), so the type is half the key."""
    out, cur, body = [], None, []
    for line in text.splitlines():
        m = re.match(r"^\[([^\]]+)\]$", line.strip())
        if m:
            if cur:
                out.append((cur, "\n".join(body)))
            cur, body = m.group(1), []
        elif cur is not None:
            body.append(line)
    if cur:
        out.append((cur, "\n".join(body)))
    typed = {}
    for name, b in out:
        m = re.search(r"(?m)^type=(\w+)$", b)
        if not m:
            fail("pjsip.conf section %r declares no type=" % name)
        if (name, m.group(1)) in typed:
            fail("pjsip.conf declares %r as type=%s twice" % (name, m.group(1)))
        typed[(name, m.group(1))] = b
    return typed


def has(body, opt, val):
    return re.search(r"(?m)^%s=%s$" % (opt, re.escape(val)), body) is not None


def extract_block(manifest, key):
    """One ConfigMap data key, un-indented out of its block scalar."""
    try:
        body = manifest.split(key + ": |\n", 1)[1]
    except IndexError:
        fail("the rendered ConfigMap has no %s key" % key)
    out = []
    for line in body.splitlines():
        if line.strip() and not line.startswith("    "):
            break
        out.append(line[4:])
    return "\n".join(out)


def check_pjsip_is_generated(manifest):
    """The ConfigMap holding pjsip.conf must not be reloader-ignored: Asterisk reads the file
    once at start, so a trunk change would otherwise never reach a running pod."""
    docs = [d for d in manifest.split("\n---") if "\n  pjsip.conf: |\n" in d]
    if len(docs) != 1:
        fail("the chart renders pjsip.conf into %d ConfigMaps; exactly one owns it" % len(docs))
    if re.search(r'(?m)^\s*reloader\.stakater\.com/ignore: "true"$', docs[0]):
        fail("pjsip.conf is in a reloader-ignored ConfigMap; a trunk change would never "
             "reach a running pod")
    return extract_block(docs[0], "pjsip.conf")


def check_pjsip(registrations, pjsip_conf, bridge_host):
    """The res_pjsip object set behind every registration, on the GENERATED file."""
    objects = pjsip_objects(pjsip_conf)

    for name, reg in sorted(registrations.items()):
        peer = reg["peer"]
        if reg["disabled"]:
            left = sorted(ty for (n, ty) in objects if n == peer)
            if left:
                fail("registration %r is disabled but pjsip.conf still defines type=%s"
                     % (name, ", type=".join(left)))
            if ("#include %s-secret.conf" % name) in pjsip_conf:
                fail("registration %r is disabled but its secret is still #included" % name)
            continue

        # A trunk is four objects under one name; a missing one fails differently each time.
        missing = [ty for ty in ("auth", "aor", "registration", "endpoint")
                   if (peer, ty) not in objects]
        if missing:
            fail("registration %r: %r has no type=%s" % (name, peer, ", type=".join(missing)))
        ep, rg = objects[(peer, "endpoint")], objects[(peer, "registration")]
        # Caller ID: nothing else pins the presented From.
        for opt, want in (("from_user", reg["user"]), ("from_domain", reg["host"])):
            if not has(ep, opt, want):
                fail("endpoint %r does not pin %s=%s" % (peer, opt, want))
        for opt, want in (("client_uri", "sip:%s@%s" % (reg["user"], reg["host"])),
                          ("server_uri", "sip:%s" % reg["host"]),
                          ("contact_user", reg["contact_user"]),
                          ("endpoint", peer)):
            if not has(rg, opt, want):
                fail("registration object %r does not set %s=%s" % (peer, opt, want))
        if not has(objects[(peer, "auth")], "username", reg["user"]):
            fail("auth %r does not carry username=%s" % (peer, reg["user"]))
        # The password arrives through this #include, written by render-config from the
        # Secret key named after the entry.
        if ("#include %s-secret.conf" % name) not in pjsip_conf:
            fail("pjsip.conf does not #include %s-secret.conf; %r would authenticate with "
                 "no password" % (name, name))
        if not has(ep, "aors", peer) or not re.search(
                r"(?m)^contact=sip:", objects[(peer, "aor")]):
            fail("endpoint %r has no AOR with a contact, so a dial reaches nothing" % peer)
        # identify_by=ip needs a type=identify naming the endpoint; nothing else does.
        if has(ep, "identify_by", "ip"):
            ident = objects.get((peer, "identify"))
            if ident is None or not has(ident, "endpoint", peer):
                fail("endpoint %r is identify_by=ip with no type=identify" % peer)

    # The inbound fallback: a call the carrier does not tag with our ;line= is matched by
    # source address, so each host needs exactly ONE type=identify, on its default.
    live = {n: r for n, r in registrations.items() if not r["disabled"]}
    for host in sorted({r["host"] for r in live.values()}):
        on_host = sorted(n for n, r in live.items() if r["host"] == host)
        default = [n for n in on_host if live[n]["entrypoint"]] or (
            on_host if len(on_host) == 1 else [])
        if not default:
            fail("host %r has %d registrations and no entrypoint" % (host, len(on_host)))
        identified = [n for n in on_host if (live[n]["peer"], "identify") in objects]
        if identified != default:
            fail("host %r has its type=identify on %s but its default is %s"
                 % (host, ", ".join(identified) or "nothing", ", ".join(default)))
        for n in on_host:
            if n not in default and has(objects[(live[n]["peer"], "endpoint")],
                                        "identify_by", "ip"):
                fail("endpoint %r is identify_by=ip but not %s's default" % (n, host))

    # An object under a name nothing claims answers calls with nothing to attribute it by.
    claimed = ({r["peer"] for r in live.values()} | set(IN_CLUSTER)
               | {n for (n, ty) in objects if ty == "transport"})
    for name in sorted({n for n, _ in objects} - claimed):
        fail("pjsip.conf defines %r, which is no transport, in-cluster peer or registration"
             % name)

    transports = {t["name"] for t in VALUES["transports"]}
    for peer, (context, challenges) in sorted(IN_CLUSTER.items()):
        ep = objects.get((peer, "endpoint"))
        if ep is None:
            fail("pjsip.conf has no endpoint %r" % peer)
        m = re.search(r"(?m)^transport=(\S+)$", ep)
        if not m or m.group(1) not in transports:
            fail("endpoint %r names a transport values.yaml does not define" % peer)
        # Neither peer registers, so the From user part is the whole match.
        if not has(ep, "identify_by", "username") or not has(ep, "context", context):
            fail("endpoint %r must be identify_by=username in context %s" % (peer, context))
        # livekit's INVITE is challenged (auth=, never outbound_auth); the bridge has no
        # credentials to challenge.
        challenged = has(ep, "auth", peer) and (peer, "auth") in objects
        if challenged != challenges or re.search(r"(?m)^outbound_auth=", ep) or (
                not challenges and re.search(r"(?m)^auth=", ep)):
            fail("endpoint %r has the wrong auth for an in-cluster peer" % peer)
    want = "sip:%s:5060;transport=tcp" % bridge_host
    if not has(objects[("matrixbridge", "aor")], "contact", want):
        fail("the [matrixbridge] AOR contact is not %s (bridge_host)" % want)


RING_ADDRESS = re.compile(r"^(xmpp:|matrix:)")
GLOB = re.compile(r"^(\*|xmpp:\*|matrix:\*|xmpp:(\*|[^@]+)@[^@]+|matrix:@(\*|[^:]+):[^:]+)$")
# An emergency to_pattern may hold digits and character classes only: it is the one rule that
# bypasses the caller gate, so ".*" there is an open trunk.
EMERGENCY_PATTERN = re.compile(r"^\^\((?:[0-9]|\[[0-9]+\])+\)\$$")


def parse_lua(lua):
    """A shallow reader for the shape templates/configmap.yaml emits; coupled to its line
    layout on purpose. Anything it cannot read is a failure, never a skip."""
    registrations, lines, outbound = {}, {}, []
    bridge_host = (re.search(r'(?m)^\s*bridge_host = "(.*)",$', lua) or [None, None])[1]
    section, cur = None, None
    for line in lua.splitlines():
        s = line.strip()
        if s in ("registrations = {", "lines = {", "outbound = {"):
            section, cur = s.split()[0], None
            continue
        if section == "registrations":
            m = re.match(r'^\["(.+?)"\] = \{ (.*) \},$', s)
            if m:
                fields = dict(re.findall(r'(\w+) = "((?:[^"\\]|\\.)*)"', m.group(2)))
                # The dialplan's peer is the entry name: from_user is pinned per endpoint,
                # so each presentable DID needs a section of its own.
                fields["peer"] = m.group(1)
                fields["entrypoint"] = " entrypoint = true" in (" " + m.group(2))
                fields["disabled"] = " disabled = true" in (" " + m.group(2))
                registrations[m.group(1)] = fields
        elif section == "lines":
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
        elif section == "outbound":
            m = re.match(r'^\{ to_pattern = "(.*?)"(.*) \},$', s)
            if m:
                rest = m.group(2)
                rule = dict(re.findall(r'(\w+) = "((?:[^"\\]|\\.)*)"', rest))
                rule["to_pattern"] = m.group(1)
                rule["emergency"] = ", emergency = true" in rest
                for key, body in re.findall(r"(\w+) = \{ ([^}]*) \}", rest):
                    rule[key] = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
                # A field this loop cannot read parses as absent, and an assertion over an
                # absent field passes vacuously.
                seen = set(rule) | {"to_pattern", "emergency"}
                for key in re.findall(r"(\w+) =", rest):
                    if key not in seen:
                        fail("outbound rule %d has a field %r this parser cannot read"
                             % (len(outbound), key))
                outbound.append(rule)
    if not registrations or not lines or not outbound or not bridge_host:
        fail("could not read registrations, lines, outbound and bridge_host from routing.lua")
    return registrations, lines, outbound, bridge_host


def check(registrations, lines, outbound, pjsip_conf):
    """Invariants a JSON Schema cannot express, against the rendered table."""
    sip_host, sip_user = env_value("SIP_HOST"), env_value("SIP_USER")
    endpoints = {n for (n, t) in pjsip_objects(pjsip_conf) if t == "endpoint"}
    bodies = {n: b for (n, t), b in pjsip_objects(pjsip_conf).items() if t == "endpoint"}
    enabled = {n: r for n, r in registrations.items() if not r["disabled"]}

    for name, reg in sorted(registrations.items()):
        if reg["disabled"] and reg["entrypoint"]:
            fail("registration %r is both disabled and the entrypoint" % name)
    for host in sorted({r["host"] for r in enabled.values()}):
        on_host = sorted(n for n, r in enabled.items() if r["host"] == host)
        entry = [n for n in on_host if enabled[n]["entrypoint"]]
        if len(entry) > 1:
            fail("host %r has more than one entrypoint (%s)" % (host, ", ".join(entry)))
        if len(on_host) > 1 and not entry:
            fail("host %r has %d registrations and no entrypoint" % (host, len(on_host)))

    for name, reg in registrations.items():
        for field in ("user", "host", "contact_user", "did", "line"):
            if not reg.get(field):
                fail("registration %r rendered without a %s" % (name, field))
        if reg["disabled"] == (name in endpoints):
            fail("registration %r is %s but %s an endpoint in pjsip.conf"
                 % (name, "disabled" if reg["disabled"] else "enabled",
                    "still" if reg["disabled"] else "not"))
        if reg["line"] not in lines:
            fail("registration %r belongs to undefined line %r" % (name, reg["line"]))

    # SIP_USER at SIP_HOST is the PSTN trunk the component appends to a bare number; it is
    # written in a different file from the table.
    if not any(r["user"] == sip_user and r["host"] == sip_host for r in enabled.values()):
        fail("no enabled registration is %s at %s (cheogram-sip.env)" % (sip_user, sip_host))

    # Inbound attribution is (host, contact_user) -> registration; ambiguous otherwise.
    seen, dids = {}, {}
    for name, reg in sorted(registrations.items()):
        key = (reg["host"].lower(), reg["contact_user"].lower())
        if key in seen:
            fail("registrations %r and %r register the same contact at %r"
                 % (seen[key], name, reg["host"]))
        seen[key] = name
        if reg["did"] in dids:
            fail("registrations %r and %r both claim DID %r" % (dids[reg["did"]], name,
                                                                 reg["did"]))
        dids[reg["did"]] = name
    # A line name and a trunk host both follow "#" in a destination; one that is both is
    # ambiguous. Disabled hosts included, so re-enabling cannot introduce the clash.
    trunk_hosts = {r["host"].lower() for r in registrations.values()} | {(sip_host or "").lower()}
    for name in lines:
        if name.lower() in trunk_hosts:
            fail("line %r has the same name as a trunk host" % name)

    for name, line in sorted(lines.items()):
        reg = registrations.get(line["primary"])
        if reg is None:
            fail("line %r presents undefined registration %r" % (name, line["primary"]))
        # A carrier may reject a From the account does not own.
        if reg["line"] != name:
            fail("line %r presents %r, which belongs to line %r" % (name, line["primary"],
                                                                      reg["line"]))
        if reg["disabled"]:
            live = sorted(n for n, r in enabled.items() if r["line"] == name)
            if live:
                fail("line %r has a disabled primary but enabled member(s) %s"
                     % (name, ", ".join(live)))
        if not line["callers"]:
            fail("line %r has no callers; nobody could present it" % name)
        for c in line["callers"]:
            if not GLOB.match(c):
                fail("line %r: caller %r is not an address pattern" % (name, c))
        stages = line["inbound_route"]
        for j, s in enumerate(stages):
            if "record" in s and j != len(stages) - 1:
                fail("line %r: a record stage must be last" % name)
            for addr in s.get("ring", []):
                if not RING_ADDRESS.match(addr):
                    fail("line %r stage %d: ring address %r is neither xmpp: nor matrix:"
                         % (name, j + 1, addr))
            if has_duplicate_mxid(s.get("ring", [])):
                fail("line %r stage %d: a duplicate matrix: target" % (name, j + 1))
        if not any("ring" in s for s in stages):
            fail("line %r has no ring stage" % name)

    for i, r in enumerate(outbound):
        # The renderer anchors once; unanchored, "11[02]" would match "0112345".
        if not (r["to_pattern"].startswith("^(") and r["to_pattern"].endswith(")$")):
            fail("outbound rule %d was rendered unanchored: %r" % (i, r["to_pattern"]))
        # Lua 5.1 has no regex engine: a regex does not error, it matches nothing.
        bad = set("{}|\\") & set(r["to_pattern"])
        if bad:
            fail("outbound rule %d has regex-only characters %s" % (i, "".join(sorted(bad))))
        if r["emergency"] and not EMERGENCY_PATTERN.match(r["to_pattern"]):
            fail("outbound rule %d is an emergency route with a non-digit pattern %r; it "
                 "would be an ungated trunk" % (i, r["to_pattern"]))
        # First match wins: an ordinary rule above an emergency one can swallow 110.
        if not r["emergency"] and any(x["emergency"] for x in outbound[i + 1:]):
            fail("outbound rule %d precedes an emergency route" % i)
        for g in r.get("from_glob", []):
            if not GLOB.match(g):
                fail("outbound rule %d: from_glob %r is not an address pattern" % (i, g))
        if r.get("registration"):
            if not r["emergency"]:
                fail("outbound rule %d pins a registration without being an emergency route"
                     % i)
            if r["registration"] not in registrations:
                fail("outbound rule %d pins unknown registration %r" % (i, r["registration"]))
            if registrations[r["registration"]]["disabled"]:
                fail("outbound rule %d pins disabled registration %r" % (i, r["registration"]))
            presented = r["registration"]
        else:
            if r["emergency"]:
                fail("outbound rule %d is an emergency route but pins no registration" % i)
            if r.get("line") not in lines:
                fail("outbound rule %d selects unknown line %r" % (i, r.get("line")))
            presented = lines[r["line"]]["primary"]
            # Allowed: the dialplan refuses it as "disabled" and never tries the next rule.
            if registrations[presented]["disabled"]:
                continue
        # The endpoint the call leaves by pins from_user, which is the presented number.
        reg = registrations[presented]
        if not has(bodies[reg["peer"]], "from_user", reg["user"]):
            fail("outbound rule %d presents %r but its endpoint does not pin from_user=%s"
                 % (i, presented, reg["user"]))


def check_component_files(registrations):
    """What render-config and Asterisk need from the files around the chart."""
    # One Secret key per entry, disabled included so re-enabling stays a one-line change.
    # Only the VALUES are encrypted, so the names are readable.
    keys, in_data = set(), False
    for line in (APP / "secret-registrations.yaml").read_text(encoding="utf-8").splitlines():
        if not line[:1].isspace():
            in_data = line.rstrip() == "stringData:"
        elif in_data and re.match(r"^\s+[A-Za-z0-9._-]+:", line):
            keys.add(line.split(":")[0].strip())
    if keys != set(registrations):
        fail("secret-registrations.yaml keys %s differ from the registrations %s"
             % (sorted(keys), sorted(registrations)))

    # The config is rendered by the binary that parses it, and it is our fork.
    res = yaml.safe_load_all(RESOURCES.read_text(encoding="utf-8"))
    dep = next(d for d in res if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    images = {c["name"]: c["image"] for c in dep["initContainers"] + dep["containers"]}
    if images["render-config"] != images["asterisk"] or not re.fullmatch(
            r"ghcr\.io/lhns/asterisk:\S+@sha256:[0-9a-f]{64}", images["asterisk"]):
        fail("render-config and asterisk must run the same digest-pinned "
             "ghcr.io/lhns/asterisk image")

    # Asterisk exits without modules.conf; a ConfBridge() profile that is not defined fails
    # the call; xmpp.conf without the include connects with the image's default component.
    if not (APP / "modules.conf").read_text(encoding="utf-8").strip():
        fail("modules.conf is empty")
    cb = (APP / "confbridge.conf").read_text(encoding="utf-8")
    for p in ("default_bridge", "default_user", "matrix_caller", "matrix_marked"):
        if ("[%s]" % p) not in cb:
            fail("confbridge.conf does not define the %s profile" % p)
    if "#include xmpp-component.conf" not in (APP / "xmpp.conf").read_text(encoding="utf-8"):
        fail("xmpp.conf does not #include xmpp-component.conf")


def check_rtp_range(out):
    """cheogram-sip-lb forwards exactly the UDP ports rtp.conf hands out."""
    rtp = extract_block(out, "rtp.conf")
    start = int(re.search(r"(?m)^rtpstart=(\d+)$", rtp).group(1))
    end = int(re.search(r"(?m)^rtpend=(\d+)$", rtp).group(1))
    lb = next(d for d in yaml.safe_load_all(RESOURCES.read_text(encoding="utf-8"))
              if d["kind"] == "Service" and d["metadata"]["name"] == "cheogram-sip-lb")
    ports = sorted(p["port"] for p in lb["spec"]["ports"] if p["protocol"] == "UDP")
    if ports != list(range(start, end + 1)):
        fail("cheogram-sip-lb forwards UDP %s..%s (%d ports) but rtp.conf hands out %d..%d"
             % (ports[0], ports[-1], len(ports), start, end))
    if not re.search(r"(?m)^__POD_IP__ => " + re.escape(VALUES["external_address"]) + "$", rtp):
        fail("rtp.conf's ice_host_candidates does not map to external_address")


def reject(values, what, expect):
    """Assert Helm refuses these values, and for the stated reason."""
    rc, _, err = helm_template_text(values)
    if rc == 0:
        fail("helm accepted %s; values.schema.json is not being enforced" % what)
    if expect not in err:
        fail("helm rejected %s but not on %r:\n%s" % (what, expect, err.strip()))


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
    """network.line_members for apps/matrix: per line the union of its literal matrix: ring
    targets and its `members`. A line with no one, or a disabled primary, is omitted."""
    out = {}
    for name, line in lines.items():
        if (registrations or {}).get(line.get("primary"), {}).get("disabled"):
            continue
        found, seen = set(), set()
        for m in line.get("members") or []:
            if not MEMBER.match(m):
                fail("line %r: member %r is not a literal matrix:@localpart:server" % (name, m))
            if m.lower() in seen:
                fail("line %r: member %r is listed twice" % (name, m))
            seen.add(m.lower())
            if not any(glob_permits(g, m) for g in line.get("callers") or []):
                fail("line %r: member %r is not permitted by the line's callers" % (name, m))
            found.add(m)
        for stage in line.get("inbound_route") or []:
            found.update(a for a in stage.get("ring") or [] if MEMBER.match(a))
        if found:
            out[name] = sorted(a[len("matrix:"):] for a in found)
    return out


def check_line_members(rendered):
    """The sip-line-members ConfigMap against derive_line_members, then the derivation's own
    rejections."""
    expected = {"network": {"line_members": derive_line_members(VALUES["lines"],
                                                                VALUES["registrations"])}}
    got = yaml.safe_load(extract_block(rendered, "line-members.yaml"))
    if got != expected:
        fail("sip-line-members renders %r, but the routing table derives %r" % (got, expected))

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
    ring = ["matrix:@x:a.de", "matrix:", "xmpp:y@z"]
    got = derive_line_members({"l": {**base, "members": ["matrix:@x:a.de"],
                                     "inbound_route": [{"ring": ring}]}})
    off = derive_line_members({"l": {**base, "primary": "p", "members": ["matrix:@x:a.de"]}},
                              {"p": {"disabled": True}})
    if got != {"l": ["@x:a.de"]} or derive_line_members({"l": base}) != {} or off != {}:
        fail("derive_line_members returned %r" % (got,))


def check_schema_is_enforced():
    """The negative cases: values.schema.json protects nothing if Helm stops reading it (a
    wrong $schema dialect, a renamed file)."""
    reject("outbound:\n  - {to_pattern: '[0-9]{1,5}', line: home}\n",
           "a regex to_pattern", "to_pattern")
    # A NEW entry, not an override: -f merges maps, so a field "removed" from an existing
    # entry is still supplied by values.yaml.
    reject("registrations:\n  broken: {user: '1234', did: '1234', line: home}\n",
           "a registration with no host", "host")
    reject("registrations:\n  broken: {user: 'a b', host: h, did: '1234', line: home}\n",
           "a registration whose user contains a space", "user")
    reject("registrations:\n  Broken_Name: {user: '1', host: h, did: '1', line: home}\n",
           "a registration name that is no Secret key", "registrations")
    reject("lines:\n  back_office: {primary: %s, inbound_route:"
           " [{ring: ['xmpp:a@b.example'], timeout: 25}]}\n" % OTHER,
           "a line name with an underscore", "lines")
    reject("outbound:\n  - {to_pattern: '.*', line: home, registration: %s,"
           " emergency: true}\n" % ENTRY,
           "a rule selecting both a line and a registration", "outbound")
    reject("outbound:\n  - {to_pattern: '11[02]', registration: %s}\n" % ENTRY,
           "a registration-pinning rule that is not an emergency route", "outbound")
    reject("lines:\n  home: {primary: %s, inbound_route:"
           " [{ring: ['tel:+1555000000'], timeout: 25}]}\n" % ENTRY,
           "a ring address that is neither xmpp: nor matrix:", "ring")
    # The gate: a line with no callers would present to anyone the dialplan let through.
    reject("lines:\n  spare: {primary: %s, inbound_route:"
           " [{ring: ['xmpp:a@b.example'], timeout: 25}]}\n" % OTHER,
           "a line with no callers list", "callers")
    reject("lines:\n  home: {callers: [], primary: %s, inbound_route:"
           " [{ring: ['xmpp:a@b.example'], timeout: 25}]}\n" % ENTRY,
           "a line with an empty callers list", "callers")
    reject("lines:\n  home: {callers: ['not-an-address'], primary: %s,"
           " inbound_route: [{ring: ['xmpp:a@b.example'], timeout: 25}]}\n" % ENTRY,
           "a caller that is not an address pattern", "callers")
    # Disabling the entrypoint would take the PSTN trunk and the inbound fallback with it.
    reject("registrations:\n  %s: {disabled: true}\n" % ENTRY,
           "a disabled entrypoint", ENTRY)
    reject("outbound:\n  - {to_pattern: '.*', from_glob: 'xmpp:a@b.example', line: home}\n",
           "a from_glob that is a bare string", "from_glob")


def check_reenable(registrations, out, write_lua):
    """Re-enabling is deleting the flag and nothing else: rendered with every `disabled`
    cleared, the table passes every check, and the live render is that one minus the disabled
    entries' pjsip objects and the flag itself."""
    disabled = sorted(n for n, r in registrations.items() if r["disabled"])
    override = "registrations:\n" + "".join(
        "  %s:\n    disabled: null\n" % n for n in disabled)
    rc, full, err = helm_template_text(override) if disabled else (0, out, "")
    if rc != 0:
        fail("the table does not render with its disabled entries enabled:\n" + err.strip())
    lua = extract_block(full, "routing.lua")
    regs, lines, outbound, bridge_host = parse_lua(lua)
    pjsip_conf = check_pjsip_is_generated(full)
    if any(r["disabled"] for r in regs.values()):
        fail("clearing `disabled` left an entry disabled")
    check(regs, lines, outbound, pjsip_conf)
    check_pjsip(regs, pjsip_conf, bridge_host)

    if extract_block(out, "routing.lua").replace(", disabled = true }", " }") != lua:
        fail("routing.lua differs from the re-enabled render by more than the disabled flag")
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
    registrations, lines, outbound, bridge_host = parse_lua(lua)
    pjsip_conf = check_pjsip_is_generated(out)
    check(registrations, lines, outbound, pjsip_conf)
    check_pjsip(registrations, pjsip_conf, bridge_host)
    check_rtp_range(out)
    check_component_files(registrations)
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
