"""Shared by the check scripts: which clusters exist, and what an ArtifactGenerator copies.

A cluster is any top-level <dir>/flux-system/kustomization.yaml: what Flux applies there. Its
generator root is found by content, not file name: the HelmRelease in flux-system/ whose values
have `groups`, and the ArtifactGenerator copying Chart.yaml. A cluster without them has no
generated components.

copy() mirrors source-watcher's internal/builder for the forms this repo uses:
- a path without glob characters must exist; a file goes into `to` (a dir if it ends in /),
  a directory keeps its own name under `to`;
- a glob is fs.Glob (one level per *, no **), must match, and keeps the full matched path;
- an exclude matches the path from the source root, the path from the copied directory,
  and, without a "/", the bare file name.
The source is the git tree (`git ls-files`), as source-controller checks it out.
"""
import fnmatch, pathlib, posixpath, re, subprocess

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
GLOB = re.compile(r"[*?\[\]]")


def clusters():
    return sorted(p.parent.parent.name for p in ROOT.glob("*/flux-system/kustomization.yaml"))


def components(cluster):
    """(component dir, group) for every component the cluster's groups discover."""
    out = []
    for g in values(cluster).get("groups") or []:
        out += [(k.parent.relative_to(ROOT).as_posix(), g) for k in sorted((ROOT / g["base"]).glob("*/kustomization.yaml"))]
    return out


def docs(path):
    return [d for d in yaml.safe_load_all((ROOT / path).read_text(encoding="utf-8")) if d]


def _flux_docs(cluster, kind):
    """(repo path, doc) of every `kind` object in <cluster>/flux-system/*.yaml."""
    out = []
    for p in sorted((ROOT / cluster / "flux-system").glob("*.yaml")):
        if f"kind: {kind}" not in p.read_text(encoding="utf-8"):
            continue  # skips gotk-components.yaml's CRDs cheaply
        rel = p.relative_to(ROOT).as_posix()
        out += [(rel, d) for d in docs(rel) if d.get("kind") == kind]
    return out


def _one(found, what, cluster):
    if len(found) > 1:
        raise SystemExit(f"{cluster}/flux-system: more than one {what}: {', '.join(p for p, _ in found)}")
    return found[0] if found else (None, None)


def root_release(cluster):
    """(repo path, doc) of the generator's root HelmRelease, or (None, None)."""
    found = [(p, d) for p, d in _flux_docs(cluster, "HelmRelease")
             if "groups" in ((d.get("spec") or {}).get("values") or {})]
    return _one(found, "generator root HelmRelease (values.groups)", cluster)


def root_artifact(cluster):
    """(repo path, doc) of the ArtifactGenerator that builds the root chart, or (None, None)."""
    found = [(p, d) for p, d in _flux_docs(cluster, "ArtifactGenerator")
             if any(op.get("from") == "@repo/Chart.yaml"
                    for a in (d.get("spec") or {}).get("artifacts") or [] for op in a.get("copy") or [])]
    return _one(found, "root ArtifactGenerator (copies Chart.yaml)", cluster)


def values(cluster):
    _, rel = root_release(cluster)
    return rel["spec"]["values"] if rel else {}


def tracked():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True)
    return {f for f in out.stdout.split("\0") if f and (ROOT / f).is_file()}


def _match(pattern, path):
    """fs.Glob/doublestar for single-level patterns: * never crosses a /."""
    p, s = pattern.split("/"), path.split("/")
    return len(p) == len(s) and all(fnmatch.fnmatchcase(b, a) for a, b in zip(p, s))


def _excluded(path, base, excludes):
    rel = posixpath.relpath(path, base) if base not in (".", "") else path
    for pat in excludes:
        for candidate in {path, rel}:
            if _match(pat, candidate) or ("/" not in pat and fnmatch.fnmatchcase(posixpath.basename(path), pat)):
                return True
    return False


def copy(op, files):
    """{artifact path: repo path} for one copy operation; raises ValueError where source-watcher fails."""
    src = op["from"].removeprefix("@repo/")
    to = op["to"].removeprefix("@artifact/")
    excludes = op.get("exclude") or []
    out = {}

    def tree(path, dest, base):
        hit = False
        for f in sorted(files):
            if f == path or f.startswith(path + "/"):
                hit = True
                # WalkDir skips an excluded directory with everything below it.
                parts = f[len(path):].strip("/").split("/") if f != path else []
                walked = [posixpath.join(path, *parts[:i]) for i in range(1, len(parts) + 1)] or [f]
                if not any(_excluded(w, base, excludes) for w in walked):
                    out[dest if f == path else posixpath.join(dest, posixpath.relpath(f, path))] = f
        return hit

    if not GLOB.search(src):
        src = src.rstrip("/")
        if src in files:
            if not _excluded(src, ".", excludes):
                out[posixpath.join(to, posixpath.basename(src)) if op["to"].endswith("/") else to] = src
        elif not tree(src, posixpath.join(to, posixpath.basename(src)), src):
            raise ValueError(f"source '{src}' does not exist")
        return out

    depth = src.count("/") + 1
    matches = {"/".join(f.split("/")[:depth]) for f in files if f.count("/") + 1 >= depth}
    matches = sorted(m for m in matches if _match(src, m))
    if not matches:
        raise ValueError(f"no files match pattern '{src}'")
    base = _prefix(src)
    for m in matches:
        if _excluded(m, base, excludes):
            continue
        tree(m, posixpath.join(to, m), base)
    return out


def _prefix(pattern):
    parts = []
    for part in pattern.split("/"):
        if GLOB.search(part):
            break
        parts.append(part)
    return "/".join(parts) or "."


def artifact(spec, files):
    """{artifact path: repo path} for one artifact of an ArtifactGenerator."""
    out = {}
    for op in spec.get("copy") or []:
        try:
            out.update(copy(op, files))
        except ValueError as e:
            raise ValueError(f"{spec['name']}: {e}") from None
    return out


if __name__ == "__main__":
    # For the shell scripts: `clusters.py` lists the clusters, `clusters.py components [CLUSTER]`
    # their component dirs, `clusters.py k0s-upgrade CLUSTER` its k0sUpgrade.path if any.
    import sys
    sys.stdout.reconfigure(newline="\n")  # no \r for bash on Windows
    if sys.argv[1:2] == ["k0s-upgrade"]:
        path = (values(sys.argv[2]).get("k0sUpgrade") or {}).get("path")
        if path:
            print(path)
    elif sys.argv[1:2] == ["components"]:
        for c in sys.argv[2:] or clusters():
            for d, _ in components(c):
                print(d)
    else:
        for c in clusters():
            print(c)
