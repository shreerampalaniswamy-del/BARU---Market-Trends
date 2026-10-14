"""Checks the watched-channel collector and its brief section against a faked YouTube API.

Covers: lookup by handle, fallback to a name search (capped, and a miss is remembered), what was found being
remembered, the publish-date window, the jewellery-relevance filter (title-only for media/auction groups),
metrics and top items, honest statuses for channels without numbers, quota exhaustion (both 403 and 429 forms)
and the brief section.

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


def iso(days_ago):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


CHANNELS = {   # id -> (title, handle, subs)
    "UC_CARTIER": ("Cartier", "@cartier", 900000),
    "UC_GEM": ("Gem Palace", "@gempalace", 12000),
    "UC_VOGUE": ("Vogue", "@vogue", 16000000),
}
UPLOADS = {
    "UC_CARTIER": [("c1", 2), ("c2", 5), ("c4", 6), ("c3", 40)],      # c3 is outside the 14-day window
    "UC_GEM": [],
    "UC_VOGUE": [("v1", 1), ("v2", 3), ("v3", 4)],
}
VIDEOS = {  # id -> (title, description, views)
    "c1": ("The new High Jewelry film", "", 50000),
    "c2": ("A story in four stones", "Discover the diamond necklace", 150000),   # relevant through the description
    "c4": ("Eyewear campaign", "Sunglasses for the season", 80000),              # not jewellery: dropped
    "c3": ("Old jewel film", "", 999999),
    "v1": ("Beauty routine with a star", "Shop earrings and jewellery on our site", 3000000),   # description only
    "v2": ("Inside a High Jewelry gala in Paris", "", 120000),                                   # title: kept
    "v3": ("Street style from the shows", "", 900000),
}


def params_has(call, key, value):
    return call[1].get(key) == value


class Api:
    def __init__(self, quota_after=None, quota_status=403):
        self.calls = []
        self.quota_after = quota_after
        self.quota_status = quota_status

    def __call__(self, url, params=None, headers=None, tries=3, **kw):
        path = url.rsplit("/", 1)[1]
        self.calls.append((path, dict(params)))
        if self.quota_after is not None and len(self.calls) > self.quota_after:
            if self.quota_status == 403:
                return FakeResp({}, 403, '{"error": {"errors": [{"reason": "quotaExceeded"}]}}')
            return FakeResp({}, 429, '{"error": {"code": 429, "message": "Quota exceeded for quota metric"}}')
        if path == "channels":
            handle = params.get("forHandle")
            if handle:
                ids = [i for i, c in CHANNELS.items() if c[1] == handle]
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
                {"id": v, "snippet": {"title": VIDEOS[v][0], "description": VIDEOS[v][1], "channelTitle": "x",
                                      "publishedAt": iso(1)},
                 "statistics": {"viewCount": str(VIDEOS[v][2]), "likeCount": "10", "commentCount": "1"}}
                for v in params["id"].split(",")]})
        raise AssertionError(path)


def main():
    cfg = load_config()
    cfg["youtube"]["channels"] = [
        {"name": "Cartier", "group": "Houses (international)", "handle": "@cartier"},
        {"name": "Gem Palace", "group": "Houses (India)"},                         # no handle: found by name
        {"name": "Ghost House", "group": "Houses (India)", "handle": "@missing"},  # handle misses, name misses too
        {"name": "Unknown Atelier", "group": "Houses (India)"},                    # name search capped out
        {"name": "Vogue", "group": "Media and publications", "handle": "@vogue"},
    ]
    cfg["youtube"]["max_name_lookups"] = 2
    with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "test-key"}):
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "c.db")
            api = Api()
            with mock.patch("collect.get_json", side_effect=api):
                collect.collect_channels(conn, cfg, "2026-10-14T00:00:00Z")

            m = lambda g, n, k: conn.execute(
                "SELECT value FROM metrics WHERE source='youtube_channel' AND market=? AND term=? AND metric=?",
                (g, n, k)).fetchone()[0]
            hi = "Houses (international)"
            assert m(hi, "Cartier", "videos_scanned") == 3                 # c3 is outside the window
            assert m(hi, "Cartier", "videos_posted") == 2                  # the eyewear video is not jewellery
            assert m(hi, "Cartier", "views_sum") == 200000
            assert m(hi, "Cartier", "subscribers") == 900000
            top = [t[0] for t in conn.execute("SELECT title FROM items WHERE source='youtube_channel' AND "
                                              "term='Cartier' ORDER BY views DESC")]
            assert top == ["A story in four stones", "The new High Jewelry film"], top

            # media: the title must say so; a description mention is not enough
            media = "Media and publications"
            assert m(media, "Vogue", "videos_scanned") == 3 and m(media, "Vogue", "videos_posted") == 1
            assert m(media, "Vogue", "views_sum") == 120000

            assert m("Houses (India)", "Gem Palace", "videos_posted") == 0   # found, nothing new
            # handle misses fall back to a name search; the cap of 2 is used up by Gem Palace and Ghost House
            searches = [c for c in api.calls if c[0] == "search"]
            assert len(searches) == 2, searches
            msg = lambda n: conn.execute("SELECT message FROM runs WHERE term=? AND status='error'", (n,)).fetchone()[0]
            assert "not found by name" in msg("Ghost House")
            assert "limit reached" in msg("Unknown Atelier")

            # what was found is remembered; a miss is remembered too (no repeat 100-unit searches)
            conn.execute("DELETE FROM runs WHERE status != 'error'")
            api2 = Api()
            with mock.patch("collect.get_json", side_effect=api2):
                collect.collect_channels(conn, cfg, "2026-10-20T00:00:00Z")
            searches2 = [c for c in api2.calls if c[0] == "search"]
            assert len(searches2) == 1 and "Unknown" in searches2[0][1]["q"], searches2   # Ghost House not retried
            assert not [c for c in api2.calls if params_has(c, "forHandle", "@cartier")]

            # an immediate re-run fetches nothing new
            api3 = Api()
            with mock.patch("collect.get_json", side_effect=api3):
                collect.collect_channels(conn, cfg, "2026-10-20T00:01:00Z")
            assert [c for c in api3.calls if c[0] in ("playlistItems", "videos", "search")] == [], api3.calls

            # changing the relevance rules re-collects channels that were fresh
            cfg["youtube"]["channel_relevance"]["words"] = cfg["youtube"]["channel_relevance"]["words"] + ["eyewear"]
            api4 = Api()
            with mock.patch("collect.get_json", side_effect=api4):
                collect.collect_channels(conn, cfg, "2026-10-20T00:02:00Z")
            assert any(c[0] == "playlistItems" for c in api4.calls)
            assert m(hi, "Cartier", "videos_posted") == 3                  # eyewear now counts

            # the brief section
            sec = analyze.channel_section(conn, cfg)
            assert "### Houses (international)" in sec and "### Media and publications" in sec
            cart = next(l for l in sec.splitlines() if l.startswith("| Cartier |"))
            assert "3 of 3" in cart and "900,000" in cart, cart
            vogue = next(l for l in sec.splitlines() if l.startswith("| Vogue |"))
            assert "1 of 3" in vogue and "High Jewelry gala" in vogue, vogue
            assert "| Ghost House | not found: add its handle or channel_id" in sec, sec
            assert "| Unknown Atelier | not found: add its handle" in sec, sec

            signals = analyze.compute_signals(conn, cfg["analysis"])
            header, body = analyze.render_brief(conn, cfg, signals, dt.date.today())
            md = header + "\n" + body
            assert "## What watched YouTube channels published" in md and "**YouTube channels**" in md
            assert "Open YouTube search" not in md and "YouTube search" not in md.split("## Coverage")[1]

        # quota exhaustion stops cleanly, in both forms YouTube uses, and the table says so
        for status in (403, 429):
            with tempfile.TemporaryDirectory() as tmp:
                conn = open_db(Path(tmp) / f"q{status}.db")
                api = Api(quota_after=2, quota_status=status)
                with mock.patch("collect.get_json", side_effect=api):
                    collect.collect_channels(conn, cfg, "2026-10-14T00:00:00Z")
                assert conn.execute("SELECT 1 FROM runs WHERE status='error' AND message LIKE '%quota%'").fetchone()
                assert len(api.calls) <= 4, len(api.calls)                 # it stopped instead of grinding on
        # a spent quota is never retried (retrying cannot help until the day rolls over)
        r429 = mock.Mock(status_code=429, text='{"error": {"message": "Quota exceeded for quota metric"}}')
        with mock.patch("collect.requests.get", return_value=r429) as g, mock.patch.object(collect.time, "sleep") as sl:
            collect.get_json("http://x")
        assert g.call_count == 1 and sl.call_count == 0

        # no key: skipped, not an error
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "n.db")
            with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": ""}):
                assert collect.collect_channels(conn, cfg, "2026-10-14T00:00:00Z") == 0
            assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='skipped'").fetchone()[0] == 1

    print("Channel checks passed.")


if __name__ == "__main__":
    main()
