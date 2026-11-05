# Learning from losses: 2026-11-05

Branch `feat/loss-learning`. The goal is that every paper loss teaches the bot something. This needs three things:

1. Each loss is explained by what went wrong.
2. The learning trades can actually happen.
3. Each strategy trades with the exits it was tested with, so a loss says something about the strategy.

## What was wrong

* **Losses weren't explained.** Only 7 of the closed trades had been explained (attributed), and every 3% stop was
  labelled a "catastrophe stop". Nothing recorded whether the market, the sector, an earnings report or the
  strategy's own signal caused the loss. Attribution ran once a night (16:45 New York time), so one skipped run left a
  day of trades unexplained.
* **Paper didn't trade what was backtested.** Every older strategy (MA crossover, MACD, RSI mean reversion,
  Bollinger, channel breakout, trend pullback, ensemble, model-predictive) was scored in
  `strategy-scorecard-20261028.md` with the backtester's **3% stop, 6% target and 20-day time stop, and no exit on a
  SELL signal**. On paper they used a **3% stop, 3% target, no time stop, and SELL-signal exits**:
  * break-even win rate: about 38% under the tested exits, above 50% plus costs under the paper exits;
  * so paper results could neither confirm nor refute the backtest. A loss told us about the exits, not the
    strategy.
* **Learning trades were too small to fill.** Exploration (learning) trades are sized at 0.25% of equity, about $250.
  That's less than one share of any symbol over $250, such as SPY, META, MSFT or LLY. Broker brackets need whole
  shares, so 320 learning buys in one week were skipped.

## What changed

* **Loss diagnosis** (`services/loss_diagnosis.py`, recorded in each attribution's `detail.diagnosis`). Every closed
  round trip gets one cause, measured against SPY and its sector ETF over the same hold:

  | Family | Cause | Meaning |
  |---|---|---|
  | missed | `missed_earnings` | an earnings report landed inside the hold |
  | missed | `missed_market_move` | SPY fell ≥ 1% and the stock fell with it (within 1%) |
  | missed | `missed_sector_move` | the sector ETF fell ≥ 1% and the stock fell with it |
  | news | `news_wrong` | the entry read positive news and the stock fell on its own |
  | strategy | `signal_wrong` | the stock fell more than 1% beyond the market and its sector |
  | noise | `noise` | loss under 0.5% |
  | win | `edge` / `market_tailwind` | a win the stock earned, vs. one the market carried |

  Contributing flags:
  * `crowded_symbol`: another lot held the same symbol;
  * `clustered_stops`: 3 or more stops filled in one session;
  * `exit_untested`: the entry's legs didn't match the strategy's backtested exits;
  * `market_down`.

  Earnings dates come from `earnings_history` (cached; off in tests).
* **Repeat tracking** (`repeating_failures`, in `attribution_summary`). A cause that is the primary cause of at least
  3 losses, and at least 40% of a strategy's losses among its last 20 trades, becomes a lesson with a proposed
  experiment (`auto_apply: false`). An account-wide `all` entry catches failures that hit every strategy at once,
  such as a market drop.
* **Coverage check.** `attribution_coverage` counts closed round trips against attributed ones and lists any missing.
  It's included in every attribution run.
* **Attribution runs during the session.** New `trade_attribution_job`, at :05 and :35 from 9:00 to 16:59 New York
  time on weekdays (learning queue). It's idempotent, alongside the nightly loss feedback.
* **Tested exits.** The eight older strategies declare `BACKTESTED_BRACKET_EXIT_POLICY`: 3% stop, 6% target,
  20-session time stop, `sell_signal_exit=False`.
  * New `ExitPolicy.sell_signal_exit` flag: when false, a SELL signal is recorded as observed and not acted on.
  * RSI(2) and IBS keep their own tested policy.
  * Existing open positions keep the legs they were opened with. Re-armed exits use the new plan.
* **One-share floor.** `paper_min_one_share_equity_fraction` (default 1.5%, 0 disables): a sub-share size still buys
  one whole share when that share costs at most 1.5% of equity ($1,500 on $100k). Every other exposure cap still
  applies at reservation.

## The diagnosis on the real trades

These are the 20 Alpaca paper round trips that closed between Oct 30 and Nov 5. They were replayed with Alpaca IEX
minute bars for SPY and the sector ETFs:

| Cause | Trades |
|---|---:|
| `signal_wrong` (strategy) | 7 |
| `missed_market_move` | 3 |
| `missed_earnings` (AAPL, −5.4% across its Oct 30 report) | 1 |
| `missed_sector_move` (ADBE, with XLK −2.8%) | 1 |
| `noise` | 1 |
| `edge` (wins) | 7 |

| Contributing flag | Trades |
|---|---:|
| `exit_untested` | all 20 |
| `clustered_stops` | 11 |
| `crowded_symbol` | 9 (GOOGL ×3, META ×2, AMD ×2, AAPL ×2 held at once) |
| `market_down` | 7 |

What this says:
* **The exits were never the tested ones.** That's fixed here.
* **A third of the losses were things the strategies don't look at:** the market, the sector, earnings. Those are the
  next experiments: a market gate and a full-hold earnings blackout. The repeat tracker will flag them once they
  reach 3 losses for one strategy.
* **Seven losses were the signals themselves.** Once they trade the tested exits, those strategies get a fair test.

## Not done here

* No market gate, one-position-per-symbol rule, or new strategies. Those should come from the repeat tracker's
  evidence, not be set by hand.
* Attribution doesn't diagnose trial-lot trades yet (only autotrader round trips).
