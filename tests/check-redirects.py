#!/usr/bin/env python3
"""Offline test of scripts/check-redirects.py: the fixed pattern passes, the old one fails."""
import copy, importlib.util, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("redirects_check", HERE / "scripts/check-redirects.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

API = "traefik.io/v1alpha1"
NOOP = {"name": "noop@internal", "kind": "TraefikService"}


def mw(name, **s):
    return {"apiVersion": API, "kind": "Middleware", "metadata": {"name": name, "namespace": "t"}, "spec": s}


def route(match, services, *middlewares):
    return {"apiVersion": API, "kind": "IngressRoute", "metadata": {"name": "r", "namespace": "t"},
            "spec": {"routes": [{"kind": "Rule", "match": match, "services": services,
                                 "middlewares": [{"name": m} for m in middlewares]}]}}


GOOD = mw("go", redirectRegex={"regex": "^https?://[^/]+/(.*)", "replacement": "https://app.example/${1}"})
OLD = mw("go", redirectRegex={"regex": "^https://alias.example/(.*)", "replacement": "https://app.example/${1}"})
APP = [{"name": "app", "port": 80}]
HOST = "Host(`alias.example`)"
passed = failed = 0


def case(name, expect, *docs):
    """expect: a substring some finding must contain, or None for a clean run."""
    global passed, failed
    got = r.findings(list(copy.deepcopy(docs)), "fixture")
    ok = (not got) if expect is None else any(expect in line for line in got)
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got or 'nothing'}"))
    passed, failed = passed + ok, failed + (not ok)


case("host-agnostic redirect on noop passes", None, GOOD, route(HOST, [NOOP], "go"))
case("old pattern: real service behind a redirect", "forwards to 'app'", GOOD, route(HOST, APP, "go"))
case("old pattern: literal host in the regex", "names a host", OLD, route(HOST, [NOOP], "go"))
case("old pattern, both", "forwards to", OLD, route(HOST, APP, "go"))
case("noop without kind TraefikService", "forwards to", GOOD, route(HOST, [{"name": "noop@internal"}], "go"))
case("chain of redirects is redirect-only", "forwards to", GOOD, mw("ch", chain={"middlewares": [{"name": "go"}]}),
     route(HOST, APP, "ch"))
case("redirectScheme is a redirect", "forwards to", mw("https", redirectScheme={"scheme": "https"}),
     route(HOST, APP, "https"))
case("redirect before forwardAuth is a real route", None, GOOD, mw("auth", forwardAuth={"address": "http://a"}),
     route(HOST, APP, "go", "auth"))
case("no middleware is a real route", None, route(HOST, APP))
case("conditional redirect on a real route", None,
     mw("home", redirectRegex={"regex": "^(https?://[^/]+)/web$", "replacement": "${1}/web/index.html"}),
     route(HOST, APP, "home"))
case("path-scoped redirect on the app", "forwards to",
     mw("p", redirectRegex={"regex": "^https?://[^/]+/app/(.*)", "replacement": "https://app.example/${1}"}),
     route(HOST + " && PathPrefix(`/app/`)", APP, "p"))
case("escaped literal host", "names a host",
     mw("e", redirectRegex={"regex": "^https://alias\\.example", "replacement": "https://x"}))
case("host-agnostic with a path", None,
     mw("p", redirectRegex={"regex": "^https?://[^/]+/(?:app/)?(.*)", "replacement": "https://x/${1}"}))

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
