# Independent model review — prop-edge player hit model

Reviewer: external, adversarial. Date: 2026-09-08.
Scope: `scripts/backtest.py`, `scripts/collect.py`, `index.html`,
`accuracy.html`, `method.html`, `scripts/schema.sql`, the two workflows, plus
a full security pass.

Everything below was reproduced from the live Sleeper 2025 feed
(168,966 projection rows, 39,929 actual rows) and from the payload the site
was actually serving at the time of review
(`backtest.generated = 2026-09-08T22:31:40Z`).

---

## (b) Reproduction first — what holds up

Re-running the model as it stood on `main`:

| reported (AUDIT.md) | reproduced | verdict |
|---|---|---|
| graded pairs 18,390 | 18,390 | exact |
| Brier raw 0.2597 | 0.2597 | exact |
| Brier league 0.2804 | 0.2804 | exact |
| holdout n 7,988 | 7,988 | exact |
| holdout raw 0.2538 | 0.2538 | exact |
| holdout calibrated 0.2407 | 0.2407 | exact |
| holdout log loss 0.6746 | 0.6744 | close (feed revisions) |
| per-stat table (6 rows) | all 6 exact | exact |
| payload 211 KB, 603 players, 15 stats | 603 players, 15 stats | exact |
| "`normName` identical on 10 tricky names" | **refuted** — see C4 | fails on U+2019 |

So the arithmetic in the AUDIT is right. **What is wrong is the comparison it
is put next to, and what the site does with the result.**

---

## (a) Findings by severity

### CRITICAL

**C1. The headline gain is measured against the wrong null, and against the
right one it is ~2%.**

AUDIT compares holdout 0.2407 to a constant 0.5 (0.25) and to a
"league-average baseline" (0.2804). Neither is the null. Over Sleeper-proxy
lines the over hits 44.79% of the time in the training weeks; a constant
predictor of 0.4479 scores **0.2455** on the holdout. Measured against that:

| | holdout Brier | skill vs base rate |
|---|---|---|
| calibrated model | 0.2407 | **+1.96%** |
| raw model | 0.2538 | −3.63% |
| base-rate constant | 0.2455 | 0 |
| "league baseline" (0.2804) | — | a weaker model, not a null |

The Murphy decomposition says the same thing: resolution 0.0077, reliability
0.0032. Almost all of the "improvement" is the removal of a reliability
penalty — i.e. it is calibration, not discrimination. The model has real but
small skill. The 0.2804 comparison inflates it by roughly 8x.

**C2. On 8 of 14 stats the model has no rank skill, and the site printed
90–98% probabilities on them anyway.**

Holdout AUC (0.50 = coin flip), from the code as shipped:

| stat | holdout n | AUC | skill vs base rate |
|---|---|---|---|
| rec_tgt | 1,339 | 0.606 | +4.4% |
| rec | 1,290 | 0.602 | +2.6% |
| rush_rec_yds | 1,031 | 0.586 | +2.2% |
| rec_yds | 1,423 | 0.572 | +1.8% |
| rush_yds | 840 | 0.536 | +1.6% |
| rush_att | 693 | 0.530 | +0.5% |
| pass_att | 169 | 0.547 | −3.6% |
| rush_tds | 95 | 0.499 | −29.6% |
| pass_comp | 169 | 0.469 | −14.8% |
| pass_yds | 169 | 0.470 | −16.8% |
| any_td | 214 | 0.453 | −9.3% |
| pass_rush_yds | 169 | 0.440 | −5.1% |
| **pass_int** | 156 | **0.392** | −15.8% |

The split is entirely sample size: every stat with skill has >1,500 graded
pairs, every stat without has <520. Below that, a 21-knot isotonic curve is
fitted on ~14 points per knot, the ±0.20 clamp saturates on noise, and the
calibration *manufactures* the locks it was supposed to prevent —
`cal(0.75) = 0.950` on four passing stats, `cal(0.90) = 0.980`.

What that looked like on the live board, computed from the served payload:

- **9 of 9 interception props picked OVER**, median 69%, two at ≥85%.
  Geno Smith and Baker Mayfield were both shown at **90% to throw a pick** —
  from the stat with the worst discrimination on the board (AUC 0.392).
- **Matthew Stafford 98% over 1.5 passing TDs.** Raw model 85%; the
  calibration curve carried it to 98%.

These are not conservative errors. They are the most confident numbers on the
page, on the stats where the model knows least.

**C3. The calibration is fitted on lines the market does not post, and the
fitted shift does not transfer.**

The proxy is Sleeper's weekly projection. Against those projections the over
came in **32.0%** of the time on rushing yards — and the fitted curve
duly maps a raw 0.50 to **0.333**. That 17-point shift is a measurement of
Sleeper's bias, not of the player. A real sportsbook line is priced near
50/50, so applying this curve to a FanDuel number mis-prices every rushing
prop toward the under by construction.

The proxy line *distribution* is wrong too, not just its center:

| stat | proxy line median | half-integer lines | real market line |
|---|---|---|---|
| rush_yds | **10.4 yds** | 2.1% | typically 30–90 |
| any_td | 0.66 | 5.5% | 0.5, essentially always |
| pass_int | 0.73 | 0.7% | 0.5 |

The `line < 0.5` filter is nowhere near enough: the rushing-yards curve is
fitted overwhelmingly on fringe backs at 1–10 yard "lines" that no book ever
posts. On the live board this produced a 10:1 skew — **543 props reading
"Vegas advantage" against 54 reading "Your advantage."**

To validate the transfer you need the thing that does not exist yet: real
lines. The only honest path is the live record, which brings us to C6.

**C4. `normName` diverged between Python and JS, silently dropping curves.**

Python does `NFKD → encode("ascii","ignore")`, which drops *every* non-ASCII
code point. JS stripped only `̀-ͯ`. So:

```
"Ja’Marr Chase"  (U+2019)  py "jamarr chase"  js "ja’marr chase"   MISMATCH
"Bjørn Hansen"             py "bjrn hansen"   js "bjørn hansen"    MISMATCH
```

Both feeds are free to send a typographic apostrophe. When they do, the
frontend looks up a key that does not exist and the player's curve vanishes
with no error — the card just says "no curve yet." AUDIT §5 claims these
match on 10 tricky names; the 10 chosen happened to use ASCII apostrophes.

**C5. Clicking any sort button silently filtered the board to under-side
props.**

`.fbtn.srt` also matches `.fbtn`, so both listeners fired. The first set
`SIDE = b.dataset.side` — `undefined` on a sort button — and the filter
`SIDE==="all" || (h && (SIDE==="over" ? h.p>=0.5 : h.p<0.5))` then fell
through to the under branch. Every over-side prop and every prop without a
curve disappeared, and no side button showed as active.

**C6. The live record grades a signal the board does not show.**

`collect.py` grades the projection-gap side. The board leads with the
player-model side. On the live board **61.2% of priced props have the two
pointing in opposite directions** (468 of 765). So the Accuracy page's record
is evidence about a different product than the one on screen. The AUDIT
flags this in §4 and defers it; it should not have shipped that way, because
it is also the only mechanism that could ever validate C3.

### MAJOR

**M1. Baseline pool leak (the same leak the AUDIT says it fixed, one level
up).** The walk-forward loop was fixed to zero-fill from `ever_pre`. But
`Baselines.__init__` consumes `logs[k]["stats"]`, which `build_logs` still
built with a *full-season* `ever` set. So the position baseline queried at
week 5 contained zeros placed there by week-14 information. Measured impact:
holdout raw 0.2538 → 0.2545, calibrated 0.2407 → 0.2407. Real leak, no
material effect on the conclusions — but the AUDIT's claim that the fix is
complete is wrong.

**M2. Sigma floors are the model, not a floor.** Share of published curves
sitting *exactly* at the hand-set floor: `pass_rush_yds` 75.7%,
`rush_att` 58.8%, `rush_yds` 52.3%, `rec_tgt` 45.3%. For those stats the
median sigma **equals** the floor. The "spread" the card shows the user is a
constant chosen by eye, not a property of the player. The floors are also an
untracked hyperparameter tuned on the same season the model is graded on.

**M3. A Gaussian on near-binary stats was destroying signal, measurably.**
Swapping the count stats to Poisson on the same walk-forward pairs:

| stat | raw Brier G→P | AUC G→P |
|---|---|---|
| any_td | 0.2540 → 0.2384 | 0.458 → **0.561** |
| pass_int | 0.2717 → 0.2651 | 0.398 → 0.472 |
| pass_tds | 0.2504 → 0.2468 | 0.518 → 0.556 |
| rush_tds | 0.2504 → 0.2385 | 0.499 → 0.523 |

`any_td` went from *worse than random* to real discrimination. The AUDIT
calls the Gaussian "crude, but bounded... acceptable." It was not bounded: at
a real 0.5 line the Gaussian could not put any player above 0.698, and **85%
of `any_td` curves picked UNDER** — a structural, direction-locked answer.

**M4. The head-to-head on the Accuracy page compares three different
samples.** `errors.sleeper`, `errors.espn` and `errors.avg` have different
`n` (avg exists only where both models projected). The page picked a
"CLOSEST" winner by comparing medians across them and reported "Sleeper was
the more accurate model on N stats." The payload already contains
`errors_matched` — the actual matched-sample head-to-head — and the page did
not use it.

**M5. The board fetched at most 1000 of 1672 rows, and pulled college
football onto an NFL page.** `latest_board?select=*` has no `league` filter
and no `limit`; the view returns the newest run *per league*. Live counts at
review time: 1,352 NFL + 320 CFB = 1,672 rows, of which PostgREST returned
1,000. ~35% of the NFL board never reached the page, the CFB rows competed
for the cap with no ordering guarantee, and the "N PROPS" count and the lede
were computed on the truncated set. (CFB rows are then dropped by the
projection filter, so the visible symptom was missing NFL props, not visible
CFB ones. Zero CFB/NFL name collisions today — but nothing prevents one.)

**M6. The under-side card printed the over probability.** The card showed
"41% / 59%" and the sentence underneath read *"stays under the 30.5 line 41%
of the time."* The template used `hp` (the over chance) in both branches.
Every under-side card on the board contradicted its own price.

**M7. Live grading dropped every under that won on a zero.** In
`grade_finished_weeks`, `av is None → continue`. ESPN omits the key for a
stat a player recorded zero of, so a player who played and caught nothing was
skipped — discarding a guaranteed under win. `backtest.py` treats the same
case as `0.0`. The live record and the backtest disagreed on what a zero is.

### MINOR

- **N1.** `MIN_GAMES = 3` on a *recency-weighted* mean: 3 games at a 5-game
  half-life is an effective sample near 2.7. The Bhayshul Tuten card on the
  live board (n=15, curve mean 29.4 rush+rec yards, FanDuel line 62.5, model
  says 17% over) shows the real failure mode isn't the rookie gate — it's a
  mid-season role change, which the model structurally cannot see. It ranked
  #4 on the default sort.
- **N2.** The default EDGE sort is `(|p−0.5|) × (|gap| / s)` — magnitude only.
  It ranks a prop *higher* the more its two signals contradict each other.
  5 of the top 20 on the live board were outright contradictions, including
  Lamar Jackson rushing yards: projections +12.9 over the line, board shows
  27% over, "MODEL PICK: UNDER", ranked #7.
- **N3.** Position from first-seen row (the AUDIT's fix #2) is correct for
  leakage but wrong for mid-season position changes; nothing re-evaluates it.
- **N4.** A player's own games sit in his own position baseline. Harmless at
  n≥20 for receivers; for `pass_yds` the QB pool is small enough that a QB is
  a non-trivial share of his own prior. Weakens shrinkage; not a leak.
- **N5.** `sleeper_season`'s `seen` set is per (kind, week) and the feed
  ignores `?position=`, so the QB call absorbs every player and the
  `or pos` fallback is always "QB". Harmless (position comes from the players
  dump) but the loop does 4x the work it needs to.
- **N6.** `rec_tds` shipped a production calibration curve with **no holdout
  evaluation at all** (163 pairs cleared the 150 gate; the test window did
  not clear 30).
- **N7.** `accuracy.html` read `live_grades` with no season filter and
  `limit=200`; both break as seasons accumulate.
- **N8.** `live_grades`' unique key omits `line`, so two lines on the same
  player/stat/book collapse to one row.
- **N9.** Half-life is not doing anything (see the sensitivity table below).

### Sensitivity — asked for, and it is not reassuring

Holdout calibrated Brier / skill vs the base rate, leak-free baseline pool:

| half-life \ shrink K | 0 | 2 | 4 | 8 | 16 |
|---|---|---|---|---|---|
| 2 | .2441/+0.57% | .2424/+1.26% | .2414/+1.66% | .2406/+1.99% | .2407/+1.96% |
| 3 | .2439/+0.63% | .2417/+1.54% | .2408/+1.90% | .2403/+2.10% | .2407/+1.96% |
| **5 (shipped)** | .2438/+0.70% | .2418/+1.52% | **.2407/+1.96%** | .2403/+2.10% | .2408/+1.91% |
| 8 | .2438/+0.69% | .2416/+1.58% | .2408/+1.91% | .2403/+2.10% | .2406/+1.98% |
| 12 | .2433/+0.88% | .2416/+1.57% | .2407/+1.95% | .2404/+2.09% | .2408/+1.92% |
| ∞ (flat mean) | .2439/+0.63% | .2418/+1.51% | .2409/+1.86% | .2406/+1.99% | .2407/+1.96% |

**The 5-game half-life is doing nothing measurable.** A flat unweighted mean
scores within 0.0002 of it. "Recent games count about twice as much as a game
five weeks back" is copy, not a finding — the whole half-life axis spans
0.1 percentage points of skill. Shrinkage is the parameter that matters
(K=0 is clearly worse), and K=8 is marginally better than the shipped K=4.

---

## Security audit

No exploitable vulnerability found. Details:

- **Secrets.** Only the Supabase `anon` JWT is committed, which is its
  intended use. Full history scan: no `service_role` key was ever committed.
  `SUPABASE_SERVICE_KEY` is referenced only through `os.environ` and GitHub
  Actions secrets. Both workflows are `schedule` + `workflow_dispatch` — no
  `pull_request_target`, no untrusted-fork execution path.
- **RLS.** `schema.sql` enables RLS on all four tables with `select`-only
  policies for `anon`; no write policy exists, so the anon key is read-only
  and writes go through the service key, which bypasses RLS server-side.
  `latest_board` is correctly `security_invoker = on`. Confirmed applied on
  the live project: re-running `schema.sql` there failed with
  `policy "public read runs" for table "runs" already exists`, and the
  `enable row level security` statements are in the same block, immediately
  above it. Not probed with an actual write, which is the only way to test it
  end to end.
- **XSS.** `esc()` did not escape `'`, and the stat-filter `<option value>`
  interpolated a DB value unescaped. Not exploitable in practice (`stat` is
  always one of a fixed mapping's values, and writing to the DB needs the
  service key), but both are fixed. All player/team/game text was already
  escaped.
- **Injection into PostgREST paths.** Every interpolated value is an `int`
  from `range()` or a literal season. `PROP_WEEK` passes through `int()`. No
  string user input reaches a query path.
- **`FANDUEL_AK`** is a public page key scraped from FanDuel's own site — not
  a user secret. The 30-minute collector schedule hitting FanDuel per-event
  endpoints is a terms-of-service exposure, not a security one, and worth a
  look independently.
- **No CSP, no SRI** on the Google Fonts stylesheet. Standard for a static
  site; worth adding if this ever handles anything but public data.

---

## (c) Recommended changes, by expected impact

**Done on this branch** (each verified, numbers re-measured):

1. **Publish gate: `MIN_PAIRS = 1000`,** enforced on both sides. The backend
   ships a curve only for the six stats with enough walk-forward sample; the
   frontend re-applies the gate from whatever the payload carries
   (`published` flag, else `published_stats`, else the graded `n` per stat)
   and fails closed. Trusting the backend's omission alone would have left the
   live board printing 90–98% locks for as long as a stale payload sat in
   Supabase, which is exactly what happened between the merge and the first
   backtest run. Kills every fake lock in C2.
2. **Poisson for count stats** (`pass_tds`, `pass_int`, `rush_tds`,
   `rec_tds`, `any_td`), mirrored exactly in JS. Fixes M3.
3. **Calibration knot support.** A knot's swing is scaled by the training
   pairs within ±0.05 of it (full swing at 50), then forced monotone. The
   ±0.20 clamp alone did not stop `cal(0.75) = 0.95`.
4. **Baseline zero-fill made prefix-only** (M1).
5. **Honest baseline reported everywhere**: `holdout_brier_base_rate`,
   `train_over_rate`, `holdout_skill_vs_base_rate`, `holdout_auc` in the
   payload, and a new Accuracy section that leads with them.
6. **`line_proxy_caveat` in the payload**, and on the Method page and the
   board: what the calibration is actually calibrated to, why the board tilts
   under, and that it is a property of the proxy.
7. **`normName` parity** (C4), **sort/side listener split** (C5),
   **`league=eq.nfl&limit=5000`** (M5), **under-side copy** (M6),
   **matched-sample head-to-head** (M4), **zero handling in live grading**
   (M7), season filter and escaping fixes (N7, XSS).
8. **`model_p` / `model_side` / `model_hit` in `live_grades`** so the live
   record grades what the board prints (C6), plus a "signals disagree" flag
   on contradicting cards.

**Not done — the author's calls, ranked by expected impact:**

1. **Refit the calibration on real market lines** as soon as `live_grades`
   is deep enough. This is the only fix for C3, and everything else is
   provisional until it happens. Roughly 1,000 graded props per stat is the
   bar the publish gate already uses.
2. **Decide whether to recenter the calibration** in the meantime. Forcing
   `cal(0.5) = 0.5` would strip the Sleeper marginal bias — the part that
   provably does not transfer — while keeping the sharpness correction. It
   would *worsen* the measured holdout Brier (which is measured against the
   biased proxy) while probably improving real-line accuracy. That trade is
   unmeasurable today, so it is left as the author's call, not a silent
   change.
3. **Fix the EDGE sort** (N2). Score the gap *in the direction of the model
   pick*, so agreement ranks above contradiction. Right now the default sort
   optimizes for internal disagreement.
4. **Raise `SHRINK_K` to 8** — marginally better across the whole half-life
   axis and slightly more conservative on thin players.
5. **Derive the sigma floors** from the position baseline instead of setting
   them by hand (M2), or state on the card when a player's spread is the
   floor rather than his own.
6. **Add this-week information.** The hit% is a season-average model priced
   against a line that already contains the matchup. Bhayshul Tuten (N1) is
   the case: no amount of calibration fixes a model that cannot see a role
   change. Option C in AUDIT §3 — projection as the mean, player's own spread
   as the volatility — is the cheapest way in and the frontend already has
   both inputs.

---

## (d) Verdict

**Before this branch: no. The probabilities should not have been displayed as
they were.** Not because the model is bad — it has small, genuine skill on
volume stats — but because the site's most confident claims sat exactly where
the model knew least. A 90% interception probability from the stat with
AUC 0.392, and a 98% passing-TD lock built by a calibration curve fitted on
230 points, are not conservative errors. They are the numbers a user acts on.

**After this branch: yes, for six stats, with the caveats stated on the page.**
The remaining honest claim is narrow and should stay narrow:

> On rushing yards, rush attempts, receiving yards, receptions, targets and
> rush+rec yards, a player's own 2025 game log predicts whether he clears a
> *Sleeper projection* about 2% better than guessing the base rate, with
> AUC 0.53–0.61. Whether that transfers to a sportsbook line is untested.

Three conditions on displaying it:

1. **The proxy caveat stays visible.** Until the calibration is refit on real
   lines, the under-tilt is an artifact and the page has to say so.
2. **The live record grades the displayed side.** Now wired; run the
   `schema.sql` migration or the model columns silently drop.
3. **The publish gate stays automatic.** It is computed from the walk-forward
   sample every week, not hand-maintained. If a stat's sample grows past
   1,000, it earns a number; if the model stops beating the base rate, the
   next refit should pull it. That last part is not yet automatic — the gate
   is sample size, not skill, and skill should be added to it once there is
   enough live data to measure it per stat.

The model is defensible. The old presentation of it was not.
