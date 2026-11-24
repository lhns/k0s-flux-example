"""Offline tests of the sessions exporter: /Sessions JSON shaped as Jellyfin 12.4 returns it."""
import copy
import hashlib
import io
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sessions-exporter"))
import jellyfin_sessions as js  # noqa: E402

TRANSCODE = {
    "Id": "0123456789abcdef0123456789abcdef", "Client": "jellyfin-probe", "DeviceName": "probe-1",
    "UserName": "jellyfin-probe",
    "PlayState": {"PositionTicks": 6060000000, "IsPaused": False, "PlayMethod": "Transcode"},
    "TranscodingInfo": {
        "IsVideoDirect": False, "IsAudioDirect": False, "Framerate": 47.952, "CompletionPercentage": 8.0,
        "TranscodeReasons": ["VideoCodecNotSupported", "ContainerBitrateExceedsLimit"]},
    "NowPlayingItem": {"Name": "Feature Film A", "MediaStreams": [
        {"Type": "Video", "AverageFrameRate": 23.976, "RealFrameRate": 24.0}, {"Type": "Audio"}]},
}
DIRECT = {
    "Id": "fedcba9876543210fedcba9876543210", "Client": "jellyfin-probe", "DeviceName": "probe-2",
    "UserName": "someone",
    "PlayState": {"PositionTicks": 6088040206, "IsPaused": True, "PlayMethod": "DirectPlay"},
    "TranscodingInfo": None,
    "NowPlayingItem": {"Name": "Anthology B", "MediaStreams": [{"Type": "Video", "AverageFrameRate": 23.976}]},
}
IDLE = {"Id": "aa", "UserName": "idle", "PlayState": {}, "NowPlayingItem": None}
SESSIONS = [TRANSCODE, DIRECT, IDLE]
NAMES = ("Feature", "Anthology", "probe", "someone", "idle")


def sid(s):
    return hashlib.sha256(s["Id"].encode()).hexdigest()[:12]


def out_for(sessions, inst="jellyfin"):
    return js.render({inst: (100, sessions)}, {inst: "u"}, 100)


def lines(text, metric):
    return [l for l in text.splitlines() if l.startswith(metric + "{")]


def value(line):
    return float(line.rsplit(" ", 1)[1])


class Render(unittest.TestCase):
    def test_transcode_and_direct(self):
        text = out_for(SESSIONS)
        base = f'jellyfin="jellyfin",session="{sid(TRANSCODE)}",play_method="Transcode"'
        self.assertIn(f"jellyfin_session_position_seconds{{{base}}} 606.0", text)
        self.assertIn(f"jellyfin_session_paused{{{base}}} 0", text)
        self.assertIn(f"jellyfin_session_transcode_framerate{{{base}}} 47.952", text)
        self.assertIn(f"jellyfin_session_transcode_completion_ratio{{{base}}} 0.08", text)
        self.assertIn(f'jellyfin_session_transcode_direct{{{base},stream="video"}} 0', text)
        self.assertIn(f'jellyfin_session_transcode_direct{{{base},stream="audio"}} 0', text)
        dbase = f'jellyfin="jellyfin",session="{sid(DIRECT)}",play_method="DirectPlay"'
        self.assertIn(f"jellyfin_session_position_seconds{{{dbase}}} 608.8040206", text)
        self.assertIn(f"jellyfin_session_paused{{{dbase}}} 1", text)
        self.assertEqual(len(lines(text, "jellyfin_session_position_seconds")), 2)
        self.assertEqual(len(lines(text, "jellyfin_session_transcode_framerate")), 1)

    def test_no_names_or_raw_ids(self):
        text = out_for(SESSIONS)
        for n in NAMES + (TRANSCODE["Id"], DIRECT["Id"]):
            self.assertNotIn(n, text)

    def test_help_and_type(self):
        text = out_for(SESSIONS)
        for name in js.HELP:
            self.assertIn(f"# HELP {name} ", text)
            self.assertIn(f"# TYPE {name} gauge", text)

    def test_speed(self):
        (line,) = lines(out_for(SESSIONS), "jellyfin_session_transcode_speed")
        self.assertAlmostEqual(value(line), 47.952 / 23.976)

    def test_speed_falls_back_to_real_frame_rate(self):
        s = copy.deepcopy(TRANSCODE)
        s["NowPlayingItem"]["MediaStreams"][0]["AverageFrameRate"] = None
        (line,) = lines(out_for([s]), "jellyfin_session_transcode_speed")
        self.assertAlmostEqual(value(line), 47.952 / 24.0)

    def test_speed_absent_without_framerate(self):
        s = copy.deepcopy(TRANSCODE)
        del s["TranscodingInfo"]["Framerate"], s["TranscodingInfo"]["CompletionPercentage"]
        text = out_for([s])
        for m in ("transcode_speed", "transcode_framerate", "transcode_completion_ratio"):
            self.assertEqual(lines(text, "jellyfin_session_" + m), [])
        self.assertEqual(len(lines(text, "jellyfin_session_transcode_reason")), 2)

    def test_speed_absent_when_video_direct(self):
        s = copy.deepcopy(TRANSCODE)
        s["TranscodingInfo"]["IsVideoDirect"] = True
        text = out_for([s])
        self.assertEqual(lines(text, "jellyfin_session_transcode_speed"), [])
        self.assertEqual(len(lines(text, "jellyfin_session_transcode_framerate")), 1)
        self.assertIn('stream="video"} 1', text)

    def test_reasons(self):
        got = lines(out_for(SESSIONS), "jellyfin_session_transcode_reason")
        self.assertEqual(len(got), 2)
        self.assertTrue(any('reason="VideoCodecNotSupported"' in l and l.endswith(" 1") for l in got))
        self.assertTrue(any('reason="ContainerBitrateExceedsLimit"' in l for l in got))

    def test_counts_include_zeros(self):
        text = out_for(SESSIONS)
        self.assertIn('jellyfin_sessions{jellyfin="jellyfin",play_method="Transcode"} 1', text)
        self.assertIn('jellyfin_sessions{jellyfin="jellyfin",play_method="DirectPlay"} 1', text)
        self.assertIn('jellyfin_sessions{jellyfin="jellyfin",play_method="DirectStream"} 0', text)
        text = out_for([])
        self.assertEqual(len(lines(text, "jellyfin_sessions")), 3)
        self.assertIn('jellyfin_sessions_up{jellyfin="jellyfin"} 1', text)

    def test_escaping(self):
        self.assertEqual(js.esc('a"b\\c\nd'), 'a\\"b\\\\c\\nd')
        s = copy.deepcopy(TRANSCODE)
        s["TranscodingInfo"]["TranscodeReasons"] = ['we"ird\\']
        self.assertIn('reason="we\\"ird\\\\"', out_for([s]))

    def test_stale_snapshot_is_down(self):
        snap = {"jellyfin": (100, SESSIONS)}
        fresh = js.render(snap, {"jellyfin": "u"}, 100 + 4 * 15, 15)
        self.assertIn('jellyfin_sessions_up{jellyfin="jellyfin"} 1', fresh)
        stale = js.render(snap, {"jellyfin": "u"}, 100 + 4 * 15 + 1, 15)
        self.assertIn('jellyfin_sessions_up{jellyfin="jellyfin"} 0', stale)
        self.assertEqual(lines(stale, "jellyfin_session_paused"), [])
        self.assertEqual(lines(stale, "jellyfin_sessions"), [])

    def test_instance_never_polled_is_down(self):
        text = js.render({}, {"jellyfin": "u"}, 100)
        self.assertIn('jellyfin_sessions_up{jellyfin="jellyfin"} 0', text)


class Polling(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.calls = []

    def key(self, name, text):
        with open(os.path.join(self.dir.name, name), "w") as f:
            f.write(text)

    def poller(self, fetch):
        return js.Poller({"a": "http://a", "b": "http://b"}, self.dir.name, fetch, clock=lambda: 100)

    def render(self, p):
        return js.render(p.snapshot(), {"a": "", "b": ""}, 100)

    def test_key_is_read_fresh_and_stripped(self):
        def fetch(url, key):
            self.calls.append((url, key))
            return []
        self.key("a", "k1\n")
        p = self.poller(fetch)
        p.poll_once()
        self.key("a", "k2\n")
        p.poll_once()
        self.assertEqual(self.calls, [("http://a", "k1"), ("http://a", "k2")])

    def test_missing_key_is_down_without_fetch(self):
        self.key("a", "k")
        p = self.poller(lambda url, key: [TRANSCODE])
        p.poll_once()
        text = self.render(p)
        self.assertIn('jellyfin_sessions_up{jellyfin="a"} 1', text)
        self.assertIn('jellyfin_sessions_up{jellyfin="b"} 0', text)
        self.assertEqual(len(lines(text, "jellyfin_session_paused")), 1)
        self.assertIn('jellyfin="a"', lines(text, "jellyfin_session_paused")[0])

    def test_http_error_is_down_and_key_not_logged(self):
        self.key("a", "sekret")
        self.key("b", "sekret")

        def fetch(url, key):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO())
        p = self.poller(fetch)
        with self.assertLogs("jellyfin-sessions", "INFO") as cm:
            p.poll_once()
            p.poll_once()
        self.assertEqual(cm.output, ["INFO:jellyfin-sessions:a: HTTP 401", "INFO:jellyfin-sessions:b: HTTP 401"])
        text = self.render(p)
        self.assertIn('jellyfin_sessions_up{jellyfin="a"} 0', text)
        self.assertEqual(lines(text, "jellyfin_sessions"), [])
        self.assertNotIn("sekret", text)

    def test_recovery(self):
        self.key("a", "k")
        self.key("b", "k")
        fail = [True]

        def fetch(url, key):
            if fail[0]:
                raise OSError("down")
            return []
        p = self.poller(fetch)
        p.poll_once()
        self.assertIn('jellyfin_sessions_up{jellyfin="a"} 0', self.render(p))
        fail[0] = False
        p.poll_once()
        self.assertIn('jellyfin_sessions_up{jellyfin="a"} 1', self.render(p))


class Config(unittest.TestCase):
    def test_parse_instances(self):
        self.assertEqual(js.parse_instances("a=http://x:1/, b = http://y"), {"a": "http://x:1", "b": "http://y"})
        self.assertEqual(list(js.parse_instances(js.DEFAULT_INSTANCES)), ["jellyfin", "jellyfin-sqlite"])


if __name__ == "__main__":
    unittest.main()
