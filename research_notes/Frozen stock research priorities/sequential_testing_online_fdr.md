# Sequential Testing and Online False-Discovery-Rate Control for the Frozen_stock Experiment Engine

Scope: (a) calling forward verdicts on adopted gate changes earlier than the fixed 20-trade horizon while keeping valid error rates; (b) budgeting false adoptions across an open-ended weekly stream of experiments. Researched 2026-11-07. Numbers marked "[own sim]" come from a Monte Carlo I ran for this note (numpy, 4,000 replications, Student-t(3) per-trade returns scaled to the stated std, so fat-tailed). Script: `/private/tmp/claude-501/-Users-ainz-ool-gown-Cluade-AI/2ea8229a-b216-4986-9843-7f5aa979dc05/scratchpad/sim/s.py` and `s2.py`. These are simulations, not literature results.

Baseline arithmetic that frames everything below [own sim / standard formula n = ((z_{1-a}+z_{power})·s/d)^2, one-sided a=0.05, 80% power]:

| per-trade std s | edge 5 bps | 10 bps | 20 bps | 50 bps |
|---|---|---|---|---|
| 1% | 2,474 | 619 | 155 | 25 |
| 2% | 9,896 | 2,474 | 619 | 99 |
| 3% | 22,267 | 5,567 | 1,392 | 223 |

At 20 trades with s = 2%, the standard error of mean per-trade return is 200/sqrt(20) = 44.7 bps. So the current "revert if expectancy >= 10 bps worse after 20 trades" rule fires on about 41% of changes that have no true effect, and on about 9% of changes that are truly +50 bps better. This ignores the extra "loss cause no rarer" condition, which lowers both rates by an unknown amount [own sim, normal approximation]. A 20-trade verdict is a coin flip for edges under about 50 bps. No sequential method fixes this. Sequential methods save trades only when the true effect is large, and they stop wasting trades on clear cases. Effects of 5-20 bps cannot be confirmed forward at a handful of trades per week: they need thousands of trades. The forward stage should be built as a harm guardrail plus slow confirmation, not as a 20-trade judge.

## Always-valid inference, anytime-valid confidence sequences, e-values, mSPRT: applying them to mean per-trade return with optional stopping; heavy-tailed and bounded variants

### Takeaway
Use an e-process (test supermartingale) on per-trade returns. For long-only unlevered trades, the "betting" e-process K_t = prod(1 + lambda_i X_i) is valid for H0: mean <= 0 with no assumption beyond a known lower bound on the return. A normal-mixture mSPRT, or Eppo-style asymptotic confidence sequence with plug-in variance, has more power but is only asymptotically valid with fat tails. In simulation the mSPRT stops at a median of about 85 trades for a 50 bps edge at 2% std, and about 22 trades for a 100 bps edge. It almost never stops early when the effect is zero.

### Cited Findings
- **Core concepts.** A confidence sequence (CS) is a sequence of intervals C_t with P(for all t: mu in C_t) >= 1 - alpha. Coverage therefore holds at any data-dependent stopping time. The confseq package implements the uniform boundaries and CSs from Howard, Ramdas, McAuliffe and Sekhon ("Time-uniform, nonparametric, nonasymptotic confidence sequences", arXiv 1810.08240; I did not re-verify in this session that it appeared in Annals of Statistics in 2021) — [confseq on PyPI](https://pypi.org/project/confseq/)
- **mSPRT and always-valid p-values (Johari, Pekelis and Walsh, "Always Valid Inference", arXiv 1512.04922, v3 2019).**
  - Mixture likelihood ratio: Lambda_n = integral of prod_i f_theta(X_i)/f_theta0(X_i) dH(theta). Stop at the first n with Lambda_n >= 1/alpha.
  - Always-valid p-value: p_n = inf{alpha : T(alpha) <= n}. The always-valid CI is {theta : p_n^theta > alpha} and stays valid at any stopping time.
  - Choosing the mixing scale tau: it should be "in the ballpark" of the expected effect. Being off by about one order of magnitude costs under 5% power and up to 10% run length. Being off by two orders costs about 20% power and 40% run length.
  - The paper cites a companion 2017 conference paper on the Optimizely implementation.
  - Source: [Johari et al. arXiv HTML](https://arxiv.org/html/1512.04922v3)
- **Closed form of the normal mixture** (standard derivation, my algebra rather than quoted). With known variance sigma^2, null theta0 and H = N(theta0, tau^2):
  - Lambda_n = sqrt(sigma^2/(sigma^2 + n tau^2)) · exp( n^2 tau^2 (xbar_n - theta0)^2 / (2 sigma^2 (sigma^2 + n tau^2)) ).
  - p_n = min(p_{n-1}, 1/Lambda_n).
  - Inverting gives the CS xbar_n ± sqrt( sigma^2 (sigma^2 + n tau^2) / (n^2 tau^2) · log((sigma^2 + n tau^2)/(sigma^2 alpha^2)) ). Its form matches the Eppo formula below.
  - Source: [Johari et al.](https://arxiv.org/html/1512.04922v3) for the general form.
- **Eppo's production asymptotic CS.**
  - [L, U] = mu_hat ± sigma_hat · B.
  - B = (1/sqrt(t)) · sqrt((t + rho) · log((t + rho)/(rho (1 - c)^2))).
  - rho = N_tune / [log(log(e/(1 - c)^2)) - 2 log(1 - c)].
  - N_tune is where the ratio of sequential width to fixed-sample width is smallest. Eppo uses N_tune = 10,000, which is far too large for Frozen_stock.
  - Eppo describes this as a modified Eq. 14 of Howard et al., rescaled by the estimated standard error.
  - Source: [Eppo statistical details](https://docs.geteppo.com/statistics/confidence-intervals/statistical-nitty-gritty/)
- **Asymptotic CSs (Waudby-Smith, Arbour, Sinha, Kennedy and Ramdas, "Time-uniform central limit theory and asymptotic confidence sequences", arXiv 2103.06476, v9 March 2024).**
  - They "forgo nonasymptotic validity for CLT-like versatility" and use strong invariance principles to approximate the whole sample-mean process by a Gaussian process.
  - This is the theory behind plug-in-variance CSs such as Eppo's. Validity is in the limit only, so coverage at 20-60 fat-tailed trades is not guaranteed.
  - Source: [arXiv 2103.06476](https://arxiv.org/abs/2103.06476)
- **Betting CSs for bounded data (Waudby-Smith and Ramdas, "Estimating means of bounded random variables by betting", arXiv 2010.09686).** Assumptions: X_t in [0,1], E[X_t | F_{t-1}] = mu for all t (independence not required), and predictable bets lambda_t.
  - **PrPl-EB CS:**
    - mu_hat_t = (1/2 + sum X_i)/(t+1).
    - sigma_hat^2_t = (1/4 + sum (X_i - mu_hat_i)^2)/(t+1).
    - lambda_t = min( sqrt(2 log(2/alpha) / (sigma_hat^2_{t-1} · t · log(1+t))), c ), with c = 1/2 or 3/4.
    - v_i = 4 (X_i - mu_hat_{i-1})^2 and psi_E(lambda) = (-log(1 - lambda) - lambda)/4.
    - C_t = sum lambda_i X_i / sum lambda_i ± (log(2/alpha) + sum v_i psi_E(lambda_i)) / sum lambda_i. The running intersection is also valid.
  - **Hedged capital CS:**
    - K_t^±(m) = max{theta K_t^+(m), (1 - theta) K_t^-(m)}, where K^+ = prod(1 + lambda_i^+(m)(X_i - m)) and K^- = prod(1 - lambda_i^-(m)(X_i - m)).
    - lambda^+ = |lambda~| ∧ c/m and lambda^- = |lambda~| ∧ c/(1 - m).
    - The CS is {m : K_t^±(m) < 1/alpha}. Compute it by grid search over m or by root-finding.
    - The paper reports these "empirically vastly outperform" Hoeffding and empirical-Bernstein bounds.
  - Source: [ar5iv 2010.09686](https://ar5iv.labs.arxiv.org/html/2010.09686) and [arXiv abstract](https://arxiv.org/abs/2010.09686)
- **Heavy tails.** Wang and Ramdas, "Catoni-style confidence sequences for heavy-tailed mean estimation" (Stochastic Processes and their Applications 2023, arXiv 2202.01250):
  - Requires only a known upper bound on the variance, not boundedness or sub-Gaussianity.
  - Width is O~(t^{-1/2}), roughly matching sub-Gaussian methods.
  - Extends to infinite variance with a finite p-th moment for 1 < p < 2.
  - Source: [arXiv 2202.01250](https://arxiv.org/abs/2202.01250)
- **Bayesian optional stopping is not a free lunch.** Microsoft's Deng, Lu and Chen (2016) show Bayesian testing stays valid under continuous monitoring only when the stopping rule is "proper". A 2025 critique argues that a fixed posterior-probability threshold under peek-and-stop does not control false positives — [Deng et al. arXiv 1602.05549](https://arxiv.org/abs/1602.05549); [Molas 2025](https://alexmolas.com/2025/10/30/bayesian-ab-test-peeking.html)
- **Online multiple testing with e-values.** E-values are central to sequential testing ("every admissible sequential test uses an e-value") — [Xu and Ramdas, AISTATS 2024](https://proceedings.mlr.press/v238/xu24a.html)
- **Simulated stopping times [own sim].** Setup: t(3) returns, s = 2%, alpha = 0.05 one-sided, 10-trade burn-in, plug-in variance floored at 1%.
  - **No true effect:**
    - mSPRT (tau = 30-100 bps) falsely stopped by 600 trades in 1.5-2.1% of runs. It is conservative even with fat tails and plug-in variance.
    - PrPl-EB on returns clipped to ±10% falsely stopped in 0.1% of runs.
  - **True +50 bps edge:**
    - mSPRT (tau = 50 bps) had stopped by 20 / 40 / 60 / 100 / 200 trades in 12% / 25% / 37% / 57% / 85% of runs. Median stopping time was about 85 trades, and the mean over runs was about 113-120.
    - PrPl-EB mean stopping time was about 278 trades, roughly 2.4 times slower, because the [-10%, +10%] range is wide relative to the 2% std.
  - **True +100 bps edge:** median stop was 22 trades, and 91% of runs had stopped by 60.
  - **Harm detection** (true -50 bps, mirrored test): median 88 trades.

### Inferences
- **Assumption check against Frozen_stock data.**
  - Per-trade returns are not i.i.d. Regimes shift and trades overlap. The betting and e-process constructions only need E[X_t | F_{t-1}] <= 0 under H0 (for each trade given the past), which is weaker than i.i.d. That is the right assumption to target.
  - Neither returns nor P&L are bounded in the textbook sense, because gaps can jump through stops.
- **Practical bound trick (my derivation from the betting construction).** For H0: E[X_t | F_{t-1}] <= 0, K_t = prod_i (1 + lambda_i X_i) with predictable lambda_i in [0, 1/B) is a nonnegative supermartingale whenever X_i >= -B.
  - Only a lower bound is needed. Long-only, unlevered returns satisfy X >= -1 with certainty, so B = 1 gives a fully assumption-free test.
  - Its power is poor, though. The growth rate is lambda·mu - lambda^2 s^2/2. For mu = 50 bps and s = 2% the optimum is lambda* = mu/s^2 = 12.5, far above the cap of 1.
  - Using B = the worst plausible single-trade loss (for example 10-15%, from stop-loss settings plus gap history) permits lambda near the optimum. Treat any observed X < -B as a model-breach alarm that freezes the verdict.
  - Testing harm (H0: mean >= 0, with K = prod(1 - lambda X)) needs an upper bound on gains instead.
- **Recommended e-process (implementable in about 30 lines).** Use a mixture over a fixed lambda grid:
  - E_t = (1/K) sum_k prod_{i<=t} (1 + lambda_k X_i), with lambda_k in {0.5, 1, 2, 4, 8, 12}/B ∧ 0.9/B.
  - An average of test martingales is a test martingale, so this is valid without tuning and is robust to not knowing the edge.
  - Run two of them: "improvement" on X and "harm" on -X (the harm version needs the upper bound).
- **Paired shadow differences cut variance.** Feed the e-process paired differences, not raw returns. For a gate (filter) change, the forward difference between new and old settings equals minus the return of the trades the new gate blocked, plus any trades it newly admits.
  - If the bot shadow-tracks blocked trades (the "live shadow gate" concept), each blocked trade is direct evidence, recorded at zero cost.
  - The test becomes: is the mean return of blocked trades < 0? Market exposure that is common to both arms cancels.
  - Subtracting SPY's return over the same holding period, or beta·SPY, from each trade also removes the shared market factor. This reduces variance and the cross-strategy dependence discussed under online FDR below.
- **Predictability pitfall with overlapping trades.** lambda_i must be fixed using only trades that closed before trade i opened, not before trade i closed. Otherwise partial-path information leaks in.
  - Trades opened on the same day should be treated as one batch observation (their average). A product over batches is still valid.
- **Tuning for 20-200 trades.**
  - mSPRT: tau ≈ the edge you care about (30-50 bps).
  - Eppo-style CS: N_tune ≈ 50-100 trades, not 10,000.
  - Betting: lambda grid as above, alpha_harm = 0.10-0.20 because reverting is cheap, alpha_confirm per the online-FDR budget.
  - If parametric power is wanted, the mSPRT with plug-in variance can be the primary test. Its asymptotic validity looked fine in the fat-tailed simulation, with false stops below alpha. The betting e-process can serve as the assumption-light cross-check.

### Gaps
- I could not render the Howard et al. PDF in this session. Equation 14 is quoted only through Eppo's adaptation, and the confseq API function names were not verified.
- confseq licence: the PyPI classifier says MIT, but I did not find the LICENSE file. It needs C++14 and pybind11 to build — [PyPI](https://pypi.org/project/confseq/)
- I found no paper evaluating CSs specifically on per-trade strategy returns.
- The ±10% clip used in my PrPl-EB simulation biases the mean if real returns exceed it. The lower-bound-only betting test above avoids the clip, but I did not simulate it.

## Classic SPRT and group-sequential designs (O'Brien-Fleming, Pocock, alpha-spending): when preferable and how many trades they save

### Takeaway
When a maximum sample size can be fixed in advance (for example 60 or 110 trades), group-sequential tests with 3-5 looks have more power than always-valid methods. Spotify found about 0.90 vs 0.67-0.77 power. In simulation they cut expected trades by 30-70% when the effect is large. They save nothing when the effect is zero, and they inflate errors if you keep watching past the planned maximum, which the current "watch to 60" practice invites.

### Cited Findings
- **Spotify's comparison** (2023; 500 users per group, one-sided alpha 5%, effect 0.2 SD, 100k replications):
  - Power: GST with alpha-spending about 0.90 (quadratic) and 0.93 (cubic); GAVI 0.72-0.76; mSPRT 0.67-0.77; Bonferroni about 0.75 with 14 looks but about 0.40 when streaming.
  - Overrunning the planned maximum pushed GST false positives to about 0.07-0.08 against a 0.05 target.
  - Mis-tuned GAVI (tuned for n = 10, actual n = 500) lost about 15% power relative to correctly tuned GAVI.
  - Recommendation: GST when the maximum sample size can be estimated; always-valid inference when it is unknown or overrun is likely, or for streaming early detection of large regressions.
  - The corrected-alpha approach (CAA) is not recommended.
  - Caveat: the comparison assumed known variance and normal data.
  - Source: [Spotify Engineering](https://engineering.atspotify.com/2023/03/choosing-sequential-testing-framework-comparisons-and-discussions)
- **Simulated GSTs [own sim].** Setup: t(3) returns, s = 2%, one-sided alpha 0.05, Nmax = 110 trades (about the fixed-n for 80% power at 50 bps), K = 5 equally spaced looks (22, 44, ..., 110). O'Brien-Fleming boundaries z_k >= 1.81·sqrt(5/k); Pocock boundary z_k >= 2.18 at every look.
  - Null false-reject rate: 4.5% (OBF) and 4.1% (Pocock). Expected stopping time 109 and 108 trades, so no savings.
  - True +50 bps: power 84.5% (OBF, E[n] = 78) and 80.4% (Pocock, E[n] = 65). That saves 29-41% against the 110-trade fixed horizon.
  - True +100 bps: power 99.6% for both, E[n] = 47 (OBF) and 32 (Pocock), a 57-71% saving.
  - A second run with Nmax = 600 and 5 looks gave E[n] about 465 at 20 bps (78% power) and about 237 at 50 bps.

### Inferences
- **Classic Wald SPRT** (simple H0: mu = 0 vs H1: mu = mu1, log-LR increments (mu1/s^2)(X - mu1/2), stop at log((1-beta)/alpha) or log(beta/(1-alpha))) needs a point alternative and a known variance. It is fragile under fat tails and a misspecified mu1, so it is not preferred.
- **Mixture SPRT** removes the point-alternative problem and is the "no maximum" option.
- **OBF vs Pocock.** OBF spends almost no alpha early, so early looks rarely stop unless the effect is huge, and its final critical value stays near fixed-n. Pocock stops sooner on large effects but costs more at the final look.
- **Choice for Frozen_stock.**
  - Use OBF-like spending for confirmation, because premature confirmation is costly.
  - Use Pocock-like spending, or the always-valid harm e-process, for reversion, because big losers should be cut fast.
  - The Lan-DeMets alpha-spending functions (OBF-type alpha(f) = 2 - 2·Phi(z_{alpha/2}/sqrt(f)), Pocock-type alpha·log(1 + (e - 1) f)) allow unequal, trade-count-driven look times. These are standard textbook formulas that I did not re-source this session.
- **Do not watch past the planned maximum.** "Confirmed at 20 / watch to 60" is effectively an unplanned extra look. If a GST is used, Nmax must be a hard stop, after which the default action applies. Otherwise use an always-valid method, which tolerates indefinite monitoring.
- **Realistic time savings at a few trades per week per strategy.**
  - At 2 trades a week, a 110-trade GST is about a one-year horizon. It ends in about 4-6 months for +/-100 bps effects and runs to the full year for small or zero effects.
  - Compared with the current 20-trade verdict (about 10 weeks), valid methods take longer on average but stop being wrong 25-41% of the time.

### Gaps
- Not verified this session: the exact Lan-DeMets formulas, and a Python GST library with its licence. Candidates I did not check include `statsmodels` (BSD), which has no GST module that I know of; `rpact` and `gsDesign` are R packages.
- I did not simulate looks at irregular trade counts.

## Online FDR control for a stream of hypotheses (alpha-investing, GAI, LORD, LOND, SAFFRON, ADDIS, e-LOND, online e-BH): robustness to dependence and to unfinished tests; applying it when a discovery is an adoption

### Takeaway
Because Frozen_stock's candidates are scored on the same shared history and strategies share market exposure, only e-value methods are safe: e-LOND, or its uniform improvement online e-BH. They control FDR under arbitrary unknown dependence. p-value methods such as LORD, SAFFRON and ADDIS assume independence, PRDS or local dependence. Count a "discovery" only when the forward e-process for an adopted change crosses its online level. Backtest selection is a screen, not the test.

### Cited Findings
- **e-LOND** (Xu and Ramdas, AISTATS 2024, PMLR 238; arXiv 2311.06412):
  - Controls online FDR under arbitrary, possibly unknown, dependence.
  - Has randomized extensions for extra power and for online selective confidence intervals.
  - Motivation: "unknown dependencies" arise when the same dataset is used to evaluate many hypotheses, which is exactly the weekly backtest replay.
  - Source: [PMLR](https://proceedings.mlr.press/v238/xu24a.html); [arXiv](https://arxiv.org/abs/2311.06412v1)
- **Online e-BH** (Fischer, Xu and Ramdas, arXiv 2407.20683) uniformly improves e-LOND. A 2026 preprint (arXiv 2603.24792) adds online e-closure and compound e-values — [arXiv 2407.20683](https://arxiv.org/pdf/2407.20683v3); [arXiv 2603.24792](https://arxiv.org/pdf/2603.24792.pdf)
- **Asynchronous online testing** (Zrnic, Ramdas and Jordan, JMLR 22(33):1-39, 2021):
  - Each test may itself be sequential, starting and stopping at arbitrary times.
  - Defines "local dependence" via conflict sets, as a middle ground between independence and arbitrary dependence.
  - Gives asynchronous LOND, LORD and SAFFRON with FDR guarantees.
  - This is the formal answer to "tests that are never finished".
  - Source: [JMLR](https://JMLR.org/papers/v22/19-910.html)
- **onlineFDR (Bioconductor R package, GPL-3)** implements LOND, LORD and LORD++ (including a "discard" variant), SAFFRON, ADDIS, alpha-investing, and asynchronous versions of LOND, LORD, SAFFRON (per Zrnic et al. 2021) and ADDIS (per Tian and Ramdas 2019).
  - ADDIS recovers the power SAFFRON loses when nulls are conservative.
  - GPL-3 means copying its code would make the derived code GPL. Re-implementing from the papers' formulas avoids that.
  - Source: [onlineFDR manual](https://free.nchc.org.tw/bioconductor/packages/devel/bioc/manuals/onlineFDR/man/onlineFDR.pdf); [rdrr package page](https://rdrr.io/bioc/onlineFDR/man/onlineFDR-package.html)
- **Algorithm sketches.** The formulas are written from my knowledge of the original papers, not quoted this session. Verify them against the PDFs before coding. The sequence gamma_t >= 0 sums to at most 1. D(t-1) is the number of discoveries before t, and tau_j is the time of the j-th discovery.
  - **LOND** (Javanmard and Montanari, arXiv 1502.06197): alpha_t = alpha · gamma_t · (D(t-1) + 1); reject if p_t <= alpha_t. Controls FDR under independence and PRDS. Arbitrary dependence needs a reshaped (harmonic-corrected) gamma, per Zrnic et al.
  - **e-LOND**: reject H_t if e_t >= 1/(alpha · gamma_t · (D(t-1) + 1)). Valid under arbitrary dependence, with no reshaping needed.
  - **LORD++** (Ramdas et al. 2017, arXiv 1710.00499): alpha_t = gamma_t W0 + (alpha - W0) gamma_{t - tau_1} + alpha · sum_{j>=2, tau_j < t} gamma_{t - tau_j}, with W0 <= alpha. The usual default is gamma_j ∝ log(max(j,2)) / (j · e^{sqrt(log j)}) (constant ≈ 0.0772). Needs independent null p-values.
  - **SAFFRON** (Ramdas et al. 2018, arXiv 1802.09098): candidates are p_t <= lambda (default lambda = 1/2, W0 = alpha/2).
    - alpha_t = min{lambda, W0 gamma_{t - C_{0+}} + ((1 - lambda) alpha - W0) gamma_{t - tau_1 - C_{1+}} + (1 - lambda) alpha · sum_{j>=2} gamma_{t - tau_j - C_{j+}}}, where C_{j+} counts candidates after tau_j.
    - Adapts to a high fraction of true effects. Here the fraction is low (memory says 0 proven), so SAFFRON's adaptivity buys little.
  - **ADDIS** (Tian and Ramdas 2019, arXiv 1905.11465): adds discarding, so p > tau_discard (0.5) is skipped. Defaults lambda = 0.25, tau = 0.5. Helps when nulls are conservative, as gate tweaks that change nothing are.
  - **Alpha-investing** (Foster and Stine 2008, JRSSB):
    - Start with wealth W(0) = alpha·eta.
    - At each test, spend alpha_j <= W/(1 + W).
    - On rejection add a payout omega <= alpha; otherwise subtract alpha_j/(1 - alpha_j).
    - Controls mFDR = E[V]/(E[R] + eta).
    - Generalized alpha-investing (Aharoni and Rosset 2014) is the family that contains LORD and SAFFRON.

### Inferences
- **Why p-value methods break here.**
  1. Each week's backtest replay reuses almost the same history, so successive tests of the same or similar candidates are strongly positively dependent.
  2. Candidates are selected by the same data that scores them, so the backtest "p-value" is not super-uniform after selection.
  3. Strategies share market exposure.

  LORD and SAFFRON guarantees do not cover (1) or (2). e-LOND and online e-BH cover arbitrary dependence, but the e-value itself must still be valid.
- **Two-stage design** (my construction from the cited properties):
  - Stage 1 (backtest screen): keep the pre-registered rule as a selection filter only. Log every candidate tried (N_trials) to feed a deflated-Sharpe or haircut correction (see the finance section).
  - Stage 2 (forward test): on adoption, open hypothesis H_t: "the change does not improve per-trade return (paired shadow difference <= 0)". Its level is fixed then: alpha_t = alpha_FDR · gamma_t · (D + 1), with D counting confirmed discoveries so far.
  - Run the forward betting e-process. Declare a discovery ("confirmed") only when E_t >= 1/alpha_t. Forward data arrives after selection, so the forward e-process is valid conditional on the backtest selection. Using the in-sample backtest does not invalidate it.
  - Optionally multiply by a backtest e-value only if that e-value is genuinely valid. That is rare given the data reuse.
- **Unfinished tests.** An e-process can be monitored forever, and a test that never crosses simply never counts as a discovery. For asynchrony, fix alpha_t at the test's start using the discoveries known then. Fewer counted discoveries only lowers alpha_t, so this is conservative (my inference; Zrnic et al. formalize the general asynchronous case).
- **Reversion needs no FDR budget.** A revert restores the status quo, so use a separate harm e-process at alpha_harm = 0.1-0.2 per experiment. Optionally cap the family-wise rate of false reverts with alpha-spending across experiments if reverts prove costly.
- **The cost is real.**
  - Example: with alpha_FDR = 0.1 and gamma_1 ≈ 0.07, the first test needs e >= about 140, i.e. log(140) ≈ 4.9 units of log-wealth.
  - At the optimal growth for 50 bps / 2% std (about mu^2/(2s^2) = 0.031 per trade), that takes roughly 160 trades.
  - For 20-bps edges a formal confirmation is effectively out of reach, so most adopted changes stay "provisional/explore" indefinitely. That is honest; the alternative is accumulating false adoptions.
  - Use a gamma sequence that decays slowly early on (for example a front-loaded "online Bonferroni" style), or online e-BH, to soften this.
- **Alpha-death and decaying memory.** In an open-ended stream with few true effects, LORD/LOND wealth decays and later tests become nearly impossible. Ramdas et al. (2017) proposed decaying-memory FDR for this. e-LOND has the same gamma_t decay, so reset or segment the stream (for example a yearly FDR budget per strategy family) as a pragmatic, explicitly documented choice.

### Gaps
- I did not verify the exact e-LOND theorem conditions (for example whether e-values must be valid conditional on the past rejection set) or the online e-BH algorithm. Read [arXiv 2311.06412](https://arxiv.org/abs/2311.06412v1) and [2407.20683](https://arxiv.org/pdf/2407.20683v3) before implementing.
- I found no maintained, permissively licensed Python package for online FDR. I believe an older `onlinefdr`-style PyPI package exists but did not verify it. The algorithms are about 10-20 lines each, so re-implementing them is the practical route.
- The arXiv IDs 1502.06197, 1710.00499, 1802.09098 and 1905.11465 and the Foster and Stine / Aharoni and Rosset venues are from my background knowledge and were not fetched this session.

## Bayesian alternatives (posterior probability of positive edge, Bayesian sequential stopping, hierarchical shrinkage across strategies) and how they compare

### Takeaway
With a realistic skeptical prior (true edges around ±20 bps), 20 forward trades at 2% std move the posterior only about 17% of the way toward the sample mean, so Bayesian verdicts at 20 trades are mostly prior. The useful Bayesian piece is hierarchical or empirical-Bayes shrinkage across all adopted changes, which is what the engine audit's calibration factor approximates. Posterior thresholds do not by themselves control frequentist false-adoption rates under optional stopping.

### Cited Findings
- Bayesian testing with continuous monitoring is valid only for proper stopping rules, and the Bayes factor must use all observations to the stopping time — [Deng, Lu and Chen 2016](https://arxiv.org/abs/1602.05549)
- "Peek as often as you want" with a fixed posterior threshold does not control false positives — [Molas 2025](https://alexmolas.com/2025/10/30/bayesian-ab-test-peeking.html)
- Eppo offers fixed-sample, sequential and Bayesian modes side by side — [Eppo](https://docs.geteppo.com/statistics/confidence-intervals/statistical-nitty-gritty/)
- **Simulated skeptical prior [own sim].** Setup: prior N(0, (20 bps)^2), known s = 200 bps, conjugate normal.
  - Shrinkage factor on the sample mean: 0.17 at n = 20 and 0.38 at n = 60.
  - For a true +50 bps change, P(posterior P(mu > 0) > 0.95) is 0.5% at n = 20 and 21% at n = 60.
  - For a null change it is 0.1% and 0.6%.

### Inferences
- **Hierarchical model.** Write delta_k for the true effect of adopted change k, and Y_k for its forward paired mean with standard error se_k. Model delta_k ~ N(m, tau^2) and Y_k ~ N(delta_k, se_k^2).
  - Estimate m and tau^2 by marginal maximum likelihood across all changes ever adopted.
  - The posterior mean is delta_k_hat = m + (tau^2/(tau^2 + se_k^2))(Y_k - m).
  - The regression slope of realised forward gain on backtest-expected gain across experiments is exactly the "calibration factor" the engine audit tracks. Estimate it hierarchically, with an uncertainty band, rather than as a single running ratio.
- **Practical comparison.**
  - Frequentist e-processes give guaranteed error rates under optional stopping but are slow.
  - Bayesian shrinkage gives better point estimates and an honest "probability this change helps", but no guarantee if the prior is wrong.
  - A good combination: e-processes for the adopt, confirm and revert decisions; hierarchical shrinkage for sizing expectations and for the audit's calibration.
- **Bayesian decision-theoretic stopping** (expected-loss threshold, e.g. stop when E[loss of choosing arm] < epsilon) is natural here because reverting is cheap, but it should be checked by simulation for frequentist error.

### Gaps
- I found no published hierarchical-shrinkage study of trading-rule changes specifically.
- Statsig's Bayesian and sequential documentation was not fetched.

## Use in quant finance and A/B-testing practice: trading-strategy validation, multiple testing in finance, combining backtest selection with forward validation

### Takeaway
Finance practice handles multiplicity mainly at the backtest stage: Harvey-Liu-Zhu's t > 3.0 hurdle, Bailey and López de Prado's deflated Sharpe ratio, and haircuts. Sequential, anytime-valid forward validation is standard in tech A/B testing (Optimizely, Eppo, Spotify), but I found no documented use for live trading-strategy validation. Frozen_stock would be combining the two.

### Cited Findings
- **Harvey, Liu and Zhu, "...and the Cross-Section of Expected Returns"** (NBER w20592 / SSRN 2513152; Review of Financial Studies 2016): a newly discovered factor needs a t-ratio above 3.0, not 2.0, to account for multiple testing — [SSRN](https://papers.ssrn.com/abstract=2513152); [NBER](https://nber.org/papers/w20592)
- **2026 NBER follow-up** (w34898, Harvey, Sancetta and Zhao) reportedly again argues for a cutoff of at least 3.0. This is from a search snippet only; I could not read the PDF — [NBER w34898](https://www.nber.org/system/files/working_papers/w34898/w34898.pdf)
- **Deflated Sharpe Ratio** (Bailey and López de Prado, Journal of Portfolio Management 2014, SSRN 2460551):
  - Corrects for selection bias across N trials and for non-normal returns.
  - It is a probabilistic Sharpe ratio whose benchmark is the expected maximum Sharpe among N zero-skill trials.
  - The Sharpe ratio must be computed at the native frequency (per trade here).
  - Source: [SSRN](https://papers.ssrn.com/abstract=2460551); [quantstrat deflatedSharpe docs](https://www.rdocumentation.org/packages/quantstrat/versions/0.16.7/topics/deflatedSharpe)
  - My recollection of the formula, to be verified against the paper:
    - PSR(SR*) = Phi( (SR_hat - SR*) sqrt(T - 1) / sqrt(1 - g3 SR_hat + (g4 - 1)/4 · SR_hat^2) ).
    - SR* = sqrt(V[SR_n]) · ((1 - gamma_E) Phi^{-1}(1 - 1/N) + gamma_E Phi^{-1}(1 - 1/(N e))).
    - Here g3 is skew, g4 is kurtosis and gamma_E ≈ 0.5772.
- **Backtest-overfitting research at Man/Oxford-Man** (Harvey and Liu's "Backtesting") — [Man Group](https://www.man.com/insights/backtesting)
- **Production sequential testing:**
  - Spotify uses GSTs — [Spotify](https://engineering.atspotify.com/2023/03/choosing-sequential-testing-framework-comparisons-and-discussions)
  - Eppo uses asymptotic CSs — [Eppo](https://docs.geteppo.com/statistics/confidence-intervals/statistical-nitty-gritty/)
  - Optimizely uses mSPRT and always-valid p-values — [Johari et al.](https://arxiv.org/html/1512.04922v3)

### Inferences
- **Applying deflation to the engine.** Frozen_stock runs weekly searches (342-trial libraries and similar), so N_trials is large and growing. A backtest candidate's improvement should be deflated by the cumulative number of candidates tried for that strategy, not just the current week's. The current "every window not lower" rule has no multiplicity correction, which is why false adoptions accumulate.
- **Practitioner pattern to copy:** select in sample with a multiplicity-adjusted hurdle (t > 3 or DSR > 0.95 on the paired difference), then confirm out of sample on fresh data with an independent test. The forward e-process plays the role of the out-of-sample test while allowing early stopping.

### Gaps
- I found no source documenting anytime-valid or online-FDR use for live strategy validation at a fund.
- Netflix's sequential-testing papers (e.g. Lindon et al., anytime-valid tests for regressions, about 2022) were not fetched or verified.
- The Harvey and Liu "Backtesting" haircut numbers were not extracted.

## Concrete recommendation: what should replace or augment the fixed 20-trade verdict and the per-experiment adoption rule, with expected time savings

### Takeaway
Replace the fixed 20-trade verdict with two always-valid e-processes on paired shadow differences: a fast harm test that reverts at alpha 0.1-0.2, and a slow confirmation test at an e-LOND online level. Add a hard cap (for example 120 trades) after which the change stays "provisional". Add deflation by cumulative trials to the backtest rule. Expect valid harm reverts in about 20-90 trades for -50 to -100 bps changes, and no change in time-to-verdict for small effects, which honestly cannot be judged at this trade rate.

### Cited Findings
- **Always-valid methods tolerate indefinite monitoring and unknown sample size; GSTs give more power with a planned maximum but overrun inflates errors** — [Spotify](https://engineering.atspotify.com/2023/03/choosing-sequential-testing-framework-comparisons-and-discussions)
- **e-value online FDR is valid under arbitrary dependence** — [Xu and Ramdas 2024](https://proceedings.mlr.press/v238/xu24a.html)
- **Asynchronous or sequential tests within an online FDR stream** — [Zrnic et al. 2021](https://JMLR.org/papers/v22/19-910.html)
- **Simulated stopping times [own sim]:**
  - mSPRT median: about 22 trades at 100 bps, about 85 at 50 bps, about 88 for -50 bps harm.
  - GST (Nmax 110): E[n] 32-47 at 100 bps and 65-78 at 50 bps.
  - The current rule has about a 41% false-revert rate for null changes at 20 trades (s = 2%).

### Inferences
- **Proposed algorithm, per adopted change k:**
  1. **Data.** x_i is the paired per-trade difference. Use the return of trades the new gate admits minus the shadow return of trades it blocks, each net of costs and minus beta·SPY over the same holding period. Batch trades that open on the same day.
  2. **Harm e-process.** H_t = mean over a lambda grid of prod(1 - lambda x_i), with lambda in {1, 2, 4, 8}/B_up and B_up = the worst plausible single-trade gain difference (for example 15%). Revert when H_t >= 1/alpha_harm, with alpha_harm = 0.10. This guarantees at most a 10% chance of ever falsely reverting a non-harmful change. Optional: also require the loss-cause condition, which can only reduce false reverts further.
  3. **Improvement e-process.** E_t = mean over the lambda grid of prod(1 + lambda x_i), with B_low = the worst plausible loss difference. Confirm as a discovery when E_t >= 1/alpha_k, where alpha_k = alpha_FDR · gamma_k · (D + 1), alpha_FDR = 0.10, and gamma is fixed in advance and summable. Use e-LOND now and online e-BH when it is implemented.
  4. **Reporting.** Show a 90% confidence sequence (betting/PrPl-EB, or an asymptotic CS with N_tune ≈ 60) and the expected-vs-realised figures. Feed (expected, realised, se) into the hierarchical shrinkage model that sets the audit's calibration factor.
  5. **Cap.** At 120 trades with neither boundary crossed, label the change "inconclusive". Keep it if the confidence-sequence lower bound exceeds -10 bps (non-inferior); otherwise revert to the simpler configuration. Because the e-processes are always-valid, monitoring past the cap does not break them, unlike a GST.
  6. **Backtest adoption rule.** Keep the pre-registered windows rule. Add (a) the paired-difference t-stat >= 3 (Harvey, Liu and Zhu style) or a deflated Sharpe > 0.95 using cumulative N_trials for that strategy, and (b) a cap on concurrent provisional adoptions per strategy, so forward evidence is not confounded across changes.
- **Expected time and accuracy relative to the current rule** (s ≈ 2%, from the simulations):

  | True effect | Current rule (20 trades) | Proposed rule |
  |---|---|---|
  | -100 bps harm | caught at 20 trades | reverts at about 20-30 trades (similar speed, controlled error) |
  | -50 bps harm | caught at 20 trades | median about 88 trades: slower but valid; the current verdict here is noisy |
  | No effect | reverts about 41% of the time | about 10% false reverts at most; runs to the cap, then non-inferiority |
  | +50 bps | about 9% false revert | confirms at a median of about 85 trades at alpha 0.05; slower under the e-LOND alpha_k |
  | +5 to +20 bps | coin flip | stays provisional (needs 600-10,000 trades) |

  The net gain is not speed on average. It is replacing a verdict that is wrong 25-41% of the time with one whose false adoptions and false reverts are bounded, while still cutting large losers in a few weeks.
- **Pitfalls checklist:**
  - Bets computed with information from overlapping open trades.
  - Clipping returns to fake a bound, which biases the mean.
  - Plug-in-variance CSs at n < 30 with fat tails, so use a burn-in and a variance floor.
  - Re-adopting a reverted candidate without counting it as a new trial.
  - Several changes active on one strategy at once, so effects cannot be attributed.
  - GST overrun.
  - Using backtest p-values in LORD or SAFFRON despite data reuse.
  - Alpha-death in a long stream.
  - Survivorship in the trial counts.

### Gaps
- My simulations assume stationary t(3) returns and a perfect shadow pairing. Real paired-difference variance from the bot's shadow data should be measured before the lambda grid and caps are finalized.
- The trade rate per strategy (needed to convert trades to calendar time) was not available; the "2 trades/week" figure is an assumption.
- The e-LOND alpha level could not be checked against its theorem in this session.
