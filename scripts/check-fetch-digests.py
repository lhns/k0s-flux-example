#!/usr/bin/env python3
"""Every pinned download's digest must be the sha256 of what its url serves.

Covers each oci-composer `fetch:`, where a mismatch is terminal in the composer, and each shell
`url=` line followed by `digest=sha256:...` (apps/homeassistant). Renovate bumps the url
(renovate.json customManagers), not the digest. A release's checksums.txt is checked too.

  check-fetch-digests.py          verify every fetch (scripts/validate.sh); network, cached
  check-fetch-digests.py --fix    rewrite the digest of every url/digest pair not already in HEAD
                                  (renovate.json postUpgradeTasks)

Verified url+digest pairs are cached in .cache/fetch-digests, so a warm run needs no network.
"""
import hashlib
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import clusters

ROOT = clusters.ROOT
CACHE = ROOT / ".cache" / "fetch-digests"
KINDS = ("ImageComposition", "ImageBuild")
SHELL_PAIR = re.compile(r"^[ \t]*url=(\S+)[ \t]*\r?\n[ \t]*digest=(sha256:[0-9a-f]{64})[ \t]*$", re.M)


def fetches(doc):
    spec = doc.get("spec") or {}
    if doc["kind"] == "ImageComposition":
        for layer in spec.get("layers") or []:
            if isinstance(layer, dict) and isinstance(layer.get("fetch"), dict):
                yield layer["fetch"]
    else:
        f = (spec.get("context") or {}).get("fetch")
        if isinstance(f, dict):
            yield f


def collect(text):
    """(url, digest) of every fetch in one file's text."""
    out = SHELL_PAIR.findall(text)
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        if re.search(r"kind: (ImageComposition|ImageBuild)", text):
            raise
        return out
    for d in docs:
        if isinstance(d, dict) and d.get("kind") in KINDS:
            out += [(f.get("url"), f.get("digest")) for f in fetches(d)]
    return out


def at_head(path):
    try:
        rel = path.relative_to(ROOT).as_posix()
        text = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return set()
    return set(collect(text))


def get(url):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as r:
                h = hashlib.sha256()
                while chunk := r.read(1 << 20):
                    h.update(chunk)
                return "sha256:" + h.hexdigest()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            err = e
        except (urllib.error.URLError, TimeoutError) as e:
            err = e
        time.sleep(5 * (attempt + 1))
    raise err


def published(url):
    """The digest a GitHub release's checksums.txt lists for this asset, if it has one."""
    if "/releases/download/" not in url:
        return None
    base, name = url.rsplit("/", 1)
    try:
        with urllib.request.urlopen(f"{base}/checksums.txt", timeout=60) as r:
            lines = r.read().decode().splitlines()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return "sha256:" + parts[0].lower()
    return None


def actual(url):
    got = get(url)
    want = published(url)
    if want and want != got:
        raise ValueError(f"download is {got}, but checksums.txt lists {want}")
    return got


def rewrite(path, url, digest):
    """Set the `digest:` (or `digest=`) that follows `url: <url>` in the same block."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)url[:=]\s*" + re.escape(url) + r"\s*$", line)
        if not m:
            continue
        indent = m.group(1)
        for j in range(i + 1, len(lines)):
            if lines[j].strip() and not lines[j].startswith(indent):
                break  # left the mapping
            d = re.match(r"^(" + indent + r"digest[:=]\s*)\S+", lines[j])
            if d:
                lines[j] = d.group(1) + digest + lines[j][d.end():]
                break
    path.write_text("".join(lines), encoding="utf-8", newline="")


def main():
    fix = "--fix" in sys.argv[1:]
    cached = set(CACHE.read_text().split("\n")) if CACHE.exists() else set()
    problems, ok, checked = [], [], 0
    for path in sorted(p for tree in clusters.clusters() + ["shared"] for p in (ROOT / tree).rglob("*.yaml")):
        found = collect(path.read_text(encoding="utf-8"))
        if not found:
            continue
        skip = at_head(path) if fix else set()
        for url, digest in found:
            where = f"{path.relative_to(ROOT)}: {url}"
            if not url or not digest:
                problems.append(f"{where}: fetch needs both url and digest")
                continue
            if f"{url} {digest}" in cached or (url, digest) in skip:
                continue
            checked += 1
            try:
                got = actual(url)
            except urllib.error.HTTPError as e:
                gone = e.code in (404, 410)
                if gone and "/pool/" in url:
                    problems.append(f"{where}: {e.code}, Debian dropped this pool file; re-pin")
                else:
                    problems.append(f"{where}: {e}" + ("; upstream no longer serves it, re-pin" if gone else ""))
                continue
            except (OSError, ValueError) as e:
                problems.append(f"{where}: {e}")
                continue
            if got != digest and fix:
                rewrite(path, url, got)
                print(f"{where}: digest -> {got}")
                digest = got
            if got != digest:
                problems.append(f"{where}: digest is {digest}, url serves {got}")
                continue
            ok.append(f"{url} {digest}")

    if ok:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text("\n".join(sorted((cached | set(ok)) - {""})) + "\n")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        if not fix:
            print("\nfix: python3 scripts/check-fetch-digests.py --fix", file=sys.stderr)
        return 1
    print(f"fetch digests OK ({checked} downloaded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
