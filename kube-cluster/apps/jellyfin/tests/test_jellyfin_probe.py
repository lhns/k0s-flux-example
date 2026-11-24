"""Offline tests of the standing probe: skip check, run summary and the OTLP body."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "probe"))
import jellyfin_probe as jp  # noqa: E402


class Busy(unittest.TestCase):
    def test_idle(self):
        self.assertEqual(jp.busy(lambda e: 0.0), ([], []))

    def test_each_signal(self):
        for name, (expr, limit) in jp.BUSY.items():
            got = jp.busy(lambda e, x=expr, lim=limit: lim + 1 if e == x else 0.0)
            self.assertEqual(got, ([name], []))

    def test_ffmpeg_below_limit_is_idle(self):
        expr = jp.BUSY["ffmpeg_cores"][0]
        self.assertEqual(jp.busy(lambda e: 0.1 if e == expr else 0.0), ([], []))

    def test_query_failure_is_reported(self):
        def boom(e):
            raise OSError("unreachable")
        busy, failed = jp.busy(boom, wait=0)
        self.assertEqual(busy, [])
        self.assertEqual(len(failed), len(jp.BUSY))

    def test_query_retried(self):
        calls = []

        def flaky(e):
            calls.append(e)
            if len(calls) < 3:
                raise OSError("not programmed yet")
            return 0.0
        self.assertEqual(jp.busy(flaky, wait=0), ([], []))


def run(mode, events, t_request=0.0, end=None):
    """A Probe whose player saw events [(time, content seconds)], as direct() or hls() feed it."""
    old = jp.MODE
    jp.MODE = mode
    try:
        p = jp.Probe("x")
        p.t_request = t_request
        for t, secs in events:
            p.player.add(t, secs)
        p.t_end = end if end is not None else events[-1][0]
        p.player.advance(p.t_end)
        return p
    finally:
        jp.MODE = old


class Summary(unittest.TestCase):
    def tearDown(self):
        jp.MODE = "transcode"

    def test_direct_paced_without_stalls(self):
        # What direct() feeds the player: reads at media rate, a 30 s buffer.
        events = [(1.0 + 0.1 * i, 1.0) for i in range(30)] + [(4.0 + i, 1.0) for i in range(150)]
        p = run("direct", events)
        jp.MODE = "direct"
        v = jp.summarize(p)
        self.assertAlmostEqual(v["startup_seconds"], 1.2)
        self.assertAlmostEqual(v["pace"], 1.0)
        self.assertEqual(v["stalls"], 0)
        self.assertNotIn("ttfb_p50_seconds", v)

    def test_transcode_with_a_stall(self):
        # First segment after 30 s; 3 s segments every 3 s, except a 10 s wait for one.
        times = [30.0] + [30.0 + 3 * i for i in range(1, 20)] + [97.0 + 3 * i for i in range(20)]
        p = run("transcode", [(t, 3.0) for t in times])
        p.segs = [(1.0, 29.0, 29.5, 3.0)] + [(t - 0.5, 0.1 * (i % 9 + 1), 0.5, 3.0) for i, t in enumerate(times[1:])]
        jp.MODE = "transcode"
        v = jp.summarize(p)
        self.assertAlmostEqual(v["startup_seconds"], 30.0)
        self.assertEqual(v["stalls"], 1)
        self.assertAlmostEqual(v["stall_seconds"], 7.0)
        self.assertAlmostEqual(v["pace"], (154.0 - 30.0 - 7.0) / (154.0 - 30.0))
        self.assertAlmostEqual(v["ttfb_p50_seconds"], 0.5)
        self.assertAlmostEqual(v["ttfb_p95_seconds"], 0.9)
        self.assertEqual(v["segments"], 40)

    def test_open_stall_at_the_end_counts(self):
        p = run("transcode", [(0.0, 3.0), (3.0, 3.0)], end=16.0)  # dry from t=6
        v = jp.summarize(p)
        self.assertEqual(v["stalls"], 1)
        self.assertAlmostEqual(v["stall_seconds"], 10.0)
        self.assertAlmostEqual(v["pace"], 6.0 / 16.0)

    def test_failed_before_playback(self):
        self.assertEqual(jp.summarize(jp.Probe("x")), {})


class Otlp(unittest.TestCase):
    def test_body(self):
        b = jp.otlp_body({"pace": 1.0, "stalls": 0}, "ok", 123)
        rm = b["resourceMetrics"][0]
        self.assertEqual(rm["resource"]["attributes"][0]["value"]["stringValue"], "jellyfin-probe")
        ms = {m["name"]: m["gauge"]["dataPoints"][0] for m in rm["scopeMetrics"][0]["metrics"]}
        self.assertEqual(set(ms), {"jellyfin_probe_pace", "jellyfin_probe_stalls", "jellyfin_probe_runs"})
        self.assertEqual(ms["jellyfin_probe_pace"]["timeUnixNano"], "123")
        attrs = {a["key"]: a["value"]["stringValue"] for a in ms["jellyfin_probe_runs"]["attributes"]}
        self.assertEqual(attrs, {"jellyfin": jp.INSTANCE, "mode": jp.MODE, "result": "ok"})

    def test_skipped_sends_only_the_run(self):
        ms = jp.otlp_body({}, "skipped", 1)["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        self.assertEqual([m["name"] for m in ms], ["jellyfin_probe_runs"])


if __name__ == "__main__":
    unittest.main()
