# Player-level model audit

Date: 2026-09-08. Covers `scripts/backtest.py` (player model), the board wiring in
`index.html`, and the docs in `method.html`.

## 1. Leak audit — is the backtest honest?

The walk-forward grading claims every prediction for week *w* uses only games before *w*.
Verified:

- **Features**: prediction at week *w* uses only the player's games before *w*
  (`weeks[:wi]`) and the position baseline queried with `before_seq` — no future games.
  The baseline pool is built from all games but always time-filtered in walk-forward. ✓
- **Calibration**: fitted on weeks 1–12, scored on weeks 13–18. Honest temporal holdout. ✓
  (Production knots are fitted on the full season — standard practice, documented in the payload.)
- **Line proxy**: 2025 has no archived sportsbook lines, so Sleeper's pre-week projection
  stands in as the line. Empirically plausible — the errors look like genuine pre-week
  misses (pass_yds MAE 57.5, rec_yds 17.1), not retrofitted numbers. Flagged as an
  assumption in the payload (`backtest_line_source`).

**Two leaks found and fixed on this branch:**

1. `build_logs` zero-filled a missing stat using the *full-season* "ever recorded" set.
   When grading week 5, whether weeks 1–4 counted a 0 for a fringe stat depended on games
   in weeks 6–18. Measured: 9.7% of player-week-stat cells were affected. Fix: the
   walk-forward loop now zero-fills from a prefix-only set (`ever_pre`, strictly earlier
   weeks). Effect: 18,511 → 18,390 graded pairs; Brier raw 0.2600 → 0.2597, holdout
   calibrated 0.2408 → 0.2407. Real leak, small impact — conclusions unchanged.
2. Player position was taken from the *last* row of the season; now first-seen, so no
   end-of-season information feeds early-week predictions.

Notes: a player's own earlier games are included in his position baseline (harmless at
the n≥20 gate); pushes (actual == line) are dropped from calibration (standard).

## 2. Accuracy audit — does it beat the alternatives?

Holdout (2025 weeks 13–18), player model vs position-baseline, Brier score (lower is better).
Constant-0.5 scores 0.25 by construction.

| stat | n | raw | league | holdout n | holdout raw | holdout cal | holdout logloss |
|---|---|---|---|---|---|---|---|
| rush_yds | 1,914 | 0.2587 | 0.2878 | 840 | 0.2565 | 0.2267 | — |
| rec_yds | 3,218 | 0.2598 | 0.2809 | 1,423 | 0.2550 | 0.2379 | — |
| pass_yds | 403 | 0.2671 | 0.2795 | 169 | 0.2722 | 0.2926 | 0.8059 |
| rec | 2,959 | 0.2580 | 0.2806 | 1,290 | 0.2459 | 0.2430 | — |
| rush_att | 1,560 | 0.2697 | 0.3082 | 693 | 0.2663 | 0.2466 | — |
| **all** | 18,390 | 0.2597 | 0.2804 | 7,988 | 0.2538 | 0.2407 | 0.6746 |

Findings:

- The **raw** model (0.2597) is slightly *worse* than always predicting 50% (0.25) —
  its tails are overconfident. **Calibration is load-bearing, not cosmetic**: it takes the
  holdout to 0.2407 (log loss 0.6746 vs 0.693 for coin-flip). Good thing it's there.
- The player model beats the league baseline on holdout for every stat with real sample
  **except pass_yds** (n=169): calibration hurts there (0.2722 → 0.2926). The 20-point
  clamp and 150-pair gate contain the damage, but watch this stat — consider a higher
  pair gate or skipping calibration where the holdout shows harm.
- `any_td` is modeled with a normal CDF on an essentially discrete stat. Crude, but
  bounded; calibration absorbs some of it. Acceptable, flagged.
- **Biggest caveat**: calibration is fitted against *Sleeper-projection* lines and applied
  to *market* lines. Market lines are sharper than free projections, so the mapping may
  not transfer 1:1. Mitigation: refit calibration on the live graded record once 2026
  accumulates enough real market-line outcomes (see §4).

## 3. Goal-alignment audit — does it serve the project's goal?

The project's goal: *two free projection models against the real market lines, ranked by
the size of the disagreement.*

The player-level hit% answers a different question: *how often does THIS player clear
THIS line, based on his own game log?* It ignores this week's projection entirely —
the freshest signal (matchup, injuries, role change) plays no role in the hit chance.
That is a deliberate philosophical choice ("grade the player, not the model"), and the
numbers above say the resulting probabilities are honest. But it creates a real tension:

- The projection (ESTIMATE/GAP) and the hit% side can **contradict** each other — e.g.
  the model sits 4 yards under the line while the player's log says over 65%. The board
  copy now states both facts plainly, but the product is no longer purely "models vs
  the market."

Alternatives, for a future decision:

- **A (shipped here):** side + hit% from the player model; projection shown as context.
- **B:** side from the projection gap, hit% = player-curve probability for *that* side.
  Preserves the disagreement framing; prints sub-50% hit chances when the player's
  history disagrees with the projection (informative, needs careful copy).
- **C:** p = Φ((proj − line) / s_player) — the projection as the mean (fresh), the
  player's own spread as the volatility (personal). Arguably the most principled; needs
  a backend calibration refit (the frontend already has all inputs).

Recommendation: run with A (it's what's graded above) and compare B/C on real 2026
market lines once the live record is deep enough.

## 4. Coherence gap (follow-up, not in this PR)

The Accuracy page's live record (`live_grades`, via `collect.py`) grades the
**projection-gap side** — but the board now recommends the **player-model side**. The
live record no longer validates what the board displays. To close the loop, `collect.py`
should also store the player-model probability and its hit/miss per graded prop, and the
Accuracy page should show the player model's live record alongside the models'.

## 5. Verification

- `backtest.py` runs green end-to-end; payload 211 KB, 603 players, 15 stats.
- JS↔Python cross-check: `normName` identical on 10 tricky names (suffixes, apostrophes,
  dots); `playerHit` matches Python `p_over` + calibration to 1e-6 on 5 real players.
