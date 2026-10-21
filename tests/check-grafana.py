#!/usr/bin/env python3
"""Offline test of scripts/check-grafana.py: the fixture component tests/fixtures-grafana/observed
must pass as built, and each rule must fail on the deviation it exists to catch.
"""
import copy, importlib.util, pathlib, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("grafana_check", HERE / "scripts/check-grafana.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

FIXTURE = "tests/fixtures-grafana/observed"
FLUX = "app-observed"
GRAFANA_DIR = "kube-cluster/apps/grafana"


def build(path):
    r = subprocess.run(["kustomize", "build", str(HERE / path)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"kustomize build {path} failed:\n{r.stderr}")
    return [d for d in g.clusters.load_all(r.stdout) if isinstance(d, dict)]


COMPONENT = build(FIXTURE)
passed = failed = 0


def find(docs, kind):
    return next(d for d in docs if d["kind"] == kind)


def run(mutate=None, extra=()):
    """Findings for the fixture after `mutate(docs, docs)` edited a deep copy of its build."""
    docs = copy.deepcopy(COMPONENT)
    if mutate:
        mutate(docs, docs)
    return g.findings([g.Build(FIXTURE, FLUX, docs)] + list(extra), GRAFANA_DIR)


def case(name, expect, mutate=None, extra=()):
    """expect: a substring every run must produce, or None for a clean run."""
    global passed, failed
    got = run(mutate, extra)
    ok = (not got) if expect is None else any(expect in line for line in got)
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got or 'nothing'}"))
    passed, failed = passed + ok, failed + (not ok)


def group(objs):
    return find(objs, "GrafanaAlertRuleGroup")


def dash(objs):
    return find(objs, "GrafanaDashboard")


def folder(uid="fixture-folder", title="Observed", pruned=True):
    d = {"apiVersion": "grafana.integreatly.org/v1beta1", "kind": "GrafanaFolder",
         "metadata": {"name": "app-observed-folder", "namespace": "grafana",
                      "annotations": {g.SUBSTITUTE: "disabled", **({g.PRUNE: "disabled"} if pruned else {})}},
         "spec": {"title": title, "instanceSelector": {"matchLabels": g.INSTANCE}}}
    if uid:
        d["spec"]["uid"] = uid
    return d


def grafana_cr(**labels):
    return {"apiVersion": "grafana.integreatly.org/v1beta1", "kind": "Grafana",
            "metadata": {"name": "grafana", "namespace": "grafana", "labels": labels or dict(g.INSTANCE)}, "spec": {}}


case("fixture passes as built", None)
case("a Grafana CR in apps/grafana passes", None, extra=[g.Build(GRAFANA_DIR, "app-grafana", [grafana_cr()])])

# 1
case("operator object without the substitute opt-out", "substitute",
     lambda c, o: dash(o)["metadata"]["annotations"].pop(g.SUBSTITUTE))
case("dashboard ConfigMap without the substitute opt-out", "substitute",
     lambda c, o: find(o, "ConfigMap")["metadata"]["annotations"].pop(g.SUBSTITUTE))
case("object outside namespace grafana", "not grafana",
     lambda c, o: dash(o)["metadata"].update(namespace="other"))
case("Grafana CR outside apps/grafana", "belongs in",
     lambda c, o: c.append(grafana_cr()))
case("Grafana CR without the instance label", "no label",
     extra=[g.Build(GRAFANA_DIR, "app-grafana", [grafana_cr(x="y")])])
# 2
case("name outside the component", "Flux name",
     lambda c, o: dash(o)["metadata"].update(name="other-overview"))
case("a sibling's prefix is not a match", "Flux name",
     lambda c, o: dash(o)["metadata"].update(name="app-observed2"))
case("rule group spec.name outside the component", "spec.name",
     lambda c, o: group(o)["spec"].update(name="other"))
case("duplicate kind and name", "defined 2 times",
     lambda c, o: o.append(copy.deepcopy(dash(o))))
case("duplicate folder and group name", "rule group",
     lambda c, o: o.append({**copy.deepcopy(group(o)), "metadata": {**group(o)["metadata"], "name": "app-observed-alerts2"}}))
# 3
case("rule uid used twice", "defined again",
     lambda c, o: group(o)["spec"]["rules"].append(copy.deepcopy(group(o)["spec"]["rules"][0])))
case("dashboard JSON without a uid", "no uid",
     lambda c, o: find(o, "ConfigMap")["data"].update({"overview.json": "{}"}))
# 4
case("foreign instanceSelector", "instanceSelector",
     lambda c, o: dash(o)["spec"].update(instanceSelector={"matchLabels": {"a": "b"}}))
case("no instanceSelector", "instanceSelector",
     lambda c, o: group(o)["spec"].pop("instanceSelector"))
case("allowCrossNamespaceImport", "allowCrossNamespaceImport",
     lambda c, o: dash(o)["spec"].update(allowCrossNamespaceImport=False))
case("editable", "editable",
     lambda c, o: group(o)["spec"].update(editable=True))
# 5
case("unknown folderUID", "folderUID",
     lambda c, o: dash(o)["spec"].update(folderUID="nope"))
case("folderUID of a repo GrafanaFolder", None,
     lambda c, o: (o.append(folder()), dash(o)["spec"].update(folderUID="fixture-folder")))
case("folder by title", "spec.folder is set",
     lambda c, o: group(o)["spec"].update(folder="CI"))
case("folderRef", "spec.folderRef",
     lambda c, o: dash(o)["spec"].update(folderRef="x"))
# 6
case("GrafanaFolder without prune: disabled", "prune",
     lambda c, o: o.append(folder(pruned=False)))
case("GrafanaFolder without uid", "no spec.uid",
     lambda c, o: o.append(folder(uid=None)))
case("GrafanaFolder titled like an existing folder", "adopts by title",
     lambda c, o: o.append(folder(title="Ceph")))
# 7
for kind in sorted(g.FORBIDDEN):
    case(kind, "not managed from git",
         lambda c, o, kind=kind: o.append({"apiVersion": "grafana.integreatly.org/v1beta1", "kind": kind,
                                          "metadata": {"name": "app-observed-x", "namespace": "grafana"},
                                          "spec": {"instanceSelector": {"matchLabels": g.INSTANCE}}}))
# 8
case("dashboard with inline json", "spec.json",
     lambda c, o: dash(o)["spec"].update(json="{}"))
case("dashboard from a url", "spec.url",
     lambda c, o: dash(o)["spec"].update(url="https://example.com/d.json"))
case("dashboard without configMapRef", "no spec.configMapRef",
     lambda c, o: dash(o)["spec"].pop("configMapRef"))
case("configMapRef to a missing ConfigMap", "not in this build",
     lambda c, o: dash(o)["spec"]["configMapRef"].update(name="missing"))
case("configMapRef to a missing key", "no key",
     lambda c, o: dash(o)["spec"]["configMapRef"].update(key="missing.json"))
case("ConfigMap without the operator label", "lacks label",
     lambda c, o: find(o, "ConfigMap")["metadata"].pop("labels"))
# 10
case("dashboard uid equal to an existing folder uid", "folder uid",
     lambda c, o: find(o, "ConfigMap")["data"].update({"overview.json": '{"uid": "fg0li6dpvfitce"}'}))
case("dashboard uid equal to a repo folder uid", "folder uid",
     lambda c, o: (o.append(folder(uid="app-observed-overview")), dash(o)["spec"].update(folderUID="app-observed-overview")))
# 9
case("foreign datasourceUid", "datasourceUid",
     lambda c, o: group(o)["spec"]["rules"][0]["data"][0].update(datasourceUid="other"))


print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
