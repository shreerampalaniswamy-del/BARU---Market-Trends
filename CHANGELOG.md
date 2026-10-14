# Changes

## Fourth round: channels only, jewellery-relevant
- **Open YouTube keyword search switched off** (`youtube.keyword_search: false`). The brief no longer has that
  section. Set it to true to bring it back.
- **Relevance filter for watched channels** (`youtube.channel_relevance`): a video counts only if it mentions a
  jewellery word. For media and auction channels (Vogue, Harper's Bazaar, Christie's, Sotheby's and others) the word
  must be in the title. The brief shows "jewellery videos of all new" per channel (for example 1 of 9).
- **YouTube quota**: the "HTTP 429 Quota exceeded" form is now recognised and not retried; the brief says "quota
  used up" instead of "not found".
- **Channel lookup**: a missing or wrong handle falls back to a one-time name search, remembered for 14 days
  (`max_name_lookups` caps the 100-unit searches).
- Fixed a leftover duplicate of the channel table code in analyze.py.

## India-first update (supersedes the earlier GDELT and refinement packages)
- **India lens**: India and Mumbai are focus markets, listed first and collected first. Global is a market of its
  own. Core keywords are now High Jewellery, High Jewelry, Contemporary Art, art jewellery ("High Art" removed).
- **Themes**: groups of specific terms (rare stones, heritage and craft, international houses, Indian heritage houses, Hong Kong high jewellers) tracked in India
  and Global only; new "Themes (India first)" section and a Theme column in the signal tables.
- **Gentler on free limits**: series collected within 3 days are skipped on re-runs, each source stops after 3
  failures in a row, and YouTube moves to a weekly Monday pass (about 71 searches, under the 100 a day quota).
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
- **Second round of fixes (from the first India-first brief)**:
  - YouTube relevance: names that are also ordinary words or people (Van Cleef the actor, Tiffany, Amrapali,
    Boucheron ice cream, kundan, polki) now need a jewellery word in the title or description; art terms need an
    art word; gold-rate spam and adult photoshoot videos are dropped. Lists live under `youtube:` in `config.yaml`.
  - YouTube searches theme terms in India only (worldwide results repeated India's), which also cuts the weekly
    searches from 104 to about 71.
  - Where Global shows the same move as India, the brief shows one row, "India + Global".
  - "Where signals agree" no longer counts India, Mumbai and Global as separate confirmations.
  - Coverage reports Google Trends terms whose India and Global numbers are exactly identical.
  - A search interest week that rounds to 0 shows as "low volume" instead of -100%.
  - The YouTube table keeps items from the main run even when a later re-run only fetched a few series.
  - Failed requests now log the first words of the error response, to help diagnose GDELT.
- **Third round (YouTube sources)**:
  - New **watched channels** collector: instead of asking YouTube for "videos that mention a word" (which returns
    whatever is most viral, from anyone), it reads a curated list of channels (houses, publications, auction
    houses) directly. Edit the list under `youtube.channels` in `config.yaml`. Costs a few quota units per channel.
  - Keyword search now covers the core terms only (38 searches a week) and is labelled as a rough scan.
  - Bug fix: series collected in the last 3 days were never re-collected when the YouTube filters changed, so old
    unfiltered results stayed in the brief. A series is now re-collected whenever the filter settings change, and
    its old items are replaced.
- **Workflow**: timeout raised to 90 minutes; YouTube now runs on Mondays.

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
