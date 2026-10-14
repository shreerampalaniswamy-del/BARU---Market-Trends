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
    daily("gdelt", "Global", "High Jewellery", "articles", 30, 3, 2.0)                      # news spike
    for market in ("France", "New York", "Hong Kong"):                                      # same term rising in 3 markets
        daily("google_trends", market, "High Jewelry", "interest_index", 40, 3, 1.9)
    # a theme term rising in India in search and in news, with a smaller z than Hong Kong's
    daily("google_trends", "India", "polki", "interest_index", 40, 3, 1.5)
    daily("gdelt", "India", "polki", "articles", 20, 2, 1.5)
    # a one-off spike the week before last that has since gone away: must be set aside
    for i in range(90):
        d = last - dt.timedelta(days=89 - i)
        put_metric(conn, ts, d.isoformat(), "wikipedia", "fr-wiki", "Art contemporain", "pageviews",
                   (800 if 76 <= i <= 82 else 80) + rng.gauss(0, 4))

    # YouTube: 40 daily snapshots, final week jumps 2.5x on one series, flat on another
    for i in range(40):
        d = last - dt.timedelta(days=39 - i)
        jump = 2.5 if i >= 36 else 1.0
        put_metric(conn, ts, d.isoformat(), "youtube", "France", "High Jewellery", "matched_views_sum",
                   50000 * jump + rng.gauss(0, 2000))
        put_metric(conn, ts, d.isoformat(), "youtube", "Italy", "High Art", "matched_views_sum",
                   30000 + rng.gauss(0, 1500))
    # Reddit with only 10 days of history: too short for a z-score
    for i in range(10):
        d = last - dt.timedelta(days=9 - i)
        put_metric(conn, ts, d.isoformat(), "reddit", "Global", "Contemporary Art", "posts_7d",
                   40 + (25 if i >= 9 else 0))

    put_item(conn, ts, "youtube", "France", "High Jewellery", "https://www.youtube.com/watch?v=abc",
             "A | test [video]", "Test Channel", "2026-01-01T00:00:00Z", 123456, 999)
    put_item(conn, ts, "youtube", "Italy", "High Jewelry", "https://www.youtube.com/watch?v=abc",
             "A | test [video]", "Test Channel", "2026-01-01T00:00:00Z", 123456, 999)
    # Reddit was skipped in an EARLIER collection step than Google Trends, as in the real workflow.
    conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                 ("2026-01-02T00:00:00Z", "google_trends", "London", "High Jewellery", "ok", "90 days"))
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

        news = find(signals, "gdelt", "Global", "High Jewellery", "articles")
        assert news["flagged"] and news["direction"] == "rising", news

        wiki = find(signals, "wikipedia", "en-wiki", "Contemporary art", "pageviews")
        assert not wiki["flagged"], wiki

        yt_up = find(signals, "youtube", "France", "High Jewellery", "matched_views_sum")
        assert yt_up["flagged"] and yt_up["direction"] == "rising", yt_up
        yt_flat = find(signals, "youtube", "Italy", "High Art", "matched_views_sum")
        assert not yt_flat["flagged"], yt_flat

        short = find(signals, "reddit", "Global", "Contemporary Art", "posts_7d")
        assert short["z"] is None and not short["flagged"], short  # not enough history

        header, body = analyze.render_brief(conn, cfg, signals, dt.date.today())
        md = header + "\n" + body
        assert "## Rising" in md and "Hong Kong" in md and "Contemporary Art" in md
        assert "haute joaillerie" in md and "## Cooling" in md
        assert "A / test (video)" in md  # table-breaking characters cleaned in titles
        assert "REDDIT_CLIENT_ID" in md  # skipped source is reported even though a later run exists
        assert "News articles per day" in md

        # --- spike roll-off is set aside ---
        settling = find(signals, "wikipedia", "fr-wiki", "Art contemporain", "pageviews")
        assert settling["rolloff"], settling
        assert not find(signals, "wikipedia", "en-wiki", "Contemporary art", "pageviews")["rolloff"]
        assert "Set aside" in md and "Art contemporain" in md.split("## Coverage")[1]
        others = md.split("## Largest other changes")[1].split("\n## ")[0] if "## Largest other changes" in md else ""
        assert "Art contemporain" not in others

        # --- same term flagged in several markets ---
        assert "## Where signals agree" in md
        agree_row = next(l for l in md.splitlines() if l.startswith("| High Jewelry | rising |"))
        assert all(m in agree_row for m in ("France", "Hong Kong", "New York")), agree_row

        # --- grid tells "not tracked" from "no data" ---
        grid = analyze.trends_grid([s for s in signals if not s["stale"]], cfg)
        glines = grid.splitlines()
        cols = [c.strip() for c in glines[0].strip("|").split("|")][1:]
        assert cols[:2] == ["India", "Mumbai"] and cols[-1] == "Global", cols      # India first
        cells = {}
        for line in glines[2:]:
            parts = [c.strip() for c in line.strip("|").split("|")]
            cells[parts[0]] = dict(zip(cols, parts[1:]))
        assert cells["High Jewellery"]["France"] == "no data", cells["High Jewellery"]
        assert cells["haute joaillerie"]["New York"] == "·", cells["haute joaillerie"]
        assert cells["haute joaillerie"]["France"].endswith("▼"), cells["haute joaillerie"]

        # --- India first: header note, Theme column, ordering, theme tables ---
        assert "India and Mumbai come first in the tables" in md
        assert "| High Jewelry | Core |" in md
        rising_block = md.split("## Rising")[1].split("\n## ")[0]
        assert "| polki | Heritage and craft |" in rising_block
        assert rising_block.index("| India |") < rising_block.index("| Hong Kong |"), "India must lead"
        assert "## Themes (India first)" in md and "### Heritage and craft" in md
        theme_block = md.split("### Heritage and craft")[1].split("\n### ")[0]
        polki_row = next(l for l in theme_block.splitlines() if l.startswith("| polki |"))
        assert polki_row.count("▲") == 2 and "no data" in polki_row, polki_row   # India search+news up; Global empty
        polki_agree = next(l for l in md.splitlines() if l.startswith("| polki | rising |"))
        assert "Google Trends" in polki_agree and "GDELT news" in polki_agree, polki_agree

        # --- one video found under two terms and markets is listed once ---
        assert md.count("watch?v=abc") == 1
        assert "High Jewellery, High Jewelry" in md and "France, Italy" in md
        assert "weeks of history" in md

        print("All checks passed.\n")
        print(md)


if __name__ == "__main__":
    main()
