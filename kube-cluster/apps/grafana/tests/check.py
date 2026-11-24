#!/usr/bin/env python3
"""Render the matrix-payload template (the pinned Grafana) and the relay (the pinned
matrix-alerts image, against a fake homeserver) for every fixture, and compare the Matrix
messages with golden/. Needs docker; talks only to containers it starts.

  check.py <grafana component dir>        verify
  UPDATE_GOLDEN=1 check.py <dir>          rewrite golden/ after an intended change

A payload that is not valid JSON makes the relay drop every alert, so each fixture must parse
and produce one message per pseudo-alert.
"""
import base64
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

import yaml

STUB_IMAGE = "python:3.14-alpine@sha256:5862d971d1f0ae26b258cefa4e893556b1d3aba81bd2ff30888704ad378bab67"
dir_ = pathlib.Path(sys.argv[1]).resolve()
tests = dir_ / "tests"
relay_dir = dir_.parent / "matrix-alerts"
update = os.environ.get("UPDATE_GOLDEN") == "1"
tag = "mtxfmt-" + uuid.uuid4().hex[:8]


def sh(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def image(file, pattern):
    m = re.search(pattern, file.read_text(encoding="utf-8"))
    if not m:
        sys.exit(f"no image matching {pattern} in {file}")
    return m.group(1)


def start(name, port, opts, img, *cmd):
    """Start a container on the test network; return the localhost port published for `port`."""
    sh("docker", "run", "-d", "--name", f"{tag}-{name}", "--network", tag, "--network-alias", name,
       "-p", f"127.0.0.1::{port}", *opts, img, *cmd)
    return int(sh("docker", "port", f"{tag}-{name}", str(port)).splitlines()[0].rsplit(":", 1)[1])


def http(method, url, body=None, headers=None):
    req = urllib.request.Request(url, body, headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def wait(url, name):
    for _ in range(300):
        try:
            http("GET", url)
            return
        except Exception:
            time.sleep(1)
    logs = subprocess.run(["docker", "logs", "--tail", "30", f"{tag}-{name}"], capture_output=True, text=True)
    sys.exit(f"{name} did not come up\n{logs.stdout}{logs.stderr}")


def main():
    grafana_img = image(dir_ / "grafana.yaml", r"version:\s*(ghcr\.io/lhns/grafana-babel:\S+)")
    relay_img = image(relay_dir / "resources.yaml", r"image:\s*(metio/matrix-alertmanager-receiver:\S+)")
    template = yaml.safe_load((dir_ / "alerting-contactpoints.yaml").read_text(encoding="utf-8"))["templates"][0]["template"]

    work = pathlib.Path(tempfile.mkdtemp(prefix="matrix-payload-"))
    work.chmod(0o755)  # the relay runs as nonroot
    (work / "stub.py").write_text((tests / "matrix_stub.py").read_text(encoding="utf-8"), encoding="utf-8")
    cfg = yaml.safe_load((relay_dir / "config.yaml").read_text(encoding="utf-8"))
    cfg["matrix"]["homeserver-url"] = "http://matrix:8008"
    cfg["matrix"]["room-mapping"] = {"grafana": "!room:test"}
    (work / "relay.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    for f in work.iterdir():
        f.chmod(0o644)

    def mount(name):
        return ["--mount", f"type=bind,source={work / name},target=/{name},readonly"]

    sh("docker", "network", "create", tag)
    failures = []
    try:
        matrix = start("matrix", 8008, mount("stub.py"), STUB_IMAGE, "python", "/stub.py")
        wait(f"http://127.0.0.1:{matrix}/_recorded", "matrix")  # the relay exits if it cannot list rooms
        relay = start("relay", 8080, ["-e", "MATRIX_ACCESS_TOKEN=test", *mount("relay.yaml")],
                      relay_img, "--config-path", "/relay.yaml")
        grafana = start("grafana", 3000, ["-e", "GF_SECURITY_ADMIN_PASSWORD=admin"], grafana_img)
        wait(f"http://127.0.0.1:{relay}/metrics", "relay")
        wait(f"http://127.0.0.1:{grafana}/api/health", "grafana")
        auth = {"Content-Type": "application/json",
                "Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()}

        for fixture in sorted((tests / "fixtures").glob("*.json")):
            name = fixture.stem
            alerts = json.loads(fixture.read_text(encoding="utf-8"))["alerts"]
            body = json.dumps({"name": "matrix-payload", "template": template, "alerts": [
                {"labels": a["labels"], "annotations": a["annotations"], "generatorURL": a["generatorURL"],
                 "startsAt": "2026-01-01T00:00:00Z",
                 "endsAt": "0001-01-01T00:00:00Z" if a["status"] == "firing" else "2026-01-01T01:00:00Z"}
                for a in alerts]}).encode()
            try:
                res = json.loads(http("POST", f"http://127.0.0.1:{grafana}/api/alertmanager/grafana/config/api/v1/templates/test", body, auth))
                if "results" not in res or [r["name"] for r in res["results"]] != ["matrix.payload"] or "error" in res["results"][0]:
                    raise ValueError(f"template error: {res}")
                text = res["results"][0]["text"]
                payload = json.loads(text)  # invalid JSON drops every alert at the relay
                want = len({a["status"] for a in alerts})
                if len(payload["alerts"]) != want:
                    raise ValueError(f"{len(payload['alerts'])} pseudo-alerts, want {want}")
                http("DELETE", f"http://127.0.0.1:{matrix}/_recorded")
                http("POST", f"http://127.0.0.1:{relay}/alerts/grafana", text.encode("utf-8"), {"Content-Type": "application/json"})
                messages = json.loads(http("GET", f"http://127.0.0.1:{matrix}/_recorded"))
                if len(messages) != want:
                    raise ValueError(f"relay sent {len(messages)} messages, want {want}")
                dump = json.dumps({"payload": payload["alerts"], "messages": messages}, indent=1, ensure_ascii=False) + "\n"
                golden = tests / "golden" / f"{name}.json"
                if update:
                    golden.parent.mkdir(exist_ok=True)
                    golden.write_text(dump, encoding="utf-8", newline="\n")
                elif not golden.exists() or golden.read_text(encoding="utf-8") != dump:
                    raise ValueError(f"differs from {golden.name} (UPDATE_GOLDEN=1 to accept):\n{dump}")
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001 - report every fixture, then fail
                failures.append(name)
                print(f"FAIL {name}: {e}", file=sys.stderr)
        metrics = http("GET", f"http://127.0.0.1:{relay}/metrics").decode()
        for counter in ("invalid_payload", "templating_failure", "send_failure"):
            m = re.search(rf"^matrix_alertmanager_receiver_{counter}_total (\S+)", metrics, re.M)
            if m and float(m.group(1)) > 0:
                failures.append(counter)
                print(f"FAIL relay counter {counter} = {m.group(1)}", file=sys.stderr)
    finally:
        for c in ("grafana", "relay", "matrix"):
            subprocess.run(["docker", "rm", "-f", f"{tag}-{c}"], capture_output=True)
        subprocess.run(["docker", "network", "rm", tag], capture_output=True)
    if failures:
        sys.exit(f"{len(failures)} failed: {', '.join(failures)}")


main()
