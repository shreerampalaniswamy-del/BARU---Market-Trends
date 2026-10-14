"""Checks the watched-channel collector and its brief section against a faked YouTube API.

Covers: lookup by handle, fallback lookup by name (capped), remembering what was found, the publish-date
window, metrics and top items, a channel that is not found, quota exhaustion, and the brief section.

Run: python tests/test_channels.py
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analyze  # noqa: E402
import collect  # noqa: E402
from common import load_config, open_db  # noqa: E402


class FakeResp:
    def __init__(self, payload, status=200, text="{}"):
        self._payload, self.status_code, self.text = payload, status, text

    def json(self):
        return self._payload


def params_has(call, key, value):
    return call[1].get(key) == value


def iso(days_ago):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


CHANNELS = {   # id -> (title, handle, subs)
    "UC_CARTIER": ("Cartier", "@cartier", 900000),
    "UC_GEM": ("Gem Palace", "@gempalace", 12000),
}
UPLOADS = {"UC_CARTIER": [("c1", 2), ("c2", 5), ("c3", 40)],      # c3 is older than the 14-day window
           "UC_GEM": []}
VIEWS = {"c1": 50000, "c2": 150000, "c3": 999999}


class Api:
    def __init__(self, quota_after=None):
        self.calls = []
        self.quota_after = quota_after

    def __call__(self, url, params=None, headers=None, tries=3, **kw):
        path = url.rsplit("/", 1)[1]
        self.calls.append((path, dict(params)))
        if self.quota_after is not None and len(self.calls) > self.quota_after:
            return FakeResp({}, 403, '{"error": {"errors": [{"reason": "quotaExceeded"}]}}')
        if path == "channels":
            if params.get("forHandle") == "@cartier":
                ids = ["UC_CARTIER"]
            elif params.get("forHandle") == "@missing":
                ids = []
            elif "id" in params:
                ids = [params["id"]] if params["id"] in CHANNELS else []
            else:
                ids = []
            items = [{"id": i, "snippet": {"title": CHANNELS[i][0], "customUrl": CHANNELS[i][1]},
                      "statistics": {"subscriberCount": str(CHANNELS[i][2])},
                      "contentDetails": {"relatedPlaylists": {"uploads": "UU" + i}}} for i in ids]
            return FakeResp({"items": items})
        if path == "search":
            assert params["type"] == "channel"
            hit = "UC_GEM" if "Gem" in params["q"] else None
            return FakeResp({"items": [{"id": {"channelId": hit}}] if hit else []})
        if path == "playlistItems":
            cid = params["playlistId"][2:]
            return FakeResp({"items": [{"contentDetails": {"videoId": v, "videoPublishedAt": iso(d)}}
                                       for v, d in UPLOADS[cid]]})
        if path == "videos":
            return FakeResp({"items": [
                {"id": v, "snippet": {"title": f"Video {v}", "channelTitle": "x", "publishedAt": iso(1)},
                 "statistics": {"viewCount": str(VIEWS[v]), "likeCount": "10", "commentCount": "1"}}
                for v in params["id"].split(",")]})
        raise AssertionError(path)


def main():
    cfg = load_config()
    cfg["youtube"]["channels"] = [
        {"name": "Cartier", "group": "Houses (international)", "handle": "@cartier"},
        {"name": "Gem Palace", "group": "Houses (India)"},                      # found by name
        {"name": "Ghost House", "group": "Houses (India)", "handle": "@missing"},  # handle matches nothing
        {"name": "Unknown Atelier", "group": "Houses (India)"},                    # name search is capped out
    ]
    with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "test-key"}):
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "c.db")
            api = Api()
            cfg["youtube"]["max_name_lookups"] = 1                    # Ghost House's name search is not allowed
            with mock.patch("collect.get_json", side_effect=api):
                collect.collect_channels(conn, cfg, "2026-10-14T00:00:00Z")

            m = lambda g, n, k: conn.execute(
                "SELECT value FROM metrics WHERE source='youtube_channel' AND market=? AND term=? AND metric=?",
                (g, n, k)).fetchone()[0]
            assert m("Houses (international)", "Cartier", "videos_posted") == 2          # c3 is outside the window
            assert m("Houses (international)", "Cartier", "views_sum") == 200000
            assert m("Houses (international)", "Cartier", "subscribers") == 900000
            assert m("Houses (India)", "Gem Palace", "videos_posted") == 0               # found, nothing new
            top = conn.execute("SELECT title FROM items WHERE source='youtube_channel' AND term='Cartier' "
                               "ORDER BY views DESC").fetchall()
            assert [t[0] for t in top] == ["Video c2", "Video c1"], top
            err = conn.execute("SELECT message FROM runs WHERE term='Ghost House' AND status='error'").fetchone()[0]
            assert "handle" in err and "not found" in err, err
            cap = conn.execute("SELECT message FROM runs WHERE term='Unknown Atelier' AND status='error'").fetchone()[0]
            assert "limit reached" in cap, cap
            searches = [c for c in api.calls if c[0] == "search"]
            assert len(searches) == 1                                  # Gem Palace only: no search for a bad handle

            # what was found is remembered: a later run (after the 3-day skip window) makes no lookups
            conn.execute("DELETE FROM runs")
            api2 = Api()
            with mock.patch("collect.get_json", side_effect=api2):
                collect.collect_channels(conn, cfg, "2026-10-20T00:00:00Z")
            searches2 = [c for c in api2.calls if c[0] == "search"]
            assert len(searches2) == 1 and "Unknown" in searches2[0][1]["q"], searches2   # only the never-searched one
            assert not [c for c in api2.calls if c[0] == "channels" and params_has(c, "forHandle", "@cartier")]

            # an immediate re-run fetches nothing
            api3 = Api()
            with mock.patch("collect.get_json", side_effect=api3):
                collect.collect_channels(conn, cfg, "2026-10-20T00:01:00Z")
            assert [c for c in api3.calls if c[0] not in ("channels",)] == [], api3.calls[:3]
            # ... and the name that search could not find is remembered, so it is not searched (100 units) again
            assert not [c for c in api3.calls if c[0] == "search"]

            # the brief section
            sec = analyze.channel_section(conn, cfg)
            assert "### Houses (international)" in sec and "### Houses (India)" in sec
            cart = next(l for l in sec.splitlines() if l.startswith("| Cartier |"))
            assert "900,000" in cart and "200,000" in cart and "Video c2" in cart, cart
            assert "| Ghost House | not found |" in sec, sec

            # the whole brief renders with the channel section and the YouTube channel coverage line
            signals = analyze.compute_signals(conn, cfg["analysis"])
            header, body = analyze.render_brief(conn, cfg, signals, dt.date.today())
            md = header + "\n" + body
            assert "## What watched YouTube channels published" in md and "**YouTube channels**" in md

        # quota exhaustion stops cleanly
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "q.db")
            with mock.patch("collect.get_json", side_effect=Api(quota_after=2)):
                collect.collect_channels(conn, cfg, "2026-10-14T00:00:00Z")
            msg = conn.execute("SELECT message FROM runs WHERE status='error' AND message LIKE '%quota%'").fetchone()
            assert msg, "quota stop not logged"

        # no key: skipped, not an error
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "n.db")
            with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": ""}):
                assert collect.collect_channels(conn, cfg, "2026-10-14T00:00:00Z") == 0
            assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='skipped'").fetchone()[0] == 1

    print("Channel checks passed.")


if __name__ == "__main__":
    main()
