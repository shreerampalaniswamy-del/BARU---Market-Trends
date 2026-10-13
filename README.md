# Trend agent (v1, free sources)

Pulls real numbers on luxury jewellery and its art crossover, **India first**: India and Mumbai are the focus
markets, and France, New York, Hong Kong, Italy, London and Global act as leading indicators. It then flags what
is genuinely moving.

Two layers of terms:
- **Core keywords** (High Jewellery, High Jewelry, Contemporary Art, art jewellery) are tracked in every market.
  "High Art" cannot be searched directly, so *art jewellery* and the themes below stand in for it.
- **Themes** (rare stones and materials, heritage and craft such as polki and kundan, bespoke and provenance,
  occasions and value, international houses, Indian heritage houses, Hong Kong high jewellers, art and jewellery) are tracked in India and Global only. This keeps
  the free request limits safe. Edit them in `config.yaml`.

| Source | What you get | Key needed |
|---|---|---|
| Google Trends | Daily search-interest index per term and market (90 days, back-filled on day one) | none |
| Wikipedia | Daily human pageviews per article, by language edition (90 days, back-filled) | none |
| YouTube Data API | Views of recent videos that actually name each term, per market (weekly; the free search quota is about 100 searches a day, so a full pass spills into Tuesday) | free API key |
| Reddit API | Posts and engagement mentioning each term (global) | Reddit's approval first (optional, see setup) |
| GDELT | Daily count of news articles mentioning each term, by publisher country (about 3 months, back-filled) | none |

Google Trends, Wikipedia and GDELT give usable week-over-week flags from the first run. YouTube and Reddit build
history one snapshot at a time, so their flags appear after about five weeks.

## Setup (about 15 minutes)

1. **Create a private GitHub repository** and upload everything in this folder (keep the `.github` folder).
2. **YouTube key**: in Google Cloud Console create a project, enable *YouTube Data API v3*, create an API key.
3. **Reddit (optional)**: Reddit requires you to request and receive approval before using its API (see its
   Responsible Builder Policy page). Only after approval, create a "script" app at reddit.com/prefs/apps to get
   a client id and secret. Skip this step if you do not have approval.
4. **Add secrets** in the repo under Settings > Secrets and variables > Actions:
   `YOUTUBE_API_KEY`, and optionally `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `ANTHROPIC_API_KEY`
   (the last one adds a short Claude-written "Analyst read" to the top of each brief).
5. **Run it once** from the Actions tab (*trend-agent* > *Run workflow*). Then open `briefs/latest.md`.

After that it collects Wikipedia and Reddit daily at 08:00 IST, and on Mondays refreshes Google Trends, YouTube
and GDELT and writes a fresh brief. A full Monday run takes roughly 40 to 60 minutes because of polite pauses
between requests. Missing keys are fine: that source is skipped and the brief says so.

**If a source is rate-limited**: the run stops that source after 3 failures in a row. Run the workflow again
later; a series collected within the last 3 days is not fetched again, so a re-run only fills the gaps.

## What is in the brief

- **Rising / Cooling**: flagged signals only, India and Mumbai first, with a Theme column, this week vs last
  week, z-score and streak.
- **Themes (India first)**: one table per theme with search and news change for India and Global.
- **Where signals agree**: terms flagged the same way in 2+ markets or 2+ sources, a stronger hint than one series.
- **Search interest grid**: week-over-week change for every term and market.
- **Top new YouTube videos and Reddit posts**, with links and the markets they appeared in. YouTube results are
  kept only if the title or description contains the term.
- **Set aside** (in the coverage section): series just settling back after a one-off spike, hidden from the tables.
- **Coverage and data quality**: what ran, what failed, how much history exists.
- `briefs/latest-signals.csv` has every series, so each number can be traced.

## Know the limits

- Google Trends is **relative** (each term indexed to its own 90-day peak). Compare changes, not levels.
- Google has no city-level data here: Mumbai uses Maharashtra, New York uses New York state, London uses England.
- YouTube region and language are relevance hints; markets sharing a language overlap heavily.
- Reddit has no geography. Wikipedia is by language edition, not country.
- GDELT counts news articles, not people's behaviour. It filters by the publisher's country, not the country an
  article is about, and works at country level (Mumbai = India, New York = United States, London = United Kingdom).
  English phrases rarely appear in French, Italian or Hong Kong publications, so those markets lean on the
  local-language terms, and the `Global` rows cover the English keywords.
- Public attention data reflects the general public, not HNI or UHNI buyers. Treat it as a leading indicator of
  taste and conversation, and be sceptical of mass-market spikes (gold rates, wedding and festive seasons).
- Rare, specific terms (padparadscha, jadau) may return "no data" in Google Trends because volume is too low.
- A 90-day baseline cannot see seasonality (fashion weeks, Art Basel, gifting seasons). Read flags with the calendar in mind.
- Google Trends access uses the unofficial `pytrends` library, which can break or get rate-limited. The run
  keeps going and the brief reports it. The Wikipedia titles were checked for existence, but a title that
  later gets renamed shows up as "empty" in the coverage section.
- Instagram, TikTok and X are not covered. That is the paid-tool step.

## Files

`config.yaml` (terms, markets, thresholds) · `collect.py` · `analyze.py` · `narrate.py` · `common.py` ·
`tests/` (run each of `test_analyze.py`, `test_gdelt.py`, `test_youtube.py`, `test_trends.py`) · `.github/workflows/trends.yml` · `data/trends.db` (created on first run)
