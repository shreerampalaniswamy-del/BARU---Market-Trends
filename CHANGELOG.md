# Changes

## India-first update (supersedes the earlier GDELT and refinement packages)
- **India lens**: India and Mumbai are focus markets, listed first and collected first. Global is a market of its
  own. Core keywords are now High Jewellery, High Jewelry, Contemporary Art, art jewellery ("High Art" removed).
- **Themes**: groups of specific terms (rare stones, heritage and craft, international houses, Indian heritage houses, Hong Kong high jewellers) tracked in India
  and Global only; new "Themes (India first)" section and a Theme column in the signal tables.
- **Gentler on free limits**: series collected within 3 days are skipped on re-runs, each source stops after 3
  failures in a row, and YouTube moves to a weekly pass on Monday, with Tuesday picking up any searches the 100 a day quota could not cover (about 104 in total).
- **Fixes found by checking a real brief**:
  - A series jumping from zero (France "High Jewellery" 0 to 14) produced a z-score of 14 billion and a "Rising"
    flag. The z-score now has a sensible floor, and a jump from zero is listed as a change, never flagged.
  - A series that was still above its norm but had fallen week over week (Italy "alta gioielleria", -24%) was
    listed under Rising. A flag now needs the week-over-week move and the z-score to point the same way.
  - Terms removed from `config.yaml` (such as "High Art") no longer appear in the brief; their old rows stay in
    the database.
  - "Largest other changes" no longer lists low-volume noise (|z| below 1).
  - GDELT HTTP 429 (too many requests): waits 20s, 40s, 60s before giving up on a series
    (`gdelt.retry_wait_seconds`).
- **Workflow**: timeout raised to 90 minutes; YouTube now runs on Mondays and Tuesdays.

## Refinement update
- **YouTube relevance**: the search now asks for the exact phrase, and a video counts only if its title or
  description really contains the term. This removes unrelated hits (basketball for "High Art", toy merchandise for
  "High Jewelry", gold-mining videos for "haute joaillerie"). The numbers are stored under new names
  (`matched_*`), so YouTube history restarts and flags need about five weeks again. Switches in `config.yaml`:
  `youtube.exact_phrase` and `youtube.require_phrase_in_text`.
- **Search grid**: `·` = term not tracked in that market, `no data` = tracked but Google returned nothing,
  `low volume` = too few searches to read.
- **Spikes**: a series whose last week was more than 4x its norm and has since fallen back (for example a one-week
  burst of automated Wikipedia traffic) is left out of the tables and listed under "Set aside".
- **Where signals agree**: new table of terms flagged the same way in 2+ markets or 2+ sources.
- **Video and post tables**: each item appears once, with every term and market it was found under.

## GDELT update
- New source: daily count of news articles mentioning each term, by publisher country, plus a Global series.
- Coverage section: each source now shows its own last run, so skipped sources say why.
