---
name: Daily-bar entry window
description: Why RSI(2)/IBS entries are allowed all session, and how late fills should be judged.
---
Daily-bar strategies may enter from open+5 min until 3:45 PM ET (window end 375 min), set as an environment value, not a code default (code default stays 45).

**Why:** The user chose (2026-10-30) to stop losing a day's trade to a late Resume, feed hiccup or pause, accepting that later fills differ from the backtest's next-open price.

**How to apply:** Keep tests pinned to explicit window values so the deployed setting doesn't change them. Judge late fills with the paper-vs-backtest scorecard; the "prefer open, catch up later" variant and new intraday strategies were deliberately deferred.
