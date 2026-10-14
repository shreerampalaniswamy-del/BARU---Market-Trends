#!/usr/bin/env python3
"""Collect trend signals from free sources into data/trends.db.

Sources: Google Trends (pytrends), Wikipedia pageviews, YouTube Data API, Reddit API.
Each source is independent: a failure or a missing key in one never blocks the others.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
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


def is_quota(r):
    """YouTube reports a spent daily quota as HTTP 403 quotaExceeded or as HTTP 429 'Quota exceeded'."""
    text = getattr(r, "text", "") or ""
    return r.status_code in (403, 429) and any(k in text for k in ("quotaExceeded", "Quota exceeded", "RESOURCE_EXHAUSTED"))


def get_json(url, params=None, headers=None, tries=3, wait=5):
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
            if is_quota(r):
                return r                      # a spent daily quota will not clear in seconds: do not retry
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}: " + r.text.strip().replace("\n", " ")[:80]
                time.sleep(wait * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:
            last = str(e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(last or "request failed")


def all_terms(cfg, market, include_themes=True):
    """Core keywords + this market's local terms + the terms of every theme scoped to this market."""
    terms = list(cfg["keywords"]) + list(market.get("local_terms", []))
    for theme in cfg.get("themes", []):
        if include_themes and market["name"] in theme.get("markets", []):
            terms += [t for t in theme["terms"] if t not in terms]
    return terms


def ordered_markets(cfg):
    """Focus markets (India first) are collected first, so a rate limit cuts off the least important ones."""
    rank = {name: i for i, name in enumerate(cfg.get("analysis", {}).get("focus_markets", []))}
    return sorted(cfg["markets"], key=lambda m: rank.get(m["name"], len(rank)))


def recent_status(conn, source, market, term, days, tag=None):
    """'ok' or 'empty' if this series was really collected within the last `days` days, else None.

    Lets a re-run fetch only what is missing instead of hitting the rate limits all over again.
    """
    if not days or days <= 0:
        return None
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        "SELECT status FROM runs WHERE source=? AND market=? AND term=? AND status IN ('ok','empty') "
        "AND run_ts>=? AND message NOT LIKE 'not refetched%' AND message LIKE ? ORDER BY run_ts DESC LIMIT 1",
        (source, market, term, cutoff, f"%{tag}%" if tag else "%")).fetchone()
    return row[0] if row else None


def youtube_terms(cfg, market):
    """Terms searched on YouTube in a market. Theme terms only where youtube.theme_markets says so."""
    only = cfg.get("youtube", {}).get("theme_markets")      # None = themes everywhere; [] = nowhere
    return all_terms(cfg, market, include_themes=(only is None or market["name"] in only))


def filter_signature(ycfg):
    keys = ("window_days", "exact_phrase", "require_phrase_in_text", "context_terms", "context_words",
            "art_terms", "art_words", "exclude_words")
    blob = json.dumps({k: ycfg.get(k) for k in keys}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:8]


def channel_signature(ycfg):
    blob = json.dumps({"w": ycfg.get("channel_window_days"), "r": ycfg.get("channel_relevance")},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:8]


def drop_items(conn, source, market, term):
    conn.execute("DELETE FROM items WHERE source=? AND market=? AND term=?", (source, market, term))


def word_patterns(words):
    """Plain words from config -> case-insensitive regexes. Latin words match from a word start
    ('jewel' matches 'jewellery'); words of 4 letters or fewer match whole words only ('ring' not 'string')."""
    pats = []
    for w in words or []:
        w = str(w)
        if w.isascii() and " " not in w:
            pats.append(re.compile(r"\b" + re.escape(w) + (r"s?\b" if len(w) <= 4 else ""), re.I))
        else:
            pats.append(re.compile(re.escape(w), re.I))
    return pats


def kept_message(days):
    return f"not refetched (collected within {days} days)"


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
    fresh_days = int(tcfg.get("skip_if_fresh_days", 3))
    # Do not pass retries/backoff_factor: they break against current urllib3.
    pt = TrendReq(hl="en-US", tz=0, timeout=(10, 30))

    jobs = [(m, t) for m in ordered_markets(cfg) for t in all_terms(cfg, m)]
    stored = 0
    consecutive_errors = 0
    for i, (m, term) in enumerate(jobs):
        prior = recent_status(conn, "google_trends", m["name"], term, fresh_days)
        if prior:
            log(conn, run_ts, "google_trends", m["name"], term, prior, kept_message(fresh_days))
            continue
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
                last = df[term].iloc[-2] if len(df) > 1 else df[term].iloc[-1]
                log(conn, run_ts, "google_trends", m["name"], term, "ok",
                    f"{n} days (geo {m['trends_geo'] or 'worldwide'}, recent value {last})")
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
    """Snapshot of the most-viewed videos published in the last N days, per term and market.

    The search asks YouTube for the exact phrase, and each result is then kept only if its title or
    description really contains the term, so unrelated videos (a basketball player for "High Art",
    toy merchandise for "High Jewelry") do not inflate the numbers. Metrics are stored as
    matched_* so their history starts clean and is never mixed with the older unfiltered numbers.
    Region/language are relevance hints, so markets sharing a language will overlap heavily.
    """
    ycfg = cfg.get("youtube", {})
    if ycfg.get("keyword_search") is False:
        log(conn, run_ts, "youtube", None, None, "skipped", "keyword search is switched off (youtube.keyword_search)")
        return 0
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        log(conn, run_ts, "youtube", None, None, "skipped", "YOUTUBE_API_KEY not set")
        return 0

    days = int(ycfg.get("window_days", 7))
    exact = bool(ycfg.get("exact_phrase", True))
    must_name = bool(ycfg.get("require_phrase_in_text", True))
    fresh_days = int(ycfg.get("skip_if_fresh_days", 3))
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = dt.date.today().isoformat()
    base = "https://www.googleapis.com/youtube/v3"
    # A fingerprint of every setting that changes which videos count. A series collected under different
    # settings is never treated as fresh, so changing the filters always re-collects with the new rules.
    sig = filter_signature(ycfg)
    tag = f"[f:{sig}]"
    need_jewel, need_art = set(ycfg.get("context_terms", [])), set(ycfg.get("art_terms", []))
    jewel_pats, art_pats = word_patterns(ycfg.get("context_words")), word_patterns(ycfg.get("art_words"))
    excluded = [w.casefold() for w in ycfg.get("exclude_words", [])]
    stored = 0

    for m in ordered_markets(cfg):
        for term in youtube_terms(cfg, m):
            prior = recent_status(conn, "youtube", m["name"], term, fresh_days, tag)
            if prior:
                log(conn, run_ts, "youtube", m["name"], term, prior, kept_message(fresh_days))
                continue
            try:
                search_params = {
                    "part": "snippet", "q": (f'"{term}"' if exact else term), "type": "video",
                    "order": "viewCount", "publishedAfter": since, "maxResults": 50, "key": key,
                }
                if m.get("youtube_region"):          # the Global market has no region or language hint
                    search_params["regionCode"] = m["youtube_region"]
                if m.get("youtube_lang"):
                    search_params["relevanceLanguage"] = m["youtube_lang"]
                s = get_json(f"{base}/search", params=search_params)
                if is_quota(s):
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
                    drop_items(conn, "youtube", m["name"], term)
                    log(conn, run_ts, "youtube", m["name"], term, "empty", f"no videos in window {tag}")
                    continue
                v = get_json(f"{base}/videos", params={
                    "part": "snippet,statistics", "id": ",".join(ids), "key": key,
                })
                if is_quota(v):
                    log(conn, run_ts, "youtube", None, None, "error",
                        "daily quota exhausted; stopping YouTube for this run")
                    return stored
                if v.status_code != 200:
                    log(conn, run_ts, "youtube", m["name"], term, "error", f"videos HTTP {v.status_code}")
                    continue
                vids = v.json().get("items", [])
                if not vids:
                    drop_items(conn, "youtube", m["name"], term)
                    log(conn, run_ts, "youtube", m["name"], term, "empty", f"no video details {tag}")
                    continue

                def stat(x, name):
                    return int(x.get("statistics", {}).get(name, 0) or 0)

                needle = term.casefold()

                def names_term(x):
                    sn = x.get("snippet", {})
                    text = f"{sn.get('title', '')} {sn.get('description', '')}".casefold()
                    if needle not in text or any(w in text for w in excluded):
                        return False
                    if term in need_jewel and not any(p.search(text) for p in jewel_pats):
                        return False
                    if term in need_art and not any(p.search(text) for p in art_pats):
                        return False
                    return True

                matched = [x for x in vids if names_term(x)] if must_name else list(vids)
                views = [stat(x, "viewCount") for x in matched] or [0]
                for metric, value in (
                    ("matched_views_sum", sum(views)),
                    ("matched_views_median", statistics.median(views)),
                    ("matched_likes_sum", sum(stat(x, "likeCount") for x in matched)),
                    ("matched_comments_sum", sum(stat(x, "commentCount") for x in matched)),
                    ("matched_videos", len(matched)),
                    ("returned_videos", len(vids)),
                    ("results_est", sj.get("pageInfo", {}).get("totalResults", 0)),
                ):
                    put_metric(conn, run_ts, today, "youtube", m["name"], term, metric, value)
                top = sorted(matched, key=lambda x: stat(x, "viewCount"), reverse=True)[:5]
                drop_items(conn, "youtube", m["name"], term)      # replace, never mix with an older collection
                for x in top:
                    sn = x.get("snippet", {})
                    put_item(conn, run_ts, "youtube", m["name"], term,
                             f"https://www.youtube.com/watch?v={x['id']}", sn.get("title"),
                             sn.get("channelTitle"), sn.get("publishedAt"),
                             stat(x, "viewCount"), stat(x, "likeCount") + stat(x, "commentCount"))
                conn.commit()
                stored += len(top)
                if matched:
                    log(conn, run_ts, "youtube", m["name"], term, "ok",
                        f"{len(matched)} of {len(vids)} videos name the term {tag}")
                else:
                    log(conn, run_ts, "youtube", m["name"], term, "empty",
                        f"{len(vids)} videos returned, none name the term {tag}")
            except Exception as e:
                log(conn, run_ts, "youtube", m["name"], term, "error", f"{type(e).__name__}: {e}"[:200])
    return stored


# ---------------------------------------------------------------- YouTube channels

class QuotaExhausted(Exception):
    pass


def collect_channels(conn, cfg, run_ts):
    """What a curated list of channels (houses, publications, auction houses) published recently.

    This is the trustworthy YouTube view: the source is chosen by us, not by whoever mentions a keyword.
    It is also cheap (about 3 quota units per channel; a keyword search costs 100). Channels are found by
    handle or channel id when given, otherwise by name once and then remembered. The brief shows what each
    one resolved to so a wrong match is easy to spot.
    """
    ycfg = cfg.get("youtube", {})
    channels = ycfg.get("channels") or []
    if not channels:
        return 0
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        log(conn, run_ts, "youtube_channel", None, None, "skipped", "YOUTUBE_API_KEY not set")
        return 0

    window = int(ycfg.get("channel_window_days", 14))
    fresh_days = int(ycfg.get("skip_if_fresh_days", 3))
    lookups = int(ycfg.get("max_name_lookups", 20))      # name searches cost 100 units each
    rel = ycfg.get("channel_relevance") or {}
    rel_pats = word_patterns(rel.get("words"))
    title_only = set(rel.get("title_only_groups", []))
    tag = f"[f:{channel_signature(ycfg)}]"             # changes whenever the relevance rules change

    def relevant(v, group):
        """Only videos about jewellery count. Media and auction houses must say so in the TITLE."""
        if not rel_pats:
            return True
        sn = v.get("snippet", {})
        title = (sn.get("title") or "").casefold()
        text = title if group in title_only else f"{title} {(sn.get('description') or '').casefold()}"
        return any(p.search(text) for p in rel_pats)

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = dt.date.today().isoformat()
    base = "https://www.googleapis.com/youtube/v3"
    stored = 0

    def call(path, **params):
        r = get_json(f"{base}/{path}", params={**params, "key": key})
        if is_quota(r):
            raise QuotaExhausted()
        if r.status_code != 200:
            raise RuntimeError(f"{path} HTTP {r.status_code}: {r.text.strip()[:100]}")
        return r.json()

    for ch in channels:
        name, group = ch["name"], ch.get("group", "Other")
        prior = recent_status(conn, "youtube_channel", group, name, fresh_days, tag)
        if prior:
            log(conn, run_ts, "youtube_channel", group, name, prior, kept_message(fresh_days))
            continue
        try:
            spec = str(ch.get("channel_id") or ch.get("handle") or "")
            row = conn.execute("SELECT channel_id, title, uploads, spec FROM channels WHERE name=?",
                               (name,)).fetchone()
            if row and row[3] == spec and row[2]:
                channel_id, title, uploads = row[0], row[1], row[2]
            else:
                found = None
                if ch.get("channel_id"):
                    found = call("channels", part="snippet,statistics,contentDetails", id=ch["channel_id"])
                elif ch.get("handle"):
                    found = call("channels", part="snippet,statistics,contentDetails", forHandle=ch["handle"])
                items = (found or {}).get("items") or []
                if not items and ch.get("channel_id"):
                    log(conn, run_ts, "youtube_channel", group, name, "error",
                        "channel_id not found; check it in config.yaml")
                    continue
                if not items:                                  # no handle, or a handle that missed: search the name
                    miss_cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    missed = conn.execute(
                        "SELECT 1 FROM runs WHERE source='youtube_channel' AND market=? AND term=? "
                        "AND message LIKE 'not found by name%' AND run_ts>=?", (group, name, miss_cutoff)).fetchone()
                    if missed:
                        log(conn, run_ts, "youtube_channel", group, name, "error",
                            "not found by name recently; add its handle or channel_id in config.yaml")
                        continue
                    if lookups <= 0:
                        log(conn, run_ts, "youtube_channel", group, name, "error",
                            "name lookup limit reached for this run; will retry next run")
                        continue
                    lookups -= 1
                    hit = call("search", part="snippet", type="channel", q=ch.get("query") or name, maxResults=1)
                    cid = ((hit.get("items") or [{}])[0].get("id") or {}).get("channelId")
                    if cid:
                        items = call("channels", part="snippet,statistics,contentDetails", id=cid).get("items") or []
                    if not items:
                        log(conn, run_ts, "youtube_channel", group, name, "error",
                            "not found by name; add its handle or channel_id in config.yaml")
                        continue
                it = items[0]
                channel_id, title = it["id"], it.get("snippet", {}).get("title", "")
                uploads = it.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
                conn.execute(
                    "INSERT OR REPLACE INTO channels VALUES (?,?,?,?,?,?,?,?)",
                    (name, spec, channel_id, title, it.get("snippet", {}).get("customUrl", ""),
                     float(it.get("statistics", {}).get("subscriberCount", 0) or 0), uploads, run_ts))
            if not uploads:
                log(conn, run_ts, "youtube_channel", group, name, "error", "channel has no uploads playlist")
                continue

            pl = call("playlistItems", part="contentDetails", playlistId=uploads, maxResults=50)
            recent = [i["contentDetails"]["videoId"] for i in pl.get("items", [])
                      if (i.get("contentDetails", {}).get("videoPublishedAt") or "") >= since]
            allvids = []
            if recent:
                allvids = call("videos", part="snippet,statistics", id=",".join(recent[:50])).get("items", [])
            vids = [v for v in allvids if relevant(v, group)]
            views = [int(v.get("statistics", {}).get("viewCount", 0) or 0) for v in vids] or [0]
            chan = call("channels", part="statistics", id=channel_id).get("items") or []
            subs = float((chan[0].get("statistics", {}) if chan else {}).get("subscriberCount", 0) or 0)
            conn.execute("UPDATE channels SET subscribers=? WHERE name=?", (subs, name))
            for metric, value in (("videos_posted", len(vids)), ("videos_scanned", len(allvids)),
                                  ("views_sum", sum(views)),
                                  ("views_median", statistics.median(views)), ("subscribers", subs)):
                put_metric(conn, run_ts, today, "youtube_channel", group, name, metric, value)
            drop_items(conn, "youtube_channel", group, name)
            for v in sorted(vids, key=lambda x: int(x.get("statistics", {}).get("viewCount", 0) or 0),
                            reverse=True)[:3]:
                sn, st = v.get("snippet", {}), v.get("statistics", {})
                put_item(conn, run_ts, "youtube_channel", group, name,
                         f"https://www.youtube.com/watch?v={v['id']}", sn.get("title"), sn.get("channelTitle"),
                         sn.get("publishedAt"), int(st.get("viewCount", 0) or 0),
                         int(st.get("likeCount", 0) or 0) + int(st.get("commentCount", 0) or 0))
            conn.commit()
            stored += len(vids)
            log(conn, run_ts, "youtube_channel", group, name, "ok",
                f"{len(vids)} jewellery videos of {len(allvids)} in {window} days (found as '{title}') {tag}")
        except QuotaExhausted:
            log(conn, run_ts, "youtube_channel", None, None, "error",
                "daily quota exhausted; stopping YouTube channels for this run")
            return stored
        except Exception as e:
            log(conn, run_ts, "youtube_channel", group, name, "error", f"{type(e).__name__}: {e}"[:200])
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
    Markets are filtered by the PUBLISHER's country, not the country an article is about. A market
    whose gdelt_country is empty (Global) has no filter; a market without the key is left out.
    """
    gcfg = cfg.get("gdelt", {})
    if not gcfg.get("enabled", True):
        log(conn, run_ts, "gdelt", None, None, "skipped", "disabled in config.yaml")
        return 0
    timespan = gcfg.get("timespan", "3months")
    pause = float(gcfg.get("sleep_seconds", 6))  # GDELT asks for at most one request per 5 seconds
    fresh_days = int(gcfg.get("skip_if_fresh_days", 3))
    today = dt.datetime.now(dt.timezone.utc).date()

    jobs = []
    for m in ordered_markets(cfg):
        if "gdelt_country" not in m:
            continue                              # this market is not covered by GDELT
        country = m["gdelt_country"]              # "" means worldwide, no country filter
        suffix = f" sourcecountry:{country}" if country else ""
        jobs += [(m["name"], t, f'"{t}"{suffix}') for t in all_terms(cfg, m)]

    stored, consecutive_errors = 0, 0
    for i, (market, term, query) in enumerate(jobs):
        prior = recent_status(conn, "gdelt", market, term, fresh_days)
        if prior:
            log(conn, run_ts, "gdelt", market, term, prior, kept_message(fresh_days))
            continue
        err, points = "", []
        try:
            r = get_json(GDELT_URL, params={
                "query": query, "mode": "timelinevolraw", "format": "json", "timespan": timespan,
            }, wait=float(gcfg.get("retry_wait_seconds", 20)))
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
    "channels": collect_channels,
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
    ok = conn.execute("SELECT COUNT(*) FROM runs WHERE run_ts=? AND status='ok'", (run_ts,)).fetchone()[0]
    print(f"Done: {total} data points stored, {ok} series ok.")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
