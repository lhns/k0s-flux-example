#!/usr/bin/env python3
"""Offline test of scripts/check-image-subpaths.py: a directory subPath passes, #1603's file one fails."""
import copy, importlib.util, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("subpaths_check", HERE / "scripts/check-image-subpaths.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def dep(mounts, volumes=None, init=False):
    vols = volumes or [{"name": "mod", "image": {"reference": "r"}}, {"name": "cfg", "configMap": {"name": "c"}}]
    pod = {"containers": [{"name": "c", "volumeMounts": [] if init else mounts}], "volumes": vols}
    if init:
        pod["initContainers"] = [{"name": "i", "volumeMounts": mounts}]
    return {"kind": "Deployment", "metadata": {"name": "d"}, "spec": {"template": {"spec": pod}}}


def mount(name, sub):
    return {"name": name, "mountPath": "/m", "subPath": sub}


passed = failed = 0


def case(name, expect, doc):
    global passed, failed
    got = r.findings([copy.deepcopy(doc)], "fixture")
    ok = (not got) if expect is None else any(expect in line for line in got)
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got or 'nothing'}"))
    passed, failed = passed + ok, failed + (not ok)


case("#1603: file subPath on an image volume", "looks like a file", dep([mount("mod", "mod_auth_lldap.lua")]))
case("nested file subPath", "looks like a file", dep([mount("mod", "a/b/c.so")]))
case("file subPath in an init container", "looks like a file", dep([mount("mod", "x.lua")], init=True))
case("directory subPath", None, dep([mount("mod", "claude")]))
case("nested directory subPath", None, dep([mount("mod", "pymods/lib")]))
case("dotted directory name in the middle", None, dep([mount("mod", "a.b/c")]))
case("reverse-DNS directory name", None, dep([mount("mod", "app/com.github.jankaritech.mdpresentation-viewer")]))
case("file subPath on a configMap volume", None, dep([mount("cfg", "app.conf")]))
case("no subPath", None, dep([{"name": "mod", "mountPath": "/m"}]))

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
