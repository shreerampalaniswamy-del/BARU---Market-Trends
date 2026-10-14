"""Checks the YouTube collector's relevance filter against a faked API.

This tests OUR logic (exact-phrase query, keeping only videos that name the term, storing matched_*
metrics). It cannot prove how YouTube itself treats quoted phrases; the first real run shows that
through the 'N of M videos name the term' lines in the brief's coverage section.

Run: python tests/test_youtube.py
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import collect  # noqa: E402
from common import load_config, open_db  # noqa: E402


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, "{}"

    def json(self):
        return self._payload


def video(vid, title, desc, views):
    return {"id": vid,
            "snippet": {"title": title, "description": desc, "channelTitle": f"Chan {vid}",
                        "publishedAt": "2026-10-10T00:00:00Z"},
            "statistics": {"viewCount": str(views), "likeCount": "10", "commentCount": "2"}}


def make_fake():
    state = {"term": None, "queries": [], "params": []}

    def fake_get_json(url, params=None, headers=None, tries=3, **kw):
        if url.endswith("/search"):
            state["queries"].append(params["q"])
            state["params"].append(dict(params))
            term = params["q"].strip('"')
            state["term"] = term
            ids = ["v2"] if term == "高級珠寶" else ["v1", "v2", "v3"]   # HK term: only an unrelated hit
            return FakeResp({"items": [{"id": {"videoId": i}} for i in ids],
                             "pageInfo": {"totalResults": 1234}})
        assert url.endswith("/videos")
        t = state["term"]
        every = {
            "v1": video("v1", f"{t} launch at the Grand Palais", "", 1_000_000),
            "v2": video("v2", "Monster High merch haul", "toys and dolls", 500_000),
            "v3": video("v3", "Weekend vlog", f"I talk about {t.upper()} here", 50_000),
        }
        return FakeResp({"items": [every[i] for i in params["id"].split(",")]})

    return fake_get_json, state


def now_ts(offset_seconds=0):
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def metric(conn, market, term, name):
    row = conn.execute("SELECT value FROM metrics WHERE source='youtube' AND market=? AND term=? AND metric=?",
                       (market, term, name)).fetchone()
    return row[0] if row else None


def main():
    cfg = load_config()
    # the shipped config searches core terms only; these checks exercise theme searching in India as well
    default_cfg = load_config()
    assert default_cfg["youtube"]["theme_markets"] == []
    assert all(len(collect.youtube_terms(default_cfg, m)) == len(collect.all_terms(default_cfg, m, include_themes=False))
               for m in default_cfg["markets"])
    cfg["youtube"]["theme_markets"] = ["India"]
    cfg["youtube"]["keyword_search"] = True

    # the shipped config has keyword search switched off: no API calls at all, logged as skipped
    assert default_cfg["youtube"]["keyword_search"] is False
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "off.db")
        never = mock.Mock(side_effect=AssertionError("must not call the API"))
        with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "test-key"}), mock.patch("collect.get_json", never):
            assert collect.collect_youtube(conn, default_cfg, now_ts()) == 0
        assert never.call_count == 0
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='skipped' AND message LIKE '%switched off%'").fetchone()[0] == 1
    with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "test-key"}):
        # 1. default behaviour: exact phrase query, keep only videos that name the term
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "y.db")
            fake, state = make_fake()
            with mock.patch("collect.get_json", side_effect=fake):
                assert collect.collect_youtube(conn, cfg, now_ts()) > 0
            assert all(q.startswith('"') and q.endswith('"') for q in state["queries"]), state["queries"][:3]

            # India first; Global has no region or language hint; themes only in India and Global
            assert state["params"][0]["regionCode"] == "IN"
            global_market = next(m for m in cfg["markets"] if m["name"] == "Global")
            no_region = [p for p in state["params"] if "regionCode" not in p]
            assert len(no_region) == len(collect.youtube_terms(cfg, global_market)) == 4, len(no_region)   # core only
            assert all("relevanceLanguage" not in p for p in no_region)
            assert sum(1 for q in state["queries"] if q == '"polki"') == 1      # theme terms: India only on YouTube

            # a re-run keeps what was collected recently and calls the API for nothing
            again = mock.Mock(side_effect=fake)
            with mock.patch("collect.get_json", again):
                assert collect.collect_youtube(conn, cfg, now_ts(60)) == 0
            assert again.call_count == 0, again.call_count

            assert metric(conn, "France", "High Jewellery", "matched_views_sum") == 1_050_000
            assert metric(conn, "France", "High Jewellery", "matched_views_median") == 525_000
            assert metric(conn, "France", "High Jewellery", "matched_videos") == 2
            assert metric(conn, "France", "High Jewellery", "returned_videos") == 3
            assert conn.execute("SELECT COUNT(*) FROM metrics WHERE source='youtube' AND metric IN "
                                "('views_sum','views_median')").fetchone()[0] == 0   # old names never reused

            urls = {r[0] for r in conn.execute(
                "SELECT url FROM items WHERE market='France' AND term='High Jewellery'")}
            assert urls == {"https://www.youtube.com/watch?v=v1", "https://www.youtube.com/watch?v=v3"}, urls

            # a term with no relevant videos is stored as zeros, not skipped, and is reported as empty
            assert metric(conn, "Hong Kong", "高級珠寶", "matched_views_sum") == 0
            assert metric(conn, "Hong Kong", "高級珠寶", "matched_videos") == 0
            assert conn.execute("SELECT COUNT(*) FROM items WHERE term='高級珠寶'").fetchone()[0] == 0
            msg = conn.execute("SELECT message FROM runs WHERE source='youtube' AND status='empty' "
                               "AND term='高級珠寶'").fetchone()[0]
            assert "none name the term" in msg, msg
            ok = conn.execute("SELECT message FROM runs WHERE source='youtube' AND status='ok' LIMIT 1").fetchone()[0]
            assert ok.startswith("2 of 3 videos name the term [f:"), ok

        # 2. both switches off: old behaviour (unquoted query, every returned video counted)
        cfg2 = load_config()
        cfg2["youtube"]["keyword_search"] = True
        cfg2["youtube"]["exact_phrase"] = False
        cfg2["youtube"]["require_phrase_in_text"] = False
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "y2.db")
            fake, state = make_fake()
            with mock.patch("collect.get_json", side_effect=fake):
                collect.collect_youtube(conn, cfg2, now_ts())
            assert state["queries"][0] == cfg2["keywords"][0], state["queries"][0]
            assert metric(conn, "France", "High Jewellery", "matched_videos") == 3
            assert metric(conn, "France", "High Jewellery", "matched_views_sum") == 1_550_000

    # 3. ambiguous names need a jewellery word; art terms an art word; spam words are dropped
    def vid(i, title, desc=""):
        return {"id": i, "snippet": {"title": title, "description": desc, "channelTitle": "c",
                                     "publishedAt": "2026-10-10T00:00:00Z"},
                "statistics": {"viewCount": "1000", "likeCount": "1", "commentCount": "1"}}

    pool = {
        "Van Cleef": [vid("a", "Van Cleef & Arpels Alhambra necklace unboxing"),
                      vid("b", "Lee Van Cleef western film"),
                      vid("c", "Van Cleef jewellery today gold rate")],
        "Contemporary Art": [vid("a", "Contemporary Art exhibition at the Tate gallery"),
                             vid("b", "Contemporary Art nude photoshoot"),
                             vid("c", "Contemporary Art meme compilation")],
        "polki": [vid("a", "Polki necklace set for the bride"), vid("b", "#polki fridge prank vlog")],
        "Cartier": [vid("a", "Cartier Love bracelet review"), vid("b", "CARTIER REMIX (official audio)")],
        "High Jewellery": [vid("a", "High Jewellery gala in Paris")],
    }
    held = {}

    def fake_filter(url, params=None, headers=None, tries=3, **kw):
        if url.endswith("/search"):
            held["term"] = params["q"].strip('"')
            vids = pool.get(held["term"], [])
            return FakeResp({"items": [{"id": {"videoId": x["id"]}} for x in vids], "pageInfo": {"totalResults": 1}})
        return FakeResp({"items": pool.get(held["term"], [])})

    with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "test-key"}):
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "y3.db")
            with mock.patch("collect.get_json", side_effect=fake_filter):
                collect.collect_youtube(conn, cfg, now_ts())
            assert metric(conn, "India", "Van Cleef", "matched_videos") == 1            # actor and gold-rate spam out
            assert metric(conn, "India", "Contemporary Art", "matched_videos") == 1     # nude and meme out
            assert metric(conn, "India", "polki", "matched_videos") == 1                # fridge prank out
            assert metric(conn, "India", "Cartier", "matched_videos") == 1              # remix out
            assert metric(conn, "India", "High Jewellery", "matched_videos") == 1       # no context rule: kept
            assert metric(conn, "Global", "Van Cleef", "matched_videos") is None        # themes not searched in Global

    # 4. changing the filter settings re-collects series that were fresh under the old settings,
    #    and the items of the old collection are replaced, not mixed in
    with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "test-key"}):
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "y4.db")
            cfg4 = load_config()
            cfg4["youtube"]["keyword_search"] = True
            cfg4["youtube"]["theme_markets"] = []
            with mock.patch("collect.get_json", side_effect=fake_filter):
                collect.collect_youtube(conn, cfg4, now_ts())
            before = conn.execute("SELECT COUNT(*) FROM items WHERE source='youtube'").fetchone()[0]
            same = mock.Mock(side_effect=fake_filter)
            with mock.patch("collect.get_json", same):
                collect.collect_youtube(conn, cfg4, now_ts(60))
            assert same.call_count == 0                                   # same settings: nothing refetched
            cfg4["youtube"]["exclude_words"] = cfg4["youtube"]["exclude_words"] + ["alhambra"]
            again = mock.Mock(side_effect=fake_filter)
            with mock.patch("collect.get_json", again):
                collect.collect_youtube(conn, cfg4, now_ts(120))
            assert again.call_count > 0                                   # new settings: collected again
            after = conn.execute("SELECT COUNT(*) FROM items WHERE source='youtube'").fetchone()[0]
            assert after == before, (before, after)                       # replaced, not accumulated

    print("YouTube checks passed.")


if __name__ == "__main__":
    main()
