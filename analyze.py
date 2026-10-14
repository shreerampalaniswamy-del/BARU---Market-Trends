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
    "youtube": ["matched_views_sum", "matched_views_median"],
    "youtube_channel": ["views_sum"],
    "reddit": ["posts_7d", "engagement"],
    "gdelt": ["articles"],
}
DAILY = {"google_trends", "wikipedia", "gdelt"}
MIN_LEVEL = {
    ("google_trends", "interest_index"): 10,
    ("wikipedia", "pageviews"): 20,
    ("youtube", "matched_views_sum"): 1000,
    ("youtube", "matched_views_median"): 500,
    ("youtube_channel", "views_sum"): 1000,
    ("reddit", "posts_7d"): 3,
    ("reddit", "engagement"): 20,
    ("gdelt", "articles"): 3,
}
SHORT = {
    ("google_trends", "interest_index"): "Google search interest",
    ("wikipedia", "pageviews"): "Wikipedia daily views",
    ("youtube", "matched_views_sum"): "YouTube views (new videos naming the term)",
    ("youtube", "matched_views_median"): "YouTube median views",
    ("youtube_channel", "views_sum"): "Watched-channel views (new videos)",
    ("reddit", "posts_7d"): "Reddit posts",
    ("reddit", "engagement"): "Reddit engagement",
    ("gdelt", "articles"): "News articles per day",
}
SOURCE_NAME = {
    "google_trends": "Google Trends",
    "wikipedia": "Wikipedia",
    "youtube": "YouTube search",
    "youtube_channel": "YouTube channels",
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

    # A series that is just settling back after a one-off spike (last week far above its older norm,
    # this week back near it) says little about interest, so the brief sets it aside.
    older = blocks[2:]
    rolloff = False
    if prev is not None and len(older) >= acfg["min_weeks_for_z"]:
        norm = statistics.median(older)
        rolloff = norm > 0 and prev > 4 * norm and cur < 0.5 * prev

    min_level = MIN_LEVEL.get((source, metric), 0)
    z = None
    if len(hist) >= acfg["min_weeks_for_z"]:
        mu = statistics.fmean(hist)
        # Floor the spread so a very flat (or all-zero) history cannot inflate z-scores: at least 10% of
        # the mean and at least a quarter of the smallest level worth reading for this source.
        sd = max(statistics.pstdev(hist), 0.10 * abs(mu), 0.25 * min_level, 1e-9)
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
    # Flag only when the week is unusual against history AND the week-over-week move points the same way.
    # (A week that is still above the long-run norm but fell from last week is "easing", not "rising".)
    # A jump from exactly zero has no meaningful percentage, so it is listed as a change, never flagged.
    big_move = wow is not None and abs(wow) >= acfg["min_change"]
    flagged = (
        z is not None and abs(z) >= acfg["z_threshold"] and big_move
        and (z > 0) == (wow > 0)
        and max(cur, prev or 0) >= min_level
    )
    return {
        "source": source, "market": market, "term": term, "metric": metric,
        "current": cur, "previous": prev, "wow": wow, "z": z, "streak": streak,
        "weeks": len(blocks), "last_date": last_date.isoformat(), "flagged": flagged,
        "direction": ("rising" if wow > 0 else "cooling") if flagged else "",
        "low_level": max(cur, prev or 0) < min_level,
        "rolloff": rolloff,
    }


CONFIG_BOUND = ("google_trends", "gdelt", "youtube")   # sources whose series come from the term lists in config


def configured_pairs(cfg):
    """{source: {(market, term), ...}} currently in config.yaml. Series for terms (or YouTube theme searches)
    that were later removed stay in the database but are left out of the brief."""
    from collect import all_terms, youtube_terms
    every = {(m["name"], t) for m in cfg["markets"] for t in all_terms(cfg, m)}
    search_on = cfg.get("youtube", {}).get("keyword_search") is not False
    yt = {(m["name"], t) for m in cfg["markets"] for t in youtube_terms(cfg, m)} if search_on else set()
    return {"google_trends": every, "gdelt": every, "youtube": yt}


def compute_signals(conn, acfg, today=None, allowed=None):
    today = today or dt.date.today()
    rows = conn.execute(
        "SELECT source, market, term, metric, date, value FROM metrics "
        "ORDER BY source, market, term, metric, date"
    ).fetchall()
    series = {}
    for s, m, t, mt, d, v in rows:
        if allowed is not None and s in CONFIG_BOUND and (m, t) not in allowed.get(s, set()):
            continue
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


def sig_row(s, theme_of=None):
    return [
        SHORT[(s["source"], s["metric"])], s.get("market_label", s["market"]), s["term"], (theme_of or {}).get(s["term"].casefold(), "–"),
        fmt_val(s["source"], s["current"]), fmt_val(s["source"], s["previous"]),
        fmt_pct(s), f"{s['z']:+.1f}" if s["z"] is not None else "–", fmt_streak(s["streak"]),
    ]


SIG_HEADERS = ["Signal", "Market", "Term", "Theme", "This week", "Last week", "Change", "z-score", "Streak"]


def cell_text(s):
    """One table cell for a series: week-over-week change with a flag mark, or why there is no number."""
    if s is None:
        return "no data"
    if s["low_level"] or (s["current"] == 0 and s["source"] == "google_trends"):
        return "low volume"          # an index of 0 only means "under about 1% of the peak": unreadable
    if s["wow"] is None:
        return "new" if s["current"] > 0 else "–"
    mark = {"rising": " ▲", "cooling": " ▼"}.get(s["direction"], "")
    return f"{s['wow'] * 100:+.0f}%{mark}"


def theme_lookup(cfg):
    """term (case-insensitive) -> theme label, for the Theme column."""
    out = {t.casefold(): "Core" for t in cfg["keywords"]}
    for m in cfg["markets"]:
        for t in m.get("local_terms", []):
            out.setdefault(t.casefold(), "Core (local language)")
    for th in cfg.get("themes", []):
        for t in th["terms"]:
            out.setdefault(t.casefold(), th["name"])
    return out


def theme_tables(signals, cfg):
    """One table per theme: search and news change for each term, in each market the theme is scoped to."""
    idx = {(sg["source"], sg["market"], sg["term"].casefold()): sg
           for sg in signals if sg["source"] in ("google_trends", "gdelt")}
    present = [(src, label) for src, label in (("google_trends", "search"), ("gdelt", "news"))
               if any(k[0] == src for k in idx)]
    if not present:
        return []
    out = []
    for th in cfg.get("themes", []):
        markets = th.get("markets", [])
        heads = ["Term"] + [f"{m}: {label}" for m in markets for _, label in present]
        rows = [[t] + [cell_text(idx.get((src, m, t.casefold()))) for m in markets for src, _ in present]
                for t in th["terms"]]
        out.append((th["name"], md_table(heads, rows)))
    return out


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
    tracked = {(m["name"], t) for m in cfg["markets"]
               for t in list(cfg["keywords"]) + list(m.get("local_terms", []))}
    rows = []
    for t in terms:
        row = [t]
        for m in markets:
            s = by.get((m, t))
            row.append("·" if (m, t) not in tracked else cell_text(s))
        rows.append(row)
    return md_table(["Search term"] + markets, rows)


def clean(text, n=90):
    text = (text or "").replace("|", "/").replace("[", "(").replace("]", ")").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def item_table(conn, source, cfg, per_term=3):
    """Top items of the latest collection as one table. Each item appears once, with every term
    and market it was found under."""
    latest = conn.execute("SELECT MAX(collected_at) FROM items WHERE source=?", (source,)).fetchone()[0]
    if not latest:
        return None
    oldest = (dt.datetime.strptime(latest, "%Y-%m-%dT%H:%M:%SZ") - dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # newest collection of each series, so a partial re-run does not hide items from the main run
    rows = conn.execute(
        "SELECT i.market, i.term, i.url, i.title, i.author, i.views, i.engagement FROM items i "
        "JOIN (SELECT market, term, MAX(collected_at) AS m FROM items WHERE source=? AND collected_at>=? "
        "GROUP BY market, term) x ON i.market=x.market AND i.term=x.term AND i.collected_at=x.m "
        "WHERE i.source=?", (source, oldest, source)).fetchall()
    allowed = configured_pairs(cfg).get(source)
    if allowed is not None:
        rows = [r for r in rows if (r[0], r[1]) in allowed]
    by_term = {}
    for market, term, url, title, author, views, eng in rows:
        e = by_term.setdefault(term, {}).setdefault(
            url, {"title": title, "author": author, "views": views, "eng": eng, "markets": []})
        if market not in e["markets"]:
            e["markets"].append(market)

    core = list(cfg["keywords"]) + [t for m in cfg["markets"] for t in m.get("local_terms", [])]
    themed = [t for th in cfg.get("themes", []) for t in th["terms"]]
    order = core + [t for t in themed if t not in core]
    order += [t for t in by_term if t not in order]

    def score(e):
        return (e["views"] or 0) if source == "youtube" else (e["eng"] or 0)

    shown = {}
    for term in order:
        ranked = sorted(by_term.get(term, {}).items(), key=lambda kv: -score(kv[1]))[: per_term if term in core else 1]
        for url, e in ranked:
            if url not in shown:
                shown[url] = {**e, "terms": [term], "markets": list(e["markets"])}
                continue
            if term not in shown[url]["terms"]:
                shown[url]["terms"].append(term)
            for mk in e["markets"]:
                if mk not in shown[url]["markets"]:
                    shown[url]["markets"].append(mk)
    if not shown:
        return None

    table = []
    for url, e in shown.items():
        mk = ", ".join(e["markets"]) if len(e["markets"]) <= 3 else f"{len(e['markets'])} markets"
        stat = f"{int(score(e)):,}"
        table.append([", ".join(e["terms"]), f"[{clean(e['title'])}]({url})", clean(e["author"], 40), stat, mk])
    heads = (["Term", "Video", "Channel", "Views", "Markets"] if source == "youtube"
             else ["Term", "Post", "Subreddit", "Score + comments", "Markets"])
    return md_table(heads, table)


def region(market):
    """Mumbai sits inside India and Global includes India, so they are not independent confirmations."""
    return "India" if market in ("India", "Mumbai") else market


def channel_status(conn, group, name):
    """Why a watched channel has no numbers, in plain words, from its latest run."""
    r = conn.execute("SELECT message FROM runs WHERE source='youtube_channel' AND market=? AND term=? "
                     "ORDER BY run_ts DESC LIMIT 1", (group, name)).fetchone()
    msg = (r[0] if r else "") or ""
    low = msg.lower()
    if not r:
        return "not collected yet"
    if "quota" in low:
        return "waiting: YouTube quota used up, retry tomorrow"
    if "limit reached" in low:
        return "waiting: name-search limit, retry next run"
    if "not found" in low:
        return "not found: add its handle or channel_id in config.yaml"
    return msg[:60] or "not collected yet"


def channel_section(conn, cfg):
    """Jewellery-relevant videos from the curated channels in the last window, grouped (houses, media, auctions)."""
    chans = cfg.get("youtube", {}).get("channels") or []
    if not chans:
        return None
    window = int(cfg.get("youtube", {}).get("channel_window_days", 14))

    def latest(group, name, metric):
        r = conn.execute("SELECT value FROM metrics WHERE source='youtube_channel' AND market=? AND term=? "
                         "AND metric=? ORDER BY date DESC LIMIT 1", (group, name, metric)).fetchone()
        return r[0] if r else None

    out, groups = [], []
    for ch in chans:
        if ch.get("group", "Other") not in groups:
            groups.append(ch.get("group", "Other"))
    for group in groups:
        rows = []
        for ch in [c for c in chans if c.get("group", "Other") == group]:
            name = ch["name"]
            posted, scanned, views, subs = (latest(group, name, m)
                                            for m in ("videos_posted", "videos_scanned", "views_sum", "subscribers"))
            found = conn.execute("SELECT title FROM channels WHERE name=?", (name,)).fetchone()
            top = conn.execute("SELECT title, url, views FROM items WHERE source='youtube_channel' AND market=? "
                               "AND term=? ORDER BY views DESC LIMIT 1", (group, name)).fetchone()
            if posted is None:
                rows.append((-1, [name, found[0] if found else channel_status(conn, group, name),
                                  "–", "–", "–", "–"]))
                continue
            top_txt = f"[{clean(top[0], 70)}]({top[1]}) ({int(top[2]):,} views)" if top else "–"
            subs_txt = f"{int(subs):,}" if subs else "–"
            count = f"{int(posted)} of {int(scanned)}" if scanned is not None else f"{int(posted)}"
            rows.append((views or 0, [name, found[0] if found else "?", subs_txt, count,
                                      f"{int(views or 0):,}", top_txt]))
        rows.sort(key=lambda r: -r[0])
        out.append(f"\n### {group}\n")
        out.append(md_table(["Channel", "Found as", "Subscribers", f"Jewellery videos of all new ({window}d)",
                             "Views", "Top video"], [r[1] for r in rows]))
    return "\n".join(out)


def agreement_table(flagged):
    """Terms flagged in the same direction in 2+ independent places or 2+ sources: a stronger hint than
    one series. India, Mumbai and Global count as one place for this purpose."""
    groups = {}
    for sg in flagged:
        g = groups.setdefault((sg["term"].casefold(), sg["direction"]),
                              {"term": sg["term"], "markets": [], "sources": []})
        if sg["market"] not in g["markets"]:
            g["markets"].append(sg["market"])
        name = SOURCE_NAME[sg["source"]]
        if name not in g["sources"]:
            g["sources"].append(name)

    def places(g):
        return len({region(m) for m in g["markets"] if m != "Global"} or {"Global"})

    picked = [(d, g) for (_, d), g in groups.items() if places(g) >= 2 or len(g["sources"]) >= 2]
    picked.sort(key=lambda dg: (-places(dg[1]), -len(dg[1]["sources"]), dg[1]["term"]))
    if not picked:
        return None
    return md_table(["Term", "Direction", "Where", "Sources"],
                    [[g["term"], d, ", ".join(g["markets"]), ", ".join(g["sources"])] for d, g in picked])


def merge_global(rows, key):
    """Where Global shows the same move as India, drop the Global row and label India 'India + Global'."""
    india = {key(s): s for s in rows if s["market"] == "India"}
    out = []
    for s in rows:
        twin = india.get(key(s)) if s["market"] == "Global" else None
        if twin is not None:
            twin["market_label"] = "India + Global"
            continue
        out.append(s)
    return out


def identical_series_note(conn):
    """Google Trends: terms whose daily values are exactly the same in India and Global. Global then adds
    nothing for that term (either India dominates it, or the region was not applied)."""
    rows = conn.execute(
        "SELECT market, term, date, value FROM metrics WHERE source='google_trends' "
        "AND metric='interest_index' AND market IN ('India','Global') ORDER BY date DESC").fetchall()
    by = {}
    for m, t, d, v in rows:
        by.setdefault((t, m), {})[d] = v
    both = [t for (t, m) in by if m == "India" and (t, "Global") in by]
    same = [t for t in both
            if (lambda a, b: len(set(a) & set(b)) >= 14 and all(a[d] == b[d] for d in set(a) & set(b)))(
                by[(t, "India")], by[(t, "Global")])]
    if not both or not same:
        return None
    return (f"**India vs Global**: {len(same)} of {len(both)} Google Trends terms have exactly the same daily "
            f"values in both, so Global is not independent evidence for them (India probably dominates those "
            f"searches). Terms: {', '.join(sorted(same)[:12])}{'…' if len(same) > 12 else ''}.")


def coverage(conn, signals, cfg=None):
    lines = []
    search_off = (cfg or {}).get("youtube", {}).get("keyword_search") is False
    for source in PRIMARY:
        if source == "youtube" and search_off:
            continue
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
    note = identical_series_note(conn)
    if note:
        lines.append("- " + note)
    return lines


def render_brief(conn, cfg, signals, today):
    acfg = cfg["analysis"]
    top_n = int(acfg.get("top_n", 10))
    everything = [s for s in signals if not s["stale"]]
    settling = [s for s in everything if s.get("rolloff")]
    live = [s for s in everything if not s.get("rolloff")]
    flagged = [s for s in live if s["flagged"]]
    focus = list(acfg.get("focus_markets", []))
    theme_of = theme_lookup(cfg)

    def lead(sg):                       # focus markets (India first) are listed before the others
        return 0 if sg["market"] in focus else 1

    shown = merge_global(sorted(flagged, key=lambda s: s["market"] != "India"),
                         lambda s: (s["source"], s["term"].casefold(), s["direction"]))
    rising = sorted([s for s in shown if s["direction"] == "rising"],
                    key=lambda s: (lead(s), -s["z"]))[:top_n]
    cooling = sorted([s for s in shown if s["direction"] == "cooling"],
                     key=lambda s: (lead(s), s["z"]))[:top_n]

    latest = max((s["last_date"] for s in live), default=today.isoformat())
    header = (
        f"# Trend brief, {today.isoformat()}\n\n"
        f"Markets: {', '.join(m['name'] for m in cfg['markets'])}. "
        f"Core terms: {', '.join(cfg['keywords'])}, plus local-language terms and "
        f"{sum(len(th['terms']) for th in cfg.get('themes', []))} theme terms in {len(cfg.get('themes', []))} themes. "
        + (f"{' and '.join(focus)} come first in the tables; the other markets are leading indicators. "
           if focus else "")
        + f"Latest data point: {latest}.\n\n"
        "*This week* is the 7-day average ending on each source's latest complete day; *last week* is "
        "the 7 days before it. A signal is flagged only when it is statistically unusual against its own "
        "history (|z| ≥ "
        f"{acfg['z_threshold']}), has moved at least {acfg['min_change']:.0%}, and is not a tiny number.\n"
    )

    body = []
    body.append("## Rising\n")
    body.append(md_table(SIG_HEADERS, [sig_row(s, theme_of) for s in rising]) if rising
                else "Nothing flagged as rising this week.")
    body.append("\n## Cooling\n")
    body.append(md_table(SIG_HEADERS, [sig_row(s, theme_of) for s in cooling]) if cooling
                else "Nothing flagged as cooling this week.")

    body.append("\n## Where signals agree\n")
    agree = agreement_table(flagged)
    body.append(
        "The same term flagged the same way in two or more markets or sources, which is a stronger hint "
        "than a single series.\n\n" + agree if agree
        else "No term is flagged in more than one market or source this week.")

    themes = theme_tables(everything, cfg)
    if themes:
        body.append("\n## Themes (India first)\n")
        body.append("Week-over-week change in search interest and news coverage for each theme term, in India "
                    "and worldwide. ▲ / ▼ = flagged. **no data** = nothing returned, usually because the term "
                    "is too rare; **low volume** = too few to read. Single weeks are noisy, so look for themes "
                    "that rise in both search and news, or in India and Global together.")
        for name, table in themes:
            body.append(f"\n### {name}\n")
            body.append(table)

    unflagged = [s for s in live if not s["flagged"] and s["wow"] is not None and not s["low_level"]
                 and (s["z"] is None or abs(s["z"]) >= 1.0)]   # |z| < 1 is within the series' normal wobble
    unflagged = merge_global(sorted(unflagged, key=lambda s: s["market"] != "India"),
                             lambda s: (s["source"], s["term"].casefold(), s["wow"] > 0))
    movers = sorted(unflagged, key=lambda s: -abs(s["wow"]))[:8]
    if movers:
        body.append("\n## Largest other changes (not statistically confirmed; treat as directional)\n")
        body.append(md_table(SIG_HEADERS, [sig_row(s, theme_of) for s in movers]))

    grid = trends_grid(everything, cfg)
    if grid:
        body.append("\n## Google search interest, week over week by market\n")
        body.append(grid)
        body.append("\n▲ / ▼ = flagged. **·** = term not tracked in that market. **no data** = tracked, but Google "
                    "returned nothing (volume too low). **low volume** = too few searches to read. "
                    "Each term is indexed to its own 90-day peak, so compare "
                    "*changes* across cells, not levels. Mumbai = Maharashtra, New York = New York "
                    "state, London = England (Google has no city-level data here).")

    chs = channel_section(conn, cfg)
    if chs:
        body.append("\n## What watched YouTube channels published\n")
        body.append("Only videos about jewellery are counted: every video must name jewellery or gemstones (for media "
                    "and auction channels, in the title). The column *Jewellery videos of all new* shows how much "
                    "of what a channel published was relevant, which is a signal in itself. Views compare channels "
                    "only loosely (subscriber counts differ hugely); how a channel's week compares with its own "
                    "earlier weeks appears under Rising and Cooling after about five weeks. Check *Found as*: if a "
                    "name is not the official channel, put its handle or channel_id in `config.yaml`.")
        body.append(chs)
    yt = item_table(conn, "youtube", cfg) if cfg.get("youtube", {}).get("keyword_search") is not False else None
    if yt:
        body.append("\n## Open YouTube search: new videos naming the core terms\n")
        body.append("Anyone can publish a video that uses these words, so treat this as a rough scan of the open "
                    "web, not as evidence about luxury buyers. The watched-channel section above is the reliable one.")
        body.append(yt)
    rd = item_table(conn, "reddit", cfg)
    if rd:
        body.append("\n## Top Reddit posts this week\n")
        body.append(rd)

    body.append("\n## Coverage and data quality\n")
    cov = coverage(conn, signals, cfg)
    if settling:
        names = "; ".join(f"{SOURCE_NAME[x['source']]}, {x['market']}, {x['term']}" for x in settling[:4])
        more = f" (and {len(settling) - 4} more)" if len(settling) > 4 else ""
        cov.append(f"- **Set aside**: {len(settling)} series {'is' if len(settling) == 1 else 'are'} settling back after a one-off spike "
                   f"(last week was more than 4x their norm) and are left out of the tables above: {names}{more}")
    body.append("\n".join(cov))
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
    signals = compute_signals(conn, cfg["analysis"], today, allowed=configured_pairs(cfg))
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
              "weeks", "last_date", "flagged", "direction", "low_level", "stale", "rolloff"]
    with open(out / "latest-signals.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(signals, key=lambda s: (s["source"], s["market"], s["term"], s["metric"])))
    print(f"Brief written to {out / 'latest.md'} ({sum(s['flagged'] for s in signals)} flagged signals)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

