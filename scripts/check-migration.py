#!/usr/bin/env python3
"""Read-only safety gate for a risky change to a component's Kustomizations.

    snapshot <name>... --out FILE              before the pruneDisabled PR
    verify-prune-disabled <name>...            after that PR is live; gate for the next PR
    verify-moved <name>... --snapshot FILE     after a PR that moves objects between the
                                               component's Kustomizations, e.g. into namespaces/
    verify-done <name>... --snapshot FILE      after pruneDisabled is removed again

--renamed-from OLD (verify-moved/-done, one name): the snapshot was taken under OLD, before a
move between groups renamed the component (README.md, "Moving a component between groups").

<name> is the generated Kustomization name, e.g. app-freshrss. The procedure is in README.md,
"Control namespaces and risky changes". Run it from a checkout of the commit that is live: the
expected substitution sources are read from the working tree.

--cluster names the repo directory the components are read from, default kube-cluster; point
$JUMPHOST at a host whose kubectl talks to that cluster.

Only `kubectl get` runs, through the jumphost ($JUMPHOST, default admin@10.20.150.84). Secret and
ConfigMap contents, annotations and managedFields are dropped on the jumphost, before
anything leaves it. Exit 0: every check passed. 1: a deviation, listed. 2: could not check.
"""
import argparse, datetime, json, os, pathlib, shlex, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLUSTER = "kube-cluster"  # --cluster
SUFFIXES = ("", "-namespaces", "-artifacts", "-substitution-secrets")
OWNER_NAME = "kustomize.toolkit.fluxcd.io/name"
OWNER_NS = "kustomize.toolkit.fluxcd.io/namespace"
CONTROL_LABEL = "flux-component-generator/control-namespace"
KS = "kustomizations.kustomize.toolkit.fluxcd.io"
HR = "helmreleases.helm.toolkit.fluxcd.io"
EA = "externalartifacts.source.toolkit.fluxcd.io"
# source-watcher records the commit an ExternalArtifact was packed from here (originRevision).
ORIGIN = "org.opencontainers.image.revision"
# Runs on the jumphost: nothing of a Secret's value, nor a last-applied annotation that could
# hold one, crosses the wire.
STRIP = ('[.[] | if (.kind // "") | endswith("List") then .items[] else . end'
         ' | del(.data, .stringData, .binaryData, .metadata.managedFields, .metadata.annotations)]')


class CannotCheck(Exception):
    pass


def control_namespace(name):
    return f"gen-{name}"


def gen_release(name):
    return f"gen-{name}"


# ---------------------------------------------------------------- cluster access

class Live:
    """Every cluster read. The offline test replaces this with canned `kubectl get -o json`."""

    def __init__(self, host):
        self.host = host

    def get(self, *queries):
        """One ssh round trip for several `kubectl get` argument lists; a list of item lists."""
        lines = ["set -o pipefail"]
        for q in queries:
            lines.append(
                "if out=$(kubectl get " + " ".join(shlex.quote(a) for a in q)
                + " -o json --ignore-not-found); then printf '%s' \"$out\" | jq -c -s "
                + shlex.quote(STRIP) + "; else echo '@@FAILED'; fi")
        r = subprocess.run(
            ["ssh", "-n", "-o", "BatchMode=yes", self.host, "bash -c " + shlex.quote("\n".join(lines))],
            capture_output=True, text=True)
        out = r.stdout.splitlines()
        if r.returncode != 0 or len(out) != len(queries) or "@@FAILED" in out:
            raise CannotCheck(f"kubectl through {self.host} failed:\n{r.stderr.strip()}")
        return [json.loads(line) for line in out]


# ---------------------------------------------------------------- normalising

def group_of(api_version):
    return api_version.rsplit("/", 1)[0] if "/" in api_version else ""


def ready(obj):
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status"), c.get("message", "")
    return "Unknown", "no Ready condition"


def kustomization(item):
    status = item.get("status") or {}
    meta = item["metadata"]
    ok, msg = ready(item)
    src = (item.get("spec") or {}).get("sourceRef") or {}
    return {
        "sourceRef": {"kind": src.get("kind"), "name": src.get("name"),
                      "namespace": src.get("namespace") or meta["namespace"]},
        "namespace": meta["namespace"], "name": meta["name"], "uid": meta["uid"],
        "prune": (item.get("spec") or {}).get("prune"),
        "suspend": bool((item.get("spec") or {}).get("suspend")),
        "deleting": bool(meta.get("deletionTimestamp")),
        "generation": meta.get("generation"), "observedGeneration": status.get("observedGeneration"),
        "ready": ok, "message": msg, "revision": status.get("lastAppliedRevision"),
        "inventory": sorted(e["id"] for e in (status.get("inventory") or {}).get("entries") or []),
    }


def parse_id(entry_id):
    """Flux inventory id: <namespace>_<name>_<group>_<kind>; names cannot hold '_'."""
    ns, name, group, kind = entry_id.split("_")
    return {"namespace": ns, "name": name, "group": group, "kind": kind}


def key(kind, group, ns, name):
    return f"{kind}.{group}/{ns}/{name}" if group else f"{kind}/{ns}/{name}"


def id_key(entry_id):
    p = parse_id(entry_id)
    return key(p["kind"], p["group"], p["namespace"], p["name"])


def obj_record(item):
    meta = item["metadata"]
    labels = meta.get("labels") or {}
    return key(item["kind"], group_of(item["apiVersion"]), meta.get("namespace", ""), meta["name"]), {
        "uid": meta["uid"], "resourceVersion": meta.get("resourceVersion"),
        "owner": f"{labels.get(OWNER_NS, '')}/{labels.get(OWNER_NAME, '')}",
    }


def pod_record(item):
    st = item.get("status") or {}
    owners = item["metadata"].get("ownerReferences") or []
    return {
        "uid": item["metadata"]["uid"], "phase": st.get("phase"),
        "restarts": sum(c.get("restartCount", 0) for c in st.get("containerStatuses") or []),
        "job": any(o.get("kind") == "Job" for o in owners),
    }


# ---------------------------------------------------------------- reading state

def read_kustomizations(cluster, names):
    wanted = {f"{n}{s}" for n in names for s in SUFFIXES}
    (items,) = cluster.get([KS, "-A"])
    return [kustomization(i) for i in items if i["metadata"]["name"] in wanted]


def source_key(ref):
    return f"{ref['kind']}/{ref['namespace']}/{ref['name']}"


def read_sources(cluster, ks):
    """Every ExternalArtifact the Kustomizations read: {key: {"revision", "origin"}}. origin is
    the commit it was packed from ('main@sha1:<sha>')."""
    wanted = {}
    for k in ks:
        ref = k["sourceRef"]
        if ref["kind"] == "ExternalArtifact":
            wanted.setdefault(ref["namespace"], set()).add(ref["name"])
    groups = sorted(wanted.items())
    results = cluster.get(*[[EA, "-n", ns] + sorted(names) for ns, names in groups]) if groups else []
    out = {}
    for (ns, _), items in zip(groups, results):
        for i in items:
            art = (i.get("status") or {}).get("artifact") or {}
            out[f"ExternalArtifact/{ns}/{i['metadata']['name']}"] = {
                "revision": art.get("revision"), "origin": (art.get("metadata") or {}).get(ORIGIN)}
    return out


def read_release(cluster, name):
    """The component's gen- HelmRelease: UID, latest Helm release."""
    (items,) = cluster.get([HR, "-n", "flux-system", gen_release(name)])
    if not items:
        return None
    i = items[0]
    status = i.get("status") or {}
    hist = status.get("history") or []
    latest = max(hist, key=lambda h: h.get("version", 0)) if hist else {}
    ok, msg = ready(i)
    return {"uid": i["metadata"]["uid"],
            "version": latest.get("version"), "status": latest.get("status"),
            "chartName": latest.get("chartName"), "ready": ok, "message": msg,
            "generation": i["metadata"].get("generation"), "observedGeneration": status.get("observedGeneration")}


def read_objects(cluster, entry_ids):
    """The live objects behind inventory ids, keyed like key()."""
    by_ns = {}
    for e in sorted(set(entry_ids)):
        p = parse_id(e)
        ref = f"{p['kind']}.{p['group']}/{p['name']}" if p["group"] else f"{p['kind']}/{p['name']}"
        by_ns.setdefault(p["namespace"], []).append(ref)
    queries = []
    for ns, refs in sorted(by_ns.items()):
        for i in range(0, len(refs), 100):
            queries.append((["-n", ns] if ns else []) + refs[i:i + 100])
    out = {}
    for items in cluster.get(*queries) if queries else []:
        for item in items:
            k, rec = obj_record(item)
            out[k] = rec
    return out


def workload_namespaces(entry_ids):
    """The namespaces whose pods and PVCs are watched: the Namespaces the component owns, or,
    owning none, every namespace it puts objects in. Never flux-system or a control namespace."""
    parsed = [parse_id(e) for e in entry_ids]
    owned = {p["name"] for p in parsed if p["kind"] == "Namespace" and p["group"] == ""}
    used = owned or {p["namespace"] for p in parsed if p["namespace"]}
    return sorted(n for n in used if n != "flux-system" and not n.startswith("gen-"))


def read_workloads(cluster, namespaces):
    queries = [["pods,persistentvolumeclaims", "-n", ns] for ns in namespaces]
    queries += [["namespaces"] + namespaces] if namespaces else []
    queries.append(["persistentvolumes"])
    results = cluster.get(*queries)
    pods, pvcs, nss = {}, {}, {}
    for items in results[:len(namespaces)]:
        for i in items:
            ref = f"{i['metadata']['namespace']}/{i['metadata']['name']}"
            if i["kind"] == "Pod":
                pods[ref] = pod_record(i)
            elif i["kind"] == "PersistentVolumeClaim":
                pvcs[ref] = {"uid": i["metadata"]["uid"], "volumeName": (i.get("spec") or {}).get("volumeName"),
                             "phase": (i.get("status") or {}).get("phase")}
    if namespaces:
        nss = {i["metadata"]["name"]: i["metadata"]["uid"] for i in results[len(namespaces)]}
    pvs = {}
    for i in results[-1]:
        claim = (i.get("spec") or {}).get("claimRef") or {}
        if claim.get("namespace") in namespaces:
            pvs[i["metadata"]["name"]] = {
                "uid": i["metadata"]["uid"], "phase": (i.get("status") or {}).get("phase"),
                "claim": f"{claim.get('namespace')}/{claim.get('name')}", "claimUid": claim.get("uid")}
    return {"pods": pods, "pvcs": pvcs, "pvs": pvs, "namespaces": nss}


def read_control(cluster, ns):
    """The control namespace itself and the Secret/ConfigMap names in it (contents dropped)."""
    nsi, objs = cluster.get(["namespaces", ns], ["secrets,configmaps", "-n", ns])
    return {
        "exists": bool(nsi),
        "labelled": bool(nsi) and (nsi[0]["metadata"].get("labels") or {}).get(CONTROL_LABEL) == "true",
        "objects": sorted(f"{i['kind']}/{i['metadata']['name']}" for i in objs),
    }


# ---------------------------------------------------------------- the repo side

def component_dir(name):
    sys.path.insert(0, str(ROOT / "scripts"))
    import clusters
    for g in clusters.values(CLUSTER).get("groups") or []:
        prefix = g.get("prefix")
        comp = name[len(prefix) + 1:] if prefix and name.startswith(prefix + "-") else (None if prefix else name)
        if comp and (ROOT / g["base"] / comp / "kustomization.yaml").is_file():
            return ROOT / g["base"] / comp
    raise CannotCheck(f"{name}: no component directory for it in this checkout")


def expected_sources(name):
    """What substituteFrom needs in the control namespace, by the rules of _substitution.tpl."""
    import yaml
    ns, d, out = control_namespace(name), component_dir(name), {"Secret/sops-age"}
    for f in sorted((d / "substitution-secrets").glob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if isinstance(doc, dict) and doc.get("kind") in ("Secret", "ConfigMap") \
                    and (doc.get("metadata") or {}).get("namespace") == ns:
                out.add(f"{doc['kind']}/{doc['metadata']['name']}")
    for f in sorted((d / "artifacts").glob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if isinstance(doc, dict) and doc.get("kind") in ("ImageComposition", "ImageBuild") \
                    and (((doc.get("spec") or {}).get("push") or {}).get("writeRefTo") or {}).get("namespace") == ns:
                out.add(f"ConfigMap/{doc['kind'].lower()}-{doc['metadata']['namespace']}-{doc['metadata']['name']}")
    return sorted(out)


def is_ancestor(commit, revision):
    """Whether the live revision ('main@sha1:<sha>') contains commit, by the local git."""
    sha = (revision or "").rsplit(":", 1)[-1]
    r = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", commit, sha],
                       capture_output=True, text=True)
    if r.returncode > 1:
        raise CannotCheck(f"git cannot compare {commit} with {sha}; git fetch first:\n{r.stderr.strip()}")
    return r.returncode == 0


# ---------------------------------------------------------------- checks

class Report:
    def __init__(self):
        self.deviations, self.notes = [], []

    def bad(self, msg):
        self.deviations.append(msg)

    def note(self, msg):
        self.notes.append(msg)


def check_kustomizations(rep, name, ks, sources, *, prune, suffixes=None):
    where = control_namespace(name)
    mine = [k for k in ks if k["name"] in {f"{name}{s}" for s in SUFFIXES}]
    if not mine:
        rep.bad(f"{name}: no Kustomization of it found")
        return mine
    for k in mine:
        ref = f"Kustomization {k['namespace']}/{k['name']}"
        if k["namespace"] != where:
            rep.bad(f"{name}: {ref} is outside {where}")
            continue
        if k["deleting"]:
            rep.bad(f"{name}: {ref} is being deleted")
        if k["suspend"]:
            rep.bad(f"{name}: {ref} is suspended")
        if prune is not None and k["prune"] is not prune:
            rep.bad(f"{name}: {ref} has spec.prune {k['prune']}, expected {prune}")
        if k["ready"] != "True":
            rep.bad(f"{name}: {ref} is not Ready: {k['message']}")
        if k["observedGeneration"] != k["generation"]:
            rep.bad(f"{name}: {ref} has not observed its latest spec "
                    f"(generation {k['generation']}, observed {k['observedGeneration']})")
        src = sources.get(source_key(k["sourceRef"]))
        if (k["sourceRef"]["kind"], k["sourceRef"]["name"], k["sourceRef"]["namespace"]) \
                != ("ExternalArtifact", k["name"], "flux-system"):
            rep.bad(f"{name}: {ref} reads {source_key(k['sourceRef'])}, "
                    f"expected its own, ExternalArtifact/flux-system/{k['name']}")
        elif src is None:
            rep.bad(f"{name}: {ref} reads {source_key(k['sourceRef'])}, which does not exist")
        elif k["revision"] != src["revision"]:
            rep.bad(f"{name}: {ref} applied {k['revision']}, its source is at {src['revision']}")
    if suffixes is not None:
        now = sorted(k["name"][len(name):] for k in mine if k["namespace"] == where)
        if now != sorted(suffixes):
            rep.bad(f"{name}: Kustomizations {sorted(name + s for s in suffixes)} expected in {where}, "
                    f"found {sorted(name + s for s in now)}")
    return mine


def compare_objects(rep, name, snap, ks, live_objs, *, new_ns):
    """Snapshot objects still there with the same UID, and the new inventory owned by new_ns."""
    before = snap["objects"]
    inv_owner = {}
    for k in ks:
        if k["namespace"] == new_ns:
            for e in k["inventory"]:
                inv_owner[id_key(e)] = f"{new_ns}/{k['name']}"
    changed_rv = 0
    for ref, old in sorted(before.items()):
        cur = live_objs.get(ref)
        if cur is None:
            rep.bad(f"{name}: {ref} is GONE (uid {old['uid']})")
            continue
        if cur["uid"] != old["uid"]:
            rep.bad(f"{name}: {ref} was RECREATED: uid {old['uid']} -> {cur['uid']}")
        if cur["resourceVersion"] != old["resourceVersion"]:
            changed_rv += 1
        if ref not in inv_owner:
            rep.bad(f"{name}: {ref} dropped out of every inventory in {new_ns}")
    for ref, owner in sorted(inv_owner.items()):
        cur = live_objs.get(ref)
        if cur is None:
            rep.bad(f"{name}: {ref} is in the inventory of {owner} but does not exist")
            continue
        if cur["owner"] != owner:
            rep.bad(f"{name}: {ref} is labelled as owned by {cur['owner']}, expected {owner}")
        if ref not in before:
            rep.bad(f"{name}: {ref} is new; the change should add nothing")
    if changed_rv:
        rep.note(f"{name}: {changed_rv} object(s) changed resourceVersion (moving owner labels does that)")


def compare_workloads(rep, name, before, now):
    for ns, uid in sorted(before["namespaces"].items()):
        cur = now["namespaces"].get(ns)
        if cur != uid:
            rep.bad(f"{name}: Namespace {ns} " + ("is GONE" if cur is None else f"was RECREATED: uid {uid} -> {cur}"))
    for ref, old in sorted(before["pvcs"].items()):
        cur = now["pvcs"].get(ref)
        if cur is None:
            rep.bad(f"{name}: PVC {ref} is GONE (uid {old['uid']}, volume {old['volumeName']})")
            continue
        for field in ("uid", "volumeName"):
            if cur[field] != old[field]:
                rep.bad(f"{name}: PVC {ref} {field} {old[field]} -> {cur[field]}")
        if old["phase"] == "Bound" and cur["phase"] != "Bound":
            rep.bad(f"{name}: PVC {ref} is {cur['phase']}, was Bound")
    for pv, old in sorted(before["pvs"].items()):
        cur = now["pvs"].get(pv)
        if cur is None:
            rep.bad(f"{name}: PV {pv} ({old['claim']}) is GONE")
            continue
        for field in ("uid", "claim", "claimUid"):
            if cur[field] != old[field]:
                rep.bad(f"{name}: PV {pv} {field} {old[field]} -> {cur[field]}")
        if old["phase"] == "Bound" and cur["phase"] != "Bound":
            rep.bad(f"{name}: PV {pv} is {cur['phase']}, was Bound")
    for ref, old in sorted(before["pods"].items()):
        if old["job"] or old["phase"] not in ("Running", "Pending"):
            continue
        cur = now["pods"].get(ref)
        if cur is None or cur["uid"] != old["uid"]:
            rep.bad(f"{name}: pod {ref} was RECREATED or removed")
        elif cur["restarts"] != old["restarts"]:
            rep.bad(f"{name}: pod {ref} restarted: {old['restarts']} -> {cur['restarts']} restarts")
    for ref, cur in sorted(now["pods"].items()):
        if ref not in before["pods"] and not cur["job"]:
            rep.bad(f"{name}: pod {ref} is new ({cur['phase']})")


def check_control(rep, name, control, expected):
    ns = control_namespace(name)
    if not control["exists"]:
        rep.bad(f"{name}: control namespace {ns} does not exist")
        return
    if not control["labelled"]:
        rep.bad(f"{name}: Namespace {ns} lacks the label {CONTROL_LABEL}=true")
    for obj in expected:
        if obj not in control["objects"]:
            rep.bad(f"{name}: {obj} is missing from {ns}" +
                    (" (Reflector copies it; see infra/reflector/README.md)" if obj == "Secret/sops-age" else ""))


# ---------------------------------------------------------------- subcommands

def origins(sources, ks):
    """The commits the Kustomizations' sources were built from, for --commit."""
    return sorted({sources[source_key(k["sourceRef"])]["origin"] for k in ks
                   if source_key(k["sourceRef"]) in sources} - {None})


def take_snapshot(cluster, names):
    rep = Report()
    ks = read_kustomizations(cluster, names)
    sources = read_sources(cluster, ks)
    comps = {}
    for name in names:
        mine = check_kustomizations(rep, name, ks, sources, prune=None)
        if not mine:
            continue
        release = read_release(cluster, name)
        if release is None:
            rep.bad(f"{name}: HelmRelease flux-system/{gen_release(name)} not found")
        ids = sorted({e for k in mine for e in k["inventory"]})
        objs = read_objects(cluster, ids)
        for e in ids:
            if id_key(e) not in objs:
                rep.bad(f"{name}: inventory entry {id_key(e)} does not exist")
        nss = workload_namespaces(ids)
        comps[name] = {
            "kustomizations": {k["name"]: {"namespace": k["namespace"], "uid": k["uid"], "prune": k["prune"],
                                           "sourceRef": k["sourceRef"]} for k in mine},
            "release": release,
            "suffixes": sorted(k["name"][len(name):] for k in mine),
            "objects": {id_key(e): objs[id_key(e)] for e in ids if id_key(e) in objs},
            "workloadNamespaces": nss,
            "workloads": read_workloads(cluster, nss),
        }
        w = comps[name]["workloads"]
        rep.note(f"{name}: {len(ids)} inventory objects, namespaces {', '.join(nss) or '(none)'}, "
                 f"{len(w['pvcs'])} PVC(s), {len(w['pvs'])} PV(s), {len(w['pods'])} pod(s)")
    snap = {"taken": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "revisions": origins(sources, ks), "components": comps}
    return rep, snap


def verify_prune_disabled(cluster, names):
    rep = Report()
    ks = read_kustomizations(cluster, names)
    sources = read_sources(cluster, ks)
    for name in names:
        check_kustomizations(rep, name, ks, sources, prune=False)
    return rep, origins(sources, ks)


def verify_after(cluster, names, snap, *, prune, renamed_from=None):
    rep = Report()
    ks = read_kustomizations(cluster, names + ([renamed_from] if renamed_from else []))
    sources = read_sources(cluster, ks)
    if renamed_from:
        check_renamed(rep, cluster, renamed_from, names[0], ks, snap)
    for name in names:
        before = snap["components"].get(renamed_from or name)
        if before is None:
            rep.bad(f"{renamed_from or name}: not in the snapshot")
            continue
        ns = control_namespace(name)
        suffixes = list(before["suffixes"])
        # Moving the Namespace into namespaces/ adds exactly that Kustomization; its objects
        # must still be the snapshot's, which compare_objects checks by UID.
        if ("-namespaces" not in suffixes and (component_dir(name) / "namespaces").is_dir()
                and any(k["name"] == f"{name}-namespaces" and k["namespace"] == ns for k in ks)):
            suffixes.append("-namespaces")
            rep.note(f"{name}: {name}-namespaces is new, as expected after moving into namespaces/")
        mine = check_kustomizations(rep, name, ks, sources, prune=prune, suffixes=suffixes)
        ids = {e for k in mine for e in k["inventory"]}
        live = read_objects(cluster, sorted(ids | set(snapshot_ids(before))))
        compare_objects(rep, name, before, mine, live, new_ns=ns)
        compare_workloads(rep, name, before["workloads"], read_workloads(cluster, before["workloadNamespaces"]))
        check_control(rep, name, read_control(cluster, ns), expected_sources(name))
    return rep, origins(sources, ks)


def check_renamed(rep, cluster, old, new, ks, snap):
    """The old name is gone entirely. Objects in the old control namespace go with it, so a
    component that had any cannot be compared by UID."""
    before = snap["components"].get(old) or {}
    held = sorted(k for k in before.get("objects", {}) if k.split("/")[-2] == control_namespace(old))
    if held:
        raise CannotCheck(f"{old}: the snapshot holds objects in {control_namespace(old)}, which the rename "
                          f"recreates in {control_namespace(new)}: {', '.join(held)}; check those by hand")
    for k in ks:
        if k["name"] in {f"{old}{s}" for s in SUFFIXES}:
            rep.bad(f"{new}: Kustomization {k['namespace']}/{k['name']} of the old name still exists"
                    + (" (being deleted)" if k["deleting"] else ""))
    if read_release(cluster, old) is not None:
        rep.bad(f"{new}: HelmRelease flux-system/{gen_release(old)} of the old name still exists")


def snapshot_ids(comp):
    """Inventory-id form of the snapshot's object keys."""
    out = []
    for ref in comp["objects"]:
        kindgroup, ns, name = ref.split("/")
        kind, _, group = kindgroup.partition(".")
        out.append(f"{ns}_{name}_{group}_{kind}")
    return out


def main(argv=None, cluster=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["snapshot", "verify-prune-disabled", "verify-moved", "verify-done"])
    ap.add_argument("names", nargs="+", metavar="name")
    ap.add_argument("--out", help="snapshot: where to write it; never inside the repo")
    ap.add_argument("--snapshot", help="verify-moved/-done: the snapshot taken before")
    ap.add_argument("--commit", help="also require every live source's commit to contain this one")
    ap.add_argument("--cluster", default="kube-cluster", help="the cluster's repo directory")
    ap.add_argument("--renamed-from", metavar="OLD",
                    help="verify-moved/-done: the name the snapshot was taken under, before a group move")
    a = ap.parse_args(argv)
    if a.renamed_from and (a.command not in ("verify-moved", "verify-done") or len(a.names) != 1):
        ap.error("--renamed-from takes verify-moved or verify-done and exactly one name")
    global CLUSTER
    CLUSTER = a.cluster
    if not (ROOT / CLUSTER / "flux-system/kustomization.yaml").is_file():
        ap.error(f"--cluster {CLUSTER}: no {CLUSTER}/flux-system/kustomization.yaml")
    cluster = cluster or Live(os.environ.get("JUMPHOST", "admin@10.20.150.84"))
    try:
        if a.command == "snapshot":
            if not a.out:
                ap.error("snapshot needs --out")
            out = pathlib.Path(a.out).resolve()
            if out == ROOT or ROOT in out.parents:
                ap.error("--out must be outside the repo: a snapshot is live state, not source")
            rep, snap = take_snapshot(cluster, a.names)
            revisions = snap["revisions"]
        elif a.command == "verify-prune-disabled":
            rep, revisions = verify_prune_disabled(cluster, a.names)
        else:
            if not a.snapshot:
                ap.error(f"{a.command} needs --snapshot")
            snap = json.loads(pathlib.Path(a.snapshot).read_text(encoding="utf-8"))
            rep, revisions = verify_after(cluster, a.names, snap, prune=a.command == "verify-done",
                                          renamed_from=a.renamed_from)
        if a.commit:
            if not revisions:
                rep.bad(f"no live source names its commit; cannot confirm {a.commit}")
            for revision in revisions:
                if not is_ancestor(a.commit, revision):
                    rep.bad(f"the live source revision {revision} does not contain {a.commit} yet")
    except CannotCheck as e:
        print(f"CANNOT CHECK: {e}", file=sys.stderr)
        return 2
    for n in rep.notes:
        print(f"  note  {n}")
    if rep.deviations:
        print(f"== {a.command}: {len(rep.deviations)} deviation(s); STOP, do not push the next step ==",
              file=sys.stderr)
        for d in rep.deviations:
            print(f"  DEVIATION  {d}", file=sys.stderr)
        return 1
    if a.command == "snapshot":
        out.write_text(json.dumps(snap, indent=1, sort_keys=True), encoding="utf-8")
        print(f"wrote {out}")
    print(f"== {a.command} {' '.join(a.names)}: OK at {', '.join(revisions) or '(no revision)'} ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
