# Feasibility on the free live feed (IEX only): tables

First session with IEX bars: 2020-07-27; first with a 14-session IEX sigma: 2020-08-14. Sessions since then with no IEX bar at all: 1. Not part of the verdict.

## Every session with IEX signals: from 2020-08-14, 1568 sessions

Regular minutes with no IEX bar: 1.68%; decision minutes with no IEX bar: 2.05%. Median gap between the IEX and consolidated open: 0.52 bp; close: 0.72 bp.

| Variant | Days traded (consolidated / IEX) | Trade-or-not agrees | Of consolidated trade days: IEX also trades | Same first direction | Same first entry decision | Within one decision | Same day, direction and entry time (of consolidated trade days) | Decisions with the same position | Sessions identical at every decision |
|---|---|---|---|---|---|---|---|---|---|
| (c) | 534 / 529 | 98.9% | 97.9% | 100.0% | 93.1% | 97.7% | 91.2% | 99.3% | 93.2% |
| (b) | 534 / 530 | 98.9% | 97.9% | 100.0% | 93.1% | 97.5% | 91.2% | 99.0% | 96.2% |
| (a) | 961 / 952 | 98.3% | 98.1% | 99.9% | 94.5% | 98.1% | 92.7% | 98.3% | 94.5% |

Net at 1 bp a side, consolidated fills in both columns:

| Variant | Consolidated signals: total / Sharpe | IEX signals: total / Sharpe | Correlation of daily returns |
|---|---|---|---|
| (c) | 17.8% / 0.66 | 13.7% / 0.52 | 0.980 |
| (b) | 21.0% / 0.49 | 15.0% / 0.37 | 0.989 |
| (a) | 21.0% / 0.38 | 18.8% / 0.35 | 0.990 |

## Held-out period: from 2024-05-01, 635 sessions

Regular minutes with no IEX bar: 1.90%; decision minutes with no IEX bar: 2.28%. Median gap between the IEX and consolidated open: 0.46 bp; close: 0.53 bp.

| Variant | Days traded (consolidated / IEX) | Trade-or-not agrees | Of consolidated trade days: IEX also trades | Same first direction | Same first entry decision | Within one decision | Same day, direction and entry time (of consolidated trade days) | Decisions with the same position | Sessions identical at every decision |
|---|---|---|---|---|---|---|---|---|---|
| (c) | 216 / 215 | 99.2% | 98.6% | 100.0% | 91.5% | 96.2% | 90.3% | 99.1% | 92.3% |
| (b) | 216 / 215 | 99.2% | 98.6% | 100.0% | 92.0% | 96.2% | 90.7% | 99.1% | 96.4% |
| (a) | 385 / 384 | 98.6% | 98.7% | 100.0% | 94.5% | 97.9% | 93.2% | 98.5% | 95.0% |

Net at 1 bp a side, consolidated fills in both columns:

| Variant | Consolidated signals: total / Sharpe | IEX signals: total / Sharpe | Correlation of daily returns |
|---|---|---|---|
| (c) | -6.6% / -0.68 | -7.6% / -0.78 | 0.980 |
| (b) | -1.3% / -0.04 | -1.6% / -0.05 | 0.995 |
| (a) | 0.8% / 0.08 | 0.2% / 0.05 | 0.995 |

