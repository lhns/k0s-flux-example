"""Offline tests of the exporter's walk, attribution, partition and rendering (no Ceph)."""
import datetime
import json
import logging
import os
import re
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "exporter"))
import storage_usage as su  # noqa: E402

logging.getLogger("storage-usage").setLevel(logging.CRITICAL)  # the failure paths log on purpose


class FakeFS:
    """dirs: path -> rbytes; snaps: dir -> [(name, btime)]; then: (dir, snap) -> {rel: rbytes}."""

    def __init__(self, dirs, snaps=None, then=None, layouts=None):
        self.dirs, self.snapmap, self.then, self.layouts = dirs, snaps or {}, then or {}, layouts or {}

    def listdirs(self, path):
        if path not in self.dirs:
            return None
        pre = path.rstrip("/") + "/"
        return [p[len(pre):] for p in self.dirs if p.startswith(pre) and "/" not in p[len(pre):] and p != path]

    def snaps(self, path):
        return list(self.snapmap.get(path, []))

    def _rsnaps(self, path):
        pre = path.rstrip("/") + "/"
        return sum(len(v) for d, v in self.snapmap.items() if d == path or d.startswith(pre))

    def xattr(self, path, key):
        if "/.snap/" in path:
            d, rest = path.split("/.snap/", 1)
            name, _, rel = rest.partition("/")
            v = self.then.get((d or "/", name), {}).get("/" + rel if rel else "")
            return None if v is None else str(v)
        if path not in self.dirs:
            return None
        if key == "ceph.dir.rbytes":
            return str(self.dirs[path])
        if key == "ceph.dir.rsnaps":
            return str(self._rsnaps(path))
        if key == "ceph.dir.layout.pool":
            return self.layouts.get(path)
        return None


def owners(pvs):
    return su.Owners(pvs)


def pv(name, ns, pvc, phase="Bound", provisioned=False, **va):
    ann = {"pv.kubernetes.io/provisioned-by": "cephfs.csi.ceph.com"} if provisioned else {}
    return {"metadata": {"name": name, "annotations": ann},
            "spec": {"csi": {"volumeAttributes": va, "driver": "cephfs.csi.ceph.com"},
                     "claimRef": {"namespace": ns, "name": pvc}, "storageClassName": "sc"},
            "status": {"phase": phase}}


PVS = [
    pv("pv1", "immich", "immich-upload", fsName="appdata", rootPath="/docker/immich/upload"),
    pv("pv2", "arr", "arr-media", fsName="archive", rootPath="/media"),
    pv("pv3", "jellyfin", "jellyfin-media", fsName="archive", rootPath="/media"),
    pv("pv4", "navidrome", "navidrome-music", fsName="archive", rootPath="/media/music/library"),
    pv("pv5", "backrest", "backrest-appdata", fsName="appdata", rootPath="/"),
    pv("pv6", "grafana", "grafana-data", fsName="appdata", provisioned=True,
       subvolumePath="/volumes/kube/csi-vol-1/uuid-1", subvolumeName="csi-vol-1"),
    pv("pv7", "hermes", "hermes-wiki", fsName="appdata", rootPath="/volumes/kube/csi-vol-2/uuid-2"),
    pv("pv8", "victoriametrics", "vm-data", phase="Released", fsName="appdata", rootPath="/docker/vm"),
    pv("pv9", "postgres", "pg-1", pool="rbd.kube", imageName="csi-vol-abc"),
]
SWARM = ["/docker", "/eph/docker"]


class Attribution(unittest.TestCase):
    def setUp(self):
        self.o = owners(PVS)

    def a(self, fs, path):
        return su.attribute(fs, path, self.o, SWARM)

    def test_dynamic_subvolume(self):
        self.assertEqual(self.a("appdata", "/volumes/kube/csi-vol-1"), ("k8s", "grafana", "grafana-data", ""))

    def test_static_inside_a_subvolume(self):
        self.assertEqual(self.a("appdata", "/volumes/kube/csi-vol-2"), ("k8s-static", "hermes", "hermes-wiki", ""))

    def test_subvolume_without_pv(self):
        self.assertEqual(self.a("appdata", "/volumes/kube/csi-vol-9"), ("k8s", "(no PV)", "/volumes/kube/csi-vol-9", ""))

    def test_static_longest_prefix(self):
        self.assertEqual(self.a("archive", "/media/music/library"),
                         ("k8s-static", "navidrome", "navidrome-music", ""))

    def test_shared_static_path_is_named_after_the_directory(self):
        shared = ("k8s-static", "media", "arr-media,jellyfin-media", "arr,jellyfin")
        self.assertEqual(self.a("archive", "/media/series"), shared)
        self.assertEqual(self.a("archive", "/media/(files)"), shared)

    def test_same_namespace_twice_is_one_app(self):
        o = owners([pv("a", "arr", "arr-a", fsName="archive", rootPath="/x"),
                    pv("b", "arr", "arr-b", fsName="archive", rootPath="/x")])
        self.assertEqual(su.attribute("archive", "/x", o, []), ("k8s-static", "arr", "arr-a,arr-b", ""))

    def test_root_mount_owns_nothing_and_the_consumer_is_the_filesystem(self):
        self.assertEqual(self.a("appdata", "/misc/x"), ("appdata", "misc", "/misc/x", ""))
        self.assertEqual(self.a("archive", "/dump/y"), ("archive", "dump", "/dump/y", ""))
        self.assertEqual(self.a("appdata", "/(files)"), ("appdata", "(root)", "/(files)", ""))

    def test_filesystem_consumers_never_collide_with_the_others(self):
        fixed = {"k8s", "k8s-static", "swarm", "proxmox", "s3", "ceph", "(residual)", "(not measured)"}
        self.assertEqual(su.FIXED_CONSUMERS, fixed)
        for name in ("appdata", "archive", "userdata", "fastdata"):
            self.assertNotIn(su.fs_consumer(name), fixed)
        self.assertEqual(su.fs_consumer("swarm"), "fs:swarm")
        self.assertEqual(su.attribute("s3", "/a", owners([]), []), ("fs:s3", "a", "/a", ""))

    def test_swarm_roots(self):
        self.assertEqual(self.a("appdata", "/docker/immich"), ("swarm", "immich", "/docker/immich", ""))
        self.assertEqual(self.a("appdata", "/docker/immich/(files)"), ("swarm", "immich", "/docker/immich/(files)", ""))
        self.assertEqual(self.a("appdata", "/eph/docker/arr"), ("swarm", "arr", "/eph/docker/arr", ""))
        self.assertEqual(self.a("appdata", "/docker"), ("swarm", "(all stacks)", "/docker", ""))

    def test_released_pv_is_marked(self):
        self.assertEqual(self.a("appdata", "/docker/vm"), ("k8s-static", "victoriametrics", "vm-data (released)", ""))

    def test_rbd(self):
        self.assertEqual(self.o.images[("rbd.kube", "csi-vol-abc")], ("postgres", "pg-1"))

    def test_pv_info_marks_provisioned_volumes(self):
        r = su.Result()
        self.o.pv_info(r)
        by = {(l["app"], l["volume"]): l for _, l, _ in r.samples}
        self.assertEqual(len(by), len(PVS))
        self.assertEqual(by[("grafana", "grafana-data")]["static"], "false")
        self.assertEqual(by[("arr", "arr-media")]["static"], "true")
        self.assertEqual(by[("grafana", "grafana-data")]["driver"], "cephfs.csi.ceph.com")
        self.assertEqual({f for f, _, _ in r.samples}, {"ceph_usage_pv_info"})


class WalkTest(unittest.TestCase):
    def tree(self):
        return FakeFS(
            dirs={
                "/": 1000,
                "/docker": 600, "/docker/immich": 300, "/docker/immich/upload": 250, "/docker/immich/db": 40,
                "/docker/other": 200,
                "/misc": 100,
                "/volumes": 250, "/volumes/kube": 250, "/volumes/kube/csi-vol-1": 250,
                "/volumes/kube/csi-vol-1/uuid-1": 250,
            },
            snaps={"/": [("old", 10.0), ("new", 20.0)], "/docker/immich/upload": [("u1", 5.0)],
                   "/misc/deep": [("d1", 7.0)]},
            then={
                # at snapshot "old": immich/db was 100 (now 40), other 150 (now 200), a dir since deleted
                ("/", "old"): {"/docker/immich/db": 100, "/docker/other": 150, "/docker/immich": 400,
                               "/docker/immich/upload": 250, "/docker": 900, "/misc": 100,
                               "/volumes/kube/csi-vol-1": 250},
                ("/docker/immich/upload", "u1"): {"": 300},
            },
            layouts={"/volumes/kube": "fast"})

    def test_leaves_partition_the_root(self):
        fs = self.tree()
        w = su.Walk(fs, {"depth": 1}, owners(PVS).static_roots("appdata")).run()
        paths = {lf["path"]: lf for lf in w.leaves}
        # depth 1, but descends to the static root /docker/immich/upload and to the subvolume
        self.assertIn("/docker/immich/upload", paths)
        self.assertIn("/docker/immich/db", paths)
        self.assertIn("/docker/other", paths)
        self.assertIn("/misc", paths)
        self.assertIn("/volumes/kube/csi-vol-1", paths)
        self.assertNotIn("/volumes/kube/csi-vol-1/uuid-1", paths)
        self.assertEqual(sum(lf["bytes"] for lf in w.leaves), 1000)
        self.assertEqual(paths["/docker/immich/(files)"]["bytes"], 10)
        self.assertEqual(paths["/volumes/kube/csi-vol-1"]["pool"], "fast")

    def test_lower_bounds(self):
        fs = self.tree()
        fs.dirs["/misc/deep"] = 50  # below the /misc leaf; its snapshot held 80
        fs.then[("/misc/deep", "d1")] = {"": 80}
        w = su.Walk(fs, {"depth": 1}, owners(PVS).static_roots("appdata")).run()
        paths = {lf["path"]: lf for lf in w.leaves}
        self.assertEqual(paths["/docker/immich/db"]["snap"], 60)
        self.assertEqual(paths["/docker/other"]["snap"], 0)
        # /docker/immich then: 400 - 250 - 100 = 50 files, now 10
        self.assertEqual(paths["/docker/immich/(files)"]["snap"], 40)
        # /docker then 900 - (400 + 150) = 350 files (a dir deleted since), now 100
        self.assertEqual(paths["/docker/(files)"]["snap"], 250)
        # own snapshot u1 (50) beats the root's bound for that leaf (0)
        self.assertEqual(paths["/docker/immich/upload"]["snap"], 50)
        self.assertEqual(paths["/misc"]["snap"], 30)
        root = next(s for s in w.snapdirs if s["path"] == "/")
        self.assertEqual(root["pinned"], 60 + 40 + 250)
        deep = next(s for s in w.snapdirs if s["path"] == "/misc/deep")
        self.assertEqual((deep["anchor"], deep["pinned"]), ("/misc", 30))

    def test_depth_override_and_children_cap(self):
        fs = self.tree()
        w = su.Walk(fs, {"depth": 3, "depths": {"/docker": 1}}, []).run()
        paths = {lf["path"] for lf in w.leaves}
        self.assertIn("/docker", paths)
        w = su.Walk(fs, {"depth": 3}, [], max_children=1).run()
        self.assertTrue(any("kept as one leaf" in n for n in w.notes))


class FakeImage:
    def __init__(self, snaps, used):
        self._snaps, self.used, self.at = snaps, used, None

    def list_snaps(self):
        return [{"id": i, "name": n, "namespace": 0, "size": 100} for i, n in enumerate(self._snaps)]

    def set_snap(self, name):
        self.at = name

    def flags(self):
        return 0

    def size(self):
        return 100

    def diff_iterate(self, off, size, from_snap, cb, include_parent, whole_object):
        cb(0, self.used[(self.at, from_snap)], True)
        cb(0, 999, False)


class Du(unittest.TestCase):
    def setUp(self):
        su.rbd = types.SimpleNamespace(RBD_FLAG_OBJECT_MAP_INVALID=1, RBD_FLAG_FAST_DIFF_INVALID=2)

    def test_snapshot_only_bytes(self):
        img = FakeImage(["s1", "s2"], {("s1", None): 40, ("s2", "s1"): 30, (None, "s2"): 20, (None, None): 50})
        self.assertEqual(su.du(img), (50, 90))

    def test_no_snapshots(self):
        img = FakeImage([], {(None, None): 50})
        self.assertEqual(su.du(img), (50, 50))


class Rendering(unittest.TestCase):
    def test_escaping_and_types(self):
        text = su.render([("ceph_usage_bytes", {"item": 'a"b\\c', "kind": "live"}, 5),
                          ("ceph_usage_task_last_run_ok", {"task": "x"}, True),
                          ("ceph_usage_pool_raw_ratio", {"pool": "p"}, 3.0)]).decode()
        self.assertIn('ceph_usage_bytes{item="a\\"b\\\\c",kind="live"} 5\n', text)
        self.assertIn('ceph_usage_task_last_run_ok{task="x"} 1\n', text)
        self.assertIn('ceph_usage_pool_raw_ratio{pool="p"} 3.0\n', text)
        self.assertEqual(text.count("# TYPE ceph_usage_bytes gauge"), 1)

    def test_empty_labels_dropped(self):
        r = su.Result()
        r.add("ceph_usage_bytes", 1, fs=None, vmid="", item="x")
        self.assertEqual(r.samples, [("ceph_usage_bytes", {"item": "x"}, 1)])


class StateTest(unittest.TestCase):
    def test_unmeasured_and_other_pools(self):
        su.CFG = {"tasks": {"rbd_k8s": {"enabled": False}},
                  "cephfs": {"filesystems": {"a": {}}}, "rbd_proxmox": {"images": []},
                  "rbd_k8s": {"images": [{"pool": "rbd.kube"}]}, "rgw": {"data_pool": "rgw.data"}}
        st = su.State()
        st.pools = {"a.data": {"stored": 10, "used": 30, "class": "hdd"},
                    "rbd.kube": {"stored": 7, "used": 21, "class": "nvme"},
                    "a.meta": {"stored": 1, "used": 3, "class": "nvme"}}
        st.fss = {"a": ["a.data"]}
        st.rerender()
        text = st.text.decode()
        # rbd.kube's task is off; a.data's has no result yet and emits nothing
        self.assertIn('item="rbd.kube",kind="unmeasured"', text)
        self.assertNotIn('item="a.data"', text)
        self.assertIn('app="(other pools)",consumer="ceph",device_class="nvme",item="a.meta"', text)
        self.assertIn('ceph_usage_pool_raw_ratio{device_class="hdd",pool="a.data"} 3.0', text)


class Schedule(unittest.TestCase):
    def test_next_run(self):
        day = 1792713600  # 2026-10-23T00:00Z
        self.assertEqual(su.next_run(day + 10, 1440, 270), day + 270 * 60)
        self.assertEqual(su.next_run(day + 270 * 60, 1440, 270), day + 86400 + 270 * 60)
        self.assertEqual(su.next_run(day + 21 * 60, 30, 20), day + 50 * 60)


class ClosingFS(FakeFS):
    def close(self):
        pass


class CephfsTask(unittest.TestCase):
    """One failing filesystem does not fail the others."""

    POOLS = {"good.data": {"stored": 100, "class": "hdd"}, "bad.data": {"stored": 50, "class": "hdd"}}
    FSS = {"good": ["good.data"], "bad": ["bad.data"]}

    def run_task(self, failing, prev=None):
        su.CFG = {"cephfs": {"filesystems": {"good": {"depth": 1}, "bad": {"depth": 1}}}}
        orig = su.CephFS, su.list_pvs, su.STATE
        su.STATE = su.State()
        if prev:
            su.STATE.results["cephfs"] = prev

        def mount(c, name):
            if name in failing:
                raise RuntimeError("mount failed")
            return ClosingFS({"/": 100, "/a": 60})

        su.CephFS, su.list_pvs = mount, lambda: []
        try:
            return su.task_cephfs(None, self.POOLS, self.FSS)
        finally:
            su.CephFS, su.list_pvs, su.STATE = orig

    def test_failed_filesystem_is_reported_and_the_others_kept(self):
        res = self.run_task({"bad"})
        self.assertEqual(res.fs_errors, {"good": 0, "bad": 1})
        self.assertEqual(res.error_series(), [({"fs": "bad"}, 1), ({"fs": "good"}, 0)])
        self.assertTrue(res.partial)
        items = {(l.get("fs"), l.get("kind"), l.get("item")) for f, l, _ in res.samples if f == "ceph_usage_bytes"}
        self.assertIn(("good", "live", "/a"), items)
        self.assertIn(("good", "residual", "good.data:(residual)"), items)
        # nothing measured for it before: one unmeasured leaf, so the pool still sums up
        self.assertIn(("bad", "unmeasured", "bad.data:(unmeasured)"), items)
        self.assertFalse([i for i in items if i[0] == "bad" and i[1] in ("live", "residual")])

    def test_failed_filesystem_keeps_its_previous_samples(self):
        prev = su.Result()
        prev.add("ceph_usage_bytes", 7, fs="bad", kind="live", item="/old")
        prev.add("ceph_usage_bytes", 8, fs="good", kind="live", item="/gone")
        res = self.run_task({"bad"}, prev)
        items = {l.get("item") for f, l, _ in res.samples if f == "ceph_usage_bytes"}
        self.assertIn("/old", items)
        self.assertNotIn("/gone", items)
        self.assertNotIn("bad.data:(unmeasured)", items)

    def test_all_failing_keeps_the_previous_result(self):
        with self.assertRaises(RuntimeError):
            self.run_task({"good", "bad"})

    def test_all_good(self):
        res = self.run_task(set())
        self.assertFalse(res.partial)
        self.assertEqual(res.fs_errors, {"good": 0, "bad": 0})


class FakeIoctx:
    def set_namespace(self, ns):
        pass

    def close(self):
        pass


class FakeRados:
    def open_ioctx(self, pool):
        return FakeIoctx()


class RbdK8sTask(unittest.TestCase):
    """rbd_k8s over its time budget returns what it measured and says so."""

    def run_task(self, max_seconds):
        clock = [0.0]

        class Img:
            def __init__(self, io, name, read_only):
                pass

            def size(self):
                return 100

            def list_snaps(self):
                return []

            def diff_iterate(self, off, size, from_snap, cb, include_parent, whole_object):
                clock[0] += 100  # a scan takes 100 s
                cb(0, 10, True)

            def close(self):
                pass

        class RBD:
            def list(self, io):
                return ["csi-vol-a", "csi-vol-b", "csi-vol-c", "csi-snap-x"]

        orig = su.rbd, su.time, su.list_pvs
        su.rbd = types.SimpleNamespace(RBD=RBD, Image=Img, Error=OSError, ImageNotFound=KeyError)
        su.time = types.SimpleNamespace(time=lambda: clock[0])
        su.list_pvs = lambda: []
        su.CFG = {"tasks": {"rbd_k8s": {"max_seconds": max_seconds}},
                  "rbd_k8s": {"images": [{"pool": "rbd.kube", "namespace": "kube"}], "skip_prefixes": ["csi-snap-"]}}
        try:
            return su.task_rbd_k8s(FakeRados(), {"rbd.kube": {"stored": 1000, "class": "nvme"}}, {})
        finally:
            su.rbd, su.time, su.list_pvs = orig

    def bytes_by_item(self, res):
        return {l["item"]: (l["kind"], l["app"], v) for f, l, v in res.samples if f == "ceph_usage_bytes"}

    def test_timeout_is_partial(self):
        res = self.run_task(150)
        self.assertTrue(res.partial)
        got = self.bytes_by_item(res)
        self.assertEqual(got["csi-vol-a"], ("live", "(no PV)", 10))
        self.assertEqual(got["csi-vol-b"], ("live", "(no PV)", 10))
        # not measured: 0, so yesterday's value in the dashboard's 26 h window is not counted twice
        self.assertEqual(got["csi-vol-c"][2], 0)
        self.assertNotIn("csi-snap-x", got)
        self.assertEqual(got["rbd.kube:(residual)"], ("residual", "(unmeasured + snapshots)", 980))

    def test_in_time_is_complete(self):
        res = self.run_task(10000)
        self.assertFalse(res.partial)
        got = self.bytes_by_item(res)
        self.assertEqual(got["rbd.kube:(residual)"], ("residual", "(snapshots + pending trim)", 970))


class RunTask(unittest.TestCase):
    CFG = {"tasks": {}, "cephfs": {"filesystems": {}}, "rbd_proxmox": {"images": []},
           "rbd_k8s": {"images": []}, "rgw": {"data_pool": "d"}}

    def run_with(self, task, fn):
        su.CFG = self.CFG
        orig = su.connect, su.read_pools, su.TASKS
        su.STATE = su.State()
        su.connect = lambda: types.SimpleNamespace(shutdown=lambda: None)
        su.read_pools = lambda c: ({}, {})
        su.TASKS = {task: fn}
        try:
            su.run_task(task)
        finally:
            su.connect, su.read_pools, su.TASKS = orig
        return su.STATE

    def test_partial_run_keeps_its_data_but_is_not_ok(self):
        def task(c, pools, fss):
            r = su.Result()
            r.add("ceph_usage_bytes", 5, item="x")
            r.fs_errors = {"a": 1, "b": 0}
            r.partial = True
            return r

        st = self.run_with("cephfs", task)
        text = st.text.decode()
        self.assertIn('ceph_usage_bytes{item="x"} 5', text)
        self.assertIn('ceph_usage_task_last_run_ok{task="cephfs"} 0', text)
        self.assertIn('ceph_usage_task_partial{task="cephfs"} 1', text)
        self.assertIn('ceph_usage_task_item_errors{fs="a",task="cephfs"} 1', text)
        self.assertIn('ceph_usage_task_item_errors{fs="b",task="cephfs"} 0', text)
        self.assertNotIn("last_success", text)

    def test_failed_run_keeps_item_errors_and_data(self):
        def good(c, pools, fss):
            r = su.Result()
            r.add("ceph_usage_bytes", 5, item="x")
            r.errors = 3
            return r

        def bad(c, pools, fss):
            raise RuntimeError("boom")

        st = self.run_with("rgw", good)
        su.CFG = self.CFG
        orig = su.connect, su.read_pools, su.TASKS
        su.connect = lambda: types.SimpleNamespace(shutdown=lambda: None)
        su.read_pools = lambda c: ({}, {})
        su.TASKS = {"rgw": bad}
        try:
            su.run_task("rgw")
        finally:
            su.connect, su.read_pools, su.TASKS = orig
        text = su.STATE.text.decode()
        self.assertIn('ceph_usage_task_last_run_ok{task="rgw"} 0', text)
        self.assertIn('ceph_usage_task_item_errors{task="rgw"} 3', text)
        self.assertIn('ceph_usage_bytes{item="x"} 5', text)
        self.assertIn('ceph_usage_task_last_success_timestamp_seconds{task="rgw"}', text)

    def test_older_pool_read_does_not_replace_a_newer_one(self):
        st = su.State()
        su.STATE = st
        st.pools, st.fss, st.pools_at = {"p": {"stored": 1, "used": 1, "class": "hdd"}}, {}, time.time() + 1000
        su.CFG = self.CFG
        orig = su.connect, su.read_pools, su.TASKS
        su.connect = lambda: types.SimpleNamespace(shutdown=lambda: None)
        su.read_pools = lambda c: ({"q": {"stored": 2, "used": 2, "class": "hdd"}}, {})
        try:
            su.run_task("pools")
        finally:
            su.connect, su.read_pools, su.TASKS = orig
        self.assertEqual(set(st.pools), {"p"})


class Workers(unittest.TestCase):
    def test_groups_follow_the_config(self):
        path = os.path.join(os.path.dirname(__file__), "..", "exporter", "config.json")
        with open(path, encoding="utf-8") as f:
            su.CFG = json.load(f)
        self.assertEqual(su.worker_groups(), {"scheduler": ["pools", "cephfs", "rbd_proxmox", "rgw"],
                                              "scheduler-heavy": ["rbd_k8s"]})
        su.CFG["tasks"]["rbd_k8s"]["enabled"] = False
        self.assertEqual(su.worker_groups()["scheduler-heavy"], [])

    def test_long_rbd_k8s_does_not_delay_pools(self):
        su.CFG = {"tasks": {n: {"every_minutes": 10, "offset_minutes": 5} for n in ("pools", "cephfs", "rbd_k8s")}}
        release, started, seen, stop = threading.Event(), threading.Event(), [], threading.Event()

        def run(name):
            if name == "rbd_k8s":
                started.set()
                release.wait(10)
            seen.append(name)

        threads = [threading.Thread(target=su.scheduler, args=(names, stop, run), daemon=True)
                   for names in (["pools", "cephfs"], ["rbd_k8s"])]
        try:
            for t in threads:
                t.start()
            self.assertTrue(started.wait(5))
            deadline = time.time() + 5
            while "cephfs" not in seen and time.time() < deadline:
                time.sleep(0.01)
            # the slow task is still running, the others have run in their order
            self.assertEqual(seen, ["pools", "cephfs"])
            release.set()
            deadline = time.time() + 5
            while "rbd_k8s" not in seen and time.time() < deadline:
                time.sleep(0.01)
            self.assertIn("rbd_k8s", seen)
        finally:
            release.set()
            stop.set()
            for t in threads:
                t.join(5)


class SigV4(unittest.TestCase):
    def test_request_shape(self):
        now = datetime.datetime(2026, 10, 23, 8, 0, 0, tzinfo=datetime.timezone.utc)
        req = su.sigv4_get("https://s3.example", "/admin/bucket", {"stats": "true", "format": "json"},
                           "AK", "SK", now=now)
        self.assertEqual(req.full_url, "https://s3.example/admin/bucket?format=json&stats=true")
        auth = req.get_header("Authorization")
        self.assertTrue(auth.startswith("AWS4-HMAC-SHA256 Credential=AK/20261023/us-east-1/s3/aws4_request, "
                                        "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="))
        # pinned: the live RGW accepted this exact algorithm
        self.assertEqual(auth.rsplit("=", 1)[1],
                         su.sigv4_get("https://s3.example", "/admin/bucket", {"format": "json", "stats": "true"},
                                      "AK", "SK", now=now).get_header("Authorization").rsplit("=", 1)[1])


class Snapshots(unittest.TestCase):
    def test_stats_are_one_family_per_value(self):
        r = su.Result()
        su.snapshot_stats(r, [30.0, 10.0, 20.0], pool="p", item="i")
        self.assertEqual({f: v for f, _, v in r.samples},
                         {"ceph_usage_snapshots": 3, "ceph_usage_snapshot_oldest_timestamp_seconds": 10.0,
                          "ceph_usage_snapshot_newest_timestamp_seconds": 30.0})


class Rgw(unittest.TestCase):
    def test_bucket_size_and_objects(self):
        buckets = [{"bucket": "b1", "owner": "alice",
                    "usage": {"rgw.main": {"size_actual": 100, "num_objects": 3},
                              "rgw.multimeta": {"size_actual": 0, "num_objects": 2}}}]
        orig = su.sigv4_get, su.urllib.request.urlopen
        su.CFG = {"rgw": {"url": "https://s3.example", "data_pool": "d"}}
        os.environ.update(RGW_ACCESS_KEY="a", RGW_SECRET_KEY="s")
        su.sigv4_get = lambda *a, **k: None

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(buckets).encode()

        su.urllib.request.urlopen = lambda *a, **k: Resp()
        try:
            res = su.task_rgw(None, {"d": {"stored": 130, "class": "hdd"}}, {})
        finally:
            su.sigv4_get, su.urllib.request.urlopen = orig
        got = {f: (l, v) for f, l, v in res.samples if "b1" in (l.get("volume"), l.get("bucket"))}
        self.assertEqual(got["ceph_usage_rgw_bucket_objects"], ({"app": "alice", "bucket": "b1"}, 5))
        self.assertEqual(got["ceph_usage_bytes"][1], 100)
        self.assertEqual((got["ceph_usage_bytes"][0]["app"], got["ceph_usage_bytes"][0]["volume"]), ("alice", "b1"))


class DashboardTest(unittest.TestCase):
    """The dashboard must keep matching the exporter's metric names and Grafana's column names."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "..", "dashboards", "ceph-storage-owners.json")
        with open(path, encoding="utf-8") as f:
            cls.dash = json.load(f)
        cls.panels = [p for p in cls.dash["panels"] if p["type"] != "row"]

    def test_queries_use_exported_metrics(self):
        used = set(re.findall(r"\bceph_usage_[a-z_]+", json.dumps(self.dash)))
        self.assertTrue(used)
        self.assertEqual(used - set(su.FAMILIES), set())

    def test_new_metrics_are_queried(self):
        used = set(re.findall(r"ceph_usage_[a-z_]+", json.dumps(self.dash)))
        self.assertLessEqual({"ceph_usage_pv_info", "ceph_usage_rgw_bucket_objects",
                              "ceph_usage_snapshot_newest_timestamp_seconds", "ceph_usage_task_partial",
                              "ceph_usage_task_item_errors"}, used)

    def test_pvc_fill_joins_kubelet_stats_through_the_collectors_labels(self):
        # The collector names the claim's labels k8s.namespace.name and k8s.persistentvolumeclaim.name;
        # the exporter's are app and volume (namespace and pod are the scrape target's).
        panel = next(p for p in self.panels if p["title"] == "Dynamic PVC fill level")
        for t in panel["targets"]:
            e = t["expr"]
            self.assertIn('label_move(label_move(', e)
            self.assertIn('"k8s.namespace.name", "app"', e)
            self.assertIn('"k8s.persistentvolumeclaim.name", "volume"', e)
            self.assertIn('ceph_usage_pv_info{static="false"}', e)
            self.assertIn("on (app, volume)", e)

    def test_snapshot_tables_are_merged(self):
        titles = [p["title"] for p in self.panels]
        self.assertIn("RBD snapshots", titles)
        self.assertNotIn("Proxmox snapshots", titles)

    def test_ids_and_titles_are_unique(self):
        ids = [p["id"] for p in self.dash["panels"]]
        titles = [p["title"] for p in self.panels]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(titles), len(set(titles)))

    def test_single_query_tables_have_no_suffixed_value_columns(self):
        # With one query the column is "Value"; "Value #A" never matches and leaves the raw number.
        for p in self.panels:
            if p["type"] == "table" and len(p["targets"]) == 1:
                self.assertNotIn("Value #", json.dumps([p.get("transformations"), p["fieldConfig"]]), p["title"])


if __name__ == "__main__":
    unittest.main()
