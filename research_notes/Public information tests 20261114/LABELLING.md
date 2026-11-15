# Test 3a: how the 400 audit headlines are labelled

Written and committed before the sample was drawn (`news_audit.py sample`) and before the reader was run on
any sampled story. The labels are one reader's judgement (Claude's), made from the headline and the stored
summary alone, with no price and no reader score in view.

**The question for each story:** on its face, is this good news (**P**), bad news (**N**), or neither /
unclear (**Z**) for the shareholders of the named company?

`labels.csv` has one row a story: `id,label,note`. `note` is empty or one of the flags below.

## Several named companies

The reader gives a story one score and applies it to every company the story names (up to three).

* Same tone for all of them: that tone.
* One named company is plainly the subject and the others are incidental (the brokerage that issued a rating,
  a partner mentioned in passing): the tone for the subject.
* The story is about two named companies with different implications (acquirer and target, winner and loser
  of a contract or a lawsuit): **Z**, with note `mixed`, because no single score is right for both.

## Analyst notes

* Upgrade: P. Downgrade: N.
* Initiation, resumption or assumption of coverage: P with a buy-equivalent rating (Buy, Strong Buy,
  Outperform, Overweight, Positive, Accumulate, Add, Market or Sector Outperform), N with a sell-equivalent
  (Sell, Underperform, Underweight, Reduce, Negative), Z with a hold-equivalent (Hold, Neutral, Equal-Weight,
  Market Perform, Sector Perform, Peer Perform, In-Line, Sector Weight).
* Rating maintained or reiterated: **Z**, whatever happens to the price target. The note is `pt_up` when the
  target is raised and `pt_down` when it is lowered, so the audit can also report agreement had those been
  labelled P and N.
* A price-target change with no rating stated: Z, same notes.

## Results and guidance

* Results compared with estimates: beat on what is given: P; miss: N; one beats and one misses, or in line: Z.
* Results with no estimate, compared with a year earlier: clearly better: P; clearly worse: N; otherwise Z.
* Results with numbers and no comparison: Z.
* Guidance raised, or above the estimate given: P. Cut, or below: N. Reaffirmed, in line, or a range that
  brackets the estimate: Z.

## Price moves and trading

* "Shares are trading higher / lower ...": P / N, by the direction of the move, with or without a reason.
* New 52-week high, upside circuit breaker: P. New 52-week low, downside halt: N. "Halted, news pending": Z.
* Options and "whale" alerts: P when the story calls the activity bullish, N when bearish, otherwise Z.
* Short interest: a rise is N, a fall is P.
* Purchases by insiders, funds or activists: P. Sales by them: N.
* Valuation or performance explainers with no stance ("P/E ratio insights"): Z. A piece whose whole point is
  how well or badly the stock has done ("$1,000 invested ten years ago"): P or N by that point.

## Corporate events

* Being acquired at a premium, a contract award, an order, an approval (FDA or other), positive trial data, a
  favourable ruling or settlement, a dividend increase, a special dividend, a buyback, a forward stock split,
  an index addition, a credit upgrade: P.
* A share or convertible offering, a reverse split, a dividend cut or suspension, a failed or halted trial, a
  regulatory rejection, a recall, an investigation or subpoena, being sued (including law-firm class-action
  notices), a delisting notice, a credit downgrade, layoffs presented as distress, a guidance withdrawal, an
  abrupt or unexplained departure of a chief executive or finance chief: N.
* Acquiring another company (only the acquirer named), a regular dividend, a shelf registration, a product
  launch or partnership with no stated financial effect, a conference appearance, a results date, a planned
  succession or an appointment, a law-firm notice that only "investigates the fairness" of an announced
  merger: Z.

## Opinion and commentary

* A stated stance on the named company (bullish or bearish, "top pick", "avoid"): P or N.
* A question, a list with no stance, a macro or sector story that only mentions the company: Z.

## When in doubt

Z. A label is P or N only when a careful reader would not hesitate over the direction.

---

## Addendum, written after the sample was drawn and labelled, before any score or price was seen

**A fourth note, `wrong_company`.** Some stories carry a ticker that is not the company the story is about:
an analyst note tagged with the *brokerage's* ticker instead of the covered stock ("Piper Sandler Raises
Price Target on AbbVie", tagged `PIPR`), or a tag the feed itself later corrected. The conventions above did
not foresee this. The question is unchanged (is the story good or bad for the shareholders of the company the
ticker names?), so these are labelled **Z**: the story says nothing about the tagged company. They carry the
note `wrong_company` so the audit can also report agreement without them. 8 of the 400 stories: 6 of the 100
from `fiscal.ai`, 2 of the 300 from Benzinga.

Not flagged, because it cannot be judged from the text alone: stories from `fiscal.ai` about a company listed
abroad, tagged with its *local* ticker, which in the US belongs to another company or to none ("Tesco PLC",
tagged `TSCO`; "Metacon", tagged `META`; "Air Liquide", tagged `AI`). Those are labelled for the company the
story is about. How often the tag names a different US stock is measured separately, against the issuer names
in the price cache, with no price read.

**Two readings fixed while labelling** (both applied to every such story):

* Results that beat on one line and are in line on the other: P (nothing in them is negative).
* An article whose *headline* states an analyst's stance ("Sees Huge Upside", "Is Bullish On") is opinion and
  takes that stance, even when the rating behind it was only maintained. The "rating maintained: Z" rule is for
  the standard one-line rating notes.
