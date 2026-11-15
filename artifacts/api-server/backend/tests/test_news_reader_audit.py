"""The news reader after the 2026-11-14 audit (reports/public-information-tests-20261114.md).

400 Alpaca stories were labelled by hand before the reader was run on them.  On the design half the old
reader agreed with 155 of 200 labels and called 32 of the 61 plainly good or bad stories neutral.  Every case
below is a story from that half, or a pattern that misfired when the change was read against 100,000 archive
stories.  No price was used to design any of it.
"""
import pytest

from app.services import news_discovery as nd
from app.services.news_sentiment import reference_votes, score_headline_sentiment


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _tone(headline: str, summary: str = "") -> int:
    return _sign(nd._article_sentiment({"headline": headline, "summary": summary}))


@pytest.mark.parametrize("headline, summary", [
    ("Energy Fuels Shares Spike Higher On Volume As Hearing Dept. Of Energy To Execute New 10-Year Uranium "
     "Exploration, Mining Contract With Co. Subsidiary", ""),
    ("Bank7 Increases Quarterly Cash Dividend From $0.27 To $0.30 Per Share", ""),
    ("JFrog Board Authorized Up To $300M Share Repurchase Program", ""),
    ("Amigo Resources PLC Adopts Inaugural Dividend Policy",
     "Amigo Resources PLC has formally adopted its first dividend policy, targeting the distribution of an "
     "inaugural dividend within the next 12 months."),
    ("Prime Medicine Says FDA Clears IND Application For PM577a", ""),
    ("BriaCell Reports Unprecedented Anti-Tumor Activity; FDA Greenlights Bria-OTS IND", ""),
    ("Vertex Reports Phase 3 Study Of KALYDECO Met Primary Endpoint And Showed Improvements", ""),
    ("Kopin Corporation Secures New $3.2 Million Sentinel FPV Module Contract",
     "Kopin Corporation has been awarded a $3.2 million contract for its Sentinel FPV modules."),
    ("RB Global Sees FY24 Adjusted EBITDA Of $1.22B-$1.27B (Prior View: $1.20B-$1.26B)", ""),
    ("Why Applovin Stock Is Accelerating Higher Thursday",
     "Applovin Corp (NASDAQ: APP) shares are ripping higher Thursday after the company reported "
     "better-than-expected financial results."),
    ("Creatd Shares Rise 87% Premarket", ""),
    ("Oppenheimer Hldgs Q1 Adj. EPS $4.46 Up From $2.72 YoY, Sales $445.095M Up From $367.825M YoY", ""),
])
def test_good_news_the_old_reader_called_neutral(headline, summary):
    assert _tone(headline, summary) == 1


@pytest.mark.parametrize("headline, summary", [
    ("JA Solar Likely To Lose Money For The First Time In 16 Quarters", ""),
    ("U.S. DOJ Says TD Failed To Monitor $18.3T In Customer Activity", ""),
    ("Moleculin Biotech Q1 EPS $(0.20) Down From $(0.15) YoY", ""),
    ("This Is What Whales Are Betting On Alibaba Group Holding",
     "Someone with a lot of money to spend has taken a bearish stance on Alibaba Group Holding (NYSE:BABA)."),
    ("Pfizer, BioNTech's COVID-19/Flu Combo Vaccine Fails To Meet Key Goal In Late-Stage Study", ""),
])
def test_bad_news_the_old_reader_called_neutral(headline, summary):
    assert _tone(headline, summary) == -1


@pytest.mark.parametrize("headline, summary", [
    # Words with no direction by themselves.
    ("Copel Reaches Record Date for Interest on Equity Distribution", ""),
    ("LG Display Announced 1.1T Won Investment To Build OLED Infrastructure", ""),
    ("Cognizant, CrowdStrike Expand Partnership To Launch CrowdStrike Falcon-Powered Cognizant Cybersecurity", ""),
    ("Cigna Healthcare Expands AI-Driven Personalized Care Management Programs", ""),
    ("Doosan Skoda Power Publishes FY2025 Annual Report", "Reporting a consolidated net profit of 521.4 million CZK."),
    ("Allstate Reports Oct. Catastrophe Loss Of $129M After-Tax", ""),
    ("Taco Bell Launches 'L.O.C.O.S.' Emotional Support Taco Program",
     "Taco Bell has introduced its first global 'Loss Or Celebration Outcome Support' program."),
    ("Pfeiffer Vacuum Releases Technical Guidance on O-ring Lubrication",
     "Highlighting the balance between sealing performance and contamination risks."),
    ("Mayo Clinic Study Highlights Wegovy For Weight Loss", ""),
    # Patterns that misfired on the archive.
    ("Human Death Toll Rises to 161 in China from H7N9 Bird Flu -Reuters", ""),
    ("Disney Park Prices Skyrocket", "Rising costs have made it difficult for families to afford."),
    ("Ironwood Pharma Reports FDA Fast Track Designation For Treatment Of Heart Failure with Preserved Ejection "
     "Fraction", ""),
    ("Is Adobe Gaining or Losing Market Support?", ""),
    ("Report: Apple Wants To Create A Better Way To Treat Diabetes", ""),
    ("Higher Fuel Prices Are Becoming A Very Meaningful Headwind", ""),
    ("A Look Into Lowe's Companies' Debt", "Shares of Lowe's moved higher by 3.45% in the past three months."),
    ("Return On Capital Employed Overview: Hertz Global Holdings", "The company lost a total of $356 million."),
])
def test_stories_with_no_direction_stay_neutral(headline, summary):
    assert _tone(headline, summary) == 0


def test_a_rise_despite_a_downgrade_is_no_longer_read_as_bad_news():
    # The one story the old reader called the wrong way round (it scored -1 on "downgrade").
    assert _tone("Clover Health Takes A Turn For The Better: What's Next?",
                 "Clover Health Investments Corp. (NASDAQ: CLOV) rose 4.42% on Monday despite receiving a downgrade "
                 "from JPMorgan.") == 0


@pytest.mark.parametrize("text, votes", [
    ("Installed Building Products Reports Q2 Adj. EPS $0.59 vs $0.55 Est., Sales $282.2M vs $275.3M Est.", (2, 0)),
    ("Akers Biosciences Reports Q3 Sales $675K vs $1.5M Est., Loss Of $1.17M vs $989K In Prior Year Period", (0, 1)),
    ("On Deck Capital Sees Q2 Sales $108M-112M vs $110.72M Est", (0, 0)),          # a range around the estimate
    ("Apple Sees Q2 Sales $63B-$67B vs $62.45B Estimate", (1, 0)),
    ("Wyndham Hotels & Resorts Sees FY2026 Adj EPS $4.62-$4.80 vs $4.97 Est", (0, 1)),
    ("Factset Sees Q2 EPS $1.78 to $1.82 vs $1.73 Est, Revenue $293M to $298M vs $296M Est", (1, 0)),
    ("Paramount Group Raises FY24 FFO From $0.73-$0.79 To $0.75-$0.81 Vs $0.73 Est.", (1, 0)),   # not "$0.79 T"
    ("Penn West Reports Q1 EPS $0.05 vs $(0.02) Est., Gross Sales $132M", (1, 0)),
    ("Veritone Reports Q2 Loss/Share $(2.94) vs. $(0.86) Est., Sales $4.09M vs. $3.27M Est.", (1, 1)),
    ("Sees Q1 loss per share $0.10-$0.12 vs $0.08 est", (0, 1)),                    # a bigger loss is worse
    ("Iridium Expects FY24 Service Revenue Growth of 5% (Prior 4%-6%)", (0, 0)),    # narrowed inside the old range
    ("Germany Manufacturing Purchasing Managers Index Preliminary For January 44.1 Vs. 42.7 Est.; 42.5 Prior", (0, 0)),
    ("France Harmonised Index of Consumer Prices (MoM) Preliminary For May -0.2% Vs. 0.1% Est.; 0.7% Prior", (0, 0)),
    ("Top picks for the week ahead", (0, 0)),
])
def test_figures_are_read_against_their_estimate_or_prior_view(text, votes):
    assert reference_votes(text) == votes


def test_a_beat_on_one_line_and_a_miss_on_the_other_still_nets_to_neutral():
    assert score_headline_sentiment(
        "Radius Health Q2 EPS $(0.95) Misses $(0.74) Estimate, Sales $50.11M Beat $48.40M Estimate") == 0
    assert score_headline_sentiment("Veritone Reports Q2 Loss/Share $(2.94) vs. $(0.86) Est., Sales $4.09M vs. $3.27M Est.") == 0


def test_analyst_notes_and_routine_notes_are_read_as_before():
    assert _tone("Barclays Maintains Underweight on Trex Co, Raises Price Target to $46") == 0
    assert _tone("BofA Securities Upgrades Trex to Buy and Raises Price Target") == 1
    assert _tone("Morgan Stanley Downgrades Rivian to Underweight") == -1
    assert _tone("Gray Media, Inc. Declares Quarterly Cash Dividend") == 0
