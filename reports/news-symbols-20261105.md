# News symbol discovery and one position per symbol: 2026-11-05

Branch `feat/news-symbols`. This branch makes two changes:

1. The bot can now look at stocks in the news, not only its fixed list of 45 symbols.
2. It stops stacking several strategies in one stock.

## What was wrong

* **The bot could never pick a new symbol.** It only traded `paper_execution_symbols`, a fixed list of 45 ETFs and
  megacaps. The "watchlist discovery" was a hardcoded list of 10 megacaps, all already in the universe.
* **News wasn't used at all.** Paper decisions recorded `news_context` as `"excluded"`. The news reader only matched
  keywords, and it fell back to made-up `mock_news` headlines whenever yfinance or Nasdaq returned nothing.
* **The news reader misread most of the real news.** On today's 300 Alpaca news items, most were analyst notes such
  as *"Barclays **Maintains Underweight** on Trex, **Raises** Price Target to $46"*. The keyword scorer counted
  "raises" as positive, so a bearish rating scored as good news. This is "the news we got was wrong" before any trade
  was even placed.
* **Same-symbol stacking.** In the first 20 trades, 9 losses came from holding the same stock more than once at a time
  (GOOGL ×3; META, AMD and AAPL ×2). The cycle reads pending orders once per symbol, so two strategies could each
  BUY the same symbol in the same cycle.

## What changed

### News symbol discovery (`services/news_discovery.py`)

A `news_discovery_job` runs on the learning queue at :10 and :40, 8:10 to 15:40 New York time, on weekdays. Each run:

1. **Reads real news.** It fetches up to 300 Alpaca news items from the last 24 hours.
   * Roundups that name more than 3 companies are skipped.
   * Each focused story is stored as an `alpaca_news` `NewsArticle`, so the core symbols now get real stored news
     instead of placeholders.
2. **Scores tone properly.**
   * For analyst notes, the rating action decides the tone: upgrade +1, downgrade −1. A new rating counts ±0.5
     according to whether it's bullish or bearish.
   * Maintained or reiterated ratings, price-target tweaks and insider filings count as **routine** (0). They count
     toward coverage but don't set the tone.
   * Other stories use the keyword scorer, which now has more common market verbs.
3. **Ranks companies outside the universe.** A company qualifies with at least 2 stories in 24 hours and an average
   tone of at least +0.25 across its non-routine stories. Ranking is tone × coverage, with coverage capped at 5
   stories.
4. **Screens each candidate.** A candidate must pass all of these:
   * It's an active, tradable US equity on a listed exchange (no OTC).
   * It has at least 60 trusted daily bars.
   * Its price is between $5 and the one-share learning cap: 1.5% of equity, about $1,500.
   * Its 20-day dollar volume is at least $20M.
5. **Admits up to 8 symbols for 3 days.** The window extends while the company stays in the news. Admitted symbols
   join the intraday feed and the paper cycle.
6. **Expires symbols.** An expired symbol stays in the cycle for exits only while it is still held. Once it's flat,
   its asset row goes back to inactive. Discovery never deactivates an asset it didn't activate.

State is one `news_symbol_discovery` audit-log row per run. Each row records what was admitted, refreshed, rejected
(with the reason) and expired, so there's **no migration**.

**How admitted symbols trade:**
* The strategies still decide every entry. News chooses which symbols they look at, not whether to buy.
* Every entry on an admitted symbol is **learning-sized** (0.25% of equity, with the one-share floor), whatever the
  strategy's status.
* The entry's signal records `features.news_context` (tone, story count, top headline). If the trade then falls on its
  own, the loss diagnosis marks it **`news_wrong`**, and the repeat tracker will show whether news-found symbols are
  a lesson or an edge.

### One position per symbol

* A BUY is skipped when another strategy already holds that symbol, has a pending buy in it, or bought it earlier in
  the same cycle. The skip reason names the strategy that holds it.
* Exits are unaffected.
* `paper_one_position_per_symbol` (default on) turns it off.

### Settings (all with defaults)

`news_discovery_enabled`, `news_discovery_max_symbols` = 8, `news_discovery_lookback_hours` = 24,
`news_discovery_min_articles` = 2, `news_discovery_min_sentiment` = 0.25, `news_discovery_admit_days` = 3,
`news_discovery_min_price` = 5, `news_discovery_min_dollar_volume` = 20M, `paper_one_position_per_symbol` = true.

## Live check (read-only, today's Alpaca news)

| Step | Count |
|---|---:|
| News items, last 24 hours | 300 |
| Focused (3 or fewer companies) | 285 |
| Candidates before the analyst-note fix | 27, led by "Maintains Underweight … Raises Price Target" notes |
| Candidates after the fix | 7 |

The top candidates after the fix were real events:

| Symbol | News |
|---|---|
| TREX | BofA upgrade to Buy |
| APP | Q3 beat |
| FSLY | Q3 results, raised outlook |
| CG | Q3 earnings beat |
| SKT | Q3 results, raised guidance |

All are NYSE or NASDAQ listed and tradable.

## How we'll know it works

* **Discovery is running:** the `news_symbol_discovery` audit rows show what was admitted and rejected each run.
* **The news calls are right or wrong:** after a few weeks, compare `news_wrong` against `edge` on news-found trades.
  * If news-found trades lose more often than the core symbols, the news scoring is wrong and is the thing to fix.
  * If they hold up, news-driven entries (not just selection) become a candidate experiment.
* **Stacking stopped:** `crowded_symbol` should stop appearing among the contributing loss flags.

## Not done here

* News doesn't trigger buys by itself. A news-momentum entry would be a new strategy, so it should only come after the
  `news_wrong` rate shows the news calls are right.
* There is no sector mapping for discovered symbols. Each one is its own sector group, so the sector cap and the
  sector part of the loss diagnosis don't apply to them.
* There's no endpoint or UI yet. Admissions are only visible in the audit log.
