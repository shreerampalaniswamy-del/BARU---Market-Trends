"""Checks the analysis layer on synthetic data with known, injected behaviour.

Run: python tests/test_analyze.py
"""
import datetime as dt
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analyze  # noqa: E402
from collect import put_item, put_metric  # noqa: E402
from common import load_config, open_db  # noqa: E402


def build(db_path):
    rng = random.Random(7)
    conn = open_db(db_path)
    ts = "2026-01-01T00:00:00Z"
    last = dt.date.today() - dt.timedelta(days=1)

    def daily(source, market, term, metric, base, noise, last_week_mult=1.0):
        for i in range(90):
            d = last - dt.timedelta(days=89 - i)
            mult = last_week_mult if i >= 83 else 1.0
            put_metric(conn, ts, d.isoformat(), source, market, term, metric,
                       max(0.0, base * mult + rng.gauss(0, noise)))

    daily("google_trends", "Hong Kong", "Contemporary Art", "interest_index", 40, 3, 1.9)  # spike
    daily("google_trends", "London", "High Jewellery", "interest_index", 50, 3, 1.0)       # stable
    daily("google_trends", "France", "haute joaillerie", "interest_index", 60, 3, 0.5)     # cooling
    daily("google_trends", "Italy", "High Art", "interest_index", 1.5, 0.3, 3.0)           # tiny level
    daily("wikipedia", "en-wiki", "Contemporary art", "pageviews", 900, 40, 1.0)           # stable

    # YouTube: 40 daily snapshots, final week jumps 2.5x on one series, flat on another
    for i in range(40):
        d = last - dt.timedelta(days=39 - i)
        jump = 2.5 if i >= 36 else 1.0
        put_metric(conn, ts, d.isoformat(), "youtube", "France", "High Jewellery", "views_sum",
                   50000 * jump + rng.gauss(0, 2000))
        put_metric(conn, ts, d.isoformat(), "youtube", "Italy", "High Art", "views_sum",
                   30000 + rng.gauss(0, 1500))
    # Reddit with only 10 days of history: too short for a z-score
    for i in range(10):
        d = last - dt.timedelta(days=9 - i)
        put_metric(conn, ts, d.isoformat(), "reddit", "Global", "Contemporary Art", "posts_7d",
                   40 + (25 if i >= 9 else 0))

    put_item(conn, ts, "youtube", "France", "High Jewellery", "https://www.youtube.com/watch?v=abc",
             "A | test [video]", "Test Channel", "2026-01-01T00:00:00Z", 123456, 999)
    conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                 (ts, "google_trends", "London", "High Jewellery", "ok", "90 days"))
    conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                 (ts, "reddit", None, None, "skipped", "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set"))
    conn.commit()
    return conn


def find(signals, source, market, term, metric):
    for s in signals:
        if (s["source"], s["market"], s["term"], s["metric"]) == (source, market, term, metric):
            return s
    raise AssertionError(f"signal not found: {source}/{market}/{term}/{metric}")


def main():
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        conn = build(Path(tmp) / "sample.db")
        signals = analyze.compute_signals(conn, cfg["analysis"])

        spike = find(signals, "google_trends", "Hong Kong", "Contemporary Art", "interest_index")
        assert spike["flagged"] and spike["direction"] == "rising", spike
        assert 0.7 <= spike["wow"] <= 1.1, spike["wow"]

        stable = find(signals, "google_trends", "London", "High Jewellery", "interest_index")
        assert not stable["flagged"], stable

        cooling = find(signals, "google_trends", "France", "haute joaillerie", "interest_index")
        assert cooling["flagged"] and cooling["direction"] == "cooling", cooling
        assert cooling["wow"] < -0.3, cooling["wow"]

        tiny = find(signals, "google_trends", "Italy", "High Art", "interest_index")
        assert not tiny["flagged"] and tiny["low_level"], tiny

        wiki = find(signals, "wikipedia", "en-wiki", "Contemporary art", "pageviews")
        assert not wiki["flagged"], wiki

        yt_up = find(signals, "youtube", "France", "High Jewellery", "views_sum")
        assert yt_up["flagged"] and yt_up["direction"] == "rising", yt_up
        yt_flat = find(signals, "youtube", "Italy", "High Art", "views_sum")
        assert not yt_flat["flagged"], yt_flat

        short = find(signals, "reddit", "Global", "Contemporary Art", "posts_7d")
        assert short["z"] is None and not short["flagged"], short  # not enough history

        header, body = analyze.render_brief(conn, cfg, signals, dt.date.today())
        md = header + "\n" + body
        assert "## Rising" in md and "Hong Kong" in md and "Contemporary Art" in md
        assert "haute joaillerie" in md and "## Cooling" in md
        assert "A / test (video)" in md  # table-breaking characters cleaned in titles
        assert "REDDIT_CLIENT_ID" in md  # skipped source is reported, not hidden
        assert "weeks of history" in md

        print("All checks passed.\n")
        print(md)


if __name__ == "__main__":
    main()
