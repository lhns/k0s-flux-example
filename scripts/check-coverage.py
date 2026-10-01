#!/usr/bin/env python3
"""Cross-component invariants: every X has a Y.

Per-component checks cannot see drift between components, e.g. a new CSI driver that
nothing gives a VolumeSnapshotClass, which fails every Velero backup touching it.

1. Snapshot coverage. Every StorageClass whose provisioner is a CSI driver (a driver name
   is a DNS name; in-tree and local-path provisioners contain a `/`) has a
   VolumeSnapshotClass labelled velero.io/csi-volumesnapshot-class: "true" with that driver.
2. CNPG backups. Every postgresql.cnpg.io Cluster has the barman-cloud plugin with
   isWALArchiver: true, and a ScheduledBackup in its namespace targeting it.
3. Velero static-volume skip. Static PVs (csi.volumeAttributes.staticVolume) on a driver the
   velero-volume-policy does not skip are REPORTED, never failed: see infra/velero.

Not checked: uptime monitoring. uptime-kuma keeps its monitors in its SQLite DB, not in git.

Exemptions go in EXEMPT below, each with a non-empty reason.

Usage: check-coverage.py [RENDERED]  -- RENDERED is the multi-document stream validate.sh
already builds; without it every component is built here.
"""
import pathlib, re, subprocess, sys

import yaml

Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# (invariant, object) -> {"reason": ...}. invariant is "snapshot" (object: StorageClass
# name) or "cnpg" (object: namespace/name).
EXEMPT = {
}

ROOT = pathlib.Path(__file__).resolve().parent.parent
BARMAN = "barman-cloud.cloudnative-pg.io"
VELERO_LABEL = "velero.io/csi-volumesnapshot-class"
KINDS = ("StorageClass", "VolumeSnapshotClass", "Cluster", "ScheduledBackup",
         "PersistentVolume", "ConfigMap")
KIND_RE = re.compile(r"^kind: (%s)\s*$" % "|".join(KINDS), re.M)
failures, notes = [], []


def rendered():
    if len(sys.argv) > 1:
        return pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    out = []
    for k in sorted(ROOT.glob("kube-cluster/*/*/kustomization.yaml")) + \
            [ROOT / "kube-cluster/flux-system/kustomization.yaml"]:
        r = subprocess.run(["kustomize", "build", str(k.parent)], capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"kustomize build {k.parent} failed:\n{r.stderr}")
        out.append(r.stdout)
    return "\n---\n".join(out)


def docs(stream):
    # Parse only the kinds used here: the whole stream is thousands of documents.
    for raw in re.split(r"(?m)^---\s*$", stream):
        if KIND_RE.search(raw):
            doc = yaml.load(raw, Loader=Loader)
            if isinstance(doc, dict):
                yield doc


def exempt(invariant, obj):
    return (invariant, obj) in EXEMPT


for key, entry in EXEMPT.items():
    if not str((entry or {}).get("reason") or "").strip():
        failures.append(f"  EXEMPT{key}: no reason -- every exemption must say why")

storageclasses, snapclasses, clusters, backups, static_pvs = {}, set(), {}, set(), []
skipped_drivers = None
for d in docs(rendered()):
    kind, api = d.get("kind"), str(d.get("apiVersion", ""))
    meta = d.get("metadata") or {}
    spec = d.get("spec") or {}
    if kind == "StorageClass" and api.startswith("storage.k8s.io/"):
        storageclasses[meta.get("name")] = str(d.get("provisioner", ""))
    elif kind == "VolumeSnapshotClass":
        if str((meta.get("labels") or {}).get(VELERO_LABEL)) == "true":
            snapclasses.add(d.get("driver"))
    elif kind == "Cluster" and api.startswith("postgresql.cnpg.io/"):
        clusters[f"{meta.get('namespace')}/{meta.get('name')}"] = spec
    elif kind == "ScheduledBackup" and api.startswith("postgresql.cnpg.io/"):
        backups.add(f"{meta.get('namespace')}/{(spec.get('cluster') or {}).get('name')}")
    elif kind == "PersistentVolume":
        csi = spec.get("csi") or {}
        if str((csi.get("volumeAttributes") or {}).get("staticVolume")) == "true":
            static_pvs.append((meta.get("name"), csi.get("driver")))
    elif kind == "ConfigMap" and meta.get("name") == "velero-volume-policy":
        policy = yaml.load((d.get("data") or {}).get("policy.yaml") or "{}", Loader=Loader)
        skipped_drivers = {
            ((p.get("conditions") or {}).get("csi") or {}).get("driver")
            for p in (policy or {}).get("volumePolicies") or []
            if (p.get("action") or {}).get("type") == "skip"
            and str((((p.get("conditions") or {}).get("csi") or {}).get("volumeAttributes")
                     or {}).get("staticVolume")) == "true"
        }

# 1. snapshot coverage
for name, prov in sorted(storageclasses.items()):
    if "/" in prov or prov in snapclasses or exempt("snapshot", name):
        continue
    failures.append(
        f"  StorageClass/{name}: no VolumeSnapshotClass for driver {prov} labelled "
        f'{VELERO_LABEL}: "true" -- every Velero backup of its volumes fails')

# 2. CNPG
for key, spec in sorted(clusters.items()):
    if exempt("cnpg", key):
        continue
    if not any(p.get("name") == BARMAN and p.get("isWALArchiver") is True
               for p in spec.get("plugins") or []):
        failures.append(f"  Cluster {key}: no {BARMAN} plugin with isWALArchiver: true "
                        f"-- no WAL archive, no point-in-time recovery")
    if key not in backups:
        failures.append(f"  Cluster {key}: no ScheduledBackup with spec.cluster.name "
                        f"targeting it -- no base backup")

# 3. velero static-volume skip (report only)
if skipped_drivers is None:
    notes.append("  velero-volume-policy ConfigMap not found; static-volume skip not checked")
else:
    uncovered = sorted({drv for _, drv in static_pvs if drv not in skipped_drivers})
    for drv in uncovered:
        n = sum(1 for _, d in static_pvs if d == drv)
        notes.append(f"  note: {n} static PV(s) on {drv} not skipped by velero-volume-policy "
                     f"-- Velero will try to snapshot them")

for (invariant, obj), entry in EXEMPT.items():
    if (entry or {}).get("reason"):
        notes.append(f"  exempt {invariant} {obj}: {entry['reason']}")
if notes:
    print(*notes, sep="\n")

if not storageclasses or not clusters:
    failures.append("  no StorageClass or no CNPG Cluster in the rendered stream -- input is broken")

if failures:
    print("== coverage check failed ==", file=sys.stderr)
    print(*failures, sep="\n", file=sys.stderr)
    sys.exit(1)

print(f"{len(storageclasses)} StorageClass(es) snapshot-covered, {len(clusters)} CNPG "
      f"Cluster(s) WAL-archived and scheduled, {len(static_pvs)} static PV(s) seen")
