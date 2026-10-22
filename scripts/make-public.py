#!/usr/bin/env python3
"""Build a sanitised public template branch from this repo.

    python scripts/make-public.py [--config scripts/public.config.yaml]
                                  [--branch public-template] [--keep-scratch]

Reads HEAD via `git archive` into a scratch directory, removes and rewrites what the
config says, and commits the result as an ORPHAN branch — no ancestry, so no historical
commit is reachable from it.

This script never contacts the cluster and never modifies the working tree or any existing
branch. It only reads HEAD and creates/replaces the target branch.

Publishing note: making THIS repo public would publish every branch and all history.
Push the orphan branch to a separate, empty repo instead:

    git push git@github.com:<you>/<new-repo>.git public-template:main

The config is not published: it lists itself under delete_paths. It holds no secret
values — credentials are found by key name or by shape, read out of the tree at runtime,
and kept in memory only so the denylist can prove they are gone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

REPO = Path(__file__).resolve().parent.parent

SOPS_MARKER = "ENC[AES256_GCM"


def run(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    r = subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)
    return r.stdout


def is_text(p: Path) -> bool:
    """Decide by content, not by extension.

    An extension allowlist silently skips whatever it has not heard of, and a skipped file
    is neither substituted nor verified -- the worst combination, because the denylist then
    reports clean. This repo has three extensionless launcher scripts and an
    nginx.conf.template that an allowlist missed exactly that way.
    """
    try:
        return b"\x00" not in p.read_bytes()[:8192]
    except OSError:
        return False


def _lf(text: str) -> str:
    return text.replace(chr(13)+chr(13)+chr(10), chr(10)).replace(chr(13)+chr(10), chr(10)).replace(chr(13), chr(10))


def read_text_any(p: Path) -> str:
    """Read text, tolerating legacy encodings.

    Not every text file here is UTF-8: apps/mosquitto/README.md is Windows-1252 (a 0x97
    em-dash). Judging text by "decodes as UTF-8" classified it as binary, so it was neither
    rewritten nor checked -- and the run still reported clean while the file kept its real
    hostname and IP. Files are written back as UTF-8, which normalises them on the way out.

    Repair is per-byte rather than per-file: that README is *mixed*, mostly UTF-8 with one
    stray 0x97, so decoding the whole thing as cp1252 turned every real UTF-8 character
    into mojibake (-> became a broken arrow). Only the offending bytes are reinterpreted.
    """
    data = p.read_bytes()
    while True:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as e:
            bad = data[e.start:e.end]
            try:
                repl = bad.decode("cp1252").encode("utf-8")
            except UnicodeDecodeError:
                repl = b"?"
            data = data[:e.start] + repl + data[e.end:]


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            yield p


# ---------------------------------------------------------------- extraction

def extract_secrets(root: Path, cfg: dict) -> list[str]:
    """Collect real secret values from the source tree, for the denylist.

    Returned in memory only, never written anywhere. A value found here must not
    survive into the output.
    """
    found: set[str] = set()

    for key in cfg.get("redact_keys") or []:
        rx = re.compile(rf'^\s*{re.escape(key)}\s*:\s*["\']?([^"\'\s#]+)', re.M)
        for p in iter_files(root):
            if is_text(p):
                found.update(rx.findall(read_text_any(p)))

    # redact_patterns deliberately contribute nothing here. Their capture groups are
    # structural (the `postgres://` scheme, not the credential), so denylisting them
    # produces false positives on every DSN in the repo. Shape-based redaction is verified
    # differently: the pattern simply must not match the output any more (see verify()).

    # Drop anything that is obviously a placeholder rather than a live value.
    return sorted(
        v for v in found
        if v and not v.startswith(("<", "$", "{{", "CHANGEME")) and len(v) > 3
    )


# ---------------------------------------------------------------- transforms

def apply_deletes(root: Path, cfg: dict, self_path: str) -> None:
    for rel in cfg.get("delete_paths") or []:
        target = root / rel
        if not target.exists():
            # The config's own entry may legitimately be absent: it is allowed to be
            # uncommitted, and `git archive HEAD` then never exports it. What matters is
            # that it is not in the output, which is already true. Everything else must
            # exist -- a silently-skipped delete is how something private gets published.
            if Path(rel).as_posix() == Path(self_path).as_posix():
                print(f"  absent   {rel} (not committed; nothing to remove)")
                continue
            sys.exit(
                f"delete_paths: '{rel}' does not exist.\n"
                "The config has drifted from the repo - fix the entry rather than removing "
                "this check."
            )
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        print(f"  deleted  {rel}")


def apply_delete_resources(root: Path, cfg: dict) -> None:
    """Drop individual YAML documents by metadata.name, preserving the rest of the file.

    Re-serialising the whole file would reformat every document and produce an unreadable
    diff, so documents are split textually on '---' and dropped whole.
    """
    names = set(cfg.get("delete_resources") or [])
    if not names:
        return
    hit: set[str] = set()

    for p in iter_files(root):
        if p.suffix not in {".yaml", ".yml"}:
            continue
        text = read_text_any(p)
        if not any(n in text for n in names):
            continue

        parts = re.split(r"(?m)^---\s*$\n", text)
        kept = []
        for part in parts:
            try:
                doc = yaml.safe_load(part)
            except yaml.YAMLError:
                kept.append(part)
                continue
            name = (doc or {}).get("metadata", {}).get("name") if isinstance(doc, dict) else None
            if name in names:
                hit.add(name)
                print(f"  dropped  {name}  ({p.relative_to(root)})")
            else:
                kept.append(part)
        p.write_text("---\n".join(kept), encoding="utf-8", newline="\n")

    for missing in sorted(names - hit):
        sys.exit(f"delete_resources: '{missing}' matched nothing — config has drifted.")


def apply_blank_files(root: Path, cfg: dict) -> None:
    """Replace a file's contents while leaving it in place.

    Needed where the content is private but the file is still referenced -- a
    configMapGenerator source, say, whose removal would break `kustomize build`.
    """
    for entry in cfg.get("blank_files") or []:
        target = root / entry["path"]
        if not target.exists():
            sys.exit(f"blank_files: '{entry['path']}' does not exist - config has drifted.")
        target.write_text(entry.get("content", ""), encoding="utf-8", newline="\n")
        print(f"  blanked  {entry['path']}")


def _rewriter(keeps: list[str], substitutions, patterns, extra=None):
    """One masked rewrite pass: literals longest-first, then regexes, then `extra`.

    Longest-first matters: a shorter pattern that is a substring of a longer one would
    otherwise mangle it. `keep` entries are protected by masking them out first, because
    they are API groups and finalizers that merely look like hostnames — rewriting one
    breaks every manifest that references it. Scoped rules go through here too, so they
    cannot bypass `keep`.
    """
    keeps = sorted(keeps, key=len, reverse=True)
    pairs = sorted(((a, b) for a, b in (substitutions or [])),
                   key=lambda ab: len(ab[0]), reverse=True)
    pats = [(re.compile(r["pattern"]), r["replacement"]) for r in (patterns or [])]

    def rewrite(text: str) -> str:
        # Mask keeps with sentinels that no substitution can match.
        masks: dict[str, str] = {}
        for i, k in enumerate(keeps):
            if k in text:
                token = f"\x00KEEP{i}\x00"
                masks[token] = k
                text = text.replace(k, token)

        for src, dst in pairs:
            text = text.replace(src, dst)
        for rx, dst in pats:
            text = rx.sub(dst, text)
        if extra is not None:
            text = extra(text)

        for token, original in masks.items():
            text = text.replace(token, original)
        return text

    return rewrite


# A home-LAN address as the repo writes it: the host, `.x` prose, a `-C` range end, and the
# `/N` suffixes that follow (a CIDR prefix, or shorthand host lists like `.101/102/103`).
# Shared by the rewrite and by verify_lan, so what one cannot rewrite the other reports.
# Built from the config's prefix: this script is published, the real prefix must not be.
def lan_rx(prefix: str) -> re.Pattern:
    return re.compile(rf"(?<![\d.]){re.escape(prefix)}\.(\d{{1,3}}|x)\.(\d{{1,3}}|x)(?!\d)"
                      r"(?:-(\d{1,3})(?![\d.]))?((?:/\.?\d{1,3}(?!\d))*)")


def build_lan_mapper(cfg: dict):
    """Rewrite home-LAN addresses from the explicit `ip_map` table; None without one.

    A pattern that keeps the octets would leave every real address recognisable, so each /24
    and each host has its own fake number, drawn once at random and committed in the config.
    An address with no entry is left untouched, and verify_lan then fails the build.
    """
    spec = cfg.get("ip_map")
    if not spec:
        return None
    real_pfx, fake_pfx = spec["prefix"]
    cidr = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.0/24$")
    ip = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")
    subnets: dict[str, str] = {}
    for real, fake in spec["subnets"].items():
        m, f = cidr.match(real), cidr.match(fake)
        if not (m and f and f"{m[1]}.{m[2]}" == real_pfx and f"{f[1]}.{f[2]}" == fake_pfx):
            sys.exit(f"ip_map.subnets: bad entry (need {real_pfx}.N.0/24 -> {fake_pfx}.M.0/24)")
        subnets[m[3]] = f[3]
    hosts: dict[tuple[str, str], tuple[str, str]] = {}
    for real, fake in spec["hosts"].items():
        m, f = ip.match(real), ip.match(fake)
        if not (m and f and f"{m[1]}.{m[2]}" == real_pfx and f"{f[1]}.{f[2]}" == fake_pfx):
            sys.exit(f"ip_map.hosts: bad entry '{real}' -> '{fake}'")
        if subnets.get(m[3]) != f[3]:
            sys.exit(f"ip_map.hosts: '{fake}' is not in the fake subnet of '{real}'")
        hosts[(m[3], m[4])] = (f[3], f[4])
    # A second real address landing on one fake would merge two machines; a fake octet that
    # equals any real one would keep a real number visible.
    if len(set(subnets.values())) != len(subnets) or len(set(hosts.values())) != len(hosts):
        sys.exit("ip_map: two real subnets or hosts share one fake value")
    real_thirds = {t for t, _ in hosts} | set(subnets)
    real_fourths = {f for _, f in hosts}
    for (rt, rf), (ft, ff) in hosts.items():
        if ft in real_thirds or ff in real_fourths:
            sys.exit(f"ip_map: fake {fake_pfx}.{ft}.{ff} reuses a real octet")
    if set(subnets.values()) & real_thirds:
        sys.exit("ip_map: a fake subnet number equals a real one")

    def host(third: str, fourth: str):
        return hosts.get((third, fourth))

    def one(m: re.Match) -> str:
        third, fourth, rng_end, tail = m.groups()
        elems = re.findall(r"/(\.?)(\d+)", tail)
        # `/N` with N <= 32 and no dot is a prefix length; everything else is a host number.
        cut = next((i for i, (dot, n) in enumerate(elems) if not dot and int(n) <= 32), len(elems))
        out_tail = ""
        prefix_len = None
        if cut < len(elems):
            prefix_len = int(elems[cut][1])
            out_tail = "".join(f"/{d}{n}" for d, n in elems[cut:])
        if third == "x":
            return f"{fake_pfx}.x.x{out_tail}" if fourth == "x" and not elems and not rng_end else m.group(0)
        if fourth == "x":
            return f"{fake_pfx}.{subnets[third]}.x{out_tail}" if third in subnets and not rng_end and not cut else m.group(0)
        if fourth == "0" and prefix_len in (8, 16, 24) and cut == 0 and not rng_end:
            if prefix_len == 24 and third in subnets:
                return f"{fake_pfx}.{subnets[third]}.0{out_tail}"
            if prefix_len <= 16 and third == "0":
                return f"{fake_pfx}.0.0{out_tail}"
            return m.group(0)
        h = host(third, fourth)
        if h is None:
            return m.group(0)
        res = f"{fake_pfx}.{h[0]}.{h[1]}"
        if rng_end:
            e = host(third, rng_end)
            # A range stays a range only if the fake end sits the same distance away.
            if e is None or int(e[1]) - int(h[1]) != int(rng_end) - int(fourth):
                return m.group(0)
            res += f"-{e[1]}"
        for dot, n in elems[:cut]:
            e = host(third, n)
            if e is None:
                return m.group(0)
            res += f"/{dot}{e[1]}"
        return res + out_tail

    rx = lan_rx(real_pfx)
    return lambda text: rx.sub(one, text)


def verify_lan(root: Path, cfg: dict) -> int:
    """Fail on any home-LAN address that survived: it had no entry in `ip_map`."""
    spec = cfg.get("ip_map")
    if not spec:
        return 0
    rx = lan_rx(spec["prefix"][0])
    failures = 0
    for p in iter_files(root):
        if not is_text(p):
            continue
        for n, line in enumerate(read_text_any(p).splitlines(), 1):
            for m in rx.finditer(line):
                print(f"  FAIL {p.relative_to(root)}:{n}  [LAN address without ip_map entry] {m.group(0)}")
                failures += 1
    return failures


def build_substituter(cfg: dict):
    """The global rewrite: substitutions, then substitution_patterns, then the redactions."""
    redact_pats = [(re.compile(r["pattern"]), r["replacement"])
                   for r in (cfg.get("redact_patterns") or [])]
    redact_keys = cfg.get("redact_keys") or []

    def redact(text: str) -> str:
        for rx, dst in redact_pats:
            text = rx.sub(dst, text)
        for key in redact_keys:
            text = re.sub(rf'(^\s*{re.escape(key)}\s*:\s*)\S.*$', r'\1CHANGEME', text, flags=re.M)
        return text

    lan = build_lan_mapper(cfg) or (lambda text: text)
    return _rewriter(cfg.get("keep") or [], cfg.get("substitutions"),
                     cfg.get("substitution_patterns"), extra=lambda text: redact(lan(text)))


def _glob_rx(glob: str) -> re.Pattern:
    """Compile one path glob. `**` crosses directory separators, `*` and `?` do not.

    Hand-rolled rather than pathlib.glob or fnmatch: pathlib's `dir/**` yields directories
    only, so a scope written that way matches no FILE at all, and fnmatch's `*` happily
    crosses `/`. Both fail quietly in opposite directions.
    """
    out, i = [], 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def build_path_substituters(cfg: dict, root: Path):
    """Scoped rewrites: each entry's rules apply only to the files its globs match.

    For strings too short or too common to rewrite everywhere -- a trigram that also occurs
    inside base64 ciphertext, a bare four-digit extension an image digest can contain.
    Globs match the repo-relative POSIX path inside the BUILT tree, so they see it as
    delete_paths and blank_files left it.

    Scoped sources are deliberately NOT added to verify()'s denylist: surviving outside the
    scope is the point. That makes an empty glob invisible, so it is reported loudly here --
    a rule matching nothing after a rename is sanitisation switching itself off.
    """
    files = [(p, p.relative_to(root).as_posix()) for p in iter_files(root)]
    scopes = []
    for entry in cfg.get("path_substitutions") or []:
        matched: set[Path] = set()
        for g in entry.get("paths") or []:
            rx = _glob_rx(g)
            hits = {p for p, rel in files if rx.match(rel)}
            if not hits:
                print(f"  WARNING  path_substitutions: '{g}' matched no file -- renamed or "
                      "moved? Nothing was scoped-substituted for it.")
            matched |= hits
        if matched:
            scopes.append((matched, _rewriter(cfg.get("keep") or [],
                                              entry.get("substitutions"),
                                              entry.get("patterns"))))
    return scopes


def convert_sops(path: Path) -> bool:
    """Turn a SOPS-encrypted Secret into a readable plaintext example.

    No decryption involved, and none possible: `encrypted_regex: ^(data|stringData)$`
    leaves key names in the clear and appends `sops:` as a trailing top-level block. So we
    drop that block and swap each ENC[...] value for a named placeholder. This also removes
    sops.lastmodified (an activity timeline across the estate) and means the published repo
    carries no ciphertext at all — nothing to decrypt retroactively if the age private key
    ever leaks.
    """
    text = read_text_any(path)
    if SOPS_MARKER not in text:
        return False

    out, in_sops = [], False
    for line in text.splitlines():
        if re.match(r"^sops:\s*$", line):
            in_sops = True
            continue
        if in_sops:
            # The sops block is the trailing top-level key; it ends at the next unindented
            # line. A `---` is one of those and must fall through to be emitted -- dropping it
            # merges the documents, and multi-Secret files then fail yamllint on duplicate keys.
            if line and not line[0].isspace():
                in_sops = False
            else:
                continue
        # SOPS encrypts comments in the matched section too, emitting bare `#ENC[...]`
        # lines with no key. They carry no recoverable meaning once encrypted, so drop them
        # rather than leave ciphertext in a repo that is supposed to contain none.
        if re.match(r"^\s*#ENC\[AES256_GCM", line):
            continue
        m = re.match(r'^(\s*)([\w.\-]+):\s*(?:"|\')?ENC\[AES256_GCM.*$', line)
        out.append(f"{m.group(1)}{m.group(2)}: CHANGEME-{m.group(2)}" if m else line)

    body = "\n".join(out).rstrip() + "\n"
    header = (
        "# EXAMPLE — placeholder values. In the source repo this file is SOPS-encrypted\n"
        "# (age, `encrypted_regex: ^(data|stringData)$`). Replace each CHANGEME-* and\n"
        "# encrypt with `sops -e -i` before applying.\n"
    )
    path.write_text(header + body, encoding="utf-8", newline="\n")
    return True


# ---------------------------------------------------------------- verification

# Pinned: a mutable tag would change the gate's behaviour unreviewed, and the image is mounted
# over the whole tree. Bump version and digest together by hand (scripts/ is not scanned by Renovate).
GITLEAKS_IMAGE = "zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"


def run_gitleaks(root: Path, cfg: dict) -> int:
    """Independent secret scan, containerised. Returns the failure count.

    Fails closed: no docker, a crashed container, a missing or unparseable report all count
    as a failure, never as a pass (--skip-gitleaks opts out explicitly).

    Worth the extra step: the hand-written config only removes what someone thought of,
    and gitleaks is what found the MapTiler API key hiding in a URL query parameter.
    """
    if not shutil.which("docker"):
        print("  FAIL docker not found: gitleaks did not run (install docker, or pass --skip-gitleaks)")
        return 1

    # Keyed on file, rule and a hash of the matched secret: a finding of the same rule in the
    # same file but with another value is NOT admitted, and a shifting line number does not matter.
    allow = {(a["file"], a["rule"], a["secret_sha256"]): a.get("reason", "")
             for a in (cfg.get("gitleaks_allowlist") or [])}

    # `-r /dev/stdout` produces nothing in this image, so the report goes to a file. It is
    # written to the scratch parent, not into the tree, so it can never reach the branch. It is
    # not redacted (the hash needs the value) and is deleted below. MSYS_NO_PATHCONV stops Git
    # Bash rewriting /repo into a Windows path.
    report = root.parent / "gitleaks.json"
    report.unlink(missing_ok=True)
    try:
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{root}:/repo", "-v", f"{root.parent}:/out",
             GITLEAKS_IMAGE, "detect", "--source=/repo", "--no-git",
             "-f", "json", "-r", "/out/gitleaks.json"],
            capture_output=True, text=True,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )
        # 0: clean. 1: findings (gitleaks' default --exit-code). Anything else is a failure to scan.
        if r.returncode not in (0, 1):
            print(f"  FAIL gitleaks exited {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
            return 1
        if not report.exists():
            print("  FAIL gitleaks produced no report")
            return 1
        try:
            findings = json.loads(report.read_text(encoding="utf-8") or "null")
        except json.JSONDecodeError:
            print("  FAIL could not parse the gitleaks report")
            return 1
        if not isinstance(findings, list):
            print("  FAIL the gitleaks report is not a list")
            return 1
    finally:
        report.unlink(missing_ok=True)
    if (r.returncode == 1) != bool(findings):
        print(f"  FAIL gitleaks exit code {r.returncode} disagrees with its report ({len(findings)} finding(s))")
        return 1

    failures, used = 0, set()
    for f in findings:
        name = f.get("File", "").removeprefix("/repo/")
        digest = hashlib.sha256(f.get("Secret", "").encode()).hexdigest()
        key = (name, f.get("RuleID", ""), digest)
        if key in allow:
            used.add(key)
            print(f"   allowed  {name}:{key[1]}  ({allow[key]})")
            continue
        print(f"  FAIL {name}:{f.get('StartLine')}  [gitleaks {f.get('RuleID')}]  secret_sha256 {digest}")
        failures += 1
    for key in sorted(set(allow) - used):
        print(f"   note: allowlist entry {key[0]}:{key[1]} matched nothing; remove it")
    if not failures:
        print(f"   clean: {len(findings)} finding(s), all allowlisted")
    return failures


def verify(root: Path, cfg: dict, secrets: list[str]) -> int:
    """Assert nothing forbidden survived. Returns the number of failures."""
    needles: list[tuple[str, str]] = []
    needles += [(a, "substitution source") for a, _ in (cfg.get("substitutions") or [])]
    needles += [(n, "must_not_appear") for n in (cfg.get("must_not_appear") or [])]
    needles += [(s, "extracted secret") for s in secrets]
    needles.append((SOPS_MARKER + ",data:", "SOPS ciphertext"))  # convert_sops promises none survives

    keeps = cfg.get("keep") or []
    failures = 0

    # Shape-based rules are verified by non-match rather than by denylisted value: if the
    # pattern still fires, an embedded credential survived -- including one added since
    # this config was written.
    for rule in cfg.get("redact_patterns") or []:
        rx = re.compile(rule["pattern"])
        for p in iter_files(root):
            if not is_text(p):
                continue
            for n, line in enumerate(read_text_any(p).splitlines(), 1):
                if rx.search(line):
                    print(f"  FAIL {p.relative_to(root)}:{n}  [redact_pattern still matches]")
                    failures += 1

    for p in iter_files(root):
        if not is_text(p):
            continue
        text = read_text_any(p)
        lower = text.lower()
        for needle, kind in needles:
            if needle.lower() not in lower:
                continue
            # An allowed API group legitimately contains a substituted domain.
            for line_no, line in enumerate(text.splitlines(), 1):
                if needle.lower() not in line.lower():
                    continue
                # Mask each kept API group / finalizer, then look again: a real hostname or
                # secret sharing the line with one must still be reported.
                masked = line.lower()
                for k in keeps:
                    masked = masked.replace(k.lower(), "<kept>")
                if needle.lower() not in masked:
                    continue
                shown = needle if kind != "extracted secret" else f"<{kind}, {len(needle)} chars>"
                print(f"  FAIL {p.relative_to(root)}:{line_no}  [{kind}] {shown}")
                failures += 1
    return failures


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="scripts/public.config.yaml")
    ap.add_argument("--branch", default="public-template")
    ap.add_argument("--keep-scratch", action="store_true",
                    help="leave the scratch tree in place for inspection")
    ap.add_argument("--skip-gitleaks", action="store_true",
                    help="do not run the gitleaks gate (it otherwise fails when docker is missing)")
    ap.add_argument("--no-commit", action="store_true",
                    help="build and verify only; do not create the branch")
    args = ap.parse_args()

    cfg_path = REPO / args.config
    if not cfg_path.exists():
        return print(f"config not found: {cfg_path}") or 1
    # Newlines normalised first: a CRLF working tree (git autocrlf on Windows) makes
    # PyYAML count the CR and the LF as two line breaks, which doubles every line of
    # a `content: |` block and ships a blank line between each one.
    cfg = yaml.safe_load(_lf(read_text_any(cfg_path)))

    scratch = Path(tempfile.mkdtemp(prefix="k0s-flux-public-"))
    tree = scratch / "tree"
    tree.mkdir()
    try:
        # git archive exports tracked files at HEAD only — ignored .cache/ and
        # .claude/worktrees/ (a full second copy of the repo) cannot come along.
        print("== export HEAD")
        tar = scratch / "head.tar"
        with tar.open("wb") as fh:
            subprocess.run(["git", "archive", "--format=tar", "HEAD"],
                           cwd=REPO, check=True, stdout=fh)
        shutil.unpack_archive(str(tar), str(tree), format="tar")
        tar.unlink()
        print(f"   {sum(1 for _ in iter_files(tree))} files")

        print("== extract secret values for the denylist (in memory only)")
        secrets = extract_secrets(tree, cfg)
        print(f"   {len(secrets)} value(s) found via redact_keys/redact_patterns")

        print("== delete paths")
        apply_deletes(tree, cfg, args.config)

        print("== delete resources")
        apply_delete_resources(tree, cfg)

        print("== blank files")
        apply_blank_files(tree, cfg)

        print("== convert SOPS secrets to examples")
        n_sops = sum(1 for p in iter_files(tree) if p.suffix in {".yaml", ".yml"} and convert_sops(p))
        print(f"   {n_sops} converted")
        # Tells the checks they run on the template (scripts/check-sops.py: no encrypted files).
        (tree / ".public-template").write_text(
            "Built by scripts/make-public.py; secrets are CHANGEME examples, not encrypted.\n",
            encoding="utf-8", newline="\n")

        print("== substitute")
        substitute = build_substituter(cfg)
        # Scoped rules run AFTER the global ones, so a global rule still sees the
        # original text: a trunk hostname is rewritten whole before any scoped rule for
        # a bare word inside it could break it apart. Naming the real hostname here would
        # make this comment rewrite itself.
        scopes = build_path_substituters(cfg, tree)
        changed = 0
        skipped = []
        for p in iter_files(tree):
            if not is_text(p):
                skipped.append(p.relative_to(tree))
                continue
            before = read_text_any(p)
            after = substitute(before)
            for paths, scoped in scopes:
                if p in paths:
                    after = scoped(after)
            if after != before:
                p.write_text(after, encoding="utf-8", newline="\n")
                changed += 1
        print(f"   {changed} file(s) rewritten")
        # Skipped files are not verified either, so name them rather than let a
        # misclassification pass as a clean run.
        if skipped:
            print(f"   {len(skipped)} treated as binary and NOT sanitised or verified:")
            for s in skipped:
                print(f"     {s}")

        print("== verify: denylist")
        failures = verify(tree, cfg, secrets)

        print("== verify: LAN addresses")
        failures += verify_lan(tree, cfg)

        print("== verify: gitleaks")
        if args.skip_gitleaks:
            print("   SKIPPED (--skip-gitleaks): run gitleaks manually before publishing")
        else:
            failures += run_gitleaks(tree, cfg)
        if failures:
            print(f"\n{failures} leak(s) found — nothing was committed.")
            print(f"Scratch tree kept for inspection: {tree}")
            args.keep_scratch = True
            return 1
        print("   clean: no forbidden string survived")

        if args.no_commit:
            print(f"\n--no-commit: tree left at {tree}")
            args.keep_scratch = True
            return 0

        # Parent onto the existing branch when there is one, so the published repo keeps a
        # history of snapshots rather than one replaced commit. Only sanitised trees are ever
        # reachable either way -- no commit from this repo becomes an ancestor.
        prior = run("git", "-C", str(REPO), "rev-parse", "--verify", "-q",
                    f"refs/heads/{args.branch}", check=False).strip()
        print(f"== commit branch '{args.branch}'"
              + (f" on top of {prior[:12]}" if prior else " (first snapshot)"))
        run("git", "-C", str(tree), "init", "-q", "-b", args.branch)
        if prior:
            run("git", "-C", str(tree), "fetch", "-q", "--no-tags", str(REPO),
                f"{args.branch}:refs/remotes/prior/{args.branch}")
            run("git", "-C", str(tree), "update-ref", "HEAD", prior)
        run("git", "-C", str(tree), "add", "-A")
        run("git", "-C", str(tree), "-c", "user.name=make-public",
            "-c", "user.email=make-public@localhost",
            "commit", "-q", "--allow-empty", "-m",
            "k0s-flux template\n\n"
            "Sanitised snapshot generated by scripts/make-public.py. Placeholder domains, "
            "addresses and secrets throughout. Source history is never included.")
        run("git", "-C", str(REPO), "fetch", "-q", "--no-tags",
            str(tree), f"+{args.branch}:{args.branch}")

        print(f"\nBranch '{args.branch}' created in {REPO}.")
        print("Review it, then push. Each run commits on top of the last, so this is a")
        print("fast-forward -- never publish THIS repo, which would expose main and all")
        print("of its history:")
        print(f"    git push https://github.com/<you>/<public-repo>.git {args.branch}:main")
        return 0
    finally:
        if args.keep_scratch:
            print(f"(scratch: {scratch})")
        else:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
