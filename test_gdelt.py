"""Checks the GDELT collector against a faked API, then runs the result through the analysis.

This tests OUR parsing and error handling. It cannot prove GDELT's live responses match the
documented format; the first real run does that.

Run: python tests/test_gdelt.py
"""
import datetime as dt
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analyze  # noqa: E402
import collect  # noqa: E402
from common import load_config, open_db  # noqa: E402


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text if text else ("{}" if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


def timeline(base, last_week_mult=1.0, norm=1_000_000):
    """91 daily buckets ending today (today's bucket is partial and must be dropped)."""
    today = dt.datetime.now(dt.timezone.utc).date()
    pts = []
    for back in range(90, -1, -1):
        day = today - dt.timedelta(days=back)
        mult = last_week_mult if 1 <= back <= 7 else 1.0
        value = 1 if back == 0 else base * mult
        pts.append({"date": day.strftime("%Y%m%dT000000Z"), "value": value, "norm": norm})
    return {"query_details": [], "timeline": [{"series": "Article Count", "data": pts}]}


SEEN = []


def fake_get_json(url, params=None, headers=None, tries=3, **kw):
    q = params["query"]
    SEEN.append(q)
    assert params["mode"] == "timelinevolraw" and params["format"] == "json", params
    low = q.lower()
    if low == '"high jewellery"':
        return FakeResp(payload=timeline(30, last_week_mult=2.0))        # global news spike
    if '"art jewellery"' in low:
        return FakeResp(text="The specified phrase is too short.")        # plain-text error
    if "contemporary art" in low and "sourcecountry:italy" in low:
        return FakeResp(text="")                                          # no matches
    if "haute joaillerie" in low:
        assert "sourcecountry:france" in low, q
        return FakeResp(payload=timeline(12))
    return FakeResp(payload=timeline(8))


def main():
    cfg = load_config()
    cfg["gdelt"]["sleep_seconds"] = 0
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "g.db")
        run_ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with mock.patch("collect.get_json", side_effect=fake_get_json), \
                mock.patch.object(collect.time, "sleep"):
            stored = collect.collect_gdelt(conn, cfg, run_ts)
        assert stored > 0

        # scope: India first, worldwide without a filter, themes only in India and Global
        assert SEEN[0] == '"High Jewellery" sourcecountry:india', SEEN[0]
        assert '"High Jewellery"' in SEEN
        assert '"polki" sourcecountry:india' in SEEN and '"polki"' in SEEN
        assert not any("polki" in q and ("france" in q or "italy" in q) for q in SEEN)

        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        n_days = conn.execute(
            "SELECT COUNT(*) FROM metrics WHERE source='gdelt' AND market='Global' "
            "AND term='High Jewellery' AND metric='articles'").fetchone()[0]
        assert n_days == 90, n_days                                       # today's bucket dropped
        assert conn.execute("SELECT COUNT(*) FROM metrics WHERE source='gdelt' AND date=?",
                            (today,)).fetchone()[0] == 0
        share = conn.execute(
            "SELECT value FROM metrics WHERE source='gdelt' AND market='Global' AND term='High Jewellery' "
            "AND metric='article_share_pct' LIMIT 1").fetchone()[0]
        assert abs(share - 0.003) < 1e-9, share                           # 30 / 1,000,000 * 100

        queries = [r[0] for r in conn.execute(
            "SELECT DISTINCT market FROM metrics WHERE source='gdelt'")]
        assert {"Global", "India", "France", "New York", "Mumbai", "Hong Kong", "Italy", "London"} <= set(queries)

        errors = conn.execute("SELECT message FROM runs WHERE source='gdelt' AND status='error'").fetchall()
        assert errors and all(m[0].startswith("not JSON") for m in errors), errors
        empties = conn.execute("SELECT market, term FROM runs WHERE source='gdelt' AND status='empty'").fetchall()
        assert ("Italy", "Contemporary Art") in empties, empties

        signals = analyze.compute_signals(conn, cfg["analysis"])
        spike = next(s for s in signals if (s["source"], s["market"], s["term"]) ==
                     ("gdelt", "Global", "High Jewellery"))
        assert spike["flagged"] and spike["direction"] == "rising" and spike["wow"] > 0.8, spike
        flat = next(s for s in signals if (s["source"], s["market"], s["term"]) ==
                    ("gdelt", "France", "haute joaillerie"))
        assert not flat["flagged"], flat

        header, body = analyze.render_brief(conn, cfg, signals, dt.date.today())
        md = header + "\n" + body
        assert "News articles per day" in md and "GDELT news" in md and "publisher's country" in md

        # a re-run only retries what failed (never the collected series) and still stops after 3 in a row
        redo = mock.Mock(side_effect=fake_get_json)
        with mock.patch("collect.get_json", redo), mock.patch.object(collect.time, "sleep"):
            collect.collect_gdelt(conn, cfg, run_ts[:-1] + "Z")
        assert redo.call_count == 3, redo.call_count
        retried = [c.kwargs["params"]["query"] if "params" in c.kwargs else c.args[1]["query"]
                   for c in redo.call_args_list]
        assert all("art jewellery" in q for q in retried), retried
        kept = conn.execute("SELECT COUNT(*) FROM runs WHERE message LIKE 'not refetched%'").fetchone()[0]
        assert kept > 0

        # 3 failures in a row must stop the source instead of hammering the API
        conn2 = open_db(Path(tmp) / "g2.db")
        with mock.patch("collect.get_json", return_value=FakeResp(status=429)), \
                mock.patch.object(collect.time, "sleep"):
            assert collect.collect_gdelt(conn2, cfg, run_ts) == 0
        stop = conn2.execute("SELECT COUNT(*) FROM runs WHERE message LIKE '%stopping GDELT%'").fetchone()[0]
        assert stop == 1, stop

        print("GDELT checks passed.")


if __name__ == "__main__":
    main()
