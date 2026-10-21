#!/usr/bin/env python3
"""Grafana Operator objects of a component opt out of substitution, with fixed folders and uids.

Each component owns its dashboards and alert rule groups as operator CRs in namespace grafana,
in its own kustomization (grafana.yaml, dashboards/). The operator adopts and overwrites by uid
and by folder title, so two owners of one uid, or a rule group that lands in a guessed folder,
silently replace each other's content. Per cluster, over every component build, this fails on:

 1. the Grafana CR anywhere but <cluster>/apps/grafana; a grafana.integreatly.org object outside
    namespace grafana; a Grafana CR without the label grafana.example.com/instance: main; an
    operator object or a ConfigMap carrying the operator label without the annotation
    kustomize.toolkit.fluxcd.io/substitute: disabled (the component's postBuild would substitute
    the ${...} in dashboard JSON and rule templates)
 2. a CR name, or a rule group's spec.name, that is not the owning component's Flux name or
    starts with it plus "-"; a duplicate (kind, name); a duplicate (folderUID, spec.name)
 3. a rule uid used twice across the CRs; a dashboard uid used twice across the CRs' JSON
 4. an instanceSelector other than the canonical label; allowCrossNamespaceImport; editable
 5. a folderUID that is neither in FOLDERS nor a repo GrafanaFolder's spec.uid; folder or folderRef
 6. a GrafanaFolder without prune: disabled, without spec.uid, or titled like a FOLDERS entry
 7. a GrafanaNotificationPolicy, GrafanaNotificationPolicyRoute or GrafanaContactPoint
 8. a GrafanaDashboard not sourced from a configMapRef, or whose ConfigMap or key is missing
    from the same build, or whose ConfigMap lacks the operator label
 9. a rule datasourceUid other than the VictoriaMetrics one or __expr__
10. a dashboard uid equal to a folder uid (FOLDERS or a repo GrafanaFolder): Grafana's folder
    view then recurses

KUSTOMIZE_BUILDS is validate.sh's build directory; without it every build is run here.
tests/check-grafana.py runs the same rules over tests/fixtures-grafana.
"""
import collections, json, os, pathlib, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import clusters

ROOT = clusters.ROOT
GROUP = "grafana.integreatly.org"
NAMESPACE = "grafana"
INSTANCE = {"grafana.example.com/instance": "main"}
# Grafana-generated folder uids, which the repo cannot choose. Mirrors the folder table in
# kube-cluster/apps/grafana/README.md. A new folder is a GrafanaFolder with its own spec.uid.
FOLDERS = {"CI": "fg0li6dpvfitce", "Cluster": "ag1airdsjen7kc",
           "Postgres": "cg1t0rly9yuioe", "Ceph": "dg1tqkgkqcbnke"}
DATASOURCES = {"bfz43q0alv6kgc", "__expr__"}  # VictoriaMetrics (Prometheus), and expressions
FORBIDDEN = {"GrafanaNotificationPolicy", "GrafanaNotificationPolicyRoute", "GrafanaContactPoint"}
CONTENT_SOURCES = ("json", "url", "grafanaCom", "jsonnet", "gzipJson")
OPERATOR_LABEL = ("app.kubernetes.io/managed-by", "grafana-operator")
PRUNE = "kustomize.toolkit.fluxcd.io/prune"
SUBSTITUTE = "kustomize.toolkit.fluxcd.io/substitute"


class Build:
    """One component's kustomize build; dir is its path, flux its Flux name."""

    def __init__(self, dir, flux, docs):
        self.dir, self.flux, self.docs = dir, flux, docs


def meta(d):
    return d.get("metadata") or {}


def spec(d):
    return d.get("spec") or {}


def is_operator(d):
    return str(d.get("apiVersion", "")).split("/")[0] == GROUP


def dashboard_json(build, doc):
    """(uid or None, error or None) of the JSON a GrafanaDashboard's configMapRef names."""
    ref = spec(doc).get("configMapRef") or {}
    cm = next((d for d in build.docs if d.get("kind") == "ConfigMap" and meta(d).get("name") == ref.get("name")
               and meta(d).get("namespace") == meta(doc).get("namespace")), None)
    if cm is None:
        return None, f"configMapRef {ref.get('name')!r} is not in this build, in namespace {meta(doc).get('namespace')}"
    if OPERATOR_LABEL[0] not in (meta(cm).get("labels") or {}) or \
            meta(cm)["labels"][OPERATOR_LABEL[0]] != OPERATOR_LABEL[1]:
        return None, (f"ConfigMap {ref.get('name')} lacks label {OPERATOR_LABEL[0]}: {OPERATOR_LABEL[1]}, "
                      f"so the operator's informer does not see it")
    raw = (cm.get("data") or {}).get(ref.get("key"))
    if raw is None:
        return None, f"ConfigMap {ref.get('name')} has no key {ref.get('key')!r}"
    try:
        uid = json.loads(raw).get("uid")
    except (ValueError, AttributeError):
        return None, f"{ref.get('key')} is not a JSON object"
    return uid, None


def findings(builds, grafana_dir):
    """One line per finding over one cluster's builds. grafana_dir: where the Grafana CR may live."""
    out = []
    seen_names = collections.defaultdict(list)   # (kind, name) -> [where]
    seen_groups = collections.defaultdict(list)  # (folderUID, spec.name) -> [where]
    seen_rules = collections.defaultdict(list)   # rule uid -> [where]
    seen_dash = collections.defaultdict(list)    # dashboard uid -> [where]
    folders = {}                                 # spec.uid -> where, of every repo GrafanaFolder
    for b in builds:
        for d in b.docs:
            if d.get("kind") == "GrafanaFolder" and is_operator(d) and spec(d).get("uid"):
                folders[spec(d)["uid"]] = d
    allowed_folders = set(FOLDERS.values()) | set(folders)

    for b in builds:
        for d in b.docs:
            if d.get("kind") == "ConfigMap" and (meta(d).get("labels") or {}).get(OPERATOR_LABEL[0]) == OPERATOR_LABEL[1] \
                    and (meta(d).get("annotations") or {}).get(SUBSTITUTE) != "disabled":
                out.append(f"  {b.dir}: ConfigMap/{meta(d).get('name')}: no annotation {SUBSTITUTE}: disabled; "
                           f"Flux would substitute ${{...}} in the dashboard JSON")
            if not is_operator(d):
                continue
            kind, name = d.get("kind"), meta(d).get("name")
            s = spec(d)
            where = f"{b.dir}: {kind}/{name}"

            # 7
            if kind in FORBIDDEN:
                out.append(f"  {where}: {kind} is not managed from git (docs/adr: contact points and policies stay file-provisioned)")
            # 1
            if kind == "Grafana":
                if b.dir != grafana_dir:
                    out.append(f"  {where}: the Grafana CR belongs in {grafana_dir}")
                elif meta(d).get("namespace") != NAMESPACE:
                    out.append(f"  {where}: in namespace {meta(d).get('namespace')!r}, not {NAMESPACE}")
                if (meta(d).get("labels") or {}).get("grafana.example.com/instance") != "main":
                    out.append(f"  {where}: no label grafana.example.com/instance: main, so no content selects it")
                continue
            if meta(d).get("namespace") != NAMESPACE:
                out.append(f"  {where}: in namespace {meta(d).get('namespace')!r}, not {NAMESPACE}")
            if (meta(d).get("annotations") or {}).get(SUBSTITUTE) != "disabled":
                out.append(f"  {where}: no annotation {SUBSTITUTE}: disabled; Flux would substitute ${{...}} in it")
            # 2
            owned = lambda n: isinstance(n, str) and (n == b.flux or n.startswith(b.flux + "-"))
            if not owned(name):
                out.append(f"  {where}: name does not start with the component's Flux name {b.flux}")
            seen_names[(kind, name)].append(b.dir)
            # 4
            if s.get("instanceSelector") != {"matchLabels": INSTANCE}:
                out.append(f"  {where}: instanceSelector is not exactly matchLabels {INSTANCE}")
            for key in ("allowCrossNamespaceImport", "editable"):
                if key in s:
                    out.append(f"  {where}: spec.{key} is set; the design is one namespace, read-only in the UI")

            if kind == "GrafanaFolder":
                # 6
                if (meta(d).get("annotations") or {}).get(PRUNE) != "disabled":
                    out.append(f"  {where}: no annotation {PRUNE}: disabled; pruning it deletes the folder and what is in it")
                if not s.get("uid"):
                    out.append(f"  {where}: no spec.uid; the operator would pick one")
                if s.get("title") in FOLDERS:
                    out.append(f"  {where}: title {s.get('title')!r} is an existing folder, which the operator adopts by title")
                continue

            if kind in ("GrafanaDashboard", "GrafanaAlertRuleGroup"):
                # 5
                for key in ("folder", "folderRef"):
                    if key in s:
                        out.append(f"  {where}: spec.{key} is set; use folderUID")
                if s.get("folderUID") not in allowed_folders:
                    out.append(f"  {where}: folderUID {s.get('folderUID')!r} is not in FOLDERS or a repo GrafanaFolder")

            if kind == "GrafanaDashboard":
                # 8
                for key in CONTENT_SOURCES:
                    if key in s:
                        out.append(f"  {where}: spec.{key}; dashboards come from a configMapRef")
                if "configMapRef" not in s:
                    out.append(f"  {where}: no spec.configMapRef")
                else:
                    uid, err = dashboard_json(b, d)
                    if err:
                        out.append(f"  {where}: {err}")
                    elif not uid:
                        out.append(f"  {where}: the JSON has no uid; set a fixed one")
                    else:
                        seen_dash[uid].append(f"{b.dir} {name}")
                        if uid in allowed_folders:  # 11
                            out.append(f"  {where}: dashboard uid {uid!r} is also a folder uid, "
                                       f"which makes Grafana's folder view recurse")

            if kind == "GrafanaAlertRuleGroup":
                group = s.get("name") or name
                if not owned(group):
                    out.append(f"  {where}: spec.name {group!r} does not start with the component's Flux name {b.flux}")
                seen_groups[(s.get("folderUID"), group)].append(f"{b.dir} {name}")
                for rule in s.get("rules") or []:
                    uid = rule.get("uid")
                    seen_rules[uid].append(f"{b.dir} {name}")
                    # 9
                    for q in rule.get("data") or []:
                        if q.get("datasourceUid") not in DATASOURCES:
                            out.append(f"  {where}: rule {uid} query {q.get('refId')} uses datasourceUid "
                                       f"{q.get('datasourceUid')!r}, not one of {sorted(DATASOURCES)}")

    # 2, 3
    for (kind, name), at in sorted(seen_names.items(), key=str):
        if len(at) > 1:
            out.append(f"  {kind}/{name}: defined {len(at)} times ({', '.join(at)})")
    for (folder, group), at in sorted(seen_groups.items(), key=str):
        if len(at) > 1:
            out.append(f"  rule group {group!r} in folder {folder}: defined {len(at)} times ({', '.join(at)})")
    for uid, at in sorted(seen_rules.items(), key=str):
        if len(at) > 1:
            out.append(f"  rule uid {uid}: defined again ({', '.join(at)})")
    for uid, at in sorted(seen_dash.items(), key=str):
        if len(at) > 1:
            out.append(f"  dashboard uid {uid}: defined again ({', '.join(at)})")
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


def cluster_builds(cluster):
    out = []
    for dir, group in clusters.components(cluster):
        flux = f"{group['prefix']}-{dir.rsplit('/', 1)[-1]}" if group.get("prefix") else dir.rsplit("/", 1)[-1]
        text = build(dir)
        if text is not None:
            out.append(Build(dir, flux, [d for d in clusters.load_all(text) if isinstance(d, dict)]))
    return out


def main():
    failures, n = [], 0
    for cluster in clusters.clusters():
        builds = cluster_builds(cluster)
        n += sum(1 for b in builds for d in b.docs if is_operator(d) and d.get("kind") != "Grafana")
        failures += [f"{cluster}:" + f for f in findings(builds, f"{cluster}/apps/grafana")]
    if failures:
        print("== grafana check failed ==", file=sys.stderr)
        print(*failures, sep="\n", file=sys.stderr)
        sys.exit(1)
    print(f"{n} Grafana Operator object(s) keep to their component, with fixed folders and uids")


if __name__ == "__main__":
    main()
