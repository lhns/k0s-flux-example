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


def build_substituter(cfg: dict):
    """Literal substitutions, longest-first, skipping anything inside a `keep` string.

    Longest-first matters: a shorter pattern that is a substring of a longer one would
    otherwise mangle it. `keep` entries are protected by masking them out first, because
    they are API groups and finalizers that merely look like hostnames — rewriting one
    breaks every manifest that references it.
    """
    keeps = sorted(cfg.get("keep") or [], key=len, reverse=True)
    pairs = sorted(
        ((a, b) for a, b in (cfg.get("substitutions") or [])),
        key=lambda ab: len(ab[0]), reverse=True,
    )
    patterns = [(re.compile(r["pattern"]), r["replacement"])
                for r in (cfg.get("substitution_patterns") or [])]
    redact_pats = [(re.compile(r["pattern"]), r["replacement"])
                   for r in (cfg.get("redact_patterns") or [])]
    redact_keys = cfg.get("redact_keys") or []

    def substitute(text: str) -> str:
        # Mask keeps with sentinels that no substitution can match.
        masks: dict[str, str] = {}
        for i, k in enumerate(keeps):
            if k in text:
                token = f"\x00KEEP{i}\x00"
                masks[token] = k
                text = text.replace(k, token)

        for src, dst in pairs:
            text = text.replace(src, dst)
        for rx, dst in patterns:
            text = rx.sub(dst, text)
        for rx, dst in redact_pats:
            text = rx.sub(dst, text)
        for key in redact_keys:
            text = re.sub(rf'(^\s*{re.escape(key)}\s*:\s*)\S.*$', r'\1CHANGEME', text, flags=re.M)

        for token, original in masks.items():
            text = text.replace(token, original)
        return text

    return substitute


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
            # the sops block is the trailing top-level key; it ends at the next
            # unindented line (in practice, EOF or a document separator)
            if line and not line[0].isspace() and not line.startswith("---"):
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

def run_gitleaks(root: Path, cfg: dict) -> int:
    """Independent secret scan, containerised. Returns failure count (0 if unavailable).

    Worth the extra step: the hand-written config only removes what someone thought of,
    and gitleaks is what found the MapTiler API key hiding in a URL query parameter.
    """
    if not shutil.which("docker"):
        print("   docker not found - SKIPPED (run gitleaks manually before publishing)")
        return 0

    allow = {a["fingerprint"]: a.get("reason", "") for a in (cfg.get("gitleaks_allowlist") or [])}

    # `-r /dev/stdout` produces nothing in this image, so the report goes to a file. It is
    # written to the scratch parent, not into the tree, so it can never reach the branch.
    # MSYS_NO_PATHCONV stops Git Bash rewriting /repo into a Windows path.
    report = root.parent / "gitleaks.json"
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{root}:/repo", "-v", f"{root.parent}:/out",
         "zricethezav/gitleaks:latest", "detect", "--source=/repo", "--no-git",
         "--redact", "-f", "json", "-r", "/out/gitleaks.json"],
        capture_output=True, text=True,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    if not report.exists():
        print("   gitleaks produced no report - SKIPPED (run it manually before publishing)")
        return 0
    try:
        findings = json.loads(report.read_text(encoding="utf-8") or "[]") or []
    except json.JSONDecodeError:
        print("   could not parse gitleaks report - SKIPPED")
        return 0
    finally:
        report.unlink(missing_ok=True)

    failures = 0
    for f in findings:
        fp = f.get("Fingerprint", "")
        # allowlist keyed on file:rule, so a line number shifting does not silently
        # re-admit a real finding under a stale fingerprint
        key = f"{f.get('File','')}:{f.get('RuleID','')}"
        if key in allow:
            print(f"   allowed  {key}  ({allow[key]})")
            continue
        print(f"  FAIL {f.get('File')}:{f.get('StartLine')}  [gitleaks {f.get('RuleID')}]")
        failures += 1
    if not failures:
        print(f"   clean: {len(findings)} finding(s), all allowlisted")
    return failures


def verify(root: Path, cfg: dict, secrets: list[str]) -> int:
    """Assert nothing forbidden survived. Returns the number of failures."""
    needles: list[tuple[str, str]] = []
    needles += [(a, "substitution source") for a, _ in (cfg.get("substitutions") or [])]
    needles += [(n, "must_not_appear") for n in (cfg.get("must_not_appear") or [])]
    needles += [(s, "extracted secret") for s in secrets]

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
                if any(k in line for k in keeps):
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
    ap.add_argument("--no-commit", action="store_true",
                    help="build and verify only; do not create the branch")
    args = ap.parse_args()

    cfg_path = REPO / args.config
    if not cfg_path.exists():
        return print(f"config not found: {cfg_path}") or 1
    cfg = yaml.safe_load(read_text_any(cfg_path))

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

        print("== substitute")
        substitute = build_substituter(cfg)
        changed = 0
        skipped = []
        for p in iter_files(tree):
            if not is_text(p):
                skipped.append(p.relative_to(tree))
                continue
            before = read_text_any(p)
            after = substitute(before)
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

        print("== verify: gitleaks")
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

        print(f"== commit orphan branch '{args.branch}'")
        run("git", "-C", str(tree), "init", "-q", "-b", args.branch)
        run("git", "-C", str(tree), "add", "-A")
        run("git", "-C", str(tree), "-c", "user.name=make-public",
            "-c", "user.email=make-public@localhost",
            "commit", "-q", "-m",
            "k0s-flux template\n\n"
            "Sanitised snapshot generated by scripts/make-public.py. Placeholder domains, "
            "addresses and secrets throughout; no history.")
        run("git", "-C", str(REPO), "fetch", "-q", "--no-tags",
            str(tree), f"+{args.branch}:{args.branch}")

        print(f"\nBranch '{args.branch}' created in {REPO}.")
        print("Review it, then push to a SEPARATE empty repo — publishing this one would")
        print("expose main and all 799 commits:")
        print(f"    git push git@github.com:<you>/<new-repo>.git {args.branch}:main")
        return 0
    finally:
        if args.keep_scratch:
            print(f"(scratch: {scratch})")
        else:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
