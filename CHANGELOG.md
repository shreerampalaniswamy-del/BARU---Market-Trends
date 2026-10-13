# Changes

## India-first update (supersedes the earlier GDELT and refinement packages)
- **India lens**: India and Mumbai are focus markets, listed first and collected first. Global is a market of its
  own. Core keywords are now High Jewellery, High Jewelry, Contemporary Art, art jewellery ("High Art" removed).
- **Themes**: groups of specific terms (rare stones, heritage and craft, international houses, Indian heritage houses, Hong Kong high jewellers) tracked in India
  and Global only; new "Themes (India first)" section and a Theme column in the signal tables.
- **Gentler on free limits**: series collected within 3 days are skipped on re-runs, each source stops after 3
  failures in a row, and YouTube moves to a weekly pass on Monday, with Tuesday picking up any searches the 100 a day quota could not cover (about 104 in total).
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
