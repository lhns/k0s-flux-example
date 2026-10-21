#!/usr/bin/env python3
"""Redirect routes answer from Traefik itself and match any Host form.

Traefik's Host() matcher strips the port from `Host: alias:443`, but redirectRegex matches the
raw URL, which keeps it. A regex naming the host then does not match, and a redirect-only route
whose service is the real app forwards the request there, past Authelia and Anubis. Per cluster,
over every component build, this fails on:

 1. an IngressRoute route whose middlewares are all redirectRegex/redirectScheme (a chain of
    them counts), that redirect every request the route matches (a conditional one, like
    /web -> /web/index.html, is not a redirect route), and whose service is not
    `noop@internal` with kind TraefikService
 2. a redirectRegex whose regex contains a literal hostname; match any host with [^/]+

KUSTOMIZE_BUILDS is validate.sh's build directory; without it every build is run here.
tests/check-redirects.py runs the same rules over inline fixtures.
"""
import os, pathlib, re, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import clusters

ROOT = clusters.ROOT
REDIRECTS = ("redirectRegex", "redirectScheme")
HOSTNAME = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def meta(d):
    return d.get("metadata") or {}


def spec(d):
    return d.get("spec") or {}


def redirect_only(mws, ns, refs, seen=()):
    """True if the middleware refs (dicts with name, maybe namespace) are all redirects, chains followed."""
    if not refs:
        return False
    for r in refs:
        key = (r.get("namespace") or ns, str(r.get("name", "")).split("@")[0])
        mw = mws.get(key)
        if mw is None or key in seen:
            return False
        s = spec(mw)
        if "chain" in s:
            if not redirect_only(mws, key[0], (s["chain"] or {}).get("middlewares"), seen + (key,)):
                return False
        elif not any(k in s for k in REDIRECTS) or set(s) - set(REDIRECTS):
            return False
    return True


def probes(match):
    """URLs the route's match admits: its first Host and Path/PathPrefix values, else a root and a deep path with a query."""
    paths = [m.group(2) + ("x" if m.group(1) == "PathPrefix" else "")
             for m in re.finditer(r"(Path|PathPrefix)\(`([^`]+)`\)", match or "")]
    host = next(iter(re.findall(r"Host\(`([^`]+)`\)", match or "")), "h.example")
    return [f"https://{host}{p}" for p in paths or ["/", "/a/b?c=d"]]


def redirects_all(mws, ns, refs, url):
    """True if some middleware of the (redirect-only) refs redirects url."""
    for r in refs:
        mw = mws[(r.get("namespace") or ns, str(r.get("name", "")).split("@")[0])]
        s = spec(mw)
        if "chain" in s:
            if redirects_all(mws, mw["metadata"]["namespace"], s["chain"]["middlewares"], url):
                return True
        elif "redirectScheme" in s or re.search(s["redirectRegex"].get("regex", ""), url):
            return True
    return False


def findings(docs, dir):
    """One line per finding in one component's build."""
    out = []
    mws = {(meta(d).get("namespace"), meta(d).get("name")): d for d in docs if d.get("kind") == "Middleware"}
    for d in docs:
        where = f"{dir}: {d.get('kind')}/{meta(d).get('name')}"
        if d.get("kind") == "Middleware":
            regex = (spec(d).get("redirectRegex") or {}).get("regex")
            if regex and HOSTNAME.search(regex.replace("\\", "")):
                out.append(f"  {where}: regex {regex!r} names a host; use [^/]+, since Host() ignores a port "
                           f"that the raw URL keeps")
        if d.get("kind") != "IngressRoute":
            continue
        for route in spec(d).get("routes") or []:
            ns, refs = meta(d).get("namespace"), route.get("middlewares")
            if not redirect_only(mws, ns, refs) or                     not all(redirects_all(mws, ns, refs, u) for u in probes(route.get("match"))):
                continue
            for svc in route.get("services") or []:
                if svc.get("name") != "noop@internal" or svc.get("kind") != "TraefikService":
                    out.append(f"  {where}: {route.get('match')}: redirect-only route forwards to {svc.get('name')!r}; "
                               f"use name noop@internal, kind TraefikService")
    return out


def build(dir):
    done = os.environ.get("KUSTOMIZE_BUILDS")
    if done:
        out = pathlib.Path(done, dir.replace("/", "%"))
        if out.is_file() and not out.with_name(out.name + ".failed").exists():
            return out.read_text(encoding="utf-8")
        return None  # a failed build is reported by the kustomize build step
    r = subprocess.run(["kustomize", "build", str(ROOT / dir)], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def main():
    failures, n = [], 0
    for cluster in clusters.clusters():
        for dir, _ in clusters.components(cluster):
            text = build(dir)
            if text is None:
                continue
            docs = [d for d in clusters.load_all(text) if isinstance(d, dict)]
            n += sum(1 for d in docs if d.get("kind") == "Middleware" and "redirectRegex" in spec(d))
            failures += findings(docs, dir)
    if failures:
        print("== redirect check failed ==", file=sys.stderr)
        print(*failures, sep="\n", file=sys.stderr)
        sys.exit(1)
    print(f"{n} redirectRegex middleware(s) match any host; redirect-only routes use noop@internal")


if __name__ == "__main__":
    main()
