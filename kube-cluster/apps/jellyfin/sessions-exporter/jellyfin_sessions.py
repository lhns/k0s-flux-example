#!/usr/bin/env python3
"""Playback state per Jellyfin session from GET /Sessions (README.md, "Sessions exporter").

A background thread polls each instance; a scrape renders the last snapshot, so a slow Jellyfin
never blocks it. Sessions are labelled by a hash of their id: no user, device or item names.
"""
import hashlib
import http.server
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

DEFAULT_INSTANCES = ("jellyfin=http://jellyfin.jellyfin.svc.cluster.local:8096,"
                     "jellyfin-sqlite=http://jellyfin-sqlite.jellyfin.svc.cluster.local:8096")
INSTANCES = os.environ.get("INSTANCES", DEFAULT_INSTANCES)
KEY_DIR = os.environ.get("KEY_DIR", "/etc/jellyfin-sessions")
PORT = int(os.environ.get("PORT", "9924"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "15"))

METHODS = ("Transcode", "DirectStream", "DirectPlay")
log = logging.getLogger("jellyfin-sessions")


def parse_instances(text):
    """{name: url} of a comma list name=url."""
    out = {}
    for item in filter(None, (i.strip() for i in text.split(","))):
        name, _, url = item.partition("=")
        out[name.strip()] = url.strip().rstrip("/")
    return out


def fetch(url, key):
    """The JSON of GET {url}/Sessions; the key never leaves the header."""
    req = urllib.request.Request(f"{url}/Sessions?activeWithinSeconds=960",
                                 headers={"Authorization": f'MediaBrowser Token="{key}"'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def source_fps(item):
    """The video stream's average (else real) frame rate, or None."""
    for s in (item or {}).get("MediaStreams") or []:
        if s.get("Type") == "Video":
            return s.get("AverageFrameRate") or s.get("RealFrameRate") or None
    return None


def session_rows(s):
    """(play method, [(metric, extra labels, value)]) of one playing session."""
    ps = s.get("PlayState") or {}
    method = ps.get("PlayMethod") or "DirectPlay"
    sid = hashlib.sha256(str(s.get("Id", "")).encode()).hexdigest()[:12]
    base = {"session": sid, "play_method": method}
    rows = [("jellyfin_session_position_seconds", base, (ps.get("PositionTicks") or 0) / 1e7),
            ("jellyfin_session_paused", base, 1 if ps.get("IsPaused") else 0)]
    t = s.get("TranscodingInfo")
    if t:
        fps = t.get("Framerate")
        if fps is not None:
            rows.append(("jellyfin_session_transcode_framerate", base, fps))
            src = source_fps(s.get("NowPlayingItem"))
            if src and not t.get("IsVideoDirect"):
                rows.append(("jellyfin_session_transcode_speed", base, fps / src))
        if t.get("CompletionPercentage") is not None:
            rows.append(("jellyfin_session_transcode_completion_ratio", base, t["CompletionPercentage"] / 100))
        for reason in t.get("TranscodeReasons") or []:
            rows.append(("jellyfin_session_transcode_reason", {**base, "reason": reason}, 1))
        for stream, key in (("video", "IsVideoDirect"), ("audio", "IsAudioDirect")):
            if key in t:
                rows.append(("jellyfin_session_transcode_direct", {**base, "stream": stream}, 1 if t[key] else 0))
    return method, rows


def read_key(key_dir, name):
    with open(os.path.join(key_dir, name)) as f:
        key = f.read().strip()
    if not key:
        raise ValueError("empty key")
    return key


class Poller:
    """Latest snapshot per instance: {name: (time, sessions, or None when the poll failed)}."""

    def __init__(self, instances, key_dir, fetch_fn=fetch, clock=time.time):
        self.instances, self.key_dir, self.fetch, self.clock = instances, key_dir, fetch_fn, clock
        self.lock = threading.Lock()
        self.snap = {}
        self.state = {}  # last logged state per instance

    def poll_once(self):
        for name, url in self.instances.items():
            try:
                sessions = self.fetch(url, read_key(self.key_dir, name))
                if not isinstance(sessions, list):
                    raise ValueError("not a list")
                state, snap = "ok", sessions
            except Exception as e:  # noqa: BLE001 -- any failure is "down"
                code = getattr(e, "code", None)
                if isinstance(e, urllib.error.HTTPError):
                    e.close()  # it holds the response's socket
                state, snap = f"HTTP {code}" if code else type(e).__name__, None
            with self.lock:
                self.snap[name] = (self.clock(), snap)
            if self.state.get(name) != state:
                self.state[name] = state
                log.info("%s: %s", name, state)

    def run(self):
        while True:
            self.poll_once()
            time.sleep(POLL_SECONDS)

    def snapshot(self):
        with self.lock:
            return dict(self.snap)


HELP = {
    "jellyfin_sessions_up": "1 if the last poll of the instance's /Sessions succeeded and is recent.",
    "jellyfin_sessions": "Playing sessions per play method.",
    "jellyfin_session_position_seconds": "Playback position, moved by the client's progress reports (about every 10 s).",
    "jellyfin_session_paused": "1 if the session is paused.",
    "jellyfin_session_transcode_framerate": "ffmpeg's output frame rate; absent once it finished its run-ahead.",
    "jellyfin_session_transcode_speed": "ffmpeg output fps / source video fps, while the video is re-encoded.",
    "jellyfin_session_transcode_completion_ratio": "Share of the item ffmpeg has transcoded, 0 to 1.",
    "jellyfin_session_transcode_reason": "1 per reason Jellyfin gives for transcoding.",
    "jellyfin_session_transcode_direct": "1 if the stream (video or audio) is copied rather than re-encoded.",
}


def render(snapshot, instances, now, poll_seconds=POLL_SECONDS):
    out = {name: [] for name in HELP}
    for inst in instances:
        t, sessions = snapshot.get(inst, (0, None))
        up = sessions is not None and now - t <= 4 * poll_seconds
        out["jellyfin_sessions_up"].append(f'jellyfin_sessions_up{{jellyfin="{esc(inst)}"}} {int(up)}')
        if not up:
            continue
        counts = dict.fromkeys(METHODS, 0)
        for s in sessions:
            if not s.get("NowPlayingItem"):
                continue
            method, rows = session_rows(s)
            counts[method] = counts.get(method, 0) + 1
            for name, labels, v in rows:
                lbl = ",".join(f'{k}="{esc(x)}"' for k, x in {"jellyfin": inst, **labels}.items())
                out[name].append(f"{name}{{{lbl}}} {v}")
        for m, n in counts.items():
            out["jellyfin_sessions"].append(f'jellyfin_sessions{{jellyfin="{esc(inst)}",play_method="{esc(m)}"}} {n}')
    lines = []
    for name, text in HELP.items():
        lines += [f"# HELP {name} {text}", f"# TYPE {name} gauge"] + out[name]
    return "\n".join(lines) + "\n"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    instances = parse_instances(INSTANCES)
    poller = Poller(instances, KEY_DIR)
    threading.Thread(target=poller.run, daemon=True).start()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                body = render(poller.snapshot(), instances, time.time()).encode()
                ctype = "text/plain; version=0.0.4"
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

    log.info("listening on :%d", PORT)
    http.server.ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
