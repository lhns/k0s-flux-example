#!/usr/bin/env python3
"""Standing playback probe: one paced play as the jellyfin-probe user, results pushed via OTLP.

Skips when anyone streams or ffmpeg runs. Always reports the stop, kills its transcode and logs
out. Standard library only (README.md, "Standing probe").
"""
import http.client
import json
import os
import signal
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid

TICKS = 10_000_000
ENV = os.environ.get
HOST = ENV("JELLYFIN_HOST", "jellyfin.local.example.com")
INSTANCE = ENV("JELLYFIN_INSTANCE", "jellyfin-sqlite")
USER = ENV("PROBE_USER", "jellyfin-probe")
PASSWORD_FILE = ENV("PASSWORD_FILE", "/etc/jellyfin-probe/password")
MODE = ENV("MODE", "transcode")  # transcode | direct
ITEM = ENV("ITEM", "")
BITRATE = int(ENV("BITRATE", "8000000"))
START_S = float(ENV("START_S", "600"))
DURATION_S = float(ENV("DURATION_S", "150"))
BUFFER_S = float(ENV("BUFFER_S", "30"))
STARTUP_S = 3.0  # content buffered before the player clock starts
VMSELECT = ENV("VMSELECT_URL", "http://vmselect.victoriametrics-cluster.svc.cluster.local:8481/select/0/prometheus")
OTLP = ENV("OTLP_URL", "http://opentelemetry-gateway.opentelemetry-gateway.svc.cluster.local:4318/v1/metrics")

HLS = 'action=~"GetHlsVideoSegment|GetHlsAudioSegment|GetHlsVideoSegmentLegacy|GetHlsAudioSegmentLegacy"'
DIRECT = 'action=~"GetVideoStream|GetVideoStreamByContainer|GetAudioStream|GetAudioStreamByContainer|GetUniversalAudioStream"'
BOTH = 'app_kubernetes_io_name=~"jellyfin|jellyfin-sqlite"'
# Anyone streaming on either instance, or any ffmpeg on the GPU host (a transcode, or Jellyfin's
# own analysis tasks), in the last 2 minutes.
BUSY = {
    "hls_segments": (f'sum(increase(http_requests_received_total{{{BOTH}, {HLS}}}[2m]))', 0.5),
    "streams_in_flight": (f'sum(max_over_time(http_requests_in_progress{{{BOTH}, {DIRECT}}}[2m]))', 0),
    "hls_in_flight": (f'sum(max_over_time(http_requests_in_progress{{{BOTH}, {HLS}}}[2m]))', 0),
    "ffmpeg_cores": ('sum(rate(process.cpu.time{"host.name"="docker-d","process.executable.name"=~"ffmpeg.*"}[2m]))', 0.2),
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else None


# ---------------------------------------------------------------- skip check
def vm_query(expr, opener=urllib.request.urlopen):
    url = VMSELECT + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with opener(url, timeout=20) as r:
        d = json.load(r)
    res = d["data"]["result"]
    return float(res[0]["value"][1]) if res else 0.0


def busy(query=vm_query, tries=4, wait=5):
    """(busy signal names, failed queries). A failed query means do not play: never play blind.
    Retried, because a new pod's traffic is dropped until kube-router has programmed it."""
    out, failed = [], []
    for name, (expr, limit) in BUSY.items():
        for i in range(tries):
            try:
                if query(expr) > limit:
                    out.append(name)
                break
            except Exception as e:
                if i == tries - 1:
                    failed.append(f"{name}: {type(e).__name__}")
                else:
                    time.sleep(wait)
    return out, failed


# ---------------------------------------------------------------- client
class Conn:
    """One keep-alive HTTPS connection, like a player's media connection."""

    def __init__(self, host, auth):
        self.host, self.auth, self.c = host, auth, None
        self.ctx = ssl.create_default_context()

    def close(self):
        if self.c:
            try:
                self.c.close()
            except Exception:
                pass
        self.c = None

    def request(self, method, path, body=None, headers=None, read=True, retry=True):
        h = {"Authorization": self.auth(), "Accept": "*/*", "User-Agent": "jellyfin-probe/1"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        t0 = time.time()
        try:
            if self.c is None:
                self.c = http.client.HTTPSConnection(self.host, 443, timeout=60, context=self.ctx)
            self.c.request(method, path, body=data, headers=h)
            r = self.c.getresponse()
        except (http.client.HTTPException, OSError):
            self.close()
            if not retry:
                raise
            return self.request(method, path, body, headers, read, False)
        t1 = time.time()
        if not read:
            return r, t0, t1
        payload = r.read()
        if r.getheader("Connection", "").lower() == "close":
            self.close()
        return r.status, payload, t0, t1, time.time()


class Player:
    """Player clock: plays while buffered content is ahead of the playhead, stalls otherwise."""

    def __init__(self):
        self.lock = threading.Lock()
        self.buffered = self.playhead = 0.0
        self.playing = self.started = False
        self.last = None
        self.stall_since = time.time()
        self.started_at = None
        self.stalls = []  # (start, seconds) after startup

    def advance(self, now):
        with self.lock:
            if self.playing:
                avail, dt = self.buffered - self.playhead, now - self.last
                if dt <= avail:
                    self.playhead += dt
                else:
                    self.playhead, self.playing = self.buffered, False
                    self.stall_since = self.last + avail
            self.last = now

    def add(self, now, secs):
        self.advance(now)
        with self.lock:
            self.buffered += secs
            if not self.playing and self.buffered - self.playhead >= STARTUP_S - 1e-6:
                if self.started:
                    self.stalls.append((self.stall_since, now - self.stall_since))
                else:
                    self.started, self.started_at = True, now
                self.playing = True

    def ahead(self, now):
        self.advance(now)
        return self.buffered - self.playhead


class Probe:
    def __init__(self, password):
        self.password = password
        self.device_id = "probe-standing-" + uuid.uuid5(uuid.NAMESPACE_DNS, f"{INSTANCE}-{MODE}").hex[:12]
        self.token = self.user_id = self.play_session = self.ms_id = self.method = None
        self.api = Conn(HOST, self.auth)
        self.media = Conn(HOST, self.auth)
        self.player = Player()
        self.stop = threading.Event()
        self.progress = threading.Thread(target=self.progress_loop, daemon=True)
        self.segs = []  # HLS: (request start, ttfb, total, content seconds)
        self.t_request = self.t_end = None

    def auth(self):
        a = f'MediaBrowser Client="jellyfin-probe", Device="standing-{MODE}", DeviceId="{self.device_id}", Version="1.0"'
        return a + (f', Token="{self.token}"' if self.token else "")

    def call(self, method, path, body=None):
        st, data, *_ = self.api.request(method, path, body)
        if st not in (200, 204):
            raise RuntimeError(f"{method} {path.split('?')[0]} -> {st}")
        return json.loads(data) if data[:1] in (b"{", b"[") else None

    def report(self, kind):
        body = {"ItemId": ITEM, "MediaSourceId": self.ms_id, "PlaySessionId": self.play_session,
                "PlayMethod": self.method, "PositionTicks": int((START_S + self.player.playhead) * TICKS),
                "CanSeek": True, "IsPaused": False, "IsMuted": False, "RepeatMode": "RepeatNone"}
        if kind == "progress":
            body["EventName"] = "timeupdate"
        path = {"start": "/Sessions/Playing", "progress": "/Sessions/Playing/Progress",
                "stop": "/Sessions/Playing/Stopped"}[kind]
        self.api.request("POST", path, body)

    def progress_loop(self):
        while not self.stop.wait(10):
            try:
                self.player.advance(time.time())
                self.report("progress")
            except Exception as e:
                log("progress failed:", type(e).__name__)

    def run(self):
        d = self.call("POST", "/Users/AuthenticateByName", {"Username": USER, "Pw": self.password})
        self.token, self.user_id = d["AccessToken"], d["User"]["Id"]
        direct = MODE == "direct"
        profile = {
            "Name": "jellyfin-probe", "MaxStreamingBitrate": BITRATE, "MaxStaticBitrate": BITRATE,
            "DirectPlayProfiles": [{"Container": "mkv,mp4,m4v,mov,ts,webm", "Type": "Video",
                                    "VideoCodec": "h264,hevc,av1,vp9", "AudioCodec": "aac,ac3,eac3,dts,truehd,flac,mp3,opus"}] if direct else [],
            "TranscodingProfiles": [{"Container": "ts", "Type": "Video", "VideoCodec": "h264", "AudioCodec": "aac",
                                     "Protocol": "hls", "Context": "Streaming", "MaxAudioChannels": "2",
                                     "MinSegments": "1", "BreakOnNonKeyFrames": True}],
            "ContainerProfiles": [], "CodecProfiles": [],
            "SubtitleProfiles": [{"Format": f, "Method": "Embed"} for f in ("srt", "ass", "ssa", "pgssub", "subrip")],
        }
        self.t_request = time.time()
        pi = self.call("POST", f"/Items/{ITEM}/PlaybackInfo?UserId={self.user_id}", {
            "UserId": self.user_id, "MaxStreamingBitrate": BITRATE, "StartTimeTicks": int(START_S * TICKS),
            "IsPlayback": True, "AutoOpenLiveStream": True, "EnableDirectPlay": direct, "EnableDirectStream": direct,
            "EnableTranscoding": True, "SubtitleStreamIndex": -1, "AllowVideoStreamCopy": direct,
            "AllowAudioStreamCopy": False, "DeviceProfile": profile})
        ms = pi["MediaSources"][0]
        self.ms_id, self.play_session = ms["Id"], pi["PlaySessionId"]
        if direct:
            if not ms.get("SupportsDirectPlay"):
                raise RuntimeError(f"direct play refused: {ms.get('TranscodeReasons')}")
            self.method = "DirectPlay"
            self.report("start")
            self.progress.start()
            self.direct(ms)
        else:
            if not ms.get("TranscodingUrl"):
                raise RuntimeError("no TranscodingUrl")
            self.method = "Transcode"
            self.report("start")
            self.progress.start()
            self.hls(ms["TranscodingUrl"])

    def direct(self, ms):
        rate = (ms.get("Bitrate") or 8_000_000) / 8.0  # bytes per content second
        q = urllib.parse.urlencode({"static": "true", "MediaSourceId": self.ms_id,
                                    "PlaySessionId": self.play_session, "DeviceId": self.device_id})
        r, t0, t1 = self.media.request("GET", f"/Videos/{ITEM}/stream?{q}",
                                       headers={"Range": f"bytes={int(START_S * rate)}-"}, read=False)
        if r.status not in (200, 206):
            raise RuntimeError(f"stream -> {r.status}")
        end = t0 + DURATION_S
        while time.time() < end:
            if self.player.ahead(time.time()) >= BUFFER_S:
                time.sleep(0.2)
                continue
            b = r.read(256 * 1024)
            if not b:
                break
            self.player.add(time.time(), len(b) / rate)
        self.t_end = time.time()
        self.media.close()

    def hls(self, turl):
        st, master, *_ = self.media.request("GET", turl)
        base = turl.split("?")[0].rsplit("/", 1)[0] + "/"
        variant = next(l for l in master.decode().splitlines() if l and not l.startswith("#"))
        vurl = variant if variant.startswith("/") else base + variant
        st, vpl, *_ = self.media.request("GET", vurl)
        vbase = vurl.split("?")[0].rsplit("/", 1)[0] + "/"
        segs, dur = [], None
        for l in vpl.decode().splitlines():
            if l.startswith("#EXTINF:"):
                dur = float(l[8:].split(",")[0])
            elif l and not l.startswith("#"):
                segs.append((dur, l if l.startswith("/") else vbase + l))
        i, acc = 0, 0.0
        while i < len(segs) - 1 and acc + segs[i][0] <= START_S:
            acc += segs[i][0]
            i += 1
        t_start = time.time()
        while i < len(segs) and time.time() - t_start < DURATION_S:
            if self.player.ahead(time.time()) >= BUFFER_S:
                time.sleep(0.2)
                continue
            d, url = segs[i]
            st, data, s0, s1, s2 = self.media.request("GET", url)
            if st != 200:
                raise RuntimeError(f"segment -> {st}")
            self.segs.append((s0, s1 - s0, s2 - s0, d))
            self.player.add(s2, d)
            i += 1
        self.t_end = time.time()

    def close(self):
        self.stop.set()
        if self.progress.is_alive():
            self.progress.join(timeout=15)  # it shares self.api with the stop report
        self.t_end = self.t_end or time.time()
        self.player.advance(self.t_end)
        steps = [("stop", lambda: self.report("stop")),
                 ("encodings", lambda: self.call("DELETE", "/Videos/ActiveEncodings?" + urllib.parse.urlencode(
                     {"deviceId": self.device_id, "playSessionId": self.play_session}))),
                 ("logout", lambda: self.call("POST", "/Sessions/Logout"))]
        for what, fn in steps:
            if (what != "logout" and self.play_session is None) or self.token is None:
                continue
            try:
                fn()
            except Exception as e:
                log(f"cleanup {what} failed:", type(e).__name__)
        self.token = None
        self.media.close()
        self.api.close()


def summarize(p):
    """Gauges of one run. Pace: content played per wall second after startup (1 without stalls; a
    stall still open at the end counts). TTFB quantiles of the HLS segments after the first."""
    pl, segs = p.player, p.segs
    out = {}
    if pl.started_at is not None and p.t_request is not None:
        out["startup_seconds"] = pl.started_at - p.t_request
    if pl.started_at is not None and p.t_end and p.t_end > pl.started_at:
        stalls = list(pl.stalls)
        if not pl.playing:
            stalls.append((pl.stall_since, p.t_end - pl.stall_since))
        out["pace"] = pl.playhead / (p.t_end - pl.started_at)
        out["stalls"] = len(stalls)
        out["stall_seconds"] = sum(x for _, x in stalls)
    if MODE == "transcode" and len(segs) > 1:
        ttfb = [s[1] for s in segs[1:]]
        out["ttfb_p50_seconds"] = pct(ttfb, 0.5)
        out["ttfb_p95_seconds"] = pct(ttfb, 0.95)
        out["first_segment_seconds"] = segs[0][2]
        out["segments"] = len(segs)
    return out


def otlp_body(values, result, now_ns):
    attrs = [{"key": "jellyfin", "value": {"stringValue": INSTANCE}}, {"key": "mode", "value": {"stringValue": MODE}}]
    metrics = [{"name": f"jellyfin_probe_{k}", "gauge": {"dataPoints": [
        {"timeUnixNano": str(now_ns), "asDouble": float(v), "attributes": attrs}]}} for k, v in values.items()]
    metrics.append({"name": "jellyfin_probe_runs", "gauge": {"dataPoints": [
        {"timeUnixNano": str(now_ns), "asDouble": 1.0,
         "attributes": attrs + [{"key": "result", "value": {"stringValue": result}}]}]}})
    return {"resourceMetrics": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "jellyfin-probe"}}]},
                                 "scopeMetrics": [{"scope": {"name": "jellyfin-probe"}, "metrics": metrics}]}]}


def push(values, result):
    body = json.dumps(otlp_body(values, result, time.time_ns())).encode()
    req = urllib.request.Request(OTLP, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def read_password():
    with open(PASSWORD_FILE) as f:
        return f.read().strip()


def main():
    b, failed = busy()
    if failed:
        log("skipped, skip check failed:", ", ".join(failed))
        push({}, "check_failed")
        return 1
    if b:
        log("skipped, busy:", ", ".join(b))
        push({}, "skipped")
        return 0
    p = Probe(read_password())

    def term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, term)
    result = "ok"
    try:
        p.run()
    except KeyboardInterrupt:
        result = "error"
        log("terminated")
    except Exception as e:
        result = "error"
        log("error:", str(e) if isinstance(e, RuntimeError) else type(e).__name__)
    finally:
        p.close()
    values = summarize(p)
    log(result, json.dumps({k: round(v, 3) for k, v in values.items()}))
    push(values, result)
    return 0 if result == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
