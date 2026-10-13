#!/usr/bin/env python3
"""Turn the raw series in data/trends.db into a markdown brief of what is actually moving.

Method, per series:
  * Collapse the series into weekly values (7-day averages for daily sources; one snapshot per
    week for YouTube/Reddit), latest week first.
  * week-over-week change = this week / last week - 1
  * z-score = how far this week sits from the mean of the prior weeks, in standard deviations
  * streak = how many weeks in a row the value has moved in the same direction
  * A series is flagged only if the z-score is large AND the change is large AND the level is not
    trivially small, so tiny numbers cannot masquerade as trends.
"""
import argparse
import csv
import datetime as dt
import statistics
import sys
from pathlib import Path

from common import DEFAULT_CONFIG, DEFAULT_DB, ROOT, load_config, open_db, scrub
from narrate import narrate

PRIMARY = {
    "google_trends": ["interest_index"],
    "wikipedia": ["pageviews"],
    "youtube": ["views_sum", "views_median"],
    "reddit": ["posts_7d", "engagement"],
    "gdelt": ["articles"],
}
DAILY = {"google_trends", "wikipedia", "gdelt"}
MIN_LEVEL = {
    ("google_trends", "interest_index"): 10,
    ("wikipedia", "pageviews"): 20,
    ("youtube", "views_sum"): 1000,
    ("youtube", "views_median"): 500,
    ("reddit", "posts_7d"): 3,
    ("reddit", "engagement"): 20,
    ("gdelt", "articles"): 3,
}
SHORT = {
    ("google_trends", "interest_index"): "Google search interest",
    ("wikipedia", "pageviews"): "Wikipedia daily views",
    ("youtube", "views_sum"): "YouTube views (top-50 new videos)",
    ("youtube", "views_median"): "YouTube median views",
    ("reddit", "posts_7d"): "Reddit posts",
    ("reddit", "engagement"): "Reddit engagement",
    ("gdelt", "articles"): "News articles per day",
}
SOURCE_NAME = {
    "google_trends": "Google Trends",
    "wikipedia": "Wikipedia",
    "youtube": "YouTube",
    "reddit": "Reddit",
    "gdelt": "GDELT news",
}
STALE_DAYS = 10


# ---------------------------------------------------------------- series maths

def daily_blocks(points, lookback_days):
    """7-day averages ending on the series' last date, latest first."""
    last = points[-1][0]
    floor = last - dt.timedelta(days=lookback_days - 1)
    by_date = {d: v for d, v in points if d >= floor}
    blocks, k = [], 0
    while True:
        end = last - dt.timedelta(days=7 * k)
        begin = end - dt.timedelta(days=6)
        if begin < floor:
            break
        days = [begin + dt.timedelta(days=i) for i in range(7)]
        vals = [by_date[d] for d in days if d in by_date]
        if len(vals) < 5:
            break
        blocks.append(statistics.fmean(vals))
        k += 1
    return blocks


def snapshot_blocks(points, max_weeks=26):
    """One snapshot per week, at least 7 days apart, latest first."""
    pts = sorted(points, key=lambda p: p[0], reverse=True)
    blocks, anchor = [pts[0][1]], pts[0][0]
    for d, v in pts[1:]:
        if (anchor - d).days >= 7:
            blocks.append(v)
            anchor = d
            if len(blocks) >= max_weeks:
                break
    return blocks


def analyze_series(key, points, acfg):
    source, market, term, metric = key
    blocks = (daily_blocks(points, acfg["lookback_days"]) if source in DAILY
              else snapshot_blocks(points))
    if not blocks:
        return None
    cur = blocks[0]
    prev = blocks[1] if len(blocks) > 1 else None
    hist = blocks[1:]

    wow = (cur / prev - 1) if (prev is not None and prev > 0) else None

    z = None
    if len(hist) >= acfg["min_weeks_for_z"]:
        mu = statistics.fmean(hist)
        # Floor the spread at 10% of the mean so a very flat history cannot inflate z-scores.
        sd = max(statistics.pstdev(hist), 0.10 * abs(mu), 1e-9)
        z = (cur - mu) / sd

    streak, direction = 0, 0
    for i in range(len(blocks) - 1):
        step = (blocks[i] > blocks[i + 1]) - (blocks[i] < blocks[i + 1])
        if step == 0:
            break
        if direction == 0:
            direction = step
        if step != direction:
            break
        streak += 1
    streak *= direction

    last_date = points[-1][0]
    min_level = MIN_LEVEL.get((source, metric), 0)
    big_move = wow is None or abs(wow) >= acfg["min_change"]
    flagged = (
        z is not None and abs(z) >= acfg["z_threshold"] and big_move
        and max(cur, prev or 0) >= min_level
    )
    return {
        "source": source, "market": market, "term": term, "metric": metric,
        "current": cur, "previous": prev, "wow": wow, "z": z, "streak": streak,
        "weeks": len(blocks), "last_date": last_date.isoformat(), "flagged": flagged,
        "direction": ("rising" if (z or 0) > 0 else "cooling") if flagged else "",
        "low_level": max(cur, prev or 0) < min_level,
    }


def compute_signals(conn, acfg, today=None):
    today = today or dt.date.today()
    rows = conn.execute(
        "SELECT source, market, term, metric, date, value FROM metrics "
        "ORDER BY source, market, term, metric, date"
    ).fetchall()
    series = {}
    for s, m, t, mt, d, v in rows:
        if mt in PRIMARY.get(s, []):
            series.setdefault((s, m, t, mt), []).append((dt.date.fromisoformat(d), v))
    out = []
    for key, pts in series.items():
        sig = analyze_series(key, pts, acfg)
        if sig is None:
            continue
        if (today - dt.date.fromisoformat(sig["last_date"])).days > STALE_DAYS:
            sig["flagged"], sig["direction"] = False, ""
            sig["stale"] = True
        else:
            sig["stale"] = False
        out.append(sig)
    return out


# ---------------------------------------------------------------- rendering

def fmt_val(source, v):
    if v is None:
        return "–"
    if source == "google_trends":
        return f"{v:.0f}"
    return f"{v:,.0f}" if v >= 100 else f"{v:,.1f}"


def fmt_pct(sig):
    if sig["wow"] is None:
        return "new" if sig["current"] > 0 else "–"
    return f"{sig['wow'] * 100:+.0f}%"


def fmt_streak(n):
    if n > 0:
        return f"up {n}w"
    if n < 0:
        return f"down {-n}w"
    return "–"


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def sig_row(s):
    return [
        SHORT[(s["source"], s["metric"])], s["market"], s["term"],
        fmt_val(s["source"], s["current"]), fmt_val(s["source"], s["previous"]),
        fmt_pct(s), f"{s['z']:+.1f}" if s["z"] is not None else "–", fmt_streak(s["streak"]),
    ]


SIG_HEADERS = ["Signal", "Market", "Term", "This week", "Last week", "Change", "z-score", "Streak"]


def trends_grid(signals, cfg):
    by = {(s["market"], s["term"]): s for s in signals if s["source"] == "google_trends"}
    if not by:
        return None
    markets = [m["name"] for m in cfg["markets"]]
    terms = list(cfg["keywords"])
    for m in cfg["markets"]:
        for t in m.get("local_terms", []):
            if t not in terms:
                terms.append(t)
    rows = []
    for t in terms:
        row = [t]
        for m in markets:
            s = by.get((m, t))
            if s is None:
                row.append("·")
            elif s["low_level"]:
                row.append("low volume")
            elif s["wow"] is None:
                row.append("new" if s["current"] > 0 else "–")
            else:
                mark = {"rising": " ▲", "cooling": " ▼"}.get(s["direction"], "")
                row.append(f"{s['wow'] * 100:+.0f}%{mark}")
        rows.append(row)
    return md_table(["Search term"] + markets, rows)


def clean(text, n=90):
    text = (text or "").replace("|", "/").replace("[", "(").replace("]", ")").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def top_items(conn, source, per_term=3):
    latest = conn.execute("SELECT MAX(collected_at) FROM items WHERE source=?", (source,)).fetchone()[0]
    if not latest:
        return []
    rows = conn.execute(
        "SELECT term, url, title, author, views, engagement FROM items "
        "WHERE source=? AND collected_at=?", (source, latest)).fetchall()
    by_term = {}
    for term, url, title, author, views, eng in rows:
        by_term.setdefault(term, {})[url] = (title, author, views, eng)
    lines = []
    for term, items in by_term.items():
        ranked = sorted(items.items(),
                        key=lambda kv: (kv[1][2] or 0) if source == "youtube" else (kv[1][3] or 0),
                        reverse=True)[:per_term]
        for url, (title, author, views, eng) in ranked:
            if source == "youtube":
                stat = f"{int(views or 0):,} views"
            else:
                stat = f"{int(eng or 0):,} score+comments"
            lines.append(f"- **{term}**: [{clean(title)}]({url}) — {clean(author, 40)}, {stat}")
    return lines


def coverage(conn, signals):
    lines = []
    for source in PRIMARY:
        run_ts = conn.execute("SELECT MAX(run_ts) FROM runs WHERE source=?", (source,)).fetchone()[0]
        mine = [s for s in signals if s["source"] == source]
        status = {}
        errors = []
        if run_ts:
            for st, msg in conn.execute(
                    "SELECT status, message FROM runs WHERE run_ts=? AND source=?", (run_ts, source)):
                status[st] = status.get(st, 0) + 1
                if st in ("error", "skipped") and msg and msg not in errors:
                    errors.append(msg)
        name = SOURCE_NAME[source]
        if not mine:
            note = "; ".join(errors[:2]) if errors else "no data collected yet"
            lines.append(f"- **{name}**: no usable data ({note})")
            continue
        weeks = max(s["weeks"] for s in mine)
        latest = max(s["last_date"] for s in mine)
        parts = [f"{len(mine)} series", f"latest data {latest}", f"{weeks} weeks of history"]
        if weeks < 5:
            parts.append("z-scores need 5+ weeks, so flags are not available yet; changes shown are directional only")
        counts = ", ".join(f"{n} {k}" for k, n in sorted(status.items()))
        if counts:
            parts.append(f"last run: {counts}")
        line = f"- **{name}**: " + "; ".join(parts)
        if errors:
            line += f" — issues: {'; '.join(errors[:2])}"
        lines.append(line)
    return lines


def render_brief(conn, cfg, signals, today):
    acfg = cfg["analysis"]
    top_n = int(acfg.get("top_n", 10))
    live = [s for s in signals if not s["stale"]]
    flagged = [s for s in live if s["flagged"]]
    rising = sorted([s for s in flagged if s["direction"] == "rising"], key=lambda s: -s["z"])[:top_n]
    cooling = sorted([s for s in flagged if s["direction"] == "cooling"], key=lambda s: s["z"])[:top_n]

    latest = max((s["last_date"] for s in live), default=today.isoformat())
    header = (
        f"# Trend brief, {today.isoformat()}\n\n"
        f"Markets: {', '.join(m['name'] for m in cfg['markets'])}. "
        f"Terms: {', '.join(cfg['keywords'])} (plus local-language terms where configured). "
        f"Latest data point: {latest}.\n\n"
        "*This week* is the 7-day average ending on each source's latest complete day; *last week* is "
        "the 7 days before it. A signal is flagged only when it is statistically unusual against its own "
        "history (|z| ≥ "
        f"{acfg['z_threshold']}), has moved at least {acfg['min_change']:.0%}, and is not a tiny number.\n"
    )

    body = []
    body.append("## Rising\n")
    body.append(md_table(SIG_HEADERS, [sig_row(s) for s in rising]) if rising
                else "Nothing flagged as rising this week.")
    body.append("\n## Cooling\n")
    body.append(md_table(SIG_HEADERS, [sig_row(s) for s in cooling]) if cooling
                else "Nothing flagged as cooling this week.")

    unflagged = [s for s in live if not s["flagged"] and s["wow"] is not None and not s["low_level"]]
    movers = sorted(unflagged, key=lambda s: -abs(s["wow"]))[:8]
    if movers:
        body.append("\n## Largest other changes (not statistically confirmed; treat as directional)\n")
        body.append(md_table(SIG_HEADERS, [sig_row(s) for s in movers]))

    grid = trends_grid(live, cfg)
    if grid:
        body.append("\n## Google search interest, week over week by market\n")
        body.append(grid)
        body.append("\n▲ / ▼ = flagged. Each term is indexed to its own 90-day peak, so compare "
                    "*changes* across cells, not levels. Mumbai = Maharashtra, New York = New York "
                    "state, London = England (Google has no city-level data here).")

    yt = top_items(conn, "youtube")
    if yt:
        body.append("\n## Top new YouTube videos this week\n")
        body.append("\n".join(yt))
    rd = top_items(conn, "reddit")
    if rd:
        body.append("\n## Top Reddit posts this week\n")
        body.append("\n".join(rd))

    body.append("\n## Coverage and data quality\n")
    body.append("\n".join(coverage(conn, signals)))
    body.append(
        "\nEvery number above comes from `data/trends.db`; the full per-series table is in "
        "`briefs/latest-signals.csv`. YouTube and Reddit snapshots only start accumulating from the "
        "first run, so their flags appear after about five weeks. GDELT counts news articles by the "
        "publisher's country, not the country an article is about; Mumbai, New York and London map to "
        "India, the United States and the United Kingdom."
    )
    return header, "\n".join(body) + "\n"


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--out-dir", default=str(ROOT / "briefs"))
    ap.add_argument("--no-narrate", action="store_true", help="skip the Claude analyst paragraph")
    args = ap.parse_args()

    cfg = load_config(args.config)
    conn = open_db(Path(args.db))
    today = dt.date.today()
    signals = compute_signals(conn, cfg["analysis"], today)
    if not signals:
        print("No data yet. Run collect.py first.")
        return 1

    header, body = render_brief(conn, cfg, signals, today)
    analyst = ""
    if not args.no_narrate:
        try:
            text = narrate(header + "\n" + body, cfg)
            if text:
                analyst = "## Analyst read\n\n" + text + "\n\n"
        except Exception as e:
            print(scrub(f"Analyst read skipped: {type(e).__name__}: {e}"))

    md = header + "\n" + analyst + body
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{today.isoformat()}.md").write_text(md, encoding="utf-8")
    (out / "latest.md").write_text(md, encoding="utf-8")
    fields = ["source", "market", "term", "metric", "current", "previous", "wow", "z", "streak",
              "weeks", "last_date", "flagged", "direction", "low_level", "stale"]
    with open(out / "latest-signals.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(signals, key=lambda s: (s["source"], s["market"], s["term"], s["metric"])))
    print(f"Brief written to {out / 'latest.md'} ({sum(s['flagged'] for s in signals)} flagged signals)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
