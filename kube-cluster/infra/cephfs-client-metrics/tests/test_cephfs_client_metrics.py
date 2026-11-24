"""Offline tests of the exporter: tables and mountinfo as kernel 6.12 prints them."""
import os
import re
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "exporter"))
import cephfs_client_metrics as ccm  # noqa: E402

ccm.NODE = ""

FSID = "00000000-0000-4000-8000-0000000000ce"
LATENCY = """item          total       avg_lat(us)     min_lat(us)     max_lat(us)     stdev(us)
-----------------------------------------------------------------------------------
read          65          140968          746             4697773         481469
write         0           0               0               0               0
metadata      5703        1135            370             36065           1587
copyfrom      0           0               0               0               0
"""
SIZE = """item          total       avg_sz(bytes)   min_sz(bytes)   max_sz(bytes)  total_sz(bytes)
----------------------------------------------------------------------------------------
read          159         415473          4096            4194304         66060288
write         0           0               0               0               0
copyfrom      0           0               0               0               0
"""
CAPS = """item          total           miss            hit
-------------------------------------------------
d_lease       1               0               0
caps          4               0               85
"""
FILE = """item                               total
------------------------------------------
total inodes                       4
opened files                       2
pinned i_caps                      4
opened inodes                      0
"""
GLOBAL = "/var/lib/k0s/kubelet/plugins/kubernetes.io/csi/cephfs.csi.ceph.com/9643/globalmount"
POD = "/var/lib/k0s/kubelet/pods/cca9/volumes/kubernetes.io~csi/jellyfin-media-stream/mount"
MOUNTINFO = f"""\
1584 1500 0:351 / {GLOBAL} rw,relatime shared:1 - ceph kube@{FSID}.archive=/media rw,name=kube
1585 1500 0:351 / {POD} ro,relatime shared:1 - ceph kube@{FSID}.archive=/media rw,name=kube
1586 1500 0:100 / /var/lib/k0s/kubelet/x\\040y/globalmount rw - ceph kube@{FSID}.appdata=/a\\040b rw,name=kube
1587 1500 8:1 / /var/lib/k0s/kubelet rw - ext4 /dev/sda1 rw
"""


def debugfs(clients):
    root = tempfile.mkdtemp()
    for client, tables in clients.items():
        d = os.path.join(root, f"{FSID}.{client}")
        os.makedirs(os.path.join(d, "metrics") if tables else d)
        for name, text in (tables or {}).items():
            with open(os.path.join(d, name if name in ("mdsc", "osdc") else os.path.join("metrics", name)), "w") as f:
                f.write(text)
    return root


def resolved(ids):
    m = ccm.MountMap(getxattr=lambda path, name: ids[path].encode())
    m.refresh(MOUNTINFO)
    for _ in range(100):
        if not m.pending:
            return m
        time.sleep(0.01)
    raise AssertionError("xattr lookups did not finish")


class Parsing(unittest.TestCase):
    def test_tables(self):
        self.assertEqual(ccm.parse_table(LATENCY)["read"]["avg_lat(us)"], 140968)
        self.assertEqual(ccm.parse_table(SIZE)["read"]["total_sz(bytes)"], 66060288)
        self.assertEqual(ccm.parse_table(CAPS)["caps"], {"total": 4, "miss": 0, "hit": 85})
        self.assertEqual(ccm.parse_table(FILE)["opened files"], {"total": 2})

    def test_short_row_is_an_error(self):
        with self.assertRaises(ValueError):
            ccm.parse_table("item total avg\nread 1\n")

    def test_mountinfo_one_entry_per_superblock(self):
        m = ccm.parse_mountinfo(MOUNTINFO)
        self.assertEqual(sorted(m), ["0:100", "0:351"])
        self.assertEqual((m["0:351"]["fs"], m["0:351"]["root"]), ("archive", "/media"))
        self.assertEqual(m["0:351"]["mountpoints"], [GLOBAL, POD])
        self.assertEqual(m["0:100"]["root"], "/a b")


class Render(unittest.TestCase):
    def setUp(self):
        tables = {"latency": LATENCY, "size": SIZE, "caps": CAPS, "file": FILE}
        self.root = debugfs({"client4100": tables, "client7": tables, "client9": None})
        self.mounts = resolved({GLOBAL: "client4100", "/var/lib/k0s/kubelet/x y/globalmount": "client8"})

    def test_labels_from_the_mount(self):
        out = ccm.Exporter(self.mounts, self.root).render()
        self.assertIn('cephfs_client_ops_total{client="client4100",fs="archive",root="/media",'
                      'pv="jellyfin-media-stream",node="",op="read"} 65', out)
        self.assertIn('pv="jellyfin-media-stream",node="",op="read"} 9.16292', out)
        self.assertIn('item="caps",result="hit"} 85', out)
        self.assertIn('cephfs_client_open_files{client="client4100"', out)

    def test_unresolved_client_and_rbd_dirs(self):
        out = ccm.Exporter(self.mounts, self.root).render()
        self.assertIn('client="client7",fs="",root="",pv=""', out)  # no mount known
        self.assertNotIn("client9", out)  # an RBD mapping: no metrics/
        self.assertIn('cephfs_client_unresolved{node=""} 1', out)

    def test_latency_sum_never_decreases(self):
        e = ccm.Exporter(self.mounts, self.root)
        e.render()
        with open(os.path.join(self.root, f"{FSID}.client4100", "metrics", "latency"), "w") as f:
            f.write(LATENCY.replace("65          140968", "66          138000"))  # 9.108 < 9.163
        self.assertIn('op="read"} 9.16292', e.render())  # clamped, not a counter reset
        with open(os.path.join(self.root, f"{FSID}.client4100", "metrics", "latency"), "w") as f:
            f.write(LATENCY.replace("65          140968", "67          140000"))  # 9.38 > 9.163
        self.assertIn('op="read"} 9.38', e.render())  # and growth passes through again

    def test_bad_table_counts_an_error(self):
        with open(os.path.join(self.root, f"{FSID}.client7", "metrics", "size"), "w") as f:
            f.write("item total avg_sz(bytes)\nread 1\n")
        out = ccm.Exporter(self.mounts, self.root).render()
        self.assertNotIn('client="client7"', out)
        self.assertIn('cephfs_client_errors_total{node=""} 1', out)


MDSC = """\
6318\tmds0\tcreate\t(unsafe) #10000f24001/a.jpg (tmp/jellyfin/a.jpg)
6322\tmds0\tlookup\t #10000f24001/b.jpg (tmp/jellyfin/b.jpg)
"""
OSDC = """\
REQUESTS 2 homeless 0
11179\tosd0\t41.a491af60\t41.60\t[0,30,39]/0\t[0,30,39]/0\te1043167\t1002a9a24fd.00000000\t0x400014\t1\tread
11180\tosd1\t41.13e47246\t41.46\t[1,7,39]/1\t[1,7,39]/1\te1043167\t1002a9a24fe.00000000\t0x400024\t1\twrite
LINGER REQUESTS
18446462598732840961\tosd3\t41.1\t41.1\t[3]/3\t[3]/3\te1\tobj\t0x20\t0\twatch
BACKOFFS
"""
IDLE_OSDC = "REQUESTS 0 homeless 0\nLINGER REQUESTS\nBACKOFFS\n"


def latency(ops, avg, row="metadata", max_us=36065, stdev=1587):
    return LATENCY.replace("metadata      5703        1135            370             36065           1587",
                           f"{row:<14}{ops:<12}{avg:<16}370             {max_us:<16}{stdev}")


class Sampling(unittest.TestCase):
    CLIENT = "client4100"

    def setUp(self):
        self.root = debugfs({self.CLIENT: {"latency": LATENCY, "size": SIZE, "caps": CAPS, "file": FILE,
                                           "mdsc": "", "osdc": IDLE_OSDC}})
        self.mounts = resolved({GLOBAL: self.CLIENT, "/var/lib/k0s/kubelet/x y/globalmount": "client8"})
        self.e = ccm.Exporter(self.mounts, self.root, window=120)

    def write(self, name, text, client=None):
        d = os.path.join(self.root, f"{FSID}.{client or self.CLIENT}")
        with open(os.path.join(d, name if name in ("mdsc", "osdc") else os.path.join("metrics", name)), "w") as f:
            f.write(text)

    def series(self, out, name, op="metadata", extra=""):
        m = re.search(rf'^{name}{{client="{self.CLIENT}"[^}}]*op="{op}"{extra}}} (\S+)$', out, re.M)
        return float(m.group(1)) if m else None

    def test_slow_op_inside_a_busy_second(self):
        # 100 ops in one second: 99 of 1 ms and one of 5 s. The average moves from 1000 to 5544 us.
        self.write("latency", latency(1000, 1000))
        self.e.sample(0)
        self.write("latency", latency(1100, 5544))
        self.e.sample(1)
        out = self.e.render(now=1)
        self.assertAlmostEqual(self.series(out, "cephfs_client_op_latency_upper_bound_seconds"), 5.0984 + 0.0011)
        self.assertEqual(self.series(out, "cephfs_client_sample_latency_seconds_bucket", extra=',le="0.05"'), 0)
        self.assertEqual(self.series(out, "cephfs_client_sample_latency_seconds_bucket", extra=',le="0.1"'), 1)
        self.assertEqual(self.series(out, "cephfs_client_sample_latency_seconds_count"), 1)
        # The upper bound holds for the window, then drops.
        self.assertGreater(self.series(self.e.render(now=120), "cephfs_client_op_latency_upper_bound_seconds"), 5)
        self.assertEqual(self.series(self.e.render(now=122), "cephfs_client_op_latency_upper_bound_seconds"), 0)

    def test_resolution_guard_on_a_busy_mount(self):
        # 22 M ops since mount: whole-us rounding hides up to 22 s per sample.
        self.write("latency", latency(22_000_000, 3209))
        self.e.sample(0)
        self.write("latency", latency(22_000_041, 3209))  # 41 fast ops, average unchanged
        self.e.sample(1)
        self.write("latency", latency(22_000_080, 3208))  # rounding step down
        self.e.sample(2)
        self.write("latency", latency(22_000_090, 3209))  # and back up: 22 s of ds, may be nothing
        self.e.sample(3)
        self.write("latency", latency(22_000_100, 3210))  # a 1 us tick up: the same
        self.e.sample(4)
        out = self.e.render(now=4)
        self.assertEqual(self.series(out, "cephfs_client_latency_floor_seconds"), 22.0001)
        self.assertIsNone(self.series(out, "cephfs_client_sample_latency_seconds_count"))
        self.assertIsNone(self.series(out, "cephfs_client_op_latency_upper_bound_seconds"))
        # A 2 us step is at least 22 s of real latency (a 50 s stall, say) and counts.
        self.write("latency", latency(22_000_101, 3212))
        self.e.sample(5)
        out = self.e.render(now=5)
        self.assertEqual(self.series(out, "cephfs_client_sample_latency_seconds_count"), 1)
        self.assertGreaterEqual(self.series(out, "cephfs_client_op_latency_upper_bound_seconds"), 50)

    def test_remount_resets_the_baseline(self):
        self.write("latency", latency(1000, 1000))
        self.e.sample(0)
        self.write("latency", latency(10, 90000))  # same client, counters restarted
        self.e.sample(1)
        self.assertIsNone(self.series(self.e.render(now=1), "cephfs_client_sample_latency_seconds_count"))
        self.write("latency", latency(20, 50000))  # 10 ops, 10 ms each
        self.e.sample(2)
        self.assertEqual(self.series(self.e.render(now=2), "cephfs_client_sample_latency_seconds_sum"), 0.1)
        # The client goes away (unmount); a new one starts from scratch.
        os.rename(os.path.join(self.root, f"{FSID}.{self.CLIENT}"), os.path.join(self.root, f"{FSID}.client99"))
        self.e.sample(3)
        self.assertEqual(self.e.hist, {})
        self.assertNotIn((self.CLIENT, "metadata"), self.e.prev)

    def test_in_flight_age(self):
        self.write("mdsc", MDSC)
        self.e.sample(0)
        self.e.sample(1)
        self.write("osdc", OSDC)
        self.e.sample(2)
        out = self.e.render(now=2)
        self.assertIn(f'cephfs_client_requests_in_flight{{client="{self.CLIENT}"', out)
        self.assertRegex(out, r'requests_in_flight\{[^}]*kind="mds"\} 1\n')  # (unsafe) has its reply
        self.assertRegex(out, r'requests_in_flight\{[^}]*kind="osd"\} 2\n')  # LINGER is not a request
        self.assertRegex(out, r'oldest_request_age_seconds\{[^}]*kind="mds"\} 2\n')
        self.write("mdsc", "")
        self.write("osdc", IDLE_OSDC)
        self.e.sample(3)
        out = self.e.render(now=3)
        self.assertRegex(out, r'requests_in_flight\{[^}]*kind="mds"\} 0\n')
        self.assertRegex(out, r'oldest_request_age_seconds\{[^}]*kind="mds"\} 2\n')  # held for the window

    def test_since_mount_max(self):
        self.write("latency", latency(5703, 1135, max_us=5447207, stdev=12270))
        out = self.e.render()
        self.assertEqual(self.series(out, "cephfs_client_latency_max_seconds"), 5.447207)
        self.assertNotIn("stdev", out)

    def test_host_mode_labels_by_mountpoint(self):
        m = ccm.MountMap(getxattr=lambda path, name: b"client4100" if path == GLOBAL else b"client8",
                         label_by="mountpoint")
        m.refresh(MOUNTINFO)
        for _ in range(100):
            if not m.pending:
                break
            time.sleep(0.01)
        out = ccm.Exporter(m, self.root, label_by="mountpoint").render()
        self.assertIn(f'root="/media",mountpoint="{GLOBAL},{POD}",node=""', out)
        self.assertNotIn('pv="', out)


class Window(unittest.TestCase):
    def test_window_max(self):
        w = ccm.WindowMax(10)
        for t, v in ((0, 5), (1, 3), (2, 4), (11, 1)):
            w.push(t, v)
        self.assertEqual(w.get(2), 5)
        self.assertEqual(w.get(11), 4)  # 5 is older than the window
        self.assertEqual(w.get(30), 0)


class Resolve(unittest.TestCase):
    def test_hung_lookup_is_not_stacked_and_failure_is_retried(self):
        gate, calls = threading.Event(), []

        def getxattr(path, name):
            calls.append(path)
            if path == GLOBAL:
                gate.wait(5)
                return b"client1"
            raise OSError("boom")

        m = ccm.MountMap(getxattr=getxattr)
        m.refresh(MOUNTINFO)
        time.sleep(0.05)
        m.refresh(MOUNTINFO)
        time.sleep(0.05)
        self.assertEqual(calls.count(GLOBAL), 1)
        self.assertEqual(calls.count("/var/lib/k0s/kubelet/x y/globalmount"), 2)
        gate.set()
        time.sleep(0.05)
        self.assertEqual(m.labels(), {"client1": {"fs": "archive", "root": "/media", "where": "jellyfin-media-stream"}})

    def test_unmount_drops_the_mapping(self):
        m = resolved({GLOBAL: "client1", "/var/lib/k0s/kubelet/x y/globalmount": "client2"})
        m.refresh("")
        self.assertEqual(m.labels(), {})


if __name__ == "__main__":
    unittest.main()
