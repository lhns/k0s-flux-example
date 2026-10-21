#!/usr/bin/env python3
"""Triage Sonarr's "needs user interaction" queue (importBlocked / importPending).

The unit is the download (downloadId), not the queue row: a season pack has one queue row
per episode. Everything is read-only unless --yes is given.

Transport. exec (default): a generated sh program is piped into `kubectl exec -i ... sh -s`;
inside the pod it reads the ApiKey from config.xml and hands it to curl on stdin, so the key
never reaches this machine, argv or any output. direct (--url): in-process urllib, for a
host that can reach Sonarr and read config.xml.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

API = "/api/v3"
IN_POD_URL = "http://localhost:8989"
NEEDS_INTERACTION = ("importBlocked", "importPending")
DEFAULT_LANGS = "German,English"

NUMBERING_MSGS = (
    "invalid season or episode",
    "was unexpected considering",
    "was not found in the grabbed release",
    "unable to identify correct episode",
)

# Fixed priority: a download gets the first category that applies.
CATEGORIES = (
    ("dangerous-file", "deny with --blocklist"),
    ("unknown-series", "add the series, or map the files via manual import"),
    ("year-mismatch", "the release names another year than the series: often a remake or a "
                      "separately listed series; add that series and deny, or approve by id if wrong"),
    ("archive", "extract in the download client, or deny"),
    ("unparseable", "rename the release, or map the files via manual import"),
    ("anime-numbering", "see the numbering breakdown per file"),
    ("episode-not-found", "refresh the series / check TVDB, or map via manual import"),
    ("already-imported", "deny, or approve by id to import over it"),
    ("multi-episode-file", "review the file name, or map the episodes via manual import"),
    ("language", "owner policy (--want-languages), not a Sonarr rejection: deny, or approve by id"),
    ("not-upgrade", "deny; the existing file is at least as good"),
    ("title-mismatch", "files are fine; approve, or add an alternate title / scene mapping "
                       "so future releases import automatically"),
    ("sample", "only sample files; deny"),
    ("other", "read the messages"),
)
AUTO_SKIP_CATEGORIES = ("dangerous-file", "archive", "unknown-series", "year-mismatch")


class CliError(Exception):
    pass


# --------------------------------------------------------------------------- HTTP layer

def build_path(path, params=None):
    if not params:
        return path
    items = params.items() if isinstance(params, dict) else params
    enc = [(k, ("true" if v is True else "false" if v is False else v)) for k, v in items]
    return path + "?" + urlparse.urlencode(enc, quote_via=urlparse.quote)


def _api_error(method, path, status, body):
    msg = ""
    if isinstance(body, dict):
        msg = body.get("message") or body.get("errorMessage") or ""
    return CliError("%s %s -> HTTP %s%s" % (method, path.split("?")[0], status,
                                           (": " + str(msg)[:200]) if msg else ""))


class Client:
    """Subclasses implement send(requests) -> [decoded JSON]; request = (method, path, body)."""

    def __init__(self, debug=False):
        self.debug = debug

    def send(self, requests):
        """-> one decoded body per request, or the CliError for an HTTP error status."""
        raise NotImplementedError

    def _go(self, requests, tolerant=False):
        if self.debug:
            for m, p, _ in requests:
                print("debug: %s %s" % (m, p), file=sys.stderr)
        res = self.send(requests)
        for r in res:
            if isinstance(r, CliError) and not tolerant:
                raise r
        return res

    def get(self, path, params=None):
        return self._go([("GET", build_path(path, params), None)])[0]

    def get_many(self, reqs, tolerant=False):
        """tolerant: an HTTP error comes back as a CliError in its slot instead of raising."""
        reqs = [("GET", build_path(p, q), None) for p, q in reqs]
        return self._go(reqs, tolerant) if reqs else []

    def post(self, path, body):
        return self._go([("POST", path, body)])[0]

    def delete(self, path, params=None, body=None):
        return self._go([("DELETE", build_path(path, params), body)])[0]


def _decode(raw):
    text = raw.decode("utf-8", errors="replace").strip()
    return json.loads(text) if text else None


def read_api_key(config_xml):
    try:
        key = ET.parse(config_xml).getroot().findtext("ApiKey") or ""
    except (OSError, ET.ParseError):
        raise CliError("cannot read %s" % config_xml) from None
    key = key.strip()
    if not key:
        raise CliError("no ApiKey in %s" % config_xml)
    return key


class DirectClient(Client):
    def __init__(self, url, config_xml, debug=False, opener=None):
        super().__init__(debug)
        self.url = url.rstrip("/")
        self._key = read_api_key(config_xml)
        self._open = opener or urlrequest.urlopen

    def __repr__(self):
        return "DirectClient(%s)" % self.url

    def send(self, requests):
        out = []
        for method, path, body in requests:
            data = json.dumps(body).encode() if body is not None else None
            req = urlrequest.Request(self.url + path, data=data, method=method)
            req.add_header("X-Api-Key", self._key)
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with self._open(req, timeout=120) as resp:
                    out.append(_decode(resp.read()))
            except urlerror.HTTPError as e:
                try:
                    detail = _decode(e.read())
                except ValueError:
                    detail = None
                out.append(_api_error(method, path, e.code, detail))
            except (urlerror.URLError, OSError) as e:
                raise CliError("%s %s failed: %s" % (method, path.split("?")[0],
                                                     getattr(e, "reason", e))) from None
        return out


def build_argv(prefix, namespace, target, container):
    """argv running `sh -s` in the pod. ssh joins its trailing args and the remote shell
    re-splits them, so with an ssh prefix the kubectl part travels as one quoted string."""
    kcmd = ["-n", namespace, "exec", "-i", target, "-c", container, "--", "sh", "-s"]
    parts = shlex.split(prefix)
    if not parts:
        raise CliError("empty --kubectl")
    if os.path.basename(parts[0]).lower() not in ("ssh", "ssh.exe"):
        return parts + kcmd
    takes_arg = set("bcDEeFIiJLlmOopQRSWw")
    i, host = 1, None
    while i < len(parts):
        p = parts[i]
        if p.startswith("-") and len(p) > 1:
            i += 2 if (len(p) == 2 and p[1] in takes_arg) else 1
        else:
            host = i
            break
    if host is None or host + 1 >= len(parts):
        raise CliError("--kubectl with ssh needs: ssh [options] host kubectl [args]")
    # ssh -n would redirect stdin from /dev/null and swallow the script.
    ssh = [p for p in parts[:host + 1] if p != "-n"]
    return ssh + [shlex.join(parts[host + 1:] + kcmd)]


def run_subprocess(argv, data):
    try:
        p = subprocess.run(argv, input=data, capture_output=True, timeout=600)
    except FileNotFoundError:
        raise CliError("cannot run %s" % argv[0]) from None
    except subprocess.TimeoutExpired:
        raise CliError("timed out running %s" % argv[0]) from None
    return p.returncode, p.stdout, p.stderr


class ExecClient(Client):
    def __init__(self, argv, config_xml, debug=False, runner=run_subprocess):
        super().__init__(debug)
        self.argv = argv
        self.config_xml = config_xml
        self.runner = runner

    def script(self, requests):
        lines = [
            "main() {",
            "k=$(sed -n 's:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p' %s 2>/dev/null"
            " | head -n 1 | tr -d '\\r')" % shlex.quote(self.config_xml),
            'if [ -z "$k" ]; then echo "sonarr-queue: no ApiKey in config.xml" >&2; exit 3; fi',
            "call() {",
            "  o=$(mktemp) || exit 4",
            # The key goes to curl on stdin (-H @-), never into argv.
            '  if [ -n "$4" ]; then',
            "    c=$(printf 'X-Api-Key: %s\\n' \"$k\" | curl -sS -m 120 -o \"$o\" -w '%{http_code}'"
            " -X \"$2\" -H @- -H 'Content-Type: application/json' --data-binary \"$4\" \"$3\")",
            "  else",
            "    c=$(printf 'X-Api-Key: %s\\n' \"$k\" | curl -sS -m 120 -o \"$o\" -w '%{http_code}'"
            " -X \"$2\" -H @- \"$3\")",
            "  fi",
            "  r=$?",
            "  printf '@@REC %s %s %s\\n' \"$1\" \"$r\" \"$c\"",
            "  base64 \"$o\" | tr -d '\\n'; echo",
            '  rm -f "$o"',
            "}",
        ]
        for i, (method, path, body) in enumerate(requests):
            url = shlex.quote(IN_POD_URL + path)
            if body is None:
                lines.append("call %d %s %s ''" % (i, method, url))
                continue
            # Via a file, not argv: a large ManualImport body would exceed the per-argument
            # limit. json.dumps output is one ASCII line, so it cannot end the heredoc early.
            lines += ["b=$(mktemp) || exit 4", "cat > \"$b\" <<'SONARR_QUEUE_BODY'",
                      json.dumps(body), "SONARR_QUEUE_BODY",
                      "call %d %s %s \"@$b\"" % (i, method, url), 'rm -f "$b"']
        # Everything is parsed before `main` runs, so nothing in it can eat the script.
        lines += ["}", "main </dev/null", ""]
        return "\n".join(lines)

    def send(self, requests):
        rc, out, err = self.runner(self.argv, self.script(requests).encode())
        return self.parse(requests, rc, out, err)

    @staticmethod
    def parse(requests, rc, out, err):
        lines = out.decode("utf-8", errors="replace").split("\n")
        recs = {}
        for n, line in enumerate(lines):
            m = re.match(r"@@REC (\d+) (\d+) (\d+)$", line.strip())
            if m and n + 1 < len(lines):
                recs[int(m.group(1))] = (int(m.group(2)), int(m.group(3)), lines[n + 1].strip())
        detail = err.decode("utf-8", errors="replace").strip()[:300]
        result = []
        for i, (method, path, _) in enumerate(requests):
            if i not in recs:
                raise CliError("exec failed (exit %s)%s" % (rc, ": " + detail if detail else ""))
            curl_rc, status, b64 = recs[i]
            if curl_rc != 0:
                raise CliError("%s %s: curl exit %d%s" % (method, path.split("?")[0], curl_rc,
                                                          ": " + detail if detail else ""))
            try:
                body = _decode(base64.b64decode(b64))
            except ValueError:
                body = None
            result.append(body if 200 <= status < 300 else _api_error(method, path, status, body))
        return result


# --------------------------------------------------------------------------- model

def fmt_ep(season, ep):
    return "S%02dE%02d" % (season or 0, ep or 0)


def dedup(items):
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def trunc(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "~"


def norm_candidate(c):
    series = c.get("series") or None
    rej = [(r.get("reason") or "").strip() for r in c.get("rejections") or []]
    eps = [{"id": e.get("id"), "season": e.get("seasonNumber"), "episode": e.get("episodeNumber"),
            "abs": e.get("absoluteEpisodeNumber")} for e in c.get("episodes") or []]
    name = c.get("relativePath") or c.get("name") or os.path.basename(c.get("path") or "")
    return {
        "raw": c,
        "name": name,
        "folder": c.get("folderName"),
        "size": c.get("size"),
        "series_id": series.get("id") if series else None,
        "series_title": series.get("title") if series else None,
        "episodes": eps,
        "quality": ((c.get("quality") or {}).get("quality") or {}).get("name"),
        "languages": [x.get("name") for x in c.get("languages") or [] if x.get("name")],
        "release_group": c.get("releaseGroup"),
        "rejections": rej,
        "sample": any(r.lower() == "sample" for r in rej),
        "importable": bool(series and series.get("id") and eps),
    }


def row_messages(row):
    msgs = []
    for sm in row.get("statusMessages") or []:
        msgs += sm.get("messages") or []
    if row.get("errorMessage"):
        msgs.append(row["errorMessage"])
    return msgs


def parse_langs(text):
    return [x.strip().lower() for x in text.split(",") if x.strip()]


def contains(texts, *needles):
    low = [t.lower() for t in texts]
    return any(n in t for t in low for n in needles)


# A year right before SxxEyy is part of the show name ("Show.2025.S02E03"), unlike a season
# pack's "S26.Complete.2006".
TITLE_YEAR = re.compile(r"[.\s_(\[]((?:19|20)\d\d)[.\s_)\]]+S\d{1,2}E\d", re.I)


def year_mismatch(dl):
    m = TITLE_YEAR.search(dl["title"] or "")
    year = dl.get("series_year")
    return bool(m and year and int(m.group(1)) != year)


def classify(dl, want):
    """Return (primary, [secondary...]). Samples are ignored while a main file exists."""
    cands = [c for c in dl["candidates"] if not c["sample"]]
    crej = [r for c in cands for r in c["rejections"]]
    msgs = dl["messages"] + crej
    anime = dl["series_type"] == "anime"
    preds = {
        "dangerous-file": contains(dl["messages"] + [r for c in dl["candidates"]
                                                     for r in c["rejections"]],
                                   "potentially dangerous file"),
        "unknown-series": not dl["series_id"] or bool(cands and all(
            c["series_id"] is None for c in cands)),
        "year-mismatch": year_mismatch(dl),
        "archive": contains(msgs, "found archive file", "might need to be extracted"),
        "unparseable": contains(msgs, "unable to parse"),
        "anime-numbering": anime and contains(msgs, *NUMBERING_MSGS),
        "episode-not-found": not anime and contains(msgs, *NUMBERING_MSGS),
        "already-imported": contains(msgs, "already imported"),
        "multi-episode-file": contains(msgs, "single episode file contains all episodes"),
        "language": bool(cands) and (contains(msgs, "language") or any(
            not any(x.lower() in want for x in c["languages"]) for c in cands)),
        "not-upgrade": contains(msgs, "upgrade"),
        "title-mismatch": bool(cands) and contains(
            dl["messages"], "matched to series by id") and not any(c["rejections"] for c in cands),
        "sample": bool(dl["candidates"]) and not cands,
    }
    hit = [name for name, _ in CATEGORIES if preds.get(name)]
    return (hit[0], hit[1:]) if hit else ("other", [])


def build_downloads(records, manual, want):
    """records: all queue rows; manual: {downloadId: [candidate]}; want: lowercase names."""
    groups = {}
    for r in records:
        groups.setdefault(r.get("downloadId") or "row-%s" % r.get("id"), []).append(r)
    out = []
    for key, rows in groups.items():
        if not any(r.get("trackedDownloadState") in NEEDS_INTERACTION for r in rows):
            continue
        first = rows[0]
        sr = next((r.get("series") for r in rows if (r.get("series") or {}).get("id")), None) or {}
        found = manual.get(key) or []
        # A download imported or removed while we read the queue answers with an error.
        failed = ["manualimport failed: %s" % found] if isinstance(found, CliError) else []
        cands = [] if failed else [norm_candidate(c) for c in found]
        eps = [{"id": r["episode"].get("id"), "season": r["episode"].get("seasonNumber"),
                "episode": r["episode"].get("episodeNumber"),
                "abs": r["episode"].get("absoluteEpisodeNumber")}
               for r in rows if r.get("episode")]
        if not eps:
            eps = [e for c in cands for e in c["episodes"]]
        dl = {
            "download_id": key,
            "short": key[:8],
            "queue_ids": sorted(r["id"] for r in rows),
            "rows": len(rows),
            "title": first.get("title"),
            "series_id": sr.get("id") or 0,
            "series_title": sr.get("title"),
            "series_type": sr.get("seriesType"),
            "series_year": sr.get("year"),
            "use_scene": bool(sr.get("useSceneNumbering")),
            "quality": ((first.get("quality") or {}).get("quality") or {}).get("name"),
            "release_languages": dedup(x.get("name") for r in rows
                                       for x in r.get("languages") or []),
            "file_languages": dedup(x for c in cands if not c["sample"] for x in c["languages"]),
            "state": "/".join(dedup(r.get("trackedDownloadState") for r in rows)),
            "status": first.get("trackedDownloadStatus"),
            "indexer": first.get("indexer"),
            "download_client": first.get("downloadClient"),
            "episodes": eps,
            "messages": dedup([m for r in rows for m in row_messages(r)] + failed),
            "candidates": cands,
        }
        dl["category"], dl["secondary"] = classify(dl, want)
        out.append(dl)
    return out


def fetch_downloads(client, want):
    qp = {"page": 1, "pageSize": 500, "includeUnknownSeriesItems": True,
          "includeSeries": True, "includeEpisode": True}
    status, page = client.get_many([(API + "/system/status", None), (API + "/queue", qp)])
    ver = (status or {}).get("version", "")
    if client.debug:
        print("debug: sonarr version %s" % ver, file=sys.stderr)
    if ver and not ver.startswith("4."):
        print("warning: written for Sonarr 4, this is %s" % ver, file=sys.stderr)
    records = list(page.get("records") or [])
    total, n = page.get("totalRecords", 0), 1
    while len(records) < total and n < 1000:
        n += 1
        more = client.get(API + "/queue", dict(qp, page=n))
        if not more.get("records"):
            break
        records += more["records"]
    ids = dedup(r.get("downloadId") for r in records
                if r.get("trackedDownloadState") in NEEDS_INTERACTION)
    res = client.get_many([(API + "/manualimport",
                            {"downloadId": i, "filterExistingFiles": False}) for i in ids],
                          tolerant=True)
    return build_downloads(records, dict(zip(ids, res)), parse_langs(want))


def resolve(tokens, downloads):
    picked, errors = [], []
    for t in tokens:
        hits = []
        if t.isdigit():
            hits = [d for d in downloads if int(t) in d["queue_ids"]]
        if not hits:
            low = t.lower()
            hits = [d for d in downloads if d["download_id"].lower() == low]
            if not hits and len(t) >= 6:
                hits = [d for d in downloads if d["download_id"].lower().startswith(low)]
            elif not hits and not t.isdigit():
                errors.append("%r: too short for a downloadId prefix (need 6+ chars)" % t)
                continue
        if not hits:
            errors.append("%r: no such queue id or download" % t)
        elif len(hits) > 1:
            errors.append("%r: ambiguous, matches %s" % (t, ", ".join(d["short"] for d in hits)))
        elif hits[0] not in picked:
            picked.append(hits[0])
    if errors:
        raise CliError("; ".join(errors) + " (nothing done)")
    return picked


# --------------------------------------------------------------------------- formatting

def fmt_eps(eps, anime=False):
    eps = [e for e in eps if e.get("season") is not None]
    if not eps:
        return "?"
    nums = sorted(set((e["season"], e["episode"]) for e in eps))
    if len({s for s, _ in nums}) == 1:
        lo, hi = nums[0][1], nums[-1][1]
        s = fmt_ep(nums[0][0], lo)
        if len(nums) > 1:
            s += ("-E%02d" % hi) if len(nums) == hi - lo + 1 else "..E%02d (%d)" % (hi, len(nums))
    else:
        s = "%s..%s (%d)" % (fmt_ep(*nums[0]), fmt_ep(*nums[-1]), len(nums))
    ab = sorted(e["abs"] for e in eps if e.get("abs"))
    if anime and ab:
        s += " (abs %s)" % (str(ab[0]) if ab[0] == ab[-1] else "%d-%d" % (ab[0], ab[-1]))
    return s


def fmt_ids(ids):
    return str(ids[0]) + (" +%d more" % (len(ids) - 1) if len(ids) > 1 else "")


def langs(names):
    return ", ".join(names) if names else "-"


def all_reasons(dl):
    main = [c for c in dl["candidates"] if not c["sample"]] or dl["candidates"]
    return dedup(dl["messages"] + [r for c in main for r in c["rejections"]])


def print_download(dl, out):
    anime = dl["series_type"] == "anime"
    out("%s  %-28s  %-22s  [%s]" % (dl["short"], trunc(dl["series_title"] or "unknown", 28),
                                    trunc(fmt_eps(dl["episodes"], anime), 22), dl["category"]))
    out("    %s" % trunc(dl["title"], 110))
    out("    %s | release langs: %s | file langs: %s | %s | %d row(s), queue ids %s" % (
        dl["quality"] or "?", langs(dl["release_languages"]), langs(dl["file_languages"]),
        dl["state"], dl["rows"], fmt_ids(dl["queue_ids"])))
    if dl["secondary"]:
        out("    also: %s" % ", ".join(dl["secondary"]))
    for m in all_reasons(dl):
        out("      ! %s" % trunc(m, 170))


def cand_json(c):
    return {k: c[k] for k in ("name", "folder", "size", "series_id", "series_title", "episodes",
                              "quality", "languages", "release_group", "rejections", "sample",
                              "importable")}


def dl_json(dl):
    d = {k: v for k, v in dl.items() if k != "candidates"}
    d["candidates"] = [cand_json(c) for c in dl["candidates"]]
    return d


# --------------------------------------------------------------------------- numbering

RESOLUTIONS = {"480", "576", "720", "1080", "2160"}

SCHEME_LABEL = {
    "season": "SxxEyy (season)",
    "hybrid": "SxxEyy with an absolute-sized number (season+absolute hybrid)",
    "season-relative": "season marker + bare number (season-relative)",
    "absolute": "bare number (absolute)",
    "unknown": "no recognisable numbering",
}


def detect_scheme(name, season_max=None):
    """Guess which numbering a file name uses -> (scheme, season, number)."""
    base = os.path.basename(name)
    m = re.search(r"\bS(\d{1,2})E(\d{1,4})\b", base, re.I)
    if m:
        s, n = int(m.group(1)), int(m.group(2))
        too_big = n > season_max if season_max else n >= 100
        return ("hybrid" if too_big else "season"), s, n
    m = re.search(r"\bS(\d{1,2})\s*-\s*(\d{1,3})(?!\d|\.\d)", base, re.I)
    if m:
        return "season-relative", int(m.group(1)), int(m.group(2))
    m = re.search(r"\s-\s(\d{1,4})(?:v\d)?(?=[\s.\[(_-]|$)", base)
    if m:
        return "absolute", None, int(m.group(1))
    for m in re.finditer(r"[.](\d{2,3})[.]", base):
        if m.group(1) not in RESOLUTIONS:
            return "absolute", None, int(m.group(1))
    return "unknown", None, None


def suggest(dl, scheme, mapped, rejected, scene_hint):
    if scheme in ("hybrid", "season-relative"):
        return "manual import with an explicit mapping (or rename)" + (
            "; the mapping above is wrong even without a rejection" if mapped and not rejected
            else "")
    if mapped and not rejected:
        return "fine as mapped"
    if scheme == "absolute":
        if dl["series_type"] != "anime":
            return "set series type to anime"
        return "manual import with an explicit mapping (or rename); Sonarr misread the number"
    if scene_hint and not dl["use_scene"]:
        return "the series has scene numbers but useSceneNumbering is off; try enabling it"
    if not mapped:
        return "episode not in Sonarr's list: refresh the series / check TVDB"
    return "manual import with an explicit mapping (or rename)"


def numbering_details(client, downloads, parse):
    """Per-file numbering breakdown for anime-numbering / episode-not-found downloads.
    Read-only; with `parse`, anime series also get one episode listing and one parse
    per distinct file name, all in one batch."""
    sel = [d for d in downloads if d["category"] in ("anime-numbering", "episode-not-found")]
    eps_by_series, parsed = {}, {}
    anime_sel = [d for d in sel if d["category"] == "anime-numbering"]
    if parse and anime_sel:
        sids = dedup(d["series_id"] for d in anime_sel if d["series_id"])
        names = dedup(os.path.basename(c["name"]) for d in anime_sel for c in d["candidates"]
                      if not c["sample"])
        res = client.get_many([(API + "/episode", {"seriesId": s}) for s in sids]
                              + [(API + "/parse", {"title": n}) for n in names])
        eps_by_series = dict(zip(sids, res[:len(sids)]))
        parsed = dict(zip(names, res[len(sids):]))
    out = {}
    for d in sel:
        eps = eps_by_series.get(d["series_id"]) or []
        by_abs = {e["absoluteEpisodeNumber"]: e for e in eps if e.get("absoluteEpisodeNumber")}
        by_se = {(e["seasonNumber"], e["episodeNumber"]): e for e in eps}
        scene = any(e.get("sceneEpisodeNumber") or e.get("sceneAbsoluteEpisodeNumber")
                    for e in eps)
        files = []
        for c in d["candidates"]:
            if c["sample"]:
                continue
            base = os.path.basename(c["name"])
            smax = None
            m = re.search(r"\bS(\d{1,2})E", base, re.I)
            if m and eps:
                smax = max([e["episodeNumber"] for e in eps
                            if e["seasonNumber"] == int(m.group(1))] or [0]) or None
            scheme, s, n = detect_scheme(c["name"], smax)
            lookup = None
            if eps and n is not None:
                absolute = scheme in ("absolute", "hybrid")
                e = by_abs.get(n) if absolute else by_se.get((s, n))
                key = ("abs %d" % n) if absolute else fmt_ep(s, n)
                target = fmt_ep(e["seasonNumber"], e["episodeNumber"]) if e else None
                lookup = ("%s%s%s" % (key, "" if target == key else " = " + target,
                                      ", has file" if e.get("hasFile") else ", no file")
                          if e else "%s: not in Sonarr's episode list" % key)
            pi = (parsed.get(base) or {}).get("parsedEpisodeInfo") or {}
            files.append({
                "name": c["name"], "scheme": scheme, "season": s, "number": n,
                "mapped": fmt_eps(c["episodes"], True) if c["episodes"] else "nothing",
                "parse": ({"season": pi.get("seasonNumber"), "episodes": pi.get("episodeNumbers"),
                           "absolute": pi.get("absoluteEpisodeNumbers"),
                           "isAbsoluteNumbering": pi.get("isAbsoluteNumbering")} if pi else None),
                "lookup": lookup,
                "fix": suggest(d, scheme, bool(c["episodes"]), bool(c["rejections"]), scene),
            })
        out[d["download_id"]] = files
    return out


# --------------------------------------------------------------------------- commands

def cmd_list(args, client, out):
    dls = fetch_downloads(client, args.want_languages)
    if args.json:
        out(json.dumps([dl_json(d) for d in dls], indent=2, ensure_ascii=False))
        return
    if not dls:
        out("nothing needs interaction")
        return
    for d in sorted(dls, key=lambda d: (d["series_title"] or "", d["short"])):
        print_download(d, out)
    out("\n%d download(s), %d queue row(s)" % (len(dls), sum(d["rows"] for d in dls)))


def collapse(msgs):
    """Messages that differ only in numbers ("Episode 1x04 ...", "Episode 1x05 ...") -> the
    first one plus a count."""
    order, count = [], {}
    for m in msgs:
        k = re.sub(r"\d+", "#", m)
        if k not in count:
            order.append((k, m))
            count[k] = 0
        count[k] += 1
    return [m + (" (and %d more like it)" % (count[k] - 1) if count[k] > 1 else "")
            for k, m in order]


def report_groups(dls):
    groups = []
    for cat, hint in CATEGORIES:
        sel = [d for d in dls if d["category"] == cat]
        if not sel:
            continue
        by_series = {}
        for d in sel:
            by_series.setdefault(d["series_title"] or "unknown", []).append(d)
        series = [{
            "series": title,
            "downloads": len(ds),
            "files": sum(len(d["candidates"]) for d in ds),
            "reasons": collapse(dedup(r for d in ds for r in all_reasons(d))),
            "examples": [d["title"] for d in ds[:2]],
            "ids": [d["short"] for d in ds],
            "secondary": dedup(s for d in ds for s in d["secondary"]),
        } for title, ds in sorted(by_series.items())]
        groups.append({"category": cat, "hint": hint, "downloads": len(sel),
                       "files": sum(s["files"] for s in series), "series": series})
    return groups


def cmd_report(args, client, out):
    dls = fetch_downloads(client, args.want_languages)
    groups = report_groups(dls)
    details = numbering_details(client, dls, args.parse)
    by_short = {d["short"]: d for d in dls}
    if args.json:
        for g in groups:
            if g["category"] in ("anime-numbering", "episode-not-found"):
                g["numbering"] = {d["short"]: details[d["download_id"]] for d in dls
                                  if d["category"] == g["category"]}
        out(json.dumps(groups, indent=2, ensure_ascii=False))
        return
    if not groups:
        out("nothing needs interaction")
        return
    for g in groups:
        out("== %s: %d download(s), %d file(s)" % (g["category"], g["downloads"], g["files"]))
        out("   hint: %s" % g["hint"])
        for s in g["series"]:
            out("  %s -- %d download(s), %d file(s), ids %s" % (
                s["series"], s["downloads"], s["files"], ", ".join(s["ids"])))
            if s["secondary"]:
                out("    also: %s" % ", ".join(s["secondary"]))
            for r in s["reasons"]:
                out("    ! %s" % trunc(r, 170))
            if g["category"] not in ("title-mismatch", "year-mismatch"):
                for e in s["examples"]:
                    out("    e.g. %s" % trunc(e, 120))
            if g["category"] in ("title-mismatch", "year-mismatch"):
                d = by_short[s["ids"][0]]
                year = d["series_year"]
                out("    series title:  %s%s" % (d["series_title"], " (%s)" % year
                                                 if year and str(year) not in d["series_title"]
                                                 else ""))
                for t in dedup(by_short[x]["title"] for x in s["ids"])[:3]:
                    out("    release title: %s" % trunc(t, 100))
            for short in s["ids"]:
                d = by_short[short]
                files = details.get(d["download_id"], [])
                if files:
                    out("    [%s] series type %s, scene numbering %s" % (
                        d["short"], d["series_type"], "on" if d["use_scene"] else "off"))
                for f in files:
                    out("      file:   %s" % trunc(f["name"], 110))
                    out("      names:  %s; sonarr mapped: %s" % (SCHEME_LABEL[f["scheme"]],
                                                                f["mapped"]))
                    if f["parse"]:
                        p = f["parse"]
                        out("      parse:  season %s, episodes %s, absolute %s, isAbsolute %s" % (
                            p["season"], p["episodes"], p["absolute"], p["isAbsoluteNumbering"]))
                    if f["lookup"]:
                        out("      lookup: %s" % f["lookup"])
                    out("      fix:    %s" % f["fix"])
    out("\n%d download(s) in %d categor%s" % (len(dls), len(groups),
                                            "y" if len(groups) == 1 else "ies"))


def import_entry(dl, c):
    r = c["raw"]
    return {
        "path": r.get("path"), "folderName": r.get("folderName"), "seriesId": c["series_id"],
        "episodeIds": [e["id"] for e in c["episodes"]], "quality": r.get("quality"),
        "languages": r.get("languages"), "releaseGroup": r.get("releaseGroup"),
        "indexerFlags": r.get("indexerFlags"), "releaseType": r.get("releaseType"),
        "downloadId": r.get("downloadId") or dl["download_id"],
    }


def cand_line(c):
    return "%s -> %s [%s; %s]" % (trunc(c["name"], 80), fmt_eps(c["episodes"], True),
                                  c["quality"] or "?", langs(c["languages"]))


def send_import(args, client, out, entries):
    body = {"name": "ManualImport", "importMode": "auto", "files": entries}
    if not args.yes:
        out("\nrequest: POST %s/command" % API)
        out(json.dumps(body, indent=2, ensure_ascii=False) if len(entries) <= 3
            else "  ManualImport, importMode auto, %d files" % len(entries))
        out("dry run -- pass --yes to import")
        return
    res = client.post(API + "/command", body) or {}
    out("\nimport queued: command %s, status %s" % (res.get("id"), res.get("status")))


def cmd_approve(args, client, out):
    if bool(args.ids) == bool(args.auto_language):
        raise CliError("give download ids, or --auto-language (not both)")
    if (args.episodes or args.map) and args.auto_language:
        raise CliError("--episodes/--map work with one download id, not --auto-language")
    dls = fetch_downloads(client, args.want_languages)
    if args.auto_language:
        return approve_auto(args, client, out, dls)
    return approve_ids(args, client, out, dls)


def parse_spec(spec, opt):
    want = []
    for tok in spec.split(","):
        m = re.fullmatch(r"\s*S(\d{1,2})E(\d{1,4})\s*", tok, re.I)
        if not m:
            raise CliError("%s: %r is not SxxEyy" % (opt, tok))
        want.append((int(m.group(1)), int(m.group(2))))
    return want


def pick_file(files, pattern):
    """The one candidate whose name is `pattern`, or else the only one containing it."""
    exact = [c for c in files if os.path.basename(c["name"]) == pattern]
    hits = exact or [c for c in files if pattern in os.path.basename(c["name"])]
    if len(hits) != 1:
        raise CliError("--map: %r matches %d files" % (pattern, len(hits)))
    return hits[0]


def manual_plan(client, dl, files, args):
    """[(candidate, [episode resource])] for --episodes / --map. Refuses targets that are
    missing, claimed twice, or (without --allow-overwrite) already have a file."""
    if not dl["series_id"]:
        raise CliError("manual mapping needs a download with a known series")
    if args.episodes:
        if len(files) != 1:
            raise CliError("--episodes needs a download with exactly one non-sample file; "
                           "use --map FILE=SxxEyy")
        plan = [(files[0], parse_spec(args.episodes, "--episodes"))]
    else:
        plan = []
        for m in args.map:
            pattern, sep, spec = m.rpartition("=")
            if not sep or not pattern:
                raise CliError("--map: %r is not FILE=SxxEyy[,SxxEyy]" % m)
            plan.append((pick_file(files, pattern), parse_spec(spec, "--map")))
        if len({id(c) for c, _ in plan}) != len(plan):
            raise CliError("--map: a file is mapped twice")
    eps = client.get(API + "/episode", {"seriesId": dl["series_id"]}) or []
    by_se = {(e["seasonNumber"], e["episodeNumber"]): e for e in eps}
    out, used, errors = [], set(), []
    for c, want in plan:
        targets = []
        for w in want:
            e = by_se.get(w)
            if not e:
                errors.append("%s not in %s's episode list" % (fmt_ep(*w), dl["series_title"]))
            elif w in used:
                errors.append("%s is the target of two files" % fmt_ep(*w))
            elif e.get("hasFile") and not args.allow_overwrite:
                errors.append("%s already has a file (--allow-overwrite to replace it)"
                              % fmt_ep(*w))
            else:
                used.add(w)
                targets.append(e)
        out.append((c, targets))
    if errors:
        raise CliError("; ".join(errors) + " (nothing done)")
    return out


def describe(e):
    return "%s%s %r%s" % (fmt_ep(e["seasonNumber"], e["episodeNumber"]),
                          " (abs %s)" % e["absoluteEpisodeNumber"]
                          if e.get("absoluteEpisodeNumber") else "",
                          e.get("title") or "", ", has file" if e.get("hasFile") else "")


def approve_ids(args, client, out, dls):
    picked = resolve(args.ids, dls)
    manual = bool(args.episodes or args.map)
    if args.episodes and args.map:
        raise CliError("--episodes or --map, not both")
    if manual and len(picked) != 1:
        raise CliError("--episodes/--map map the files of exactly one download")
    entries, skipped = [], 0
    for dl in picked:
        out("%s  %s  [%s]" % (dl["short"], trunc(dl["title"], 90), dl["category"]))
        files = [c for c in dl["candidates"] if not c["sample"]]
        for c in dl["candidates"]:
            if c["sample"]:
                out("    skip (sample): %s" % trunc(c["name"], 90))
        if not files:
            out("    no files to import (manualimport returned none, or only samples)")
            continue
        if manual:
            plan = manual_plan(client, dl, files, args)
            mapped = {id(c) for c, _ in plan}
            for c in files:
                if id(c) not in mapped:
                    out("    not mapped, left in the queue: %s" % trunc(c["name"], 90))
            for c, eps in plan:
                out("    map: %s" % trunc(c["name"], 90))
                out("      -> %s  (Sonarr had: %s)" % ("; ".join(describe(e) for e in eps),
                                                       fmt_eps(c["episodes"], True)
                                                       if c["episodes"] else "nothing"))
                for r in c["rejections"]:
                    out("      overriding rejection: %s" % trunc(r, 150))
                entries.append(import_entry(dl, dict(c, series_id=dl["series_id"],
                                                     episodes=[{"id": e["id"]} for e in eps])))
            continue
        for c in files:
            if not c["series_id"] or not c["episodes"]:
                skipped += 1
                out("    skip (no series or episodes, map it with --map): %s"
                    % trunc(c["name"], 80))
                continue
            entries.append(import_entry(dl, c))
            out("    import: %s" % cand_line(c))
            for r in c["rejections"]:
                out("      overriding rejection: %s" % trunc(r, 150))
    if not entries:
        raise CliError("nothing to import")
    if skipped:
        out("\n%d file(s) skipped: Sonarr has no episode for them" % skipped)
    send_import(args, client, out, entries)


def plan_auto(dls, want):
    """Select clean, wanted-language files; drop episodes claimed by several downloads."""
    plan = {"downloads": [], "conflicts": []}
    claims = {}
    for dl in dls:
        skipped, sel = [], []
        why = [c for c in [dl["category"]] + dl["secondary"] if c in AUTO_SKIP_CATEGORIES]
        inconsistent = contains(
            dl["messages"] + [r for c in dl["candidates"] for r in c["rejections"]],
            "was unexpected considering", "was not found in the grabbed release")
        for c in dl["candidates"]:
            if c["sample"]:
                skipped.append((c, "sample"))
            elif why:
                skipped.append((c, "download is " + why[0]))
            elif inconsistent:
                skipped.append((c, "download has an inconsistent file->episode mapping"))
            elif c["rejections"]:
                skipped.append((c, "rejected: " + "; ".join(trunc(r, 80) for r in c["rejections"])))
            elif not c["importable"]:
                skipped.append((c, "no series or episodes"))
            elif not any(x.lower() in want for x in c["languages"]):
                skipped.append((c, "no wanted language (file: %s)" % langs(c["languages"])))
            else:
                sel.append(c)
        for c in sel:
            for e in c["episodes"]:
                claims.setdefault(e["id"], []).append((dl, c))
        plan["downloads"].append({"dl": dl, "total": len(dl["candidates"]),
                                  "selected": sel, "skipped": skipped})
    for eid, lst in sorted(claims.items()):
        if len({d["download_id"] for d, _ in lst}) < 2:
            continue
        plan["conflicts"].append((eid, lst))
        for dl, c in lst:
            for p in plan["downloads"]:
                if p["dl"] is dl and c in p["selected"]:
                    p["selected"].remove(c)
                    p["skipped"].append((c, "episode conflict"))
    return plan


def approve_auto(args, client, out, dls):
    plan = plan_auto(dls, parse_langs(args.want_languages))
    entries = []
    for p in sorted(plan["downloads"], key=lambda p: (p["dl"]["series_title"] or "",
                                                      p["dl"]["title"] or "")):
        dl = p["dl"]
        if not p["total"]:
            continue
        out("%s  %s  [%s]  %d of %d files" % (dl["short"], trunc(dl["series_title"] or "unknown", 30),
                                              dl["category"], len(p["selected"]), p["total"]))
        for c in p["selected"]:
            entries.append(import_entry(dl, c))
            out("    import: %s" % cand_line(c))
        for c, why in p["skipped"]:
            out("    skip:   %s -- %s" % (trunc(c["name"], 70), why))
    if plan["conflicts"]:
        out("\nconflicts -- pick one with approve <id>:")
        for eid, lst in plan["conflicts"]:
            e = next(x for x in lst[0][1]["episodes"] if x["id"] == eid)
            out("  %s" % fmt_ep(e["season"], e["episode"]))
            for dl, c in lst:
                out("    %s  %s [%s; %s]" % (dl["short"], trunc(dl["title"], 80),
                                             c["quality"] or "?", langs(c["languages"])))
    out("\nselected %d file(s) from %d download(s)" % (
        len(entries), sum(1 for p in plan["downloads"] if p["selected"])))
    if entries:
        send_import(args, client, out, entries)


def wait_command(client, cid, out, timeout=600, every=5):
    """Poll a queued Sonarr command until it ends; CliError unless it completed."""
    deadline = time.monotonic() + timeout
    while True:
        res = client.get(API + "/command/%s" % cid) or {}
        status = res.get("status")
        if status in ("completed", "failed", "aborted", "cancelled", "orphaned"):
            out("command %s %s: %s" % (cid, status, res.get("message") or ""))
            if status != "completed":
                raise CliError("command %s ended %s" % (cid, status))
            return res
        if time.monotonic() > deadline:
            raise CliError("command %s still %s after %ds" % (cid, status, timeout))
        time.sleep(every)


def cmd_refresh(args, client, out):
    dls = fetch_downloads(client, args.want_languages)
    picked = resolve(args.ids, dls)
    series = {}
    for d in picked:
        if not d["series_id"]:
            raise CliError("%s has no series to refresh" % d["short"])
        series[d["series_id"]] = d["series_title"]
    body = {"name": "RefreshSeries", "seriesIds": sorted(series)}
    out("refresh: %s" % ", ".join("%s (%s)" % (t, i) for i, t in sorted(series.items())))
    out("request: POST %s/command %s" % (API, json.dumps(body)))
    if not args.yes:
        out("dry run -- pass --yes to refresh")
        return
    res = client.post(API + "/command", body) or {}
    out("refresh queued: command %s, status %s" % (res.get("id"), res.get("status")))
    if args.wait and res.get("id") is not None:
        wait_command(client, res["id"], out)


def cmd_deny(args, client, out):
    dls = fetch_downloads(client, args.want_languages)
    picked = resolve(args.ids, dls)
    ids = sorted(i for d in picked for i in d["queue_ids"])
    # Sonarr defaults removeFromClient to true, so it is always sent explicitly.
    params = [("removeFromClient", args.remove_from_client), ("blocklist", args.blocklist),
              ("skipRedownload", args.skip_redownload), ("changeCategory", False)]
    for d in picked:
        out("%s  %s  [%s]  %d row(s)" % (d["short"], trunc(d["title"], 90), d["category"],
                                        d["rows"]))
    out("\nrequest: DELETE %s" % build_path(API + "/queue/bulk", params))
    out("  body %s" % json.dumps({"ids": ids}))
    if not args.yes:
        out("dry run -- pass --yes to remove %d queue row(s)" % len(ids))
        return
    client.delete(API + "/queue/bulk", params, {"ids": ids})
    out("removed %d queue row(s)" % len(ids))


# --------------------------------------------------------------------------- CLI

def make_parser():
    env = os.environ.get
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("connection (exec mode unless --url is given)")
    g.add_argument("--kubectl", default=env("SONARR_QUEUE_KUBECTL", "kubectl"),
                   help="kubectl command prefix, shlex-split (env SONARR_QUEUE_KUBECTL). Via a "
                        "jumphost: 'ssh HOST kubectl' (ssh -n is dropped: it swallows the script)")
    g.add_argument("--namespace", default="arr")
    g.add_argument("--target", default="deploy/sonarr", help="kubectl exec target")
    g.add_argument("--container", default="sonarr")
    g.add_argument("--url", default=env("SONARR_URL"),
                   help="direct mode: Sonarr base URL (env SONARR_URL); needs read access to "
                        "--config-xml on this machine")
    g.add_argument("--config-xml", default=env("SONARR_CONFIG_XML", "/config/config.xml"),
                   help="config.xml holding the ApiKey (inside the pod in exec mode)")
    common.add_argument("--want-languages", default=DEFAULT_LANGS,
                        help="comma-separated language names a file should have "
                             "(default %(default)s); owner policy, not a Sonarr rejection")
    common.add_argument("--debug", action="store_true", help="print requests (never the key)")

    p = argparse.ArgumentParser(
        prog="sonarr-queue.py", description=__doc__.split("\n\n")[0],
        epilog="Connection options, --want-languages and --debug go after the subcommand. "
               "File languages are Sonarr's own aggregate (release name, folder, file name and "
               "audio tracks); manualimport does not expose the media info separately.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list", parents=[common], help="one row per download needing interaction")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("report", parents=[common],
                       help="group by problem category and series, with reasons")
    s.add_argument("--json", action="store_true")
    s.add_argument("--parse", action=argparse.BooleanOptionalAction, default=True,
                   help="for anime-numbering, also ask Sonarr to parse the file names and fetch "
                        "the series' episodes (read-only; default on)")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("approve", parents=[common], help="import files; dry run unless --yes",
                       description="Import the importable files of the given downloads (queue "
                                   "ids, downloadIds or unique 6+ char prefixes). Samples are "
                                   "never imported; rejected files only when selected by id.")
    s.add_argument("ids", nargs="*")
    s.add_argument("--auto-language", action="store_true",
                   help="select, across all downloads, clean files (no rejection at all) with a "
                        "wanted language; episode conflicts are listed, not imported")
    s.add_argument("--episodes", metavar="SxxEyy[,SxxEyy]",
                   help="with one download id: import its single file as these episodes, "
                        "overriding Sonarr's mapping")
    s.add_argument("--map", action="append", default=[], metavar="FILE=SxxEyy[,SxxEyy]",
                   help="with one download id, repeatable: import FILE (its name, or a part "
                        "unique to it) as these episodes; unmapped files stay in the queue")
    s.add_argument("--allow-overwrite", action="store_true",
                   help="let --episodes/--map target episodes that already have a file")
    s.add_argument("--yes", action="store_true", help="send the request (default: dry run)")
    s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("deny", parents=[common],
                       help="remove downloads from the queue; dry run unless --yes")
    s.add_argument("ids", nargs="+")
    s.add_argument("--yes", action="store_true", help="send the request (default: dry run)")
    s.add_argument("--remove-from-client", action="store_true",
                   help="also delete the download from the client (Sonarr's own default is yes)")
    s.add_argument("--blocklist", action="store_true", help="blocklist the release")
    s.add_argument("--skip-redownload", action="store_true",
                   help="with --blocklist: do not let Sonarr search for a replacement")
    s.set_defaults(fn=cmd_deny)
    s = sub.add_parser("refresh", parents=[common],
                       help="refresh the series of downloads from TVDB; dry run unless --yes",
                       description="Queue Sonarr's RefreshSeries for the series of the given "
                                   "downloads, e.g. after TVDB gained the missing episodes.")
    s.add_argument("ids", nargs="+")
    s.add_argument("--yes", action="store_true", help="send the request (default: dry run)")
    s.add_argument("--wait", action="store_true", help="wait until the refresh has finished")
    s.set_defaults(fn=cmd_refresh)
    return p


def make_client(args):
    if args.url:
        return DirectClient(args.url, args.config_xml, args.debug)
    return ExecClient(build_argv(args.kubectl, args.namespace, args.target, args.container),
                      args.config_xml, args.debug)


def main(argv=None, client=None):
    args = make_parser().parse_args(argv)
    try:
        client = client or make_client(args)
        client.debug = args.debug
        args.fn(args, client, print)
    except CliError as e:
        print("sonarr-queue: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
