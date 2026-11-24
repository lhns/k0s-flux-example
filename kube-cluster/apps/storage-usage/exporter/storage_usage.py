#!/usr/bin/env python3
"""Ceph storage usage exporter: which pool, filesystem directory, RBD image and S3 bucket holds
the bytes, and who owns them. Tasks run on a schedule in two background threads (the daily
rbd_k8s scan, and all other tasks one at a time); /metrics only serves the text rendered after
the last task. Design: ../README.md."""

import datetime
import hashlib
import hmac
import http.server
import json
import logging
import math
import os
import posixpath
import re
import ssl
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request

try:
    import cephfs
    import rados
    import rbd
except ImportError:  # the unit tests run without the Ceph bindings
    cephfs = rados = rbd = None

log = logging.getLogger("storage-usage")

FAMILIES = {
    "ceph_usage_bytes": "Bytes per pool, split into disjoint leaves that sum to the pool's stored bytes. "
                        "kind: live, snapshot (CephFS: a lower bound), residual (snapshots, pending purge, "
                        "unattributed; signed), unmeasured (task switched off).",
    "ceph_usage_pool_stored_bytes": "Pool stored bytes (ceph df) when the exporter last read them.",
    "ceph_usage_pool_raw_ratio": "Raw bytes used per stored byte (replication or erasure coding, allocation).",
    "ceph_usage_snapshots": "Snapshots on a CephFS directory or an RBD image (RBD: all namespaces, trash included).",
    "ceph_usage_snapshot_oldest_timestamp_seconds": "Creation time of the oldest snapshot.",
    "ceph_usage_snapshot_newest_timestamp_seconds": "Creation time of the newest snapshot.",
    "ceph_usage_snapshot_pinned_min_bytes": "CephFS: lower bound of the bytes a directory's oldest snapshot holds "
                                            "that are no longer live (sum over the walked leaves below it).",
    "ceph_usage_rgw_bucket_objects": "Objects in an S3 bucket.",
    "ceph_usage_pv_info": "1 per claimed PersistentVolume (app: claim namespace, volume: claim name). "
                          "static=\"true\" if no CSI provisioner created it. Joins kubelet volume stats to the "
                          "dynamic PVCs.",
    "ceph_usage_task_last_success_timestamp_seconds": "End of the task's last successful run.",
    "ceph_usage_task_last_duration_seconds": "Duration of the task's last run.",
    "ceph_usage_task_last_run_ok": "1 if the task's last run succeeded.",
    "ceph_usage_task_item_errors": "Items (images, directories) the task's last run skipped on an error; "
                                   "cephfs: one series per filesystem (1 = it failed).",
    "ceph_usage_task_partial": "1 if the task's last run returned only part of its data (time budget, a "
                               "failed filesystem); the data is kept and last_run_ok is 0.",
}

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
FILES = "(files)"


# ---------------------------------------------------------------------------------------------
# Results and rendering

class Result:
    def __init__(self):
        self.samples = []  # (family, labels, value)
        self.errors = 0
        self.fs_errors = {}  # cephfs: filesystem -> 1 if it failed, else 0
        self.partial = False

    def error_series(self):
        """[(labels, value)] for ceph_usage_task_item_errors."""
        if self.fs_errors:
            return [({"fs": fs}, n) for fs, n in sorted(self.fs_errors.items())]
        return [({}, self.errors)]

    def add(self, family, value, **labels):
        self.samples.append((family, {k: str(v) for k, v in labels.items() if v not in (None, "")}, value))


def leaf(res, pools, pool, kind, value, source, consumer, app, volume, item, **extra):
    res.add("ceph_usage_bytes", value, source=source, pool=pool,
            device_class=pools.get(pool, {}).get("class", "unknown"), kind=kind,
            consumer=consumer, app=app, volume=volume, item=item, **extra)


def residual(res, pools, pool, stored, attributed, source, app, item, **extra):
    leaf(res, pools, pool, "residual", stored - attributed, source, "(residual)", app, app, item, **extra)


def esc(v):
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def fmt(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return repr(float(v))


def render(samples):
    by = {}
    for fam, labels, value in samples:
        by.setdefault(fam, []).append((labels, value))
    out = []
    for fam, help_text in FAMILIES.items():
        rows = by.get(fam)
        if not rows:
            continue
        out.append(f"# HELP {fam} {help_text}")
        out.append(f"# TYPE {fam} gauge")
        for labels, value in rows:
            ls = ",".join(f'{k}="{esc(labels[k])}"' for k in sorted(labels))
            out.append(f"{fam}{{{ls}}} {fmt(value)}" if ls else f"{fam} {fmt(value)}")
    return ("\n".join(out) + "\n").encode()


# ---------------------------------------------------------------------------------------------
# Ceph and Kubernetes access

def connect():
    cid = os.environ["CEPH_CLIENT_ID"]
    who = {"name": cid} if cid.startswith("client.") else {"rados_id": cid}
    conf = {"mon_host": CFG["mon_host"], "key": os.environ["CEPH_KEY"],
            # A degraded PG must not hang a task forever.
            "rados_osd_op_timeout": "60", "rados_mon_op_timeout": "30", "client_mount_timeout": "30",
            # The pod's uid cannot traverse 0700 dirs; read access is the cap's (mds 'allow r') to grant.
            "client_permissions": "false",
            "log_file": "", "log_to_stderr": "false", "err_to_stderr": "true"}
    # /dev/null: no ceph.conf in the image, and an empty conffile makes librados search for one.
    c = rados.Rados(conffile="/dev/null", conf=conf, **who)
    c.connect(timeout=30)
    return c


def mon(c, prefix, **args):
    ret, out, err = c.mon_command(json.dumps(dict(args, prefix=prefix, format="json")), b"", timeout=30)
    if ret:
        raise RuntimeError(f"mon command {prefix}: {ret} {err}")
    return json.loads(out)


def device_class(rule):
    for step in (rule or {}).get("steps", []):
        if step.get("op") == "take":
            name = step.get("item_name", "")
            return name.split("~", 1)[1] if "~" in name else "any"
    return "unknown"


def read_pools(c):
    """{pool: {stored, used, class}} and {fs: [data pools]}."""
    df = mon(c, "df", detail="detail")
    rules = {r["rule_id"]: r for r in mon(c, "osd crush rule dump")}
    rule_of = {p["pool_name"]: p.get("crush_rule") for p in mon(c, "osd pool ls", detail="detail")}
    pools = {p["name"]: {"stored": p["stats"]["stored"], "used": p["stats"]["bytes_used"],
                         "class": device_class(rules.get(rule_of.get(p["name"])))} for p in df["pools"]}
    fss = {f["name"]: f["data_pools"] for f in mon(c, "fs ls")}
    return pools, fss


def list_pvs():
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    req = urllib.request.Request(f"https://{host}:{port}/api/v1/persistentvolumes",
                                 headers={"Authorization": f"Bearer {token}"})
    ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read())["items"]


class Owners:
    """PV -> claim, keyed the way the exporter meets the volume in Ceph."""

    def __init__(self, pvs):
        self.subvolumes = {}  # (fs, subvolume dir) -> (namespace, volume)
        self.static = {}      # fs -> {rootPath: [(namespace, volume)]}
        self.images = {}      # (pool, image) -> (namespace, volume)
        self.claims = []      # (namespace, claim, static, driver, storage class)
        for pv in pvs:
            csi = pv.get("spec", {}).get("csi") or {}
            va = csi.get("volumeAttributes") or {}
            claim = pv["spec"].get("claimRef") or {}
            ns = claim.get("namespace") or "(unclaimed)"
            vol = claim.get("name") or pv["metadata"]["name"]
            if claim.get("name"):
                provisioned = "pv.kubernetes.io/provisioned-by" in (pv["metadata"].get("annotations") or {})
                self.claims.append((ns, claim["name"], not provisioned, csi.get("driver", ""),
                                    pv["spec"].get("storageClassName", "")))
            phase = pv.get("status", {}).get("phase", "")
            if phase and phase != "Bound":
                vol = f"{vol} ({phase.lower()})"
            if "subvolumePath" in va and "fsName" in va:
                d = posixpath.dirname(va["subvolumePath"].rstrip("/"))
                self.subvolumes[(va["fsName"], d)] = (ns, vol)
            elif "rootPath" in va and "fsName" in va:
                root = posixpath.normpath(va["rootPath"]) if va["rootPath"] != "/" else "/"
                self.static.setdefault(va["fsName"], {}).setdefault(root, []).append((ns, vol))
            elif "imageName" in va:
                self.images[(va.get("pool"), va["imageName"])] = (ns, vol)

    def static_roots(self, fs):
        """Static rootPaths that own data: "/" mounts read a whole fs (backups, file browsers)."""
        return [r for r in self.static.get(fs, {}) if r != "/"]

    def pv_info(self, res):
        for ns, claim, static, driver, sc in self.claims:
            res.add("ceph_usage_pv_info", 1, app=ns, volume=claim, static="true" if static else "false",
                    driver=driver, storage_class=sc)

    def static_owner(self, fs, path):
        """(app, volume, used_by) of the longest static rootPath above path. One namespace owns the
        path as its app; several share it, so the directory name is the app and they are `used_by`."""
        roots = self.static_roots(fs)
        hits = [r for r in roots if path == r or path.startswith(r + "/")]
        if not hits and is_subvolume_dir(path):
            # a static PV pointing into a dynamic subvolume (a migrated volume)
            hits = [r for r in roots if r.startswith(path + "/")]
        if not hits:
            return None
        root = max(hits, key=len)
        claims = sorted(set(self.static[fs][root]))
        namespaces = sorted({ns for ns, _ in claims})
        volumes = ",".join(v for _, v in claims)
        if len(namespaces) == 1:
            return namespaces[0], volumes, ""
        return posixpath.basename(root), volumes, ",".join(namespaces)


def is_subvolume_dir(path):
    parts = [p for p in path.split("/") if p]
    return len(parts) == 3 and parts[0] == "volumes"


# Consumers that are not a filesystem: a filesystem named like one gets the prefix `fs:`.
FIXED_CONSUMERS = {"k8s", "k8s-static", "swarm", "proxmox", "s3", "ceph", "(residual)", "(not measured)"}


def fs_consumer(fs):
    return f"fs:{fs}" if fs in FIXED_CONSUMERS else fs


def attribute(fs, path, owners, swarm_roots):
    """(consumer, app, volume, used_by) of a CephFS walk leaf or snapshot directory."""
    p = path[:-len("/" + FILES)] or "/" if path.endswith("/" + FILES) else path
    dyn = owners.subvolumes.get((fs, p))
    if dyn:
        return ("k8s",) + dyn + ("",)
    st = owners.static_owner(fs, p)
    if st:
        return ("k8s-static",) + st
    parts = [x for x in p.split("/") if x]
    if parts[:1] == ["volumes"]:
        return ("k8s", "(no PV)", path, "")
    for root in swarm_roots:
        rp = [x for x in root.split("/") if x]
        if parts[:len(rp)] == rp:
            return ("swarm", parts[len(rp)] if len(parts) > len(rp) else "(all stacks)", path, "")
    return (fs_consumer(fs), parts[0] if parts else "(root)", path, "")


# ---------------------------------------------------------------------------------------------
# CephFS

class CephFS:
    """The few libcephfs calls the walk needs; missing paths and xattrs read as None."""

    def __init__(self, c, name):
        self.fs = cephfs.LibCephFS(rados_inst=c)
        self.fs.init()
        self.fs.mount(filesystem_name=name.encode())

    def close(self):
        try:
            self.fs.unmount()
        finally:
            self.fs.shutdown()

    def listdirs(self, path):
        try:
            d = self.fs.opendir(path.encode())
        except cephfs.ObjectNotFound:
            return None
        out = []
        try:
            e = self.fs.readdir(d)
            while e:
                if e.d_name not in (b".", b"..") and e.is_dir():
                    out.append(e.d_name.decode(errors="surrogateescape"))
                e = self.fs.readdir(d)
        finally:
            self.fs.closedir(d)
        return out

    def xattr(self, path, key):
        try:
            return self.fs.getxattr(path.encode(errors="surrogateescape"), key).decode()
        except (cephfs.ObjectNotFound, cephfs.NoData):
            return None

    def snaps(self, path):
        """Own snapshots as [(name, btime)]; `_name_ino` entries are an ancestor's."""
        names = [n for n in (self.listdirs(join(path, ".snap")) or []) if not n.startswith("_")]
        out = []
        for n in names:
            bt = self.xattr(join(path, ".snap", n), "ceph.snap.btime")
            out.append((n, float(bt) if bt else 0.0))
        return out


def join(*parts):
    return "/" + "/".join(x.strip("/") for x in parts if x.strip("/"))


def num(fs, path, key):
    v = fs.xattr(path, key)
    return int(v) if v is not None else None


class Walk:
    """Leaves of one filesystem: directories at the configured depth, static PV roots and
    subvolumes as their own leaves, and a `(files)` leaf for the files directly in every
    expanded directory, so the leaves sum to the root's rbytes."""

    def __init__(self, fs, cfg, static_roots, max_children=2000, max_snap_dirs=5000, max_snap_depth=8):
        self.fs = fs
        self.depth = cfg.get("depth", 2)
        self.depths = cfg.get("depths", {})
        self.static_roots = static_roots
        self.max_children = max_children
        self.max_snap_dirs = max_snap_dirs
        self.max_snap_depth = max_snap_depth
        self.leaves = []    # dicts: path, bytes, pool, dir (expanded dir for (files)), snap
        self.expanded = {}  # path -> {bytes, children: [paths]}
        self.snapdirs = []  # dicts: path, snaps, anchor (leaf path if below the leaves)
        self.notes = []

    def leaf_depth(self, path):
        best, depth = "", self.depth
        for p, d in self.depths.items():
            if (path == p or path.startswith(p.rstrip("/") + "/")) and len(p) > len(best):
                best, depth = p, d
        return depth

    def expand(self, path, d):
        if any(r.startswith(path.rstrip("/") + "/") for r in self.static_roots) and not is_subvolume_dir(path):
            return True
        parts = [x for x in path.split("/") if x]
        if parts[:1] == ["volumes"] and d < 3:
            return True  # /volumes/<group>/<subvolume>: ceph-csi's layout
        return d < self.leaf_depth(path)

    def run(self):
        rb = num(self.fs, "/", "ceph.dir.rbytes")
        self.visit("/", 0, self.fs.xattr("/", "ceph.dir.layout.pool"), rb)
        self.find_deep_snapshots()
        self.lower_bounds()
        return self

    def visit(self, path, d, pool, rb):
        pool = self.fs.xattr(path, "ceph.dir.layout.pool") or pool if path != "/" else pool
        snaps = self.fs.snaps(path)
        if snaps:
            self.snapdirs.append({"path": path, "snaps": snaps, "anchor": None})
        children = self.fs.listdirs(path) if self.expand(path, d) else None
        if children is not None and len(children) > self.max_children:
            self.notes.append(f"{path}: {len(children)} subdirectories, kept as one leaf")
            children = None
        if children is None:
            self.leaves.append({"path": path, "bytes": rb, "pool": pool, "dir": None, "snap": 0})
            return
        total, paths = 0, []
        for name in sorted(children):
            cp = join(path, name)
            crb = num(self.fs, cp, "ceph.dir.rbytes")
            if crb is None:
                continue  # removed since the listing
            total += crb
            paths.append(cp)
            self.visit(cp, d + 1, pool, crb)
        self.expanded[path] = {"bytes": rb, "children": paths}
        # rstats lag: a parent can briefly read less than its children
        self.leaves.append({"path": join(path, FILES), "bytes": max(0, rb - total), "pool": pool,
                            "dir": path, "snap": 0})

    def find_deep_snapshots(self):
        """Snapshot directories below the leaves, where a leaf's rsnaps exceeds its own count.
        rsnaps counts snapshots in the subtree, not an ancestor's."""
        own = {s["path"]: len(s["snaps"]) for s in self.snapdirs}
        budget = [self.max_snap_dirs]

        def search(path, anchor, depth):
            for name in self.fs.listdirs(path) or []:
                if budget[0] <= 0:
                    return
                budget[0] -= 1
                cp = join(path, name)
                rs = num(self.fs, cp, "ceph.dir.rsnaps") or 0
                if rs <= 0:
                    continue
                snaps = self.fs.snaps(cp)
                if snaps:
                    self.snapdirs.append({"path": cp, "snaps": snaps, "anchor": anchor})
                if rs > len(snaps) and depth < self.max_snap_depth:
                    search(cp, anchor, depth + 1)

        for lf in self.leaves:
            if lf["dir"] is not None:
                continue
            rs = num(self.fs, lf["path"], "ceph.dir.rsnaps") or 0
            if rs > own.get(lf["path"], 0):
                search(lf["path"], lf["path"], lf["path"].count("/") + 1)
        if budget[0] <= 0:
            self.notes.append(f"snapshot search stopped after {self.max_snap_dirs} directories")

    def lower_bounds(self):
        """Per snapshot dir D, from its oldest snapshot: max(0, then - now) per leaf below D.
        A leaf's snapshot bytes are the largest bound over the D at or above it, or the sum over
        the D below it, whichever is larger: never more than one snapshot's bound is counted."""
        below = {}
        for sd in self.snapdirs:
            oldest = min(sd["snaps"], key=lambda s: s[1])[0]
            base = join(sd["path"], ".snap", oldest)
            sd["pinned"] = 0
            if sd["anchor"] is not None:
                then = num(self.fs, base, "ceph.dir.rbytes") or 0
                now = num(self.fs, sd["path"], "ceph.dir.rbytes") or 0
                sd["pinned"] = max(0, then - now)
                below[sd["anchor"]] = below.get(sd["anchor"], 0) + sd["pinned"]
                continue
            d = sd["path"].rstrip("/")
            for lf in self.leaves:
                p = lf["dir"] if lf["dir"] is not None else lf["path"]
                if not (p == sd["path"] or p.startswith(d + "/")):
                    continue
                rel = p[len(d):]
                then = num(self.fs, base + rel, "ceph.dir.rbytes")
                if then is None:
                    continue  # created after the snapshot
                if lf["dir"] is not None:
                    # files directly in the dir then, which includes directories deleted since
                    then -= sum(num(self.fs, base + c[len(d):], "ceph.dir.rbytes") or 0
                                for c in self.expanded[lf["dir"]]["children"])
                cand = max(0, then - lf["bytes"])
                sd["pinned"] += cand
                lf["snap"] = max(lf["snap"], cand)
        for lf in self.leaves:
            lf["snap"] = max(lf["snap"], below.get(lf["path"], 0))


def measure_cephfs(c, name, fcfg, pools, fss, owners, swarm_roots):
    """The samples of one filesystem, as a Result of their own."""
    res = Result()
    if name not in fss:
        raise RuntimeError("no such filesystem")
    t0 = time.time()
    fs = CephFS(c, name)
    try:
        w = Walk(fs, fcfg, owners.static_roots(name)).run()
    finally:
        fs.close()
    for note in w.notes:
        log.info("cephfs %s: %s", name, note)
    attributed = {p: 0 for p in fss[name]}
    for lf in w.leaves:
        consumer, app, volume, used_by = attribute(name, lf["path"], owners, swarm_roots)
        pool = lf["pool"] or fss[name][0]
        labels = dict(source="cephfs", fs=name, consumer=consumer, app=app, volume=volume, item=lf["path"],
                      used_by=used_by)
        leaf(res, pools, pool, "live", lf["bytes"], **labels)
        attributed[pool] = attributed.get(pool, 0) + lf["bytes"] + lf["snap"]
        if lf["snap"] > 0:
            leaf(res, pools, pool, "snapshot", lf["snap"], **labels)
    # Per data pool, so each pool's leaves sum to its stored bytes. A leaf's pool is the layout
    # of its directory; files below it with another layout land in the other pool's residual.
    for pool, att in attributed.items():
        if pool in pools:
            residual(res, pools, pool, pools[pool]["stored"], att, "cephfs", "(snapshots + pending purge)",
                     f"{pool}:(residual)", fs=name)
    for sd in w.snapdirs:
        consumer, app, volume, used_by = attribute(name, sd["path"], owners, swarm_roots)
        labels = dict(source="cephfs", fs=name, consumer=consumer, app=app, volume=volume, item=sd["path"],
                      used_by=used_by)
        snapshot_stats(res, [t for _, t in sd["snaps"]], **labels)
        res.add("ceph_usage_snapshot_pinned_min_bytes", sd["pinned"], **labels)
    log.info("cephfs %s: %d leaves, %d snapshot dirs, %.1fs", name, len(w.leaves), len(w.snapdirs),
             time.time() - t0)
    return res


def task_cephfs(c, pools, fss):
    """A filesystem that fails keeps its last samples (stale, flagged by partial and its item error),
    or is one `unmeasured` leaf per data pool if it has none; the others are not affected.
    Every filesystem failing raises, so the previous result stays whole."""
    res = Result()
    owners = Owners(list_pvs())
    owners.pv_info(res)
    cfg = CFG["cephfs"]
    swarm_roots = cfg.get("swarm_roots", [])
    prev = STATE.results.get("cephfs")
    for name, fcfg in cfg["filesystems"].items():
        try:
            fres = measure_cephfs(c, name, fcfg, pools, fss, owners, swarm_roots)
        except Exception:
            log.warning("cephfs %s failed:\n%s", name, traceback.format_exc())
            res.errors += 1
            res.fs_errors[name] = 1
            res.partial = True
            old = [smp for smp in (prev.samples if prev else []) if smp[1].get("fs") == name]
            if old:
                res.samples.extend(old)
            else:
                for pool in fss.get(name, []):
                    if pool in pools:
                        leaf(res, pools, pool, "unmeasured", pools[pool]["stored"], "cephfs", "(not measured)",
                             f"cephfs {name} failed", pool, f"{pool}:(unmeasured)", fs=name)
            continue
        res.fs_errors[name] = 0
        res.samples.extend(fres.samples)
    if res.fs_errors and all(res.fs_errors.values()):
        raise RuntimeError("every filesystem failed")
    return res


# ---------------------------------------------------------------------------------------------
# RBD

VMID = re.compile(r"^(?:vm|base)-(\d+)-")


def allocated(img, size, from_snap):
    """Bytes of the objects that exist (whole objects), or that changed since from_snap."""
    total = [0]

    def cb(_offset, length, exists):
        if exists:
            total[0] += length

    img.diff_iterate(0, size, from_snap, cb, include_parent=False, whole_object=True)
    return total[0]


def du(img):
    """What `rbd du` reports, from the object map: (head bytes, head + snapshot bytes).
    Only user-namespace snapshots can be selected by name."""
    snaps = sorted((s for s in img.list_snaps() if s.get("namespace", 0) == 0), key=lambda s: s["id"])
    total, prev = 0, None
    for s in snaps:
        img.set_snap(s["name"])
        if img.flags() & (rbd.RBD_FLAG_OBJECT_MAP_INVALID | rbd.RBD_FLAG_FAST_DIFF_INVALID):
            raise ValueError(f"object map of snapshot {s['name']} invalid")
        total += allocated(img, s["size"], prev)
        prev = s["name"]
    img.set_snap(None)
    size = img.size()
    total += allocated(img, size, prev)
    head = allocated(img, size, None) if prev else total
    return head, total


def snap_stamps(img):
    """[(snapshot name, creation time)] of every snapshot, any namespace."""
    return [(s["name"], img.get_snap_timestamp(s["id"]).timestamp()) for s in img.list_snaps()]


def snapshot_stats(res, times, **labels):
    res.add("ceph_usage_snapshots", len(times), **labels)
    res.add("ceph_usage_snapshot_oldest_timestamp_seconds", min(times), **labels)
    res.add("ceph_usage_snapshot_newest_timestamp_seconds", max(times), **labels)


def rbd_images(c, pool, namespace, fn, res, deadline=None, skipped=None):
    """fn(name, image) for every image. Returns False if the deadline stopped it, with the
    names left over appended to `skipped`."""
    io = c.open_ioctx(pool)
    try:
        io.set_namespace(namespace)
        names = list(rbd.RBD().list(io))
        for i, name in enumerate(names):
            if deadline and time.time() > deadline:
                log.warning("%s/%s: over the time budget, %d of %d images not measured", pool, namespace,
                            len(names) - i, len(names))
                if skipped is not None:
                    skipped.extend(names[i:])
                return False
            try:
                img = rbd.Image(io, name, read_only=True)
            except rbd.ImageNotFound:
                continue  # removed since the listing (Proxmox fleece images come and go)
            try:
                fn(name, img)
            except (rbd.Error, ValueError) as e:
                log.warning("%s/%s/%s: %s", pool, namespace, name, e)
                res.errors += 1
            finally:
                img.close()
        return True
    finally:
        io.close()


def task_rbd_proxmox(c, pools, fss):
    res = Result()
    for spec in CFG["rbd_proxmox"]["images"]:
        pool, ns = spec["pool"], spec["namespace"]
        attributed = [0]

        def measure(name, img):
            m = VMID.match(name)
            vmid = m.group(1) if m else ""
            if not img.features() & rbd.RBD_FEATURE_FAST_DIFF or \
                    img.flags() & (rbd.RBD_FLAG_OBJECT_MAP_INVALID | rbd.RBD_FLAG_FAST_DIFF_INVALID):
                raise ValueError("no valid fast-diff; skipped rather than scanned")
            head, total = du(img)
            stamps = snap_stamps(img)
            labels = dict(source="rbd-proxmox", consumer="proxmox", app=vmid or "(unknown)", volume=name, item=name,
                          vmid=vmid)
            leaf(res, pools, pool, "live", head, **labels)
            if total > head:
                leaf(res, pools, pool, "snapshot", total - head, **labels)
            attributed[0] += total
            if stamps:
                snapshot_stats(res, [t for _, t in stamps], pool=pool, **labels)

        rbd_images(c, pool, ns, measure, res)
        residual(res, pools, pool, pools[pool]["stored"], attributed[0], "rbd-proxmox",
                 "(other namespaces + pending trim)", f"{pool}:(residual)")
    return res


def task_rbd_k8s(c, pools, fss):
    """Heads only: these images have no object map, so every head is a scan of its objects.
    Clones (`csi-snap-*`) write nothing and read from the parent's trash snapshots.
    Over `max_seconds` the run is partial: the images measured so far are emitted, the rest as 0
    (the dashboard reads this task over 26 h, which would otherwise count yesterday's value
    twice) and the residual is renamed, so the pool's leaves still sum to its stored bytes."""
    res = Result()
    tcfg = CFG["tasks"]["rbd_k8s"]
    deadline = time.time() + tcfg.get("max_seconds", 3600)
    skip = tuple(CFG["rbd_k8s"].get("skip_prefixes", []))
    owners = Owners(list_pvs())
    for spec in CFG["rbd_k8s"]["images"]:
        pool, ns = spec["pool"], spec["namespace"]
        attributed = [0]
        left = []

        def labels_of(name):
            ns_, vol = owners.images.get((pool, name), ("(no PV)", name))
            return dict(source="rbd-k8s", consumer="k8s", app=ns_, volume=vol, item=name)

        def measure(name, img):
            if name.startswith(skip):
                return
            labels = labels_of(name)
            head = allocated(img, img.size(), None)
            leaf(res, pools, pool, "live", head, **labels)
            attributed[0] += head
            stamps = snap_stamps(img)
            if stamps:
                snapshot_stats(res, [t for _, t in stamps], pool=pool, **labels)

        complete = rbd_images(c, pool, ns, measure, res, deadline, left)
        for name in left:
            if not name.startswith(skip):
                leaf(res, pools, pool, "live", 0, **labels_of(name))
        res.partial = res.partial or not complete
        residual(res, pools, pool, pools[pool]["stored"], attributed[0], "rbd-k8s",
                 "(snapshots + pending trim)" if complete else "(unmeasured + snapshots)", f"{pool}:(residual)")
    return res


# ---------------------------------------------------------------------------------------------
# RGW

def sigv4_get(base, path, query, access_key, secret_key, now=None):
    """GET signed with AWS Signature V4 (RGW ignores the region)."""
    u = urllib.parse.urlsplit(base)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amzdate, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    q = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
                 for k, v in sorted(query.items()))
    payload = hashlib.sha256(b"").hexdigest()
    headers = {"host": u.netloc, "x-amz-content-sha256": payload, "x-amz-date": amzdate}
    signed = ";".join(sorted(headers))
    canonical = "\n".join(["GET", urllib.parse.quote(path), q,
                           "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, payload])
    scope = f"{date}/us-east-1/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret_key).encode()
    for part in (date, "us-east-1", "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return urllib.request.Request(f"{base}{path}?{q}", headers={
        "x-amz-content-sha256": payload, "x-amz-date": amzdate, "Authorization": auth})


def task_rgw(c, pools, fss):
    res = Result()
    cfg = CFG["rgw"]
    req = sigv4_get(cfg["url"], "/admin/bucket", {"stats": "true", "format": "json"},
                    os.environ["RGW_ACCESS_KEY"], os.environ["RGW_SECRET_KEY"])
    with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r:
        buckets = json.loads(r.read())
    pool = cfg["data_pool"]
    attributed = 0
    for b in buckets:
        usage = b.get("usage", {}).values()
        size = sum(u.get("size_actual", 0) for u in usage)
        labels = dict(source="rgw", consumer="s3", app=b.get("owner", ""), volume=b["bucket"])
        leaf(res, pools, pool, "live", size, item=b["bucket"], **labels)
        res.add("ceph_usage_rgw_bucket_objects", sum(u.get("num_objects", 0) for u in usage),
                app=labels["app"], bucket=b["bucket"])
        attributed += size
    residual(res, pools, pool, pools[pool]["stored"], attributed, "rgw", "(unattributed + pending gc)",
             f"{pool}:(residual)")
    return res


# ---------------------------------------------------------------------------------------------
# Scheduling and state

TASKS = {"cephfs": task_cephfs, "rbd_proxmox": task_rbd_proxmox, "rbd_k8s": task_rbd_k8s, "rgw": task_rgw}


def owned_pools(task, fss):
    if task == "cephfs":
        return {p for f in CFG["cephfs"]["filesystems"] for p in fss.get(f, [])}
    if task in ("rbd_proxmox", "rbd_k8s"):
        return {s["pool"] for s in CFG[task]["images"]}
    if task == "rgw":
        return {CFG["rgw"]["data_pool"]}
    return set()


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.pools, self.fss, self.pools_at = {}, {}, 0.0
        self.results = {}  # task -> Result of its last run that returned data (partial ones too)
        self.status = {}   # task -> {last_success, duration, ok, errors [(labels, value)], partial}
        self.text = b""

    def rerender(self):
        samples = []
        owned = {}
        for task in TASKS:
            for p in owned_pools(task, self.fss):
                owned[p] = task
        for task, res in self.results.items():
            samples.extend(res.samples)
        for pool, p in sorted(self.pools.items()):
            cls = p["class"]
            samples.append(("ceph_usage_pool_stored_bytes", {"pool": pool, "device_class": cls}, p["stored"]))
            if p["stored"]:
                samples.append(("ceph_usage_pool_raw_ratio", {"pool": pool, "device_class": cls},
                                p["used"] / p["stored"]))
            task = owned.get(pool)
            if task is None:
                samples.append(("ceph_usage_bytes", {
                    "source": "pools", "pool": pool, "device_class": cls, "kind": "live", "consumer": "ceph",
                    "app": "(other pools)", "volume": pool, "item": pool}, p["stored"]))
            elif not CFG["tasks"].get(task, {}).get("enabled", True):
                # A pool whose task is switched off. An enabled task without a result yet emits
                # nothing: the dashboard bridges a restart with its last values.
                samples.append(("ceph_usage_bytes", {
                    "source": "pools", "pool": pool, "device_class": cls, "kind": "unmeasured",
                    "consumer": "(not measured)", "app": task, "volume": pool, "item": pool}, p["stored"]))
        for task, st in self.status.items():
            lab = {"task": task}
            if st.get("last_success"):
                samples.append(("ceph_usage_task_last_success_timestamp_seconds", lab, st["last_success"]))
            samples.append(("ceph_usage_task_last_duration_seconds", lab, st["duration"]))
            samples.append(("ceph_usage_task_last_run_ok", lab, st["ok"]))
            for labels, n in st.get("errors", []):
                samples.append(("ceph_usage_task_item_errors", dict(lab, **labels), n))
            if "partial" in st:
                samples.append(("ceph_usage_task_partial", lab, st["partial"]))
        self.text = render(samples)


STATE = State()


def run_task(task):
    """One run of a task. Called from the two scheduler threads: each task owns its result and
    status slot, and the shared state is only touched under the lock."""
    t0 = time.time()
    c = None
    res = pools = None
    ok = False
    try:
        c = connect()
        read_at = time.time()
        pools, fss = read_pools(c)
        res = TASKS[task](c, pools, fss) if task != "pools" else Result()
        ok = not res.partial
        log.info("task %s %s in %.1fs, %d samples, %d item errors", task, "ok" if ok else "PARTIAL",
                 time.time() - t0, len(res.samples), res.errors)
    except Exception:
        log.warning("task %s failed:\n%s", task, traceback.format_exc())
    finally:
        if c is not None:
            c.shutdown()
        with STATE.lock:
            st = STATE.status.setdefault(task, {"last_success": None})
            # The slow task's pools are minutes old when it ends: never replace a newer read.
            if pools is not None and read_at > STATE.pools_at:
                STATE.pools, STATE.fss, STATE.pools_at = pools, fss, read_at
            if res is not None:
                if task != "pools":
                    STATE.results[task] = res
                st.update(errors=res.error_series(), partial=int(res.partial))
                if ok:
                    st["last_success"] = time.time()
            # A failed run keeps the last errors and partial flag: they describe the data still shown.
            st.update(ok=ok, duration=time.time() - t0)
            STATE.rerender()


def next_run(now, every_min, offset_min):
    """Next time after now that is offset_min past a multiple of every_min (UTC epoch)."""
    period, off = every_min * 60, offset_min * 60
    return (math.floor((now - off) / period) + 1) * period + off


# The daily object scan gets a thread of its own: it takes minutes and must not delay the
# rest. One thread per task would load Ceph in parallel, which the schedule avoids.
HEAVY = ("rbd_k8s",)


def scheduler(names, stop, run=None):
    """Runs the named tasks, one at a time, each when due, until `stop` is set."""
    tasks = {name: CFG["tasks"][name] for name in names}
    now = time.time()
    due = {name: (now if t.get("at_start", True) else next_run(now, t["every_minutes"], t["offset_minutes"]))
           for name, t in tasks.items()}
    order = ["pools"] + list(TASKS)
    while due and not stop.is_set():
        name = min(due, key=lambda n: (due[n], order.index(n)))
        wait = due[name] - time.time()
        if wait > 0:
            stop.wait(min(wait, 60))
            continue
        (run or run_task)(name)
        t = tasks[name]
        due[name] = next_run(time.time(), t["every_minutes"], t["offset_minutes"])


def worker_groups():
    """Thread name -> the enabled tasks it runs, in schedule order."""
    enabled = [name for name, t in CFG["tasks"].items() if t.get("enabled", True)]
    return {"scheduler": [n for n in enabled if n not in HEAVY], "scheduler-heavy": [n for n in enabled if n in HEAVY]}


def start_workers(stop):
    workers = [threading.Thread(target=scheduler, args=(names, stop), name=name, daemon=True)
               for name, names in worker_groups().items() if names]
    for w in workers:
        w.start()
    return workers


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status, ctype = 200, "text/plain"
        if self.path == "/metrics":
            with STATE.lock:
                body = STATE.text
            ctype = "text/plain; version=0.0.4; charset=utf-8"
        elif self.path == "/healthz":
            dead = [w.name for w in WORKERS if not w.is_alive()]
            ok = not dead
            body = b"ok\n" if ok else f"stopped: {', '.join(dead)}\n".encode()
            status = 200 if ok else 500
        else:
            self.send_error(404)
            return
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


CFG = {}
WORKERS = []


def main():
    global CFG, WORKERS
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    with open(os.environ.get("CONFIG", "/etc/storage-usage/config.json")) as f:
        CFG = json.load(f)
    WORKERS = start_workers(threading.Event())
    port = int(os.environ.get("PORT", "9922"))
    http.server.ThreadingHTTPServer(("", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
