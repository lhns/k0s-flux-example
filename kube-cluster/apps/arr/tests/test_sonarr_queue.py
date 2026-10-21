"""Tests for tools/sonarr-queue.py. Synthetic fixtures, no network, no cluster."""
import base64
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from urllib import error as urlerror
from urllib import parse as urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "sonarr_queue", os.path.join(HERE, "..", "tools", "sonarr-queue.py"))
sq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sq)

DUMMY_KEY = "placeholder-placeholder"  # low entropy, so secret scanners skip it


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


class FakeClient(sq.Client):
    """Serves fixtures by path; records every request."""

    def __init__(self, page_size=None, queue=None, manual=None):
        super().__init__()
        self.queue = queue or fixture("queue.json")
        self.manual = manual or fixture("manualimport.json")
        self.page_size = page_size
        self.log = []

    def send(self, requests):
        out = []
        for method, full, body in requests:
            self.log.append((method, full, body))
            path, _, qs = full.partition("?")
            q = dict(urlparse.parse_qsl(qs))
            if method != "GET":
                out.append({"id": 77, "status": "queued"} if method == "POST" else {})
            elif path.endswith("/system/status"):
                out.append(fixture("status.json"))
            elif path.endswith("/queue"):
                size = self.page_size or int(q["pageSize"])
                recs = self.queue["records"]
                page = int(q["page"])
                out.append({"page": page, "pageSize": size, "totalRecords": len(recs),
                            "records": recs[(page - 1) * size:page * size]})
            elif path.endswith("/manualimport"):
                out.append(self.manual[q["downloadId"]])
            elif path.endswith("/episode"):
                out.append(fixture("episodes-anime.json"))
            elif path.endswith("/parse"):
                out.append(fixture("parse.json"))
            else:
                raise AssertionError("unexpected request " + full)
        return out

    def writes(self):
        return [r for r in self.log if r[0] != "GET"]


def run(argv, client=None):
    out, err = io.StringIO(), io.StringIO()
    client = client or FakeClient()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = sq.main(argv, client=client)
    return rc, out.getvalue(), err.getvalue(), client


def downloads(want="German,English", **kw):
    return sq.fetch_downloads(FakeClient(**kw), want)


def by_prefix(dls, prefix):
    return next(d for d in dls if d["download_id"].startswith(prefix))


class Classification(unittest.TestCase):
    def setUp(self):
        self.dls = downloads()

    def cat(self, prefix):
        d = by_prefix(self.dls, prefix)
        return d["category"]

    def test_every_category(self):
        expect = {
            "1111AAAA": "title-mismatch",
            "2222BBBB": "episode-not-found",
            "3333CCCC": "anime-numbering",
            "4444DDDD": "language",
            "5555EEEE": "not-upgrade",
            "8888BBBB": "dangerous-file",
            "9999CCCC": "unknown-series",
            "AAAA1010": "archive",
            "BBBB1111": "unparseable",
            "CCCC1212": "already-imported",
            "DDDD1313": "multi-episode-file",
            "EEEE1414": "sample",
            "FFFF1515": "anime-numbering",
            "0000AAAA": "other",
        }
        for prefix, cat in expect.items():
            self.assertEqual(self.cat(prefix), cat, prefix)
        # year-mismatch is covered by test_year_mismatch on a modified download.
        self.assertEqual({c for c, _ in sq.CATEGORIES},
                         set(expect.values()) | {"other", "year-mismatch"})

    def test_downloading_not_listed(self):
        self.assertFalse([d for d in self.dls if d["download_id"].startswith("1717")])
        self.assertEqual(len(self.dls), 16)

    def test_empty_candidates_is_dangerous_not_crash(self):
        self.assertEqual(by_prefix(self.dls, "8888BBBB")["candidates"], [])

    def test_sample_ignored_when_main_file_exists(self):
        d = by_prefix(self.dls, "2222BBBB")
        self.assertTrue(any(c["sample"] for c in d["candidates"]))
        self.assertNotIn("sample", [d["category"]] + d["secondary"])

    def test_season_pack_is_one_download_with_all_rows(self):
        d = by_prefix(self.dls, "2222BBBB")
        self.assertEqual(d["rows"], 3)
        self.assertEqual(len(d["queue_ids"]), 3)

    def test_secondary_categories(self):
        d = by_prefix(self.dls, "3333CCCC")
        self.assertEqual(d["category"], "anime-numbering")

    def test_language_is_derived_and_configurable(self):
        d = by_prefix(downloads(want="French"), "4444DDDD")
        self.assertNotEqual(d["category"], "language")

    def test_year_mismatch(self):
        d = by_prefix(self.dls, "1111AAAA")
        d["series_year"] = 2005
        d["title"] = "Example.Show.2025.S01E02.GERMAN.1080p.WEB-Group-A"
        self.assertEqual(sq.classify(d, ["german"])[0], "year-mismatch")
        d["category"], d["secondary"] = sq.classify(d, ["german"])
        p = sq.plan_auto([d], ["german"])["downloads"][0]
        self.assertEqual(p["selected"], [])
        self.assertIn("year-mismatch", p["skipped"][0][1])
        # A year after the season marker is the pack's year, not part of the show name.
        d["title"] = "Example.Show.S01.Complete.2006.GERMAN.1080p.WEB-Group-A"
        self.assertEqual(sq.classify(d, ["german"])[0], "title-mismatch")

    def test_manualimport_error_is_reported_not_fatal(self):
        class Racing(FakeClient):
            def send(self, requests):
                return [sq.CliError("GET /api/v3/manualimport -> HTTP 500: File doesn't exist")
                        if "1111AAAA" in r[1] else x
                        for r, x in zip(requests, super().send(requests))]
        dls = sq.fetch_downloads(Racing(), "German")
        d = by_prefix(dls, "1111AAAA")
        self.assertEqual((d["category"], d["candidates"]), ("other", []))
        self.assertIn("manualimport failed: GET /api/v3/manualimport -> HTTP 500", d["messages"][-1])
        self.assertEqual(by_prefix(dls, "2222BBBB")["category"], "episode-not-found")

    def test_title_mismatch_requires_clean_candidates(self):
        d = by_prefix(self.dls, "1111AAAA")
        d["candidates"][0]["rejections"] = ["Some rejection"]
        self.assertNotEqual(sq.classify(d, ["german"])[0], "title-mismatch")

    def test_file_and_release_languages(self):
        d = by_prefix(self.dls, "3333CCCC")
        self.assertEqual(d["file_languages"], ["Japanese", "English"])
        self.assertEqual(d["release_languages"], ["German"])

    def test_paging(self):
        dls = downloads(page_size=5)
        self.assertEqual(len(dls), 16)


class Numbering(unittest.TestCase):
    def test_schemes(self):
        d = sq.detect_scheme
        self.assertEqual(d("Show S02E05.mkv")[0], "season")
        self.assertEqual(d("[G] Anime Example S14E922 [1080p].mkv")[0], "hybrid")
        self.assertEqual(d("[G] Anime Example S2 - 06.mkv"), ("season-relative", 2, 6))
        # A half episode is not episode 12 of a season-relative pack.
        self.assertNotEqual(d("[G] Anime Example S2 - 12.5 (Special).mkv")[0], "season-relative")
        self.assertEqual(d("[G] Anime Example - 123 [1080p].mkv"), ("absolute", None, 123))
        self.assertEqual(d("Anime.Example.123.1080p.mkv"), ("absolute", None, 123))
        self.assertEqual(d("Anime.Example.1080p.mkv")[0], "unknown")
        self.assertEqual(d("Show S02E20.mkv", season_max=12)[0], "hybrid")

    def test_report_breakdown_with_parse(self):
        dls = downloads()
        client = FakeClient()
        det = sq.numbering_details(client, dls, True)
        rel = det[by_prefix(dls, "3333CCCC")["download_id"]]
        self.assertEqual([f["scheme"] for f in rel], ["season-relative"] * 2)
        self.assertIn("explicit mapping", rel[0]["fix"])
        self.assertEqual(rel[0]["lookup"], "S02E05, no file")
        self.assertEqual(rel[0]["parse"]["absolute"], [6])
        hyb = det[by_prefix(dls, "FFFF1515")["download_id"]][0]
        self.assertEqual(hyb["scheme"], "hybrid")
        self.assertEqual(hyb["mapped"], "nothing")
        self.assertIn("abs 922: not in Sonarr's episode list", hyb["lookup"])
        # one batched call per anime series plus one parse per file name
        gets = [r for r in client.log if r[0] == "GET"]
        self.assertEqual(sum("/episode" in r[1] for r in gets), 1)

    def test_no_parse_makes_no_extra_requests(self):
        client = FakeClient()
        sq.numbering_details(client, downloads(), False)
        self.assertEqual(client.log, [])

    def test_absolute_on_standard_series_says_anime(self):
        dl = {"series_type": "standard", "use_scene": False}
        self.assertEqual(sq.suggest(dl, "absolute", False, False, False), "set series type to anime")

    def test_scene_hint(self):
        dl = {"series_type": "anime", "use_scene": False}
        self.assertIn("useSceneNumbering", sq.suggest(dl, "season", False, False, True))


class Reports(unittest.TestCase):
    def test_collapse_messages_differing_in_numbers(self):
        self.assertEqual(sq.collapse(["Episode 1x04 was unexpected", "Episode 1x05 was unexpected",
                                      "Sample"]),
                         ["Episode 1x04 was unexpected (and 1 more like it)", "Sample"])

    def test_grouping_and_counts(self):
        groups = sq.report_groups(downloads())
        self.assertEqual([g["category"] for g in groups],
                         [c for c, _ in sq.CATEGORIES if c in {g["category"] for g in groups}])
        anime = next(g for g in groups if g["category"] == "anime-numbering")
        self.assertEqual(anime["downloads"], 2)
        self.assertEqual(anime["files"], 3)
        self.assertEqual(anime["series"][0]["series"], "Anime Example")
        enf = next(g for g in groups if g["category"] == "episode-not-found")
        self.assertEqual(enf["series"][0]["files"], 4)
        self.assertEqual(enf["series"][0]["reasons"], ["Invalid season or episode"])

    def test_report_text(self):
        rc, out, err, _ = run(["report"])
        self.assertEqual(rc, 0, err)
        self.assertIn("== dangerous-file: 1 download(s), 0 file(s)", out)
        self.assertIn("owner policy", out)
        self.assertIn("release title: Other.Name.S01E02", out)
        self.assertIn("series title:  Example Show", out)
        self.assertIn("season-relative", out)

    def test_report_json(self):
        rc, out, _, _ = run(["report", "--json"])
        data = json.loads(out)
        self.assertEqual(data[0]["category"], "dangerous-file")
        self.assertIn("numbering", next(g for g in data if g["category"] == "anime-numbering"))

    def test_list_text_and_json(self):
        rc, out, _, _ = run(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("3333CCCC", out)
        self.assertIn("S02E05-E06 (abs 17-18)", out)
        self.assertIn("16 download(s)", out)
        rc, out, _, _ = run(["list", "--json"])
        data = json.loads(out)
        self.assertEqual(len(data), 16)
        self.assertIn("rejections", next(d for d in data if d["candidates"])["candidates"][0])


class Resolve(unittest.TestCase):
    def setUp(self):
        self.dls = downloads()

    def test_by_queue_id_full_id_and_prefix(self):
        d = by_prefix(self.dls, "2222BBBB")
        self.assertEqual(sq.resolve([str(d["queue_ids"][1])], self.dls), [d])
        self.assertEqual(sq.resolve([d["download_id"]], self.dls), [d])
        self.assertEqual(sq.resolve(["2222bbbb"], self.dls), [d])
        self.assertEqual(sq.resolve([str(d["queue_ids"][0]), "2222BBBB"], self.dls), [d])

    def test_ambiguous_prefix(self):
        with self.assertRaises(sq.CliError) as cm:
            sq.resolve(["0000AA"], self.dls + [dict(by_prefix(self.dls, "0000AAAA"),
                                                    download_id="0000AAAA" + "2" * 32)])
        self.assertIn("ambiguous", str(cm.exception))

    def test_unknown_and_short(self):
        for t in ("99999", "ZZZZZZ", "2222"):
            with self.assertRaises(sq.CliError, msg=t):
                sq.resolve([t], self.dls)

    def test_nothing_done_on_bad_id(self):
        rc, out, err, client = run(["deny", "1111AAAA", "nope12", "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("nothing done", err)
        self.assertFalse(client.writes())


class Deny(unittest.TestCase):
    def test_dry_run_default(self):
        rc, out, _, client = run(["deny", "2222BBBB"])
        self.assertEqual(rc, 0)
        self.assertIn("dry run", out)
        self.assertIn("removeFromClient=false&blocklist=false&skipRedownload=false&changeCategory=false", out)
        self.assertFalse(client.writes())

    def test_yes_sends_all_rows_with_flags_off(self):
        rc, out, _, client = run(["deny", "2222BBBB", "--yes"])
        (method, path, body), = client.writes()
        self.assertEqual(method, "DELETE")
        self.assertEqual(path, "/api/v3/queue/bulk?removeFromClient=false&blocklist=false"
                               "&skipRedownload=false&changeCategory=false")
        d = by_prefix(downloads(), "2222BBBB")
        self.assertEqual(body, {"ids": d["queue_ids"]})
        self.assertEqual(len(body["ids"]), 3)

    def test_flags(self):
        _, _, _, client = run(["deny", "8888BBBB", "--yes", "--remove-from-client", "--blocklist"])
        self.assertIn("removeFromClient=true&blocklist=true&skipRedownload=false",
                      client.writes()[0][1])
        _, _, _, client = run(["deny", "8888BBBB", "--yes", "--blocklist", "--skip-redownload"])
        self.assertIn("removeFromClient=false&blocklist=true&skipRedownload=true",
                      client.writes()[0][1])


class Refresh(unittest.TestCase):
    class Commands(FakeClient):
        def __init__(self, statuses):
            super().__init__()
            self.statuses = list(statuses)

        def send(self, requests):
            if requests[0][1].startswith("/api/v3/command/"):
                self.log.append(requests[0])
                return [{"id": 77, "status": self.statuses.pop(0), "message": "Completed"}]
            return super().send(requests)

    def test_dry_run_then_send_and_wait(self):
        rc, out, _, client = run(["refresh", "2222BBBB", "5555EEEE"])
        self.assertEqual(rc, 0)
        self.assertIn('{"name": "RefreshSeries", "seriesIds": [10]}', out)
        self.assertFalse(client.writes())
        client = self.Commands(["started", "completed"])
        with mock.patch.object(sq.time, "sleep") as sleep:
            rc, out, err, _ = run(["refresh", "2222BBBB", "--yes", "--wait"], client)
        self.assertEqual(rc, 0, err)
        self.assertEqual(client.writes(), [("POST", "/api/v3/command",
                                            {"name": "RefreshSeries", "seriesIds": [10]})])
        self.assertEqual(sleep.call_count, 1)
        self.assertIn("command 77 completed", out)

    def test_failed_refresh_is_an_error(self):
        with mock.patch.object(sq.time, "sleep"):
            rc, _, err, _ = run(["refresh", "2222BBBB", "--yes", "--wait"],
                                self.Commands(["failed"]))
        self.assertEqual(rc, 1)
        self.assertIn("ended failed", err)


class Approve(unittest.TestCase):
    def test_payload_shape_and_override(self):
        rc, out, err, client = run(["approve", "5555EEEE", "--yes"])
        self.assertEqual(rc, 0, err)
        (method, path, body), = client.writes()
        self.assertEqual((method, path), ("POST", "/api/v3/command"))
        self.assertEqual(body["name"], "ManualImport")
        self.assertEqual(body["importMode"], "auto")
        f, = body["files"]
        self.assertEqual(f["seriesId"], 10)
        self.assertEqual(f["episodeIds"], [105])
        self.assertEqual(f["downloadId"], "5555EEEE" + "0" * 32)
        self.assertEqual(f["releaseType"], "singleEpisode")
        self.assertEqual(f["quality"]["quality"]["name"], "WEBDL-720p")
        self.assertEqual(set(f), {"path", "folderName", "seriesId", "episodeIds", "quality",
                                  "languages", "releaseGroup", "indexerFlags", "releaseType",
                                  "downloadId"})
        self.assertIn("overriding rejection: Not a Custom Format upgrade", out)
        self.assertIn("command 77", out)

    def test_dry_run_default(self):
        rc, out, _, client = run(["approve", "1111AAAA"])
        self.assertEqual(rc, 0)
        self.assertIn("dry run -- pass --yes to import", out)
        self.assertIn('"episodeIds"', out)
        self.assertFalse(client.writes())

    def test_samples_never_imported_and_unimportable_reported(self):
        rc, out, err, client = run(["approve", "2222BBBB", "--yes"])
        self.assertEqual(rc, 0, err)
        self.assertIn("1 file(s) skipped", out)
        self.assertIn("skip (sample)", out)
        files = client.writes()[0][2]["files"]
        self.assertEqual([f["episodeIds"] for f in files], [[201], [202]])
        self.assertFalse(any("sample" in f["path"] for f in files))

    def test_manual_episode_mapping(self):
        # An unmapped hybrid-numbered file, imported as an explicit episode.
        rc, out, err, client = run(["approve", "FFFF1515", "--episodes", "S02E02", "--yes"])
        self.assertEqual(rc, 0, err)
        f, = client.writes()[0][2]["files"]
        self.assertEqual((f["seriesId"], f["episodeIds"]), (20, [3014]))
        self.assertIn("overriding rejection: Invalid season or episode", out)
        self.assertIn("(Sonarr had: nothing)", out)

    def test_manual_episode_mapping_guards(self):
        for argv, msg in [
                (["approve", "FFFF1515", "--episodes", "S09E99"], "not in"),
                (["approve", "FFFF1515", "--episodes", "E2"], "not SxxEyy"),
                (["approve", "FFFF1515", "4444DDDD", "--episodes", "S01E02"], "exactly one download"),
                (["approve", "2222BBBB", "--episodes", "S01E02"], "exactly one non-sample file"),
                (["approve", "--auto-language", "--episodes", "S01E02"], "not --auto-language"),
                (["approve", "FFFF1515", "--episodes", "S01E02"], "already has a file"),
                (["approve", "3333CCCC", "--map", "S2 - 0=S02E05"], "matches 2 files"),
                (["approve", "3333CCCC", "--map", "S2 - 05=S02E05", "--map", "S2 - 06=S02E05"],
                 "target of two files"),
                (["approve", "3333CCCC", "--map", "S02E05"], "not FILE=SxxEyy"),
                (["approve", "3333CCCC", "--map", "S2 - 05=S02E05", "--episodes", "S02E05"],
                 "not both")]:
            rc, _, err, client = run(argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn(msg, err, argv)
            self.assertFalse(client.writes())

    def test_map_files_of_a_pack(self):
        # Season-relative pack: only the mapped files go, the rest stay in the queue.
        rc, out, err, client = run(["approve", "3333CCCC", "--map", "S2 - 06=S02E06", "--yes"])
        self.assertEqual(rc, 0, err)
        f, = client.writes()[0][2]["files"]
        self.assertTrue(f["path"].endswith("S2 - 06.mkv"))
        self.assertEqual(f["episodeIds"], [3018])
        self.assertIn("not mapped, left in the queue: [Group-B] Anime Example S2 - 05.mkv", out)
        rc, _, err, client = run(["approve", "FFFF1515", "--episodes", "S01E02",
                                  "--allow-overwrite", "--yes"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(client.writes()[0][2]["files"][0]["episodeIds"], [3002])

    def test_nothing_importable(self):
        rc, _, err, client = run(["approve", "8888BBBB", "--yes"])
        self.assertEqual(rc, 1)
        self.assertFalse(client.writes())

    def test_needs_ids_or_auto(self):
        rc, _, err, _ = run(["approve"])
        self.assertEqual(rc, 1)


class AutoLanguage(unittest.TestCase):
    def setUp(self):
        self.plan = sq.plan_auto(downloads(), ["german", "english"])
        self.p = {p["dl"]["short"]: p for p in self.plan["downloads"]}

    def names(self, short):
        return [c["name"] for c in self.p[short]["selected"]]

    def test_partial_download_selects_clean_siblings(self):
        self.assertEqual(self.names("2222BBBB"), ["example.show.s02e01.mkv", "example.show.s02e02.mkv"])
        self.assertEqual(len(self.p["2222BBBB"]["skipped"]), 2)  # invalid episode + sample

    def test_unexpected_considering_skips_whole_download(self):
        self.assertEqual(self.names("3333CCCC"), [])
        self.assertTrue(all("inconsistent" in w for _, w in self.p["3333CCCC"]["skipped"]))

    def test_language_excluded(self):
        self.assertEqual(self.names("4444DDDD"), [])
        self.assertIn("no wanted language", self.p["4444DDDD"]["skipped"][0][1])

    def test_not_upgrade_excluded(self):
        self.assertEqual(self.names("5555EEEE"), [])

    def test_skipped_categories(self):
        for s in ("8888BBBB", "9999CCCC", "AAAA1010"):
            self.assertEqual(self.names(s), [])
        self.assertEqual(self.p["9999CCCC"]["skipped"][0][1], "download is unknown-series")

    def test_clean_title_mismatch_selected(self):
        self.assertEqual(len(self.names("1111AAAA")), 1)

    def test_conflict_selects_neither(self):
        self.assertEqual(self.names("6666FFFF"), [])
        self.assertEqual(self.names("7777AAAA"), [])
        (eid, lst), = self.plan["conflicts"]
        self.assertEqual(eid, 501)
        self.assertEqual(len(lst), 2)

    def test_output_and_dry_run(self):
        rc, out, err, client = run(["approve", "--auto-language"])
        self.assertEqual(rc, 0, err)
        self.assertIn("2 of 4 files", out)
        self.assertIn("conflicts -- pick one with approve <id>", out)
        self.assertIn("S04E01", out)
        self.assertIn("selected 3 file(s) from 2 download(s)", out)
        self.assertIn("dry run", out)
        self.assertFalse(client.writes())

    def test_yes_sends_one_command(self):
        rc, out, _, client = run(["approve", "--auto-language", "--yes"])
        (method, path, body), = client.writes()
        self.assertEqual(len(body["files"]), 3)
        self.assertEqual(sorted(e for f in body["files"] for e in f["episodeIds"]), [101, 201, 202])

    def test_want_languages_option(self):
        rc, out, _, _ = run(["approve", "--auto-language", "--want-languages", "French"])
        self.assertIn("selected 1 file(s)", out)


class ExecTransport(unittest.TestCase):
    def client(self, responder):
        self.calls = []

        def runner(argv, data):
            self.calls.append((argv, data))
            return responder(argv, data)

        return sq.ExecClient(["kubectl", "-n", "arr", "exec"], "/config/config.xml", runner=runner)

    @staticmethod
    def frame(i, status, body, rc=0):
        b64 = base64.b64encode(json.dumps(body).encode()).decode() if body is not None else ""
        return "@@REC %d %d %d\n%s\n" % (i, rc, status, b64)

    def test_script_reads_key_in_pod_and_passes_it_on_stdin(self):
        c = self.client(lambda a, d: (0, (self.frame(0, 200, {"a": 1})
                                          + self.frame(1, 200, [1])).encode(), b""))
        res = c.get_many([("/api/v3/queue", {"page": 1, "title": "Ünï & co=1"}),
                          ("/api/v3/manualimport", {"downloadId": "X"})])
        self.assertEqual(res, [{"a": 1}, [1]])
        argv, data = self.calls[0]
        self.assertIsInstance(data, bytes)
        self.assertNotIn(b"\r", data)
        script = data.decode()
        self.assertIn("<ApiKey>", script)
        self.assertIn("/config/config.xml", script)
        self.assertIn("-H @-", script)
        self.assertIn("printf 'X-Api-Key: %s\\n' \"$k\"", script)
        self.assertNotIn("X-Api-Key:", " ".join(argv))
        self.assertIn("'http://localhost:8989/api/v3/queue?page=1&title=%C3%9Cn%C3%AF%20%26%20co%3D1'",
                      script)
        self.assertEqual(script.count("\ncall "), 2)
        self.assertTrue(script.rstrip().endswith("main </dev/null"))

    def test_body_goes_through_a_quoted_heredoc_file(self):
        c = self.client(lambda a, d: (0, self.frame(0, 200, {"id": 1}).encode(), b""))
        c.post("/api/v3/command", {"name": "it's $HOME"})
        script = self.calls[0][1].decode()
        # Quoted delimiter: no expansion; a file, not argv, so size is not capped per argument.
        self.assertIn("<<'SONARR_QUEUE_BODY'\n{\"name\": \"it's $HOME\"}\nSONARR_QUEUE_BODY\n",
                      script)
        self.assertIn('call 0 POST http://localhost:8989/api/v3/command "@$b"', script)

    def test_non_2xx_names_method_path_status(self):
        c = self.client(lambda a, d: (0, self.frame(0, 404, {"message": "NotFound"}).encode(), b""))
        with self.assertRaises(sq.CliError) as cm:
            c.get("/api/v3/queue", {"page": 1})
        self.assertEqual(str(cm.exception), "GET /api/v3/queue -> HTTP 404: NotFound")

    def test_curl_failure_and_truncated_output(self):
        c = self.client(lambda a, d: (0, self.frame(0, 0, None, rc=7).encode(), b"curl: (7) refused"))
        with self.assertRaises(sq.CliError) as cm:
            c.get("/x")
        self.assertIn("curl exit 7", str(cm.exception))
        c = self.client(lambda a, d: (1, b"", b"error: pod not found"))
        with self.assertRaises(sq.CliError) as cm:
            c.get("/x")
        self.assertIn("pod not found", str(cm.exception))

    def test_empty_body(self):
        c = self.client(lambda a, d: (0, self.frame(0, 200, None).encode(), b""))
        self.assertIsNone(c.delete("/api/v3/queue/bulk", [("blocklist", False)], {"ids": [1]}))

    def test_argv_plain_kubectl(self):
        self.assertEqual(sq.build_argv("kubectl", "arr", "deploy/sonarr", "sonarr"),
                         ["kubectl", "-n", "arr", "exec", "-i", "deploy/sonarr", "-c", "sonarr",
                          "--", "sh", "-s"])

    def test_argv_ssh_is_one_remote_string_and_drops_dash_n(self):
        argv = sq.build_argv("ssh -n -p 2222 jump kubectl --context c", "arr", "deploy/sonarr", "sonarr")
        self.assertEqual(argv[:4], ["ssh", "-p", "2222", "jump"])
        self.assertEqual(len(argv), 5)
        self.assertEqual(argv[4], "kubectl --context c -n arr exec -i deploy/sonarr -c sonarr -- sh -s")
        with self.assertRaises(sq.CliError):
            sq.build_argv("ssh jump", "arr", "t", "c")

    def test_key_never_in_argv(self):
        argv = sq.build_argv("ssh jump kubectl", "arr", "deploy/sonarr", "sonarr")
        self.assertNotIn("ApiKey", " ".join(argv))


class DirectTransport(unittest.TestCase):
    def config(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8")
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_key_parsed_and_sent_as_header_only(self):
        path = self.config("<Config><Port>8989</Port><ApiKey>%s</ApiKey></Config>" % DUMMY_KEY)
        seen = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()

        def opener(req, timeout):
            seen.append(req)
            return Resp(b'{"ok": true}')

        c = sq.DirectClient("http://sonarr:8989/", path, opener=opener)
        self.assertEqual(c.get("/api/v3/queue", {"page": 1}), {"ok": True})
        req = seen[0]
        self.assertEqual(req.get_full_url(), "http://sonarr:8989/api/v3/queue?page=1")
        self.assertEqual(req.get_header("X-api-key"), DUMMY_KEY)
        self.assertNotIn(DUMMY_KEY, repr(c))

    def test_http_error_has_no_headers(self):
        path = self.config("<Config><ApiKey>%s</ApiKey></Config>" % DUMMY_KEY)

        def opener(req, timeout):
            raise urlerror.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))

        c = sq.DirectClient("http://sonarr:8989", path, opener=opener)
        with self.assertRaises(sq.CliError) as cm:
            c.get("/api/v3/queue")
        self.assertEqual(str(cm.exception), "GET /api/v3/queue -> HTTP 401")

    def test_bad_config(self):
        for text in ("<Config><ApiKey> </ApiKey></Config>", "<Config/>", "not xml"):
            with self.assertRaises(sq.CliError) as cm:
                sq.read_api_key(self.config(text))
            self.assertNotIn("not xml", str(cm.exception))
        with self.assertRaises(sq.CliError):
            sq.read_api_key(os.path.join(HERE, "missing.xml"))


class EndToEnd(unittest.TestCase):
    """The key lives only in the config file; nothing a command prints may contain it."""

    def test_no_key_in_any_output(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False)
        f.write("<Config><ApiKey>%s</ApiKey></Config>" % DUMMY_KEY)
        f.close()
        self.addCleanup(os.unlink, f.name)
        fake = FakeClient()
        for argv in (["list"], ["report"], ["approve", "--auto-language", "--yes", "--debug"],
                     ["deny", "2222BBBB", "--debug"], ["list", "--url", "http://x",
                                                       "--config-xml", f.name]):
            if "--url" in argv:
                direct = sq.DirectClient("http://x", f.name)
                direct.send = fake.send
                client = direct
            else:
                client = fake
            rc, out, err, _ = run(argv, client)
            self.assertEqual(rc, 0, err)
            self.assertNotIn(DUMMY_KEY, out + err)
            self.assertNotIn("X-Api-Key", out + err)

    def test_error_exit_is_one_line_on_stderr(self):
        rc, out, err, _ = run(["deny", "nope123"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.count("\n"), 1)

    def test_old_major_warns(self):
        client = FakeClient()
        orig = client.send

        def send(reqs):
            res = orig(reqs)
            return [dict(r, version="3.0.1") if isinstance(r, dict) and "version" in r else r
                    for r in res]

        client.send = send
        rc, _, err, _ = run(["list"], client)
        self.assertIn("written for Sonarr 4", err)

    def test_unicode_titles_print(self):
        self.assertEqual(sq.trunc("Ünï  code\n", 20), "Ünï code")


if __name__ == "__main__":
    unittest.main()
