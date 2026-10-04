#!/usr/bin/env python3
"""Offline test of scripts/check-migration.py: no cluster, no ssh.

A fake replaces the jumphost and answers each `kubectl get` from canned `-o json` objects,
walking the component tests/fixtures/apps/controlled through moving its Namespace into
namespaces/. Each step must pass as given, and fail on every deviation the gate exists to catch.
"""
import contextlib, copy, importlib.util, io, json, pathlib, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gate", HERE / "scripts/check-migration.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
gate.component_dir = lambda name: HERE / "tests/fixtures/apps/controlled"

NAME, OLD, NEW = "app-controlled", "flux-system", "gen-app-controlled"
REV1, REV2 = "main@sha1:1111111111111111111111111111111111111111", "main@sha1:2222222222222222222222222222222222222222"
RESOURCES = {
    "kustomizations.kustomize.toolkit.fluxcd.io": "Kustomization",
    "externalartifacts.source.toolkit.fluxcd.io": "ExternalArtifact",
    "helmreleases.helm.toolkit.fluxcd.io": "HelmRelease",
    "pods": "Pod", "persistentvolumeclaims": "PersistentVolumeClaim",
    "persistentvolumes": "PersistentVolume", "namespaces": "Namespace",
    "secrets": "Secret", "configmaps": "ConfigMap",
}
CLUSTER_SCOPED = {"PersistentVolume", "Namespace"}
passed, failed = 0, 0


class Fake:
    """Answers Live.get from a list of objects, as kubectl get + the jumphost's jq would."""

    def __init__(self, objects):
        self.objects = objects

    def get(self, *queries):
        return [self.one(list(q)) for q in queries]

    def one(self, q):
        ns, everywhere = None, "-A" in q
        if "-n" in q:
            i = q.index("-n")
            ns = q[i + 1]
            del q[i:i + 2]
        q = [a for a in q if a != "-A"]
        out = []
        if "/" in q[0]:  # Kind.group/name refs
            for ref in q:
                kg, name = ref.split("/")
                kind, _, group = kg.partition(".")
                out += [o for o in self.objects if o["kind"] == kind and gate.group_of(o["apiVersion"]) == group
                        and o["metadata"]["name"] == name and o["metadata"].get("namespace", "") == (ns or "")]
        else:
            kinds = {RESOURCES[r] for r in q[0].split(",")}
            names = set(q[1:])
            for o in self.objects:
                m = o["metadata"]
                if o["kind"] not in kinds or (names and m["name"] not in names):
                    continue
                if o["kind"] not in CLUSTER_SCOPED and not everywhere and m.get("namespace") != ns:
                    continue
                out.append(o)
        stripped = copy.deepcopy(out)
        for o in stripped:
            for k in ("data", "stringData", "binaryData"):
                o.pop(k, None)
            o["metadata"].pop("annotations", None)
            o["metadata"].pop("managedFields", None)
        return stripped


def obj(api, kind, name, ns=None, uid=None, rv="1", owner=None, **extra):
    meta = {"name": name, "uid": uid or f"uid-{kind}-{ns}-{name}", "resourceVersion": rv}
    if ns:
        meta["namespace"] = ns
    if owner:
        meta["labels"] = {gate.OWNER_NS: owner[0], gate.OWNER_NAME: owner[1]}
    return {"apiVersion": api, "kind": kind, "metadata": meta, **extra}


def art_rev(name, origin):
    """An ExternalArtifact's revision: a content hash, here one per name and commit."""
    return f"latest@sha256:{name}-{origin[-4:]}"


def ks(name, ns, prune, inventory, origin, gen=2, observed=2, ready="True"):
    rev = art_rev(name, origin)
    o = obj("kustomize.toolkit.fluxcd.io/v1", "Kustomization", name, ns, uid=f"uid-ks-{ns}-{name}",
            spec={"prune": prune, "sourceRef": {"kind": "ExternalArtifact", "name": name, "namespace": OLD}},
            status={"observedGeneration": observed, "lastAppliedRevision": rev,
                    "conditions": [{"type": "Ready", "status": ready, "message": f"Applied revision: {rev}"}],
                    "inventory": {"entries": [{"id": i, "v": "v1"} for i in inventory]}})
    o["metadata"]["generation"] = gen
    return o


def release(version, uid="uid-hr", status="deployed"):
    spec = {"chartRef": {"kind": "ExternalArtifact", "name": f"gen-{NAME}", "namespace": OLD}}
    o = obj("helm.toolkit.fluxcd.io/v2", "HelmRelease", f"gen-{NAME}", OLD, uid=uid, spec=spec,
            status={"observedGeneration": 1, "conditions": [{"type": "Ready", "status": "True", "message": ""}],
                    "history": [{"version": v, "status": "deployed" if v < version else status,
                                 "chartName": "generators"} for v in range(max(1, version - 2), version + 1)]})
    o["metadata"]["generation"] = 1
    return o


def artifact(name, origin):
    return obj("source.toolkit.fluxcd.io/v1", "ExternalArtifact", name, OLD,
               status={"artifact": {"revision": art_rev(name, origin), "metadata": {
                   "org.opencontainers.image.revision": origin}}})


INV_MAIN = ["_controlled__Namespace", "controlled_app__ConfigMap", "controlled_web_apps_Deployment",
            "controlled_data__PersistentVolumeClaim"]
INV_NS = ["_controlled__Namespace"]
INV_SECRETS = ["gen-app-controlled_controlled-values__Secret"]


def state(step):
    """The cluster after each step. step: before, pruneoff, moved (the Namespace into
    namespaces/, prune still off), done (prune back on)."""
    prune = step in ("before", "done")
    rev = REV1 if step == "before" else REV2
    rv = "1" if step == "before" else "2"
    moved = step in ("moved", "done")
    o = [artifact(n, rev) for n in (NAME, f"{NAME}-substitution-secrets", f"{NAME}-namespaces", f"gen-{NAME}")]
    o.append(ks(NAME, NEW, prune, [i for i in INV_MAIN if not (moved and i in INV_NS)], rev))
    o.append(ks(f"{NAME}-substitution-secrets", NEW, prune, INV_SECRETS, rev))
    if moved:
        o.append(ks(f"{NAME}-namespaces", NEW, prune, INV_NS, rev))
    main, sec = (NEW, NAME), (NEW, f"{NAME}-substitution-secrets")
    o += [
        obj("v1", "Namespace", "controlled", rv=rv, owner=(NEW, f"{NAME}-namespaces") if moved else main),
        obj("v1", "ConfigMap", "app", "controlled", rv=rv, owner=main, data={"k": "v"}),
        obj("apps/v1", "Deployment", "web", "controlled", rv=rv, owner=main),
        obj("v1", "PersistentVolumeClaim", "data", "controlled", rv=rv, owner=main,
            spec={"volumeName": "pv-data"}, status={"phase": "Bound"}),
        obj("v1", "PersistentVolume", "pv-data", spec={"claimRef": {"namespace": "controlled", "name": "data",
                                                                     "uid": "uid-PersistentVolumeClaim-controlled-data"}},
            status={"phase": "Bound"}),
        obj("v1", "Pod", "web-abc", "controlled", status={"phase": "Running",
                                                          "containerStatuses": [{"restartCount": 3}]}),
        obj("v1", "Pod", "cron-1", "controlled", status={"phase": "Succeeded"},
            **{"metadata": {"name": "cron-1", "namespace": "controlled", "uid": "uid-cron-1",
                            "ownerReferences": [{"kind": "Job", "name": "cron"}]}}),
        obj("v1", "Namespace", NEW, **{"metadata": {"name": NEW, "uid": "uid-gen",
                                                   "labels": {gate.CONTROL_LABEL: "true"}}}),
        obj("v1", "Secret", "controlled-values", NEW, owner=sec, data={"VALUE": "c2VjcmV0"}),
        obj("v1", "Secret", "sops-age", NEW, data={"age.agekey": "c2VjcmV0"}),
        obj("v1", "ConfigMap", "imagebuild-controlled-built", NEW, data={"ref": "x"}),
    ]
    o.append(release(4 if step == "before" else 5))
    return o


def find(objects, kind, name, ns=None):
    return next(o for o in objects if o["kind"] == kind and o["metadata"]["name"] == name
                and o["metadata"].get("namespace") == ns)


def run(argv, objects):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = gate.main(argv, cluster=Fake(objects))
    return rc, out.getvalue() + err.getvalue()


def expect(label, argv, objects, rc, *needles):
    global passed, failed
    got, text = run(argv, objects)
    missing = [n for n in needles if n not in text]
    if got == rc and not missing:
        passed += 1
        print(f"  ok    {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}: rc {got} (want {rc}), missing {missing}\n{text}", file=sys.stderr)


def mutated(step, change):
    o = state(step)
    change(o)
    return o


with tempfile.TemporaryDirectory() as tmp:
    snap = str(pathlib.Path(tmp) / "snap.json")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            run(["snapshot", NAME, "--out", str(HERE / "tests/snap.json")], state("before"))
        failed += 1
        print("  FAIL  snapshot wrote into the repo", file=sys.stderr)
    except SystemExit:
        passed += 1
        print("  ok    snapshot refuses a path in the repo")
    expect("snapshot", ["snapshot", NAME, "--out", snap], state("before"), 0, "5 inventory objects",
           "namespaces controlled", "1 PVC(s)")
    snapshot_text = pathlib.Path(snap).read_text(encoding="utf-8")
    if "c2VjcmV0" in snapshot_text:
        failed += 1
        print("  FAIL  the snapshot holds a Secret value", file=sys.stderr)

    expect("snapshot refuses an unready component", ["snapshot", NAME, "--out", snap + ".2"],
           mutated("before", lambda o: find(o, "Kustomization", NAME, NEW)["status"]["conditions"][0]
                   .update(status="False")), 1, "is not Ready")

    # step 1: pruneDisabled
    expect("prune-disabled fails while prune is on", ["verify-prune-disabled", NAME], state("before"), 1,
           "has spec.prune True, expected False")
    expect("prune-disabled", ["verify-prune-disabled", NAME], state("pruneoff"), 0)
    expect("an unknown --commit cannot be checked", ["verify-prune-disabled", NAME, "--commit", "0" * 40],
           state("pruneoff"), 2, "git fetch first")
    expect("prune-disabled fails on an unobserved spec", ["verify-prune-disabled", NAME],
           mutated("pruneoff", lambda o: find(o, "Kustomization", NAME, NEW)["status"]
                   .update(observedGeneration=1)), 1, "has not observed its latest spec")
    expect("prune-disabled fails on an old revision", ["verify-prune-disabled", NAME],
           mutated("pruneoff", lambda o: find(o, "Kustomization", NAME, NEW)["status"]
                   .update(lastAppliedRevision=art_rev(NAME, REV1))), 1,
           f"applied {art_rev(NAME, REV1)}, its source is at {art_rev(NAME, REV2)}")
    expect("prune-disabled fails on a Kustomization outside the control namespace",
           ["verify-prune-disabled", NAME], state("pruneoff") + [ks(NAME, OLD, False, INV_MAIN, REV2)], 1,
           f"Kustomization {OLD}/{NAME} is outside {NEW}")

    # step 2: the Namespace moves into namespaces/
    moved = ["verify-moved", NAME, "--snapshot", snap]
    expect("moved", moved, state("moved"), 0, f"{NAME}-namespaces is new")
    expect("moved fails on prune: true", moved,
           mutated("moved", lambda o: find(o, "Kustomization", NAME, NEW)["spec"].update(prune=True)), 1,
           "has spec.prune True, expected False")
    expect("moved fails on a recreated object", moved,
           mutated("moved", lambda o: find(o, "Deployment", "web", "controlled")["metadata"].update(uid="new")), 1,
           "Deployment.apps/controlled/web was RECREATED")
    expect("moved fails on a deleted object", moved,
           mutated("moved", lambda o: o.remove(find(o, "ConfigMap", "app", "controlled"))), 1,
           "ConfigMap/controlled/app is GONE")
    expect("moved fails on a new object", moved,
           mutated("moved", lambda o: (o.append(obj("v1", "ConfigMap", "extra", "controlled", owner=(NEW, NAME))),
                                       find(o, "Kustomization", NAME, NEW)["status"]["inventory"]["entries"]
                                       .append({"id": "controlled_extra__ConfigMap", "v": "v1"}))), 1,
           "ConfigMap/controlled/extra is new")
    expect("moved fails on a stale owner label", moved,
           mutated("moved", lambda o: find(o, "Deployment", "web", "controlled")["metadata"]["labels"]
                   .update({gate.OWNER_NS: OLD})), 1, "is labelled as owned by flux-system/app-controlled")
    expect("moved fails while the Namespace is still owned by the component", moved,
           mutated("moved", lambda o: find(o, "Namespace", "controlled")["metadata"]["labels"]
                   .update({gate.OWNER_NAME: NAME})), 1, "is labelled as owned by")
    expect("moved fails on a recreated namespace", moved,
           mutated("moved", lambda o: find(o, "Namespace", "controlled")["metadata"].update(uid="new")), 1,
           "Namespace//controlled was RECREATED", "Namespace controlled was RECREATED")
    expect("moved fails on a new PVC uid", moved,
           mutated("moved", lambda o: find(o, "PersistentVolumeClaim", "data", "controlled")["metadata"]
                   .update(uid="new")), 1, "PVC controlled/data uid")
    expect("moved fails on a released PV", moved,
           mutated("moved", lambda o: find(o, "PersistentVolume", "pv-data")["status"].update(phase="Released")),
           1, "PV pv-data is Released, was Bound")
    expect("moved fails on a deleted PV", moved,
           mutated("moved", lambda o: o.remove(find(o, "PersistentVolume", "pv-data"))), 1, "PV pv-data")
    expect("moved fails on a restart", moved,
           mutated("moved", lambda o: find(o, "Pod", "web-abc", "controlled")["status"]["containerStatuses"][0]
                   .update(restartCount=4)), 1, "pod controlled/web-abc restarted: 3 -> 4")
    expect("moved fails on a recreated pod", moved,
           mutated("moved", lambda o: find(o, "Pod", "web-abc", "controlled")["metadata"].update(uid="new")), 1,
           "pod controlled/web-abc was RECREATED")
    expect("moved ignores Job pods", moved,
           mutated("moved", lambda o: o.remove(find(o, "Pod", "cron-1", "controlled"))), 0)
    expect("moved fails without sops-age", moved,
           mutated("moved", lambda o: o.remove(find(o, "Secret", "sops-age", NEW))), 1,
           "Secret/sops-age is missing from gen-app-controlled")
    expect("moved fails without the ref ConfigMap", moved,
           mutated("moved", lambda o: o.remove(find(o, "ConfigMap", "imagebuild-controlled-built", NEW))), 1,
           "ConfigMap/imagebuild-controlled-built is missing")
    expect("moved fails on an unlabelled control namespace", moved,
           mutated("moved", lambda o: find(o, "Namespace", NEW)["metadata"].pop("labels")), 1,
           "lacks the label")
    expect("moved fails on a lost Kustomization", moved,
           mutated("moved", lambda o: o.remove(find(o, "Kustomization", f"{NAME}-substitution-secrets", NEW))), 1,
           "expected in gen-app-controlled")
    expect("moved fails on a Kustomization outside the control namespace", moved,
           state("moved") + [ks(NAME, OLD, False, INV_MAIN, REV2)], 1, f"Kustomization {OLD}/{NAME} is outside")

    # step 3: pruneDisabled removed
    done = ["verify-done", NAME, "--snapshot", snap]
    expect("done", done, state("done"), 0)
    expect("done fails while prune is off", done, state("moved"), 1, "has spec.prune False, expected True")
    expect("done fails on a deleted object", done,
           mutated("done", lambda o: o.remove(find(o, "Deployment", "web", "controlled"))), 1,
           "Deployment.apps/controlled/web is GONE")

    # Sources: each Kustomization on its own ExternalArtifact.
    pd = ["verify-prune-disabled", NAME]
    expect("prune-disabled names the commit", pd, state("pruneoff"), 0, "OK at " + REV2)
    expect("prune-disabled fails on someone else's artifact", pd,
           mutated("pruneoff", lambda o: find(o, "Kustomization", NAME, NEW)["spec"]["sourceRef"]
                   .update(name=f"{NAME}-substitution-secrets")), 1, "expected its own")
    expect("prune-disabled fails on a GitRepository source", pd,
           mutated("pruneoff", lambda o: find(o, "Kustomization", NAME, NEW)["spec"]["sourceRef"]
                   .update(kind="GitRepository", name="flux-system")), 1,
           f"reads GitRepository/{OLD}/flux-system, expected its own")
    expect("prune-disabled fails on a missing artifact", pd,
           mutated("pruneoff", lambda o: o.remove(find(o, "ExternalArtifact", NAME, OLD))), 1,
           f"ExternalArtifact/{OLD}/{NAME}, which does not exist")

    def no_origin(o):
        for a in o:
            if a["kind"] == "ExternalArtifact":
                a["status"]["artifact"]["metadata"] = {}
    expect("--commit fails when no artifact names its commit", pd + ["--commit", "HEAD"],
           mutated("pruneoff", no_origin), 1, "no live source names its commit")

    snap3 = str(pathlib.Path(tmp) / "snap3.json")
    expect("snapshot with prune off", ["snapshot", NAME, "--out", snap3], state("pruneoff"), 0)
    rec = json.loads(pathlib.Path(snap3).read_text(encoding="utf-8"))["components"][NAME]
    if rec["release"]["version"] != 5 or rec["kustomizations"][NAME]["sourceRef"]["kind"] != "ExternalArtifact":
        failed += 1
        print(f"  FAIL  the snapshot does not record the release and sourceRef: {rec['release']}", file=sys.stderr)
    expect("snapshot fails without the gen- release", ["snapshot", NAME, "--out", snap3 + ".2"],
           mutated("pruneoff", lambda o: o.remove(find(o, "HelmRelease", f"gen-{NAME}", OLD))), 1,
           f"HelmRelease flux-system/gen-{NAME} not found")


class Broken:
    def get(self, *queries):
        raise gate.CannotCheck("ssh: connect to host: timed out")


err = io.StringIO()
with contextlib.redirect_stderr(err):
    rc = gate.main(["verify-prune-disabled", NAME], cluster=Broken())
if rc == 2 and "CANNOT CHECK" in err.getvalue():
    passed += 1
    print("  ok    an unreachable cluster is exit 2, never a pass")
else:
    failed += 1
    print(f"  FAIL  unreachable cluster: rc {rc}", file=sys.stderr)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
