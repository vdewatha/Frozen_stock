# News tone as an entry condition, 2026-11-06

Branch `claude/reverent-saha-8339be`. Follows `self-evolution-roadmap-20261106.md` and PR #5 (news symbol
discovery). **Question:** news only chose which symbols the bot looked at (Alpaca news, tone scored with
analyst-note handling, up to 8 symbols admitted for 3 days at learning size). Can news tone be an entry
condition the bot tests like any other rule and, if it survives the fixed gate, trades?

**Goal used:** profit after costs, through the unchanged strategy-discovery gate (`strategy_discovery.GATE`).
Nothing here adopts anything; the news candidates are trials in the library and earn a paper slot only by
clearing the same bar as every other candidate.

## Verdict

* **The tone history exists and reaches back to January 2015** for the 29 single stocks of the paper
  universe: 156,957 focused articles, 44,853 of 86,391 sessions carry news. Coverage is enough for the gate's
  100-trade minimum on every candidate (299 to 3,412 trades each).
* **None of the 12 news candidates clears the gate; none is `explore`-eligible either.**
  * *Positive news in an uptrend* (tone ≥ +0.5, ≥ 2 stories, close > SMA200, out above SMA5) **loses** after
    costs: −13 to −21 bps a trade over ~3,400 trades, negative in the holdout too. Good news is already in the
    price by the close; buying it the next morning pays the spread for nothing.
  * *Negative-news dip* (tone ≤ −0.5 and RSI(2) < 10, out above SMA5) is **positive but not significant**:
    +5 to +28 bps over 299–673 trades, 95% CI straddling zero, holdout +16 to +62 bps. Worth watching as the
    history lengthens, not a claim of edge.
  * *Post-upgrade drift* (uniformly positive tone and a ≥ 1% gap up, hold 20 sessions) earns **+169 to +186
    bps a trade, CI [81, 268], holdout +169 to +174 over 182–259 trades**, but **does not beat holding the
    stock**: the excess-over-drift CI is [−90, 93]. The signal fires on AAPL, NVDA, TSLA, AMZN and MSFT in
    their best years, and a 20-session hold of those names drifts about as much on its own. The 10-session
    variants earn +43 to +55 bps, CI including zero.
* **News selection has not earned an entry role yet.** The local database has no closed attributions to
  compare news-found trades with the core universe (table below is the production query the owner runs);
  the paper account's `news_vs_core` scoreboard section and `news_wrong` counts remain the forward evidence.

So: news tone is now a feature any rule can use, the candidates sit in the library as trials, and the gate
says no today. The thing that would change the answer is a tone definition that leads price rather than
describes it (see "Not done").

## What was built

1. **Per symbol-day tone history** (`app/services/news_history.py`). One row per symbol and session,
   `(date, tone, articles, directional)`:
   * tone = the discovery scorer's reading of each story (`news_discovery._article_sentiment`: analyst-note
     rating actions, routine notes neutral, keyword tone otherwise), averaged over the session's *directional*
     stories as `rank_candidates` does; 0 when a session has coverage but no view;
   * session = the first session that could trade on the story: published before the 16:00 ET close on a
     weekday, that day; later, the next weekday. `attach_tone` rolls a session with no bar (holiday) onto the
     next bar, never back;
   * `tone_series(symbol, db)` = the cached backfill plus stored `alpaca_news` rows outside the cached span
     (the last day or two), so the paper path and the replay read one definition;
   * **backfill**: Alpaca's archive starts 2015-01 (a 2012 query returns nothing), 50 items a page, 200
     requests a minute. `backfill` spends `news_history_backfill_requests_per_run` (default 120) requests per
     discovery run on weekly windows, one query per window with every single stock in it (the `symbols`
     parameter takes a list), backwards from today to 2015 and forwards to yesterday, under
     `history_dir()/news/<SYMBOL>.csv` with `coverage.json`. A window counts as covered only when every page
     was read; a 429 or an exhausted budget drops the partial window and the next run retries it. The full
     archive took about 4,400 requests here (31 one-minute rounds at the limit); at the default budget and the 8:10–15:40 ET
     job cadence the deployed cache is complete in about two and a half job days. The discovery job
     (`run_news_discovery`) calls it after each run and records `history` in its audit payload.
2. **Rule features** (`strategies/rules.py`): `news_tone` / `news_articles` (the bar's own session, every
   story before that close) and `news_tone_N` / `news_articles_N` (N bars earlier; `news_tone_1` is the
   previous session's). Read from optional frame columns like `earn_surprise`; 0 on a bar without news and 0
   everywhere without a tone history, so a news rule simply does not fire. `RuleStrategy` attaches the runtime
   series (`news_history.runtime_tone`, 10-minute per-process cache, fails neutral) when a rule uses news and
   the frame lacks the columns. Tests check that each feature on a prefix equals the vectorised value and that
   future bars never change it.
3. **Candidates** (`strategy_discovery.NEWS_FAMILIES`, single stocks only, 12 trials): positive news in an
   uptrend (tone at lag 0 or 1, hold 5 or 10), negative-news dip (lag 0 or 1, with or without trend), post-
   upgrade drift (with or without trend, hold 10 or 20). `load_frames` attaches the cached tone to single
   stocks (`news_loader=` for tests). The library grows from 342 to 354 trials for the 45-symbol universe;
   the deflated Sharpe ratio counts them.
4. **A named rule** (`strategies/news_tone.py`, slug `news_tone`, registered, never seeded): the same
   `RuleStrategy` machinery under a stable slug so an operator can forward-test one news rule on paper with
   its own attribution record before the gate has an answer.
5. **The record so far** (`news_history.news_track_record`, `scripts/news_entries_report.py`): news-found
   trades vs core trades from the attributions (the scoreboard's `news_vs_core`), the `news_wrong` count and
   the loss causes of news-found trades, as a Markdown table.
6. Tests: `tests/test_news_entries.py` (14). Full backend suite: 2762 passed, 33 skipped.

## The candidates on real history

Universe: the 29 single stocks of `DEFAULT_PAPER_EXECUTION_SYMBOLS`, yfinance adjusted daily bars 2000 →
2026-11-05 (news from 2015-01-01), today's list (the point-in-time universe setting is for the deployed run;
this evaluation ran on the cached 45-symbol history). Holdout 2024-11-05 → 2026-11-05. Execution model as the
backtester: decision on the close, fill at the next open, volatility-scaled costs both sides, stop first inside
a bar, one position per symbol. All 12 are `rejected`; the deflated Sharpe check fails for every candidate in
the library (354 trials) and is left out of the last column.

| Family | Entry (and exit) | Hold | Trades | Win | Net bps [95% CI] | Excess over drift CI | Holdout trades / bps | Other failed checks |
|---|---|---|---|---|---|---|---|---|
| negative_news_dip | dist_sma_200 > 0 and news_tone <= -0.5 and rsi_2 < 10 → exit dist_sma_5 > 0 | 10 | 340 | 0.61 | 22 [-25, 65] | [-61, 30] | 99 / 16 | net expectancy, excess over drift |
| negative_news_dip | dist_sma_200 > 0 and news_tone_1 <= -0.5 and rsi_2 < 10 → exit dist_sma_5 > 0 | 10 | 299 | 0.63 | 5 [-52, 62] | [-85, 29] | 76 / 41 | net expectancy, excess over drift |
| negative_news_dip | news_tone <= -0.5 and rsi_2 < 10 → exit dist_sma_5 > 0 | 10 | 673 | 0.62 | 28 [-20, 71] | [-58, 35] | 229 / 50 | net expectancy, excess over drift |
| negative_news_dip | news_tone_1 <= -0.5 and rsi_2 < 10 → exit dist_sma_5 > 0 | 10 | 582 | 0.64 | 25 [-17, 68] | [-51, 34] | 187 / 62 | net expectancy, excess over drift |
| positive_news_trend | dist_sma_200 > 0 and news_tone >= 0.5 and news_articles >= 2 → exit dist_sma_5 > 0 | 5 | 3392 | 0.51 | -15 [-29, -1] | [-49, -19] | 1128 / -15 | net expectancy, excess over drift, since 2015, holdout |
| positive_news_trend | dist_sma_200 > 0 and news_tone >= 0.5 and news_articles >= 2 → exit dist_sma_5 > 0 | 10 | 3351 | 0.51 | -13 [-27, 2] | [-48, -18] | 1099 / -14 | net expectancy, excess over drift, since 2015, holdout |
| positive_news_trend | dist_sma_200 > 0 and news_tone_1 >= 0.5 and news_articles_1 >= 2 → exit dist_sma_5 > 0 | 5 | 3412 | 0.51 | -21 [-36, -8] | [-56, -27] | 1138 / -17 | net expectancy, excess over drift, since 2015, holdout |
| positive_news_trend | dist_sma_200 > 0 and news_tone_1 >= 0.5 and news_articles_1 >= 2 → exit dist_sma_5 > 0 | 10 | 3367 | 0.52 | -19 [-34, -5] | [-55, -25] | 1116 / -12 | net expectancy, excess over drift, since 2015, holdout |
| post_upgrade_drift | dist_sma_200 > 0 and news_tone >= 1 and gap > 0.01 | 10 | 801 | 0.53 | 43 [-25, 108] | [-118, 11] | 220 / 104 | net expectancy, excess over drift |
| post_upgrade_drift | dist_sma_200 > 0 and news_tone >= 1 and gap > 0.01 | 20 | 691 | 0.57 | 169 [81, 262] | [-90, 81] | 182 / 169 | excess over drift |
| post_upgrade_drift | news_tone >= 1 and gap > 0.01 | 10 | 1054 | 0.53 | 55 [-10, 116] | [-104, 19] | 318 / 91 | net expectancy, excess over drift |
| post_upgrade_drift | news_tone >= 1 and gap > 0.01 | 20 | 916 | 0.58 | 186 [97, 268] | [-71, 93] | 259 / 174 | excess over drift |

Reading the drift rows: `news_tone >= 1` means every directional story of the session was maximally positive.
An analyst upgrade scores +1, but so does a keyword headline such as "shares surge on record quarter", which
*describes* the gap it is paired with. The rule is therefore mostly "gapped up and the day's coverage was
glowing", a momentum condition on the names that happened to be in their best decade; the drift check says the
same thing. A tone that leads the move (the upgrade published before the open, with the keyword stories of the
session excluded) is the natural next candidate, see below.

For scale, the best non-news rows of the same run: `calm_market_dip` on index ETFs, 230 trades, +45 bps
[6, 57], holdout 36 / +35 (`explore`); `weekly_reversal` on sector ETFs, 313 trades, +65 bps [3, 92], holdout
41 / +79 (`explore`).

## Coverage

| | Sessions since 2015 | With news | Tone ≥ +0.5 and ≥ 2 stories | Tone ≤ −0.5 | Tone = +1 | Focused articles |
|---|---|---|---|---|---|---|
| All 29 stocks | 86,391 | 44,853 (52%) | 8,063 | 6,304 | 8,932 | 156,957 |
| AAPL | 2,979 | 2,900 | 743 | 678 | 647 | 21,246 |
| TSLA | 2,979 | 2,855 | 591 | 663 | 468 | 25,106 |
| PG (least covered) | 2,979 | 674 | 80 | 67 | 106 | 1,234 |

Every symbol is covered 2015-01-01 → 2026-11-06. The deployed cache starts empty (`history_dir()` is
gitignored); until the backfill reaches a few years back, the news candidates have too few trades, fail
`enough_trades`, and stay `rejected` while still counting as trials. The discovery job's audit payload
(`history.earliest`, `history.complete`) says how far it has got.

## The record so far: news-found vs core trades

The local SQLite has no attribution ledger, so the table below is what the script prints here. Against the
deployed database it fills from the attributions (latest revision per round trip), the discovery runs'
`admitted` lists (any symbol ever admitted counts as news-found) and the loss diagnosis:

```
python scripts/news_entries_report.py
```

| Trades | Count | Wins | Win rate | Avg net return | Net P&L |
|---|---|---|---|---|---|
| News-found symbols | (no ledger locally) | | | | |
| Core universe | (no ledger locally) | | | | |

`news_wrong` losses among news-found trades: not readable locally. The same numbers are the scoreboard's
`news_vs_core` section (`GET /stock-paper/learning-scoreboard`). The decision rule the owner can apply once
it fills: news keeps its selection role while news-found trades are not worse than core trades after costs;
it earns an entry role only through a candidate that clears the discovery gate, never by this table alone.

## Not done

* A **leading** tone: score only stories published before the open (pre-market analyst actions, overnight
  earnings headlines) and drop same-session keyword stories that narrate the move. That is one more feature
  (`news_tone_pre`) on the same cache, and the right next candidate for the drift family.
* News on the discovered symbols: the backfill covers the configured single stocks plus currently admitted
  news symbols; a symbol admitted later is backfilled from its admission on (one query per window at the
  normal budget), so a news rule on a news-found symbol starts with a short history.
* The point-in-time universe run (`replay_point_in_time_universe`) replays past S&P members; their tone is
  not cached (the budget goes to the 29 configured names first), so on the deployed run the news candidates
  are judged on the configured names only, which is where they would trade.
* No scoreboard entry (`learning_scoreboard.CHANGES`) for this change: nothing adopted.
