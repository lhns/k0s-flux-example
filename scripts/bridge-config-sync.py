#!/usr/bin/env python3
"""Bring a mautrix bridge's git config in line with its image's config upgrader.

  bridge-config-sync.py [--image IMAGE] [BRIDGE...]   rewrite the configs (Renovate postUpgradeTask)
  bridge-config-sync.py --check [BRIDGE...]            change nothing; exit 1 if a config is out of sync

With no BRIDGE, every kube-cluster/apps/matrix/<bridge>-bridge-config.yaml. --image keeps only the
bridges running that image name (Renovate's depName). Each config is run through the bridge's own
upgrader (`<binary> -g`, in the image the Deployment names, fed on stdin: no volumes needed, so it
works through a Docker socket mounted into another container). Then:

  key only in the upgraded copy   -> added to git (its value: the new default, or a moved secret)
  key only in git                 -> removed from git (renamed or dropped upstream)
  key in both                     -> git's line, untouched, with its comments and formatting

Maps are compared key by key; a list or scalar is a leaf and git's stays. Credentials are `${VAR}`
placeholders. Each occurrence is replaced by a unique marker before the upgrader runs (a string
`SUBST_<hex>_<VAR>`, or a random integer for an unquoted placeholder such as telegram's api_id),
so a secret that moves to a renamed key takes its marker along. Every marker must appear exactly
once in the result, else nothing is written and the exit status is 2: a secret was dropped or
duplicated and a human must look. Only key paths are printed, never values.
"""
import argparse
import pathlib
import random
import re
import secrets
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "kube-cluster" / "apps" / "matrix"
PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")
# `-g` regenerates these two; git keeps its placeholders, so they are never taken from the copy.
NEVER_ADD = {"appservice.as_token", "appservice.hs_token"}


class SyncError(Exception):
    pass


# --- markers ---------------------------------------------------------------------------------

def markerize(text):
    """Replace every placeholder with a unique marker. Returns (text, {marker: placeholder})."""
    markers = {}
    out = []
    for line in text.split("\n"):
        # `key: ${VAR}` bare: the field is a number (telegram api_id), so the marker must be one.
        bare = re.fullmatch(r"\s*[\w.-]+:\s+(\$\{[A-Z0-9_]+\})\s*(#.*)?", line)

        def sub(m):
            if bare and m.group(0) == bare.group(1):
                while True:
                    mk = str(random.SystemRandom().randrange(10**8, 10**9))
                    if mk not in text and mk not in markers:
                        break
            else:
                mk = f"SUBST_{secrets.token_hex(6)}_{m.group(1)}"
            markers[mk] = m.group(0)
            return mk

        out.append(PLACEHOLDER.sub(sub, line))
    return "\n".join(out), markers


def _pattern(marker):
    return re.compile(rf"(?<!\d){marker}(?!\d)" if marker.isdigit() else re.escape(marker))


def count(text, marker):
    return len(_pattern(marker).findall(text))


def unmarker(text, markers):
    for mk, ph in markers.items():
        text = _pattern(mk).sub(lambda _: ph, text)
    return text


# --- structure -------------------------------------------------------------------------------

def compose(text, what):
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
    except yaml.YAMLError as e:
        raise SyncError(f"{what}: not valid YAML: {e}") from None
    if not isinstance(node, yaml.MappingNode):
        raise SyncError(f"{what}: top level is not a map")
    return loader, node


def children(node):
    return {k.value: (k, v) for k, v in node.value}


def diff(git, up, path=()):
    """Yield ('add', path, key, value_node, preceding key) and ('remove', path, key)."""
    g, u = children(git), children(up)
    for k in g:
        if k not in u:
            yield ("remove", path, k)
    prev = None
    for k, (_, vn) in u.items():
        if k not in g:
            yield ("add", path, k, vn, prev)
        elif isinstance(g[k][1], yaml.MappingNode) and isinstance(vn, yaml.MappingNode):
            yield from diff(g[k][1], vn, path + (k,))
        prev = k


class Dumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def render(key, value, indent, markers):
    """Lines for one added `key: value`; marker-bearing strings double-quoted like the file's."""
    text_markers = [m for m in markers if not m.isdigit()]

    def rep_str(dumper, s):
        quoted = any(m in s for m in text_markers)
        return dumper.represent_scalar("tag:yaml.org,2002:str", s, style='"' if quoted else None)

    Dumper.add_representer(str, rep_str)
    out = yaml.dump({key: value}, Dumper=Dumper, default_flow_style=False, sort_keys=False,
                    width=10**6, allow_unicode=True)
    return [" " * indent + l for l in out.rstrip("\n").split("\n")]


class Block:
    """Line extents of the keys of one block map; the comments directly above a key belong to it."""

    def __init__(self, lines, node, end):
        if not node.value or node.flow_style:
            raise SyncError("cannot edit a flow-style or empty map")
        self.indent = node.value[0][0].start_mark.column
        self.keys = [k.value for k, _ in node.value]
        starts = [self._lead(lines, k.start_mark.line) for k, _ in node.value]
        self.span = dict(zip(self.keys, zip(starts, starts[1:] + [self._trim(lines, end)])))

    def _indent_of(self, line):
        return len(line) - len(line.lstrip())

    def _lead(self, lines, i):
        while i > 0 and lines[i - 1].lstrip().startswith("#") and self._indent_of(lines[i - 1]) == self.indent:
            i -= 1
        return i

    def _trim(self, lines, e):
        # Blank lines and shallower comments after the last key belong to what follows.
        e = min(e, len(lines))
        while e > 0 and (not lines[e - 1].strip() or (
                lines[e - 1].lstrip().startswith("#") and self._indent_of(lines[e - 1]) <= self.indent)):
            e -= 1
        return e


def block_at(lines, root, path):
    node, end = root, len(lines)
    for k in path:
        blk = Block(lines, node, end)
        node, end = children(node)[k][1], blk.span[k][1]
    return Block(lines, node, end)


def sync_text(text, run_upgrader):
    """(new text, ['+ path', '- path', ...]) for one config; raises SyncError."""
    marked, markers = markerize(text)
    lines = marked.split("\n")
    _, groot = compose(marked, "git config")
    uloader, uroot = compose(run_upgrader(marked), "upgraded copy")

    edits, report = [], []  # (start, end, new lines); a removal has no new lines
    for op in diff(groot, uroot):
        dotted = ".".join(op[1] + (op[2],))
        blk = block_at(lines, groot, op[1])
        if op[0] == "remove":
            edits.append((*blk.span[op[2]], []))
            report.append(f"- {dotted}")
        elif dotted not in NEVER_ADD:
            _, _, key, vn, prev = op
            pos = blk.span[prev][1] if prev in blk.span else blk.span[blk.keys[0]][0]
            edits.append((pos, pos, render(key, uloader.construct_object(vn, deep=True), blk.indent, markers)))
            report.append(f"+ {dotted}")

    # Bottom first so earlier line numbers stay valid. At one line a removal goes before the
    # insertions there, and insertions go last-first so they end up in the upgraded order.
    for i in sorted(range(len(edits)), key=lambda i: (edits[i][0], edits[i][0] != edits[i][1], i), reverse=True):
        s, e, new = edits[i]
        lines[s:e] = new
    out = "\n".join(lines)

    lost = sorted({markers[m] for m in markers if count(out, m) != 1})
    if lost:
        raise SyncError("secret not carried exactly once (dropped, regenerated or duplicated): " + ", ".join(lost))
    result = unmarker(out, markers)
    try:
        yaml.safe_load(result)
    except yaml.YAMLError as e:
        raise SyncError(f"result is not valid YAML: {e}") from None
    return result, report


# --- bridges and images ----------------------------------------------------------------------

def image_name(ref):
    ref = ref.split("@")[0]
    head, _, last = ref.rpartition("/")
    return (head + "/" if head else "") + last.split(":")[0]


def discover():
    """{bridge: (image, binary)} from the Deployments mounting <bridge>-bridge-config."""
    found = {}
    with open(APP / "resources.yaml", encoding="utf-8") as f:
        for doc in yaml.safe_load_all(f):
            if not doc or doc.get("kind") != "Deployment":
                continue
            spec = doc["spec"]["template"]["spec"]
            for vol in spec.get("volumes", []):
                name = (vol.get("secret") or {}).get("secretName", "")
                if not name.endswith("-bridge-config"):
                    continue
                for c in spec["containers"]:
                    binary = next((a for a in c.get("command", []) if a.startswith("/usr/bin/mautrix-")), None)
                    if binary:
                        found[name[: -len("-bridge-config")]] = (c["image"], binary)
    return found


def docker_upgrader(image, binary):
    script = f"cat >/tmp/c.yaml && {binary} -g -c /tmp/c.yaml -r /tmp/r.yaml >&2 && cat /tmp/c.yaml"

    def run(text):
        p = subprocess.run(["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "sh",
                            image, "-c", script], input=text, capture_output=True, text=True, encoding="utf-8")
        if p.returncode:
            raise SyncError(f"{binary} -g in {image_name(image)} exited {p.returncode}:\n"
                            + "\n".join(p.stderr.splitlines()[-15:]))
        return p.stdout

    return run


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("bridges", nargs="*")
    ap.add_argument("--check", action="store_true", help="change nothing; exit 1 if out of sync")
    ap.add_argument("--image", help="only bridges running this image name, e.g. dock.mau.dev/mautrix/signal")
    args = ap.parse_args(argv)

    bridges = discover()
    want = args.bridges or sorted(b for b in bridges if (APP / f"{b}-bridge-config.yaml").exists())
    if args.image:
        want = [b for b in want if image_name(bridges[b][0]) == image_name(args.image)]
        if not want:
            print(f"no bridge runs {image_name(args.image)}; nothing to do")
    status = 0
    for b in want:
        if b not in bridges:
            print(f"{b}: no Deployment mounts {b}-bridge-config", file=sys.stderr)
            status = 2
            continue
        image, binary = bridges[b]
        path = APP / f"{b}-bridge-config.yaml"
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        try:
            new, report = sync_text(text, docker_upgrader(image, binary))
        except SyncError as e:
            print(f"{b}: ERROR {e}", file=sys.stderr)
            status = 2
            continue
        if not report:
            print(f"{b}: in sync")
            continue
        print(f"{b}: {len(report)} change(s) for {image_name(image)}")
        for r in report:
            print(f"  {r}")
        if args.check:
            status = status or 1
        else:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(new)
    return status


if __name__ == "__main__":
    sys.exit(main())
