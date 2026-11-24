#!/usr/bin/env python3
"""Offline test of scripts/bridge-config-sync.py: the merge and the marker round trip, with a
fake upgrader in place of the bridge image."""
import importlib.util, pathlib, sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bridge_sync", HERE / "scripts/bridge-config-sync.py")
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

GIT = """\
# header
network:
  api_id: ${API_ID}
  pickle_key: "${PICKLE}"
  # kept comment
  name: x
  tags: [a, b]
bridge:
  # old option, goes away
  old_opt: 1
  keep: true
  mid: 2
appservice:
  as_token: "${AS}"
  database: postgres://u:${DBPW}@db/x
  nested:
    a: 1
    b: 2
double_puppet:
  secrets:
    h: "as_token:${AS}"
"""


def upgrader(fn):
    def run(text):
        d = yaml.safe_load(text)
        fn(d)
        return yaml.safe_dump(d, sort_keys=False)
    return run


def regen(d):
    d["appservice"]["as_token"] = "regenerated"


def add_remove(d):
    regen(d)
    del d["bridge"]["old_opt"]
    d["bridge"]["keep"] = False  # git wins
    d["bridge"] = {k: v for k, v in d["bridge"].items()}
    d["bridge"]["new_opt"] = {"x": 1, "y": [1, 2]}
    d["appservice"]["nested"]["c"] = 3
    d["fresh"] = {"k": "v"}


def rename_secret(d):
    regen(d)
    d["network"]["pickle_v2"] = d["network"].pop("pickle_key")
    d["network"]["id2"] = d["network"].pop("api_id")


def drop_secret(d):
    regen(d)
    del d["network"]["pickle_key"]


def dup_secret(d):
    regen(d)
    d["network"]["copy"] = d["network"]["pickle_key"]


def drop_db(d):
    regen(d)
    d["appservice"]["database"] = "postgres://regenerated"


passed = failed = 0


def check(name, ok, info=""):
    global passed, failed
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f": {info}"))
    passed, failed = passed + ok, failed + (not ok)


def sync(fn):
    return s.sync_text(GIT, upgrader(fn))


out, rep = sync(regen)
check("no change: text identical, nothing reported", out == GIT and rep == [], (rep, out))

out, rep = sync(add_remove)
lines = out.split("\n")
check("added and removed keys reported", rep == ["- bridge.old_opt", "+ bridge.new_opt", "+ appservice.nested.c", "+ fresh"], rep)
check("removed key takes its comment along", "old option" not in out and "old_opt" not in out)
check("kept key keeps git's value and comment", "  keep: true" in lines and "  # kept comment" in lines)
check("addition lands after its sibling, indented", lines.index("  new_opt:") == lines.index("  mid: 2") + 1)
check("nested map and list added in block style", "    x: 1" in lines and "    y:" in lines and "      - 1" in lines, out)
check("new key in existing nested map", "    c: 3" in lines)
check("new top-level map at the end", "fresh:" in lines and lines.index("fresh:") > lines.index("double_puppet:"))
check("placeholders survive, as_token not regenerated", "regenerated" not in out and out.count("${AS}") == 2 and "${DBPW}" in out)
check("result parses", isinstance(yaml.safe_load(out), dict))

out, rep = sync(rename_secret)
d = yaml.safe_load(out)
check("renamed secret keeps its placeholder under the new key",
      d["network"]["pickle_v2"] == "${PICKLE}" and "pickle_key" not in d["network"], out)
check("int marker moves with its key, unquoted", "  id2: ${API_ID}" in out.split("\n") and "api_id" not in out, out)
check("rename reported as - and +", "- network.pickle_key" in rep and "+ network.pickle_v2" in rep, rep)

for name, fn in [("dropped secret", drop_secret), ("duplicated secret", dup_secret)]:
    try:
        sync(fn)
        check(f"{name} is refused", False, "no error")
    except s.SyncError as e:
        check(f"{name} is refused", True)

out, rep = sync(drop_db)
check("a kept key's value is never taken from the copy", out == GIT and rep == [], rep)

check("image name strips tag and digest",
      s.image_name("dock.mau.dev/mautrix/signal:v0.1@sha256:ab") == "dock.mau.dev/mautrix/signal"
      and s.image_name("localhost:5000/x:1") == "localhost:5000/x")

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
