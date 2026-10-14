"""Checks the Google Trends collector's new behaviour against a faked pytrends: India-first order,
theme terms only in their scoped markets, worldwide scope for Global, skipping fresh series, and a
re-run that fills only the gaps.

Run: python tests/test_trends.py
"""
import datetime as dt
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import collect  # noqa: E402
from common import load_config, open_db  # noqa: E402


class FakeTrendReq:
    calls = []
    fail_terms = set()

    def __init__(self, *args, **kwargs):
        self.term = self.geo = None

    def build_payload(self, kw_list, timeframe=None, geo=""):
        self.term, self.geo = kw_list[0], geo
        FakeTrendReq.calls.append((self.term, geo))
        if self.term in FakeTrendReq.fail_terms:
            raise RuntimeError("429 Too Many Requests")

    def interest_over_time(self):
        idx = pd.date_range(end=dt.date.today(), periods=90)
        return pd.DataFrame({self.term: list(range(90)), "isPartial": [False] * 89 + [True]}, index=idx)


def run(conn, cfg, run_ts):
    FakeTrendReq.calls = []
    with mock.patch("pytrends.request.TrendReq", FakeTrendReq), mock.patch.object(collect.time, "sleep"):
        return collect.collect_trends(conn, cfg, run_ts)


def now_ts(offset_seconds=0):
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    cfg = load_config()
    expected_jobs = sum(len(collect.all_terms(cfg, m)) for m in cfg["markets"])

    with tempfile.TemporaryDirectory() as tmp:
        # 1. normal run
        conn = open_db(Path(tmp) / "t1.db")
        stored = run(conn, cfg, now_ts())
        calls = FakeTrendReq.calls
        assert len(calls) == expected_jobs, (len(calls), expected_jobs)
        assert calls[0] == ("High Jewellery", "IN"), calls[0]                   # India is collected first
        assert ("polki", "IN") in calls and ("polki", "") in calls              # themes: India and worldwide
        assert not any(t == "polki" and g not in ("IN", "") for t, g in calls)  # but not in the other markets
        assert ("haute joaillerie", "FR") in calls and ("haute joaillerie", "IN") not in calls
        n = conn.execute("SELECT COUNT(*) FROM metrics WHERE market='India' AND term='High Jewellery'").fetchone()[0]
        assert n == 89, n                                                       # today's partial day dropped
        assert stored == 89 * expected_jobs, stored

        # 2. an immediate re-run fetches nothing and says so
        stored2 = run(conn, cfg, now_ts(60))
        assert FakeTrendReq.calls == [] and stored2 == 0
        kept = conn.execute("SELECT COUNT(*) FROM runs WHERE message LIKE 'not refetched%'").fetchone()[0]
        assert kept == expected_jobs, kept

        # 3. a rate-limited run stops after three failures in a row instead of hammering Google
        conn3 = open_db(Path(tmp) / "t3.db")
        FakeTrendReq.fail_terms = {c for c in ("High Jewellery", "High Jewelry", "Contemporary Art", "art jewellery")}
        run(conn3, cfg, now_ts())
        assert len(FakeTrendReq.calls) == 9, len(FakeTrendReq.calls)           # 3 jobs x 3 attempts, then stop
        assert conn3.execute("SELECT COUNT(*) FROM runs WHERE message LIKE '%stopping Google Trends%'").fetchone()[0] == 1

        # 4. one failed series is picked up by the next run, and only that one
        conn4 = open_db(Path(tmp) / "t4.db")
        FakeTrendReq.fail_terms = {"Cartier"}
        run(conn4, cfg, now_ts())
        failed = conn4.execute("SELECT market FROM runs WHERE status='error' AND term='Cartier'").fetchall()
        assert sorted(f[0] for f in failed) == ["Global", "India"], failed
        FakeTrendReq.fail_terms = set()
        run(conn4, cfg, now_ts(60))
        assert sorted(FakeTrendReq.calls) == [("Cartier", ""), ("Cartier", "IN")], FakeTrendReq.calls
        assert conn4.execute("SELECT COUNT(*) FROM metrics WHERE term='Cartier'").fetchone()[0] == 2 * 89

    FakeTrendReq.fail_terms = set()
    print("Google Trends checks passed.")


if __name__ == "__main__":
    main()
