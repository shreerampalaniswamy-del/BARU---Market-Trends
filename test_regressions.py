"""Regression checks for problems found in a real brief: absurd z-scores for series that start from zero,
'Rising' rows whose change is negative, retired terms still shown, noise rows, and GDELT 429 handling.

Run: python tests/test_regressions.py
"""
import datetime as dt
import random
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analyze  # noqa: E402
import collect  # noqa: E402
from collect import put_metric  # noqa: E402
from common import load_config, open_db  # noqa: E402


def weekly(conn, source, market, term, metric, weeks_oldest_first, noise=0.0, seed=1):
    """Daily points whose 7-day blocks (oldest first) have the given levels."""
    rng = random.Random(seed)
    last = dt.date.today() - dt.timedelta(days=1)
    n = len(weeks_oldest_first)
    for w, level in enumerate(weeks_oldest_first):
        for d in range(7):
            day = last - dt.timedelta(days=(n - 1 - w) * 7 + (6 - d))
            put_metric(conn, "2026-01-01T00:00:00Z", day.isoformat(), source, market, term, metric,
                       max(0.0, level + rng.gauss(0, noise)))


def find(signals, market, term, source="google_trends"):
    return next(s for s in signals if (s["source"], s["market"], s["term"]) == (source, market, term))


def main():
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "r.db")

        # 1. a series that was zero for 11 weeks and is now 14: no huge z-score, never flagged
        weekly(conn, "google_trends", "France", "High Jewellery", "interest_index", [0] * 11 + [14])
        # 2. still above its older norm but falling week on week: easing, not rising
        weekly(conn, "google_trends", "Italy", "alta gioielleria", "interest_index",
               [10] * 10 + [70, 54], noise=1.0)
        # 3. a term that has since been removed from config.yaml
        weekly(conn, "google_trends", "Hong Kong", "High Art", "interest_index", [9] * 11 + [42])
        # 4. a genuine rise must still be flagged
        weekly(conn, "google_trends", "India", "High Jewellery", "interest_index",
               [40] * 11 + [64], noise=1.5)
        # 5. a low-volume series that mostly sits at 0 and occasionally blips to 14, ending 14 -> 0 (within its own noise): not listed as a top change
        weekly(conn, "google_trends", "New York", "High Jewellery", "interest_index",
               [0, 14, 0, 0, 14, 0, 0, 14, 0, 14, 14, 0], noise=0.0)

        allowed = analyze.configured_pairs(cfg)
        signals = analyze.compute_signals(conn, cfg["analysis"], allowed=allowed)

        fr = find(signals, "France", "High Jewellery")
        assert not fr["flagged"] and (fr["z"] is None or abs(fr["z"]) < 100), fr
        assert fr["wow"] is None                                           # shown as "new", not flagged

        it = find(signals, "Italy", "alta gioielleria")
        assert it["z"] > 2 and it["wow"] < -0.2, it                        # elevated but falling
        assert not it["flagged"], it

        assert not any(s["term"] == "High Art" for s in signals)           # retired term is left out
        assert any(s["term"] == "High Art" for s in analyze.compute_signals(conn, cfg["analysis"]))

        india = find(signals, "India", "High Jewellery")
        assert india["flagged"] and india["direction"] == "rising", india

        header, body = analyze.render_brief(conn, cfg, signals, dt.date.today())
        md = header + "\n" + body
        rising = md.split("## Rising")[1].split("\n## ")[0]
        assert "| India |" in rising and "alta gioielleria" not in rising and "France" not in rising
        assert "1428571" not in md and "e+" not in md
        others = md.split("## Largest other changes")[1].split("\n## ")[0] if "## Largest other changes" in md else ""
        assert "| New York | High Jewellery |" not in others, others        # |z| < 1 noise stays out

        # 6. GDELT HTTP 429: waits 20s, 40s, 60s (config.retry_wait_seconds), then gives up
        class R:
            status_code = 429
            text = ""
        with mock.patch("collect.requests.get", return_value=R()), mock.patch.object(collect.time, "sleep") as sl:
            try:
                collect.get_json("http://x", tries=3, wait=20)
            except RuntimeError as e:
                assert "429" in str(e)
            else:
                raise AssertionError("expected RuntimeError")
        assert [c.args[0] for c in sl.call_args_list] == [20, 40, 60], sl.call_args_list

    print("Regression checks passed.")


if __name__ == "__main__":
    main()
