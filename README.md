# Trend agent (v1, free sources)

Pulls real numbers on **High Jewellery, High Jewelry, Contemporary Art and High Art** for France, New York,
Mumbai, Hong Kong, Italy and London, then flags what is genuinely moving.

| Source | What you get | Key needed |
|---|---|---|
| Google Trends | Daily search-interest index per term and market (90 days, back-filled on day one) | none |
| Wikipedia | Daily human pageviews per article, by language edition (90 days, back-filled) | none |
| YouTube Data API | Views, likes, comments of the top-50 new videos per term and market | free API key |
| Reddit API | Posts and engagement mentioning each term (global) | free app credentials, approval may be required |

Google Trends and Wikipedia give usable week-over-week flags from the first run. YouTube and Reddit build
history one snapshot at a time, so their flags appear after about five weeks.

## Setup (about 15 minutes)

1. **Create a private GitHub repository** and upload everything in this folder (keep the `.github` folder).
2. **YouTube key**: in Google Cloud Console create a project, enable *YouTube Data API v3*, create an API key.
3. **Reddit (optional)**: create a "script" app at reddit.com/prefs/apps to get a client id and secret.
4. **Add secrets** in the repo under Settings > Secrets and variables > Actions:
   `YOUTUBE_API_KEY`, and optionally `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `ANTHROPIC_API_KEY`
   (the last one adds a short Claude-written "Analyst read" to the top of each brief).
5. **Run it once** from the Actions tab (*trend-agent* > *Run workflow*). Then open `briefs/latest.md`.

After that it collects Wikipedia, YouTube and Reddit daily at 08:00 IST, refreshes Google Trends and writes a
fresh brief every Monday.
Missing keys are fine: that source is skipped and the brief says so.

## What is in the brief

- **Rising / Cooling**: flagged signals only, with this week vs last week, z-score and streak.
- **Search interest grid**: week-over-week change for every term and market.
- **Top new YouTube videos and Reddit posts**, with links.
- **Coverage and data quality**: what ran, what failed, how much history exists.
- `briefs/latest-signals.csv` has every series, so each number can be traced.

## Know the limits

- Google Trends is **relative** (each term indexed to its own 90-day peak). Compare changes, not levels.
- Google has no city-level data here: Mumbai uses Maharashtra, New York uses New York state, London uses England.
- YouTube region and language are relevance hints; markets sharing a language overlap heavily.
- Reddit has no geography. Wikipedia is by language edition, not country.
- A 90-day baseline cannot see seasonality (fashion weeks, Art Basel, gifting seasons). Read flags with the calendar in mind.
- Google Trends access uses the unofficial `pytrends` library, which can break or get rate-limited. The run
  keeps going and the brief reports it. The Wikipedia titles were checked for existence, but a title that
  later gets renamed shows up as "empty" in the coverage section.
- Instagram, TikTok and X are not covered. That is the paid-tool step.

## Files

`config.yaml` (terms, markets, thresholds) · `collect.py` · `analyze.py` · `narrate.py` · `common.py` ·
`tests/test_analyze.py` (run `python tests/test_analyze.py`) · `.github/workflows/trends.yml` · `data/trends.db` (created on first run)
