#!/usr/bin/env python3
"""Collect trend signals from free sources into data/trends.db.

Sources: Google Trends (pytrends), Wikipedia pageviews, YouTube Data API, Reddit API.
Each source is independent: a failure or a missing key in one never blocks the others.
"""
import argparse
import datetime as dt
import os
import random
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

from common import DEFAULT_CONFIG, DEFAULT_DB, load_config, open_db, scrub

UA = os.environ.get("TREND_AGENT_UA", "trend-agent/0.1 (personal research project)")


# ---------------------------------------------------------------- storage helpers

def put_metric(conn, run_ts, date, source, market, term, metric, value):
    conn.execute(
        "INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?)",
        (date, source, market, term, metric, float(value), run_ts),
    )


def put_item(conn, run_ts, source, market, term, url, title, author, published, views, engagement):
    conn.execute(
        "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source, market, term, url, title, author, published, views, engagement, run_ts),
    )


def log(conn, run_ts, source, market, term, status, message=""):
    message = scrub(message)
    conn.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?)", (run_ts, source, market, term, status, message)
    )
    line = f"[{source}] {market or '-'} / {term or '-'}: {status}"
    print(line + (f" ({message})" if message else ""), flush=True)


def get_json(url, params=None, headers=None, tries=3):
    """GET with retry on rate limits / transient errors. Returns the Response."""
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(
                url,
                params=params,
                headers={"User-Agent": UA, **(headers or {})},
                timeout=30,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(5 * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:
            last = str(e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(last or "request failed")


def all_terms(cfg, market):
    return list(cfg["keywords"]) + list(market.get("local_terms", []))


# ---------------------------------------------------------------- Google Trends

def collect_trends(conn, cfg, run_ts):
    """Daily search-interest index (0-100) per term and market.

    Each term is requested on its own, so each series is indexed to its own peak in the window.
    That is right for spotting change; it is NOT comparable across terms or markets in level.
    """
    try:
        from pytrends.request import TrendReq
    except ImportError:
        log(conn, run_ts, "google_trends", None, None, "error", "pytrends not installed")
        return 0

    tcfg = cfg.get("trends", {})
    timeframe = tcfg.get("timeframe", "today 3-m")
    pause = float(tcfg.get("sleep_seconds", 8))
    # Do not pass retries/backoff_factor: they break against current urllib3.
    pt = TrendReq(hl="en-US", tz=0, timeout=(10, 30))

    jobs = [(m, t) for m in cfg["markets"] for t in all_terms(cfg, m)]
    stored = 0
    consecutive_errors = 0
    for i, (m, term) in enumerate(jobs):
        df, err = None, ""
        for attempt in range(3):
            try:
                pt.build_payload([term], timeframe=timeframe, geo=m["trends_geo"])
                df = pt.interest_over_time()
                err = ""
                break
            except Exception as e:  # pytrends raises generic errors on 429
                err = f"{type(e).__name__}: {e}"
                time.sleep(30 * (attempt + 1))

        if err:
            consecutive_errors += 1
            log(conn, run_ts, "google_trends", m["name"], term, "error", err[:200])
            if consecutive_errors >= 3:
                log(conn, run_ts, "google_trends", None, None, "error",
                    "3 failures in a row (likely rate-limited); stopping Google Trends for this run")
                break
        else:
            consecutive_errors = 0
            if df is None or df.empty or term not in df.columns:
                log(conn, run_ts, "google_trends", m["name"], term, "empty",
                    "no search interest returned (volume too low)")
            else:
                n = 0
                for ts, row in df.iterrows():
                    if "isPartial" in df.columns and bool(row["isPartial"]):
                        continue
                    put_metric(conn, run_ts, ts.date().isoformat(), "google_trends",
                               m["name"], term, "interest_index", row[term])
                    n += 1
                conn.commit()
                stored += n
                log(conn, run_ts, "google_trends", m["name"], term, "ok", f"{n} days")
        if i < len(jobs) - 1:
            time.sleep(pause + random.uniform(0, 4))
    return stored


# ---------------------------------------------------------------- Wikipedia

def collect_wikipedia(conn, cfg, run_ts):
    """Daily pageviews (human traffic) per article. No key needed."""
    wcfg = cfg.get("wikipedia", {})
    days = int(wcfg.get("lookback_days", 90))
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    stored = 0
    for a in wcfg.get("articles", []):
        label = a["article"].replace("_", " ")
        url = (
            "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
            f"{a['project']}/all-access/user/{quote(a['article'], safe='')}/daily/"
            f"{start:%Y%m%d}00/{end:%Y%m%d}00"
        )
        try:
            r = get_json(url)
        except Exception as e:
            log(conn, run_ts, "wikipedia", a["market"], label, "error", str(e)[:200])
            continue
        if r.status_code == 404:
            log(conn, run_ts, "wikipedia", a["market"], label, "empty",
                "article not found or no views; check the title in config.yaml")
            continue
        if r.status_code != 200:
            log(conn, run_ts, "wikipedia", a["market"], label, "error", f"HTTP {r.status_code}")
            continue
        views = {it["timestamp"][:8]: it["views"] for it in r.json().get("items", [])}
        if not views:
            log(conn, run_ts, "wikipedia", a["market"], label, "empty", "no items returned")
            continue
        last_day = dt.datetime.strptime(max(views), "%Y%m%d").date()
        d, n = start, 0
        while d <= last_day:
            # The API omits days with zero views; treat them as 0, but never past the last returned day.
            put_metric(conn, run_ts, d.isoformat(), "wikipedia", a["market"], label,
                       "pageviews", views.get(f"{d:%Y%m%d}", 0))
            d += dt.timedelta(days=1)
            n += 1
        conn.commit()
        stored += n
        log(conn, run_ts, "wikipedia", a["market"], label, "ok", f"{n} days")
    return stored


# ---------------------------------------------------------------- YouTube

def collect_youtube(conn, cfg, run_ts):
    """Snapshot of the top-50 most-viewed videos published in the last N days, per term and market.

    Region/language are relevance hints, so markets sharing a language will overlap heavily.
    """
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        log(conn, run_ts, "youtube", None, None, "skipped", "YOUTUBE_API_KEY not set")
        return 0

    days = int(cfg.get("youtube", {}).get("window_days", 7))
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = dt.date.today().isoformat()
    base = "https://www.googleapis.com/youtube/v3"
    stored = 0

    for m in cfg["markets"]:
        for term in all_terms(cfg, m):
            try:
                s = get_json(f"{base}/search", params={
                    "part": "snippet", "q": term, "type": "video", "order": "viewCount",
                    "publishedAfter": since, "regionCode": m["youtube_region"],
                    "relevanceLanguage": m["youtube_lang"], "maxResults": 50, "key": key,
                })
                if s.status_code == 403 and "quotaExceeded" in s.text:
                    log(conn, run_ts, "youtube", None, None, "error",
                        "daily quota exhausted; stopping YouTube for this run")
                    return stored
                if s.status_code != 200:
                    log(conn, run_ts, "youtube", m["name"], term, "error",
                        f"HTTP {s.status_code}: {s.text[:120]}")
                    continue
                sj = s.json()
                ids = [it["id"]["videoId"] for it in sj.get("items", [])
                       if it.get("id", {}).get("videoId")]
                if not ids:
                    log(conn, run_ts, "youtube", m["name"], term, "empty", "no videos in window")
                    continue
                v = get_json(f"{base}/videos", params={
                    "part": "snippet,statistics", "id": ",".join(ids), "key": key,
                })
                if v.status_code != 200:
                    log(conn, run_ts, "youtube", m["name"], term, "error", f"videos HTTP {v.status_code}")
                    continue
                vids = v.json().get("items", [])
                if not vids:
                    log(conn, run_ts, "youtube", m["name"], term, "empty", "no video details")
                    continue

                def stat(x, name):
                    return int(x.get("statistics", {}).get(name, 0) or 0)

                views = [stat(x, "viewCount") for x in vids]
                likes = sum(stat(x, "likeCount") for x in vids)
                comments = sum(stat(x, "commentCount") for x in vids)
                for metric, value in (
                    ("views_sum", sum(views)),
                    ("views_median", statistics.median(views)),
                    ("likes_sum", likes),
                    ("comments_sum", comments),
                    ("results_est", sj.get("pageInfo", {}).get("totalResults", 0)),
                ):
                    put_metric(conn, run_ts, today, "youtube", m["name"], term, metric, value)
                for x in sorted(vids, key=lambda x: stat(x, "viewCount"), reverse=True)[:5]:
                    sn = x.get("snippet", {})
                    put_item(conn, run_ts, "youtube", m["name"], term,
                             f"https://www.youtube.com/watch?v={x['id']}", sn.get("title"),
                             sn.get("channelTitle"), sn.get("publishedAt"),
                             stat(x, "viewCount"), stat(x, "likeCount") + stat(x, "commentCount"))
                conn.commit()
                stored += 5
                log(conn, run_ts, "youtube", m["name"], term, "ok", f"{len(vids)} videos")
            except Exception as e:
                log(conn, run_ts, "youtube", m["name"], term, "error", f"{type(e).__name__}: {e}"[:200])
    return stored


# ---------------------------------------------------------------- Reddit

def collect_reddit(conn, cfg, run_ts):
    """Posts mentioning each keyword in the last week (global; Reddit has no geography)."""
    cid = os.environ.get("REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        log(conn, run_ts, "reddit", None, None, "skipped", "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set")
        return 0
    try:
        tok = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(cid, secret), data={"grant_type": "client_credentials"},
            headers={"User-Agent": UA}, timeout=30,
        )
        tok.raise_for_status()
        headers = {"Authorization": f"bearer {tok.json()['access_token']}"}
    except Exception as e:
        log(conn, run_ts, "reddit", None, None, "error", f"auth failed: {type(e).__name__}: {e}"[:200])
        return 0

    today = dt.date.today().isoformat()
    stored = 0
    for term in cfg["keywords"]:
        posts, after = [], None
        try:
            for _ in range(3):  # up to 300 posts
                params = {"q": f'"{term}"', "sort": "new", "t": "week", "limit": 100, "type": "link"}
                if after:
                    params["after"] = after
                r = get_json("https://oauth.reddit.com/search", params=params, headers=headers)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                data = r.json()["data"]
                posts += [c["data"] for c in data["children"]]
                after = data.get("after")
                if not after:
                    break
                time.sleep(1)
        except Exception as e:
            log(conn, run_ts, "reddit", "Global", term, "error", f"{type(e).__name__}: {e}"[:200])
            continue
        score = sum(int(p.get("score", 0)) for p in posts)
        comments = sum(int(p.get("num_comments", 0)) for p in posts)
        for metric, value in (("posts_7d", len(posts)), ("score_sum", score),
                              ("comments_sum", comments), ("engagement", score + comments)):
            put_metric(conn, run_ts, today, "reddit", "Global", term, metric, value)
        top = sorted(posts, key=lambda p: int(p.get("score", 0)) + int(p.get("num_comments", 0)),
                     reverse=True)[:5]
        for p in top:
            created = dt.datetime.fromtimestamp(p.get("created_utc", 0), dt.timezone.utc).isoformat()
            put_item(conn, run_ts, "reddit", "Global", term,
                     "https://www.reddit.com" + p.get("permalink", ""), p.get("title"),
                     "r/" + str(p.get("subreddit", "")), created, None,
                     int(p.get("score", 0)) + int(p.get("num_comments", 0)))
        conn.commit()
        stored += 4
        log(conn, run_ts, "reddit", "Global", term, "ok", f"{len(posts)} posts")
    return stored


# ---------------------------------------------------------------- GDELT

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def collect_gdelt(conn, cfg, run_ts):
    """Daily count of news articles mentioning each term (GDELT DOC 2.0 API, no key needed).

    'articles' is the raw daily count. 'article_share_pct' is that count as a percentage of all
    articles GDELT monitored that day, which corrects for GDELT's own volume drifting over time.
    Markets are filtered by the PUBLISHER's country, not the country an article is about.
    A 'Global' series (no country filter) is added for each keyword, because English phrases
    rarely appear in French, Italian or Hong Kong publications.
    """
    gcfg = cfg.get("gdelt", {})
    if not gcfg.get("enabled", True):
        log(conn, run_ts, "gdelt", None, None, "skipped", "disabled in config.yaml")
        return 0
    timespan = gcfg.get("timespan", "3months")
    pause = float(gcfg.get("sleep_seconds", 6))  # GDELT asks for at most one request per 5 seconds
    today = dt.datetime.now(dt.timezone.utc).date()

    jobs = []
    if gcfg.get("include_global", True):
        jobs += [("Global", t, f'"{t}"') for t in cfg["keywords"]]
    for m in cfg["markets"]:
        country = m.get("gdelt_country")
        if country:
            jobs += [(m["name"], t, f'"{t}" sourcecountry:{country}') for t in all_terms(cfg, m)]

    stored, consecutive_errors = 0, 0
    for i, (market, term, query) in enumerate(jobs):
        err, points = "", []
        try:
            r = get_json(GDELT_URL, params={
                "query": query, "mode": "timelinevolraw", "format": "json", "timespan": timespan,
            })
            if r.status_code != 200:
                err = f"HTTP {r.status_code}"
            elif not r.text.strip():
                points = []  # an empty body means no matching articles
            else:
                try:
                    data = r.json()
                except ValueError:
                    # GDELT answers a bad query with a plain-text message instead of JSON
                    err = "not JSON: " + r.text.strip().replace("\n", " ")[:120]
                else:
                    series = data.get("timeline") or []
                    points = series[0].get("data", []) if series else []
        except Exception as e:
            err = f"{type(e).__name__}: {e}"

        if err:
            consecutive_errors += 1
            log(conn, run_ts, "gdelt", market, term, "error", err[:200])
            if consecutive_errors >= 3:
                log(conn, run_ts, "gdelt", None, None, "error",
                    "3 failures in a row (likely rate-limited); stopping GDELT for this run")
                break
        else:
            consecutive_errors = 0
            n = 0
            for p in points:
                try:
                    day = dt.datetime.strptime(str(p["date"])[:8], "%Y%m%d").date()
                    value = float(p.get("value", 0))
                    norm = float(p.get("norm") or 0)
                except (KeyError, ValueError, TypeError):
                    continue
                if day >= today:
                    continue  # today's bucket is still filling up
                put_metric(conn, run_ts, day.isoformat(), "gdelt", market, term, "articles", value)
                if norm > 0:
                    put_metric(conn, run_ts, day.isoformat(), "gdelt", market, term,
                               "article_share_pct", value / norm * 100)
                n += 1
            conn.commit()
            stored += n
            if n:
                log(conn, run_ts, "gdelt", market, term, "ok", f"{n} days")
            else:
                log(conn, run_ts, "gdelt", market, term, "empty", "no matching articles in window")
        if i < len(jobs) - 1:
            time.sleep(pause + random.uniform(0, 2))
    return stored


# ---------------------------------------------------------------- main

SOURCES = {
    "trends": collect_trends,
    "wikipedia": collect_wikipedia,
    "youtube": collect_youtube,
    "reddit": collect_reddit,
    "gdelt": collect_gdelt,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--sources", default=",".join(SOURCES), help="comma-separated subset")
    args = ap.parse_args()

    cfg = load_config(args.config)
    conn = open_db(Path(args.db))
    run_ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    total = 0
    for name in [s.strip() for s in args.sources.split(",") if s.strip()]:
        fn = SOURCES.get(name)
        if fn is None:
            print(f"Unknown source '{name}'. Choose from: {', '.join(SOURCES)}")
            continue
        try:
            total += fn(conn, cfg, run_ts)
        except Exception as e:
            log(conn, run_ts, name, None, None, "error", f"{type(e).__name__}: {e}"[:200])
        conn.commit()
    print(f"Done: {total} data points stored.")
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
