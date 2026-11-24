#!/usr/bin/env python3
"""CephFS kernel client metrics from debugfs, labelled with the mount they belong to (README.md).

Each superblock has its own client, /sys/kernel/debug/ceph/<fsid>.client<id>/. The mount behind
a client comes from the ceph.client_id vxattr of one of its mountpoints, read in the background:
a hung MDS blocks that call, so a scrape only reads debugfs and the cached map.

`--once` prints one scrape (after one sample interval) and exits. LABEL_BY=mountpoint labels by
mountpoint instead of PV, for a host outside Kubernetes.
"""
import collections
import http.server
import logging
import os
import re
import socket
import sys
import threading
import time

DEBUGFS = os.environ.get("DEBUGFS", "/sys/kernel/debug/ceph")
MOUNTINFO = os.environ.get("MOUNTINFO", "/proc/self/mountinfo")
NODE = os.environ.get("NODE_NAME") or socket.gethostname()
ADDRESS = os.environ.get("ADDRESS", "")
PORT = int(os.environ.get("PORT", "9923"))
REFRESH = float(os.environ.get("REFRESH_SECONDS", "30"))
SAMPLE = float(os.environ.get("SAMPLE_SECONDS", "1"))
# At least two scrape intervals, so one missed scrape does not lose a spike.
WINDOW = float(os.environ.get("WINDOW_SECONDS", "120"))
LABEL_BY = os.environ.get("LABEL_BY", "pv")  # pv | mountpoint
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

log = logging.getLogger("cephfs-client-metrics")
PV_RE = re.compile(r"/kubernetes\.io~csi/([^/]+)/mount$")


def parse_table(text):
    """{item: {column: int}} of a debugfs metrics table; its item names may contain spaces."""
    lines = [l for l in text.splitlines() if l.strip() and not set(l.strip()) <= {"-"}]
    if not lines:
        return {}
    cols = lines[0].split()[1:]
    rows = {}
    for line in lines[1:]:
        tok = line.split()
        if len(tok) <= len(cols):
            raise ValueError(f"short row: {line!r}")
        rows[" ".join(tok[:-len(cols)])] = dict(zip(cols, map(int, tok[-len(cols):])))
    return rows


def parse_mdsc(text):
    """Tids of the MDS requests still waiting for a reply. `(unsafe)` ones have their reply."""
    return {int(l.split()[0]) for l in text.splitlines() if l[:1].isdigit() and "(unsafe)" not in l}


def parse_osdc(text):
    """Tids of the OSD requests in flight: the REQUESTS section, not LINGER (watches) or BACKOFFS."""
    tids, section = set(), None
    for line in text.splitlines():
        if line[:1].isalpha():
            section = line.split()[0]
        elif section == "REQUESTS" and line[:1].isdigit():
            tids.add(int(line.split()[0]))
    return tids


def unescape(path):
    """mountinfo escapes space, tab, newline and backslash as octal."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def parse_mountinfo(text):
    """{dev: {"fs", "root", "mountpoints"}} of the ceph mounts, one entry per superblock."""
    mounts = {}
    for line in text.splitlines():
        pre, _, post = line.partition(" - ")
        f, p = pre.split(), post.split()
        if len(p) < 2 or p[0] != "ceph":
            continue
        # New-style source: <user>@<fsid>.<fs>=<path>
        m = re.match(r"[^@]*@[^.]+\.([^=]+)=(.*)$", unescape(p[1]))
        fs, root = m.groups() if m else ("", "")
        mounts.setdefault(f[2], {"fs": fs, "root": root, "mountpoints": []})["mountpoints"].append(unescape(f[4]))
    return mounts


def mount_label(mountpoints, label_by):
    if label_by == "mountpoint":
        return ",".join(sorted(mountpoints))
    return ",".join(sorted({x.group(1) for x in map(PV_RE.search, mountpoints) if x}))


class MountMap:
    """client id -> mount labels, kept current by refresh()."""

    def __init__(self, getxattr=None, label_by=LABEL_BY):
        self.getxattr = getxattr or os.getxattr
        self.label_by = label_by
        self.lock = threading.Lock()
        self.by_dev = {}  # dev -> labels incl. "client" once resolved
        self.pending = set()

    def refresh(self, mountinfo_text):
        mounts = parse_mountinfo(mountinfo_text)
        with self.lock:
            for dev in list(self.by_dev):
                if dev not in mounts or self.by_dev[dev]["mountpoints"][0] not in mounts[dev]["mountpoints"]:
                    del self.by_dev[dev]  # unmounted, or a device number reused by another mount
            for dev, m in mounts.items():
                where = mount_label(m["mountpoints"], self.label_by)
                if dev in self.by_dev:
                    self.by_dev[dev]["where"] = where
                else:
                    self.by_dev[dev] = {"fs": m["fs"], "root": m["root"], "where": where,
                                        "mountpoints": m["mountpoints"], "client": None}
                # A failed lookup is retried; a hung one stays pending and is not stacked.
                if self.by_dev[dev]["client"] is None and dev not in self.pending:
                    self.pending.add(dev)
                    threading.Thread(target=self._resolve, args=(dev, m["mountpoints"][0]), daemon=True).start()

    def _resolve(self, dev, path):
        try:
            client = self.getxattr(path, "ceph.client_id").decode().strip()
        except OSError as e:
            log.warning("ceph.client_id of %s: %s", path, e)
            client = None
        with self.lock:
            self.pending.discard(dev)
            if dev in self.by_dev and self.by_dev[dev]["mountpoints"][0] == path:
                self.by_dev[dev]["client"] = client

    def labels(self):
        with self.lock:
            return {m["client"]: {k: m[k] for k in ("fs", "root", "where")}
                    for m in self.by_dev.values() if m["client"]}


class WindowMax:
    """Maximum of the values pushed in the last `window` seconds (monotonic deque)."""

    def __init__(self, window):
        self.window, self.q = window, collections.deque()

    def push(self, now, value):
        while self.q and self.q[-1][1] <= value:
            self.q.pop()
        self.q.append((now, value))

    def get(self, now):
        while self.q and self.q[0][0] < now - self.window:
            self.q.popleft()
        return self.q[0][1] if self.q else 0


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Exporter:
    HELP = {
        "cephfs_client_ops_total": ("counter", "Completed requests since mount."),
        "cephfs_client_latency_seconds_total": (
            "counter", "Summed latency, reconstructed as ops x average (the kernel prints whole us); never decreases."),
        "cephfs_client_latency_max_seconds": ("gauge", "Slowest single request since mount."),
        "cephfs_client_sample_latency_seconds": (
            "histogram", "Mean latency of the requests completed in one sample (~1 s); samples below twice the floor are skipped."),
        "cephfs_client_op_latency_upper_bound_seconds": (
            "gauge", "Upper bound for any single request completed in the window: the largest summed latency of one sample, plus the floor."),
        "cephfs_client_latency_floor_seconds": (
            "gauge", "Rounding error of the per-sample summed latency (ops since mount x 1 us); samples below twice it are skipped."),
        "cephfs_client_requests_in_flight": ("gauge", "MDS or OSD requests waiting for a reply at the last sample."),
        "cephfs_client_oldest_request_age_seconds": (
            "gauge", "Longest wait of a request still in flight, the maximum over the window; 1 s resolution, from the first sample that saw it."),
        "cephfs_client_bytes_total": ("counter", "Bytes read, written or copied since mount."),
        "cephfs_client_caps_total": ("counter", "Capability and dentry lease lookups."),
        "cephfs_client_open_files": ("gauge", "Files open on the mount."),
        "cephfs_client_errors_total": ("counter", "Client directories the exporter could not parse."),
        "cephfs_client_unresolved": ("gauge", "Client directories whose mount is not (yet) known."),
    }

    def __init__(self, mounts, debugfs=DEBUGFS, window=WINDOW, label_by=LABEL_BY):
        self.mounts, self.debugfs, self.window = mounts, debugfs, window
        self.where = "mountpoint" if label_by == "mountpoint" else "pv"
        self.lock = threading.Lock()
        self.latency_sum = {}  # (client, op) -> last emitted value, so rate() never sees a reset
        self.errors = 0
        # Sampler state, all keyed by (client, op) or (client, kind).
        self.prev = {}    # last (ops, avg_us)
        self.hist = {}    # [bucket counts..., +Inf count, sum]
        self.upper = {}   # WindowMax of per-sample summed latency + floor
        self.floor = {}   # seconds
        self.seen = {}    # tid -> first seen
        self.inflight = {}
        self.age = {}     # WindowMax of the oldest in-flight age

    def read(self, client_dir, name):
        with open(os.path.join(self.debugfs, client_dir, "metrics", name)) as f:
            return parse_table(f.read())

    def client_dirs(self):
        """{client: dir} of the CephFS clients; RBD mappings have client directories without metrics/."""
        try:
            dirs = os.listdir(self.debugfs)
        except OSError as e:
            log.warning("%s: %s", self.debugfs, e)
            return {}
        return {d.rpartition(".")[2]: d for d in sorted(dirs)
                if d.rpartition(".")[2].startswith("client") and os.path.isdir(os.path.join(self.debugfs, d, "metrics"))}

    def sample(self, now):
        """One sample: per-op deltas of the latency table and the in-flight requests."""
        dirs = self.client_dirs()
        data = {}
        for client, d in dirs.items():  # outside the lock: a scrape never waits for debugfs
            try:
                lat = {op: (r["total"], r["avg_lat(us)"]) for op, r in self.read(d, "latency").items()}
                with open(os.path.join(self.debugfs, d, "mdsc")) as f:
                    mds = parse_mdsc(f.read())
                with open(os.path.join(self.debugfs, d, "osdc")) as f:
                    osd = parse_osdc(f.read())
            except (OSError, ValueError, KeyError) as e:
                log.debug("%s: %r", d, e)  # counted as an error by render()
                continue
            data[client] = lat, mds, osd
        with self.lock:
            for client, (lat, mds, osd) in data.items():
                for op, (ops, avg_us) in lat.items():
                    self._sample_op((client, op), ops, avg_us, now)
                for kind, tids in (("mds", mds), ("osd", osd)):
                    key = (client, kind)
                    old = self.seen.get(key, {})
                    self.seen[key] = cur = {t: old.get(t, now) for t in tids}
                    self.inflight[key] = len(cur)
                    self.age.setdefault(key, WindowMax(self.window)).push(now, now - min(cur.values()) if cur else 0)
            for state in (self.prev, self.hist, self.upper, self.floor, self.seen, self.inflight, self.age):
                for key in [k for k in state if k[0] not in dirs]:
                    del state[key]

    def _sample_op(self, key, ops, avg_us, now):
        prev = self.prev.get(key)
        self.prev[key] = (ops, avg_us)
        self.floor[key] = ops / 1e6
        if prev is None or ops < prev[0]:
            return  # first sight, or the counters restarted: a new baseline, nothing to compare
        dn = ops - prev[0]
        # avg_lat is truncated to whole us, so ops x avg is up to `ops` us low, and the true delta lies
        # within one floor either side of ds. Below two floors (a 1 us tick of the average) it may be
        # nothing but rounding.
        ds = (ops * avg_us - prev[0] * prev[1]) / 1e6
        if dn == 0 or ds < 2 * self.floor[key]:
            return
        h = self.hist.setdefault(key, [0] * (len(BUCKETS) + 2))
        mean = ds / dn
        for i, le in enumerate(BUCKETS):
            if mean <= le:
                h[i] += 1
        h[-2] += 1
        h[-1] += ds
        self.upper.setdefault(key, WindowMax(self.window)).push(now, ds + self.floor[key])

    def render(self, now=None):
        now = time.monotonic() if now is None else now
        labels = self.mounts.labels()
        out = {name: [] for name in self.HELP}
        unresolved = 0
        dirs = self.client_dirs()
        with self.lock:
            seen = set()
            for client, d in dirs.items():
                mount = labels.get(client)
                if mount is None:
                    unresolved += 1
                    mount = {"fs": "", "root": "", "where": ""}
                base = f'client="{client}",fs="{esc(mount["fs"])}",root="{esc(mount["root"])}",' \
                       f'{self.where}="{esc(mount["where"])}",node="{esc(NODE)}"'
                try:
                    lat, size = self.read(d, "latency"), self.read(d, "size")
                    caps, files = self.read(d, "caps"), self.read(d, "file")
                    rows = []
                    for op, r in lat.items():
                        key = (client, op)
                        s = max(self.latency_sum.get(key, 0.0), r["total"] * r["avg_lat(us)"] / 1e6)
                        self.latency_sum[key] = s
                        seen.add(key)
                        lo = f'{base},op="{op}"'
                        rows.append(("cephfs_client_ops_total", lo, r["total"]))
                        rows.append(("cephfs_client_latency_seconds_total", lo, s))
                        rows.append(("cephfs_client_latency_max_seconds", lo, r["max_lat(us)"] / 1e6))
                        rows += self._sampled(key, lo, now)
                    for op, r in size.items():
                        rows.append(("cephfs_client_bytes_total", f'{base},op="{op}"', r["total_sz(bytes)"]))
                    for item, r in caps.items():
                        for result in ("hit", "miss"):
                            rows.append(("cephfs_client_caps_total", f'{base},item="{item}",result="{result}"', r[result]))
                    rows.append(("cephfs_client_open_files", base, files["opened files"]["total"]))
                    for kind in ("mds", "osd"):
                        key = (client, kind)
                        if key in self.age:
                            lk = f'{base},kind="{kind}"'
                            rows.append(("cephfs_client_requests_in_flight", lk, self.inflight[key]))
                            rows.append(("cephfs_client_oldest_request_age_seconds", lk, self.age[key].get(now)))
                except (OSError, ValueError, KeyError) as e:
                    # debugfs has no ABI: a format change shows as errors, not as a dead exporter.
                    log.warning("%s: %r", d, e)
                    self.errors += 1
                    continue
                for name, lbl, v in rows:
                    family = name if name in out else name.rpartition("_")[0]  # histogram _bucket/_sum/_count
                    out[family].append(f"{name}{{{lbl}}} {v}")
            for key in set(self.latency_sum) - seen:
                del self.latency_sum[key]
            out["cephfs_client_errors_total"].append(f'cephfs_client_errors_total{{node="{esc(NODE)}"}} {self.errors}')
        out["cephfs_client_unresolved"].append(f'cephfs_client_unresolved{{node="{esc(NODE)}"}} {unresolved}')
        lines = []
        for name, (kind, text) in self.HELP.items():
            lines += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"] + out[name]
        return "\n".join(lines) + "\n"

    def _sampled(self, key, lo, now):
        """Rows of the sampler's series for one (client, op). The histogram appears once a sample counted."""
        rows = []
        if key in self.floor:
            rows.append(("cephfs_client_latency_floor_seconds", lo, self.floor[key]))
        h = self.hist.get(key)
        if h:
            name = "cephfs_client_sample_latency_seconds"
            rows += [(f"{name}_bucket", f'{lo},le="{le}"', h[i]) for i, le in enumerate(BUCKETS)]
            rows += [(f"{name}_bucket", f'{lo},le="+Inf"', h[-2]), (f"{name}_sum", lo, h[-1]), (f"{name}_count", lo, h[-2])]
            rows.append(("cephfs_client_op_latency_upper_bound_seconds", lo, self.upper[key].get(now)))
        return rows


def refresh_loop(mounts):
    while True:
        try:
            with open(MOUNTINFO) as f:
                mounts.refresh(f.read())
        except Exception:  # noqa: BLE001 -- keep serving the last map
            log.exception("mount refresh")
        time.sleep(REFRESH)


def sample_loop(exporter):
    nxt = time.monotonic()
    while True:
        try:
            exporter.sample(time.monotonic())
        except Exception:  # noqa: BLE001 -- a bad sample must not stop sampling
            log.exception("sample")
        nxt = max(nxt + SAMPLE, time.monotonic())
        time.sleep(nxt - time.monotonic())


def once():
    """Print one scrape: resolve the mounts, take two samples one interval apart."""
    mounts = MountMap()
    with open(MOUNTINFO) as f:
        mounts.refresh(f.read())
    deadline = time.monotonic() + 10
    while mounts.pending and time.monotonic() < deadline:
        time.sleep(0.05)
    exporter = Exporter(mounts)
    exporter.sample(time.monotonic())
    time.sleep(SAMPLE)
    exporter.sample(time.monotonic())
    sys.stdout.write(exporter.render())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--once" in sys.argv[1:]:
        once()
        return
    mounts = MountMap()
    exporter = Exporter(mounts)
    threading.Thread(target=refresh_loop, args=(mounts,), daemon=True).start()
    threading.Thread(target=sample_loop, args=(exporter,), daemon=True).start()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                body, ctype = exporter.render().encode(), "text/plain; version=0.0.4"
            elif self.path == "/healthz":
                body, ctype = b"ok\n", "text/plain"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    log.info("listening on %s:%d", ADDRESS or "*", PORT)
    http.server.ThreadingHTTPServer((ADDRESS, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
