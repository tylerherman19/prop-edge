# Prop Edge

NFL player props: two free projection models (Sleeper, ESPN) measured against
two free market line feeds (FanDuel, PrizePicks), ranked by the size of the
disagreement. Static site + scheduled collector + Supabase.

- `index.html` - the board, ranked by edge
- `accuracy.html` - model head-to-head backtest + live graded record
- `method.html` - sources and what the numbers mean
- `scripts/collect.py` - pulls lines + projections, computes edges, publishes
  to Supabase and grades finished weeks (needs SUPABASE_URL +
  SUPABASE_SERVICE_KEY env)
- `scripts/backtest.py` - grades Sleeper and ESPN projections vs actuals and
  fits the player-level model (2025 full season + finished 2026 weeks),
  writes out/backtest.json
- `scripts/schema.sql` - one-time database setup

Data sources are anonymous public endpoints; no keys needed for reads.

## Payload contract (for the UI)

Everything the site reads comes from Supabase REST. Three tables + one view:

### `runs` / `edges` (written by collect.py every 30 min)

`edges` columns: run_id, league (`nfl` | `cfb`), source
(`fanduel` | `prizepicks`), game, kickoff, player, team, stat, line,
over_odds, under_odds, sleeper_proj, espn_proj, sleeper_edge, espn_edge,
week, season. The site reads the `latest_board` view (latest run's rows per
league). NFL rows carry projections; CFB rows are PrizePicks lines only
(no free CFB projection source, and FanDuel posts no CFB player props), so
projection/edge fields are null there and the CFB `week` is a date bucket,
not the official CFB week number. Stat keys: pass_yds, pass_tds, pass_att, pass_comp,
pass_int, rush_yds, rush_tds, rush_att, rec_yds, rec, rec_tds, rec_tgt,
rush_rec_yds, pass_rush_yds, any_td (plus long_rec/long_rush/long_comp,
lines-only, no projections exist).

### `backtest` (single row id=`latest`, written by backtest.py weekly)

`payload` JSON, all fields additive-safe:

- `seasons.2025_sleeper` / `seasons.2025_espn`: `by_stat` (n, mae, med) and
  `by_week_mae`. Head-to-head projection accuracy, 2025.
- `errors.{sleeper,espn,avg}`: per stat `{n, q, ae_q}` - signed error and
  absolute error percentiles (101 points, index = percentile). `avg` pairs
  only weeks where both models projected the player. Used by the pooled
  hit% (legacy path).
- `errors_matched.stats`: per stat `{n, sleeper_mae, espn_mae, avg_mae,
  sleeper_closer, espn_closer, tied}` - head-to-head on the matched sample
  only (both models projected that player-week-stat). Language: "graded on
  the same X checks."
- `player_curves`: `{stat: {player_key: [n, mean, sd]}}` - the player-level
  model. mean/sd are recency-weighted over the player's own 2025 (+finished
  2026) games, already shrunk toward his position baseline. Only players
  with n >= 3 games are present; absence means show "-".
- `player_baselines`: `{stat: {pos: [n, mean, sd]}}` - league/position
  priors used in the shrinkage (context/fallback display only).
- `player_model_grade`: per stat `{n, brier_raw, brier_league, holdout_n,
  holdout_brier, holdout_brier_raw, holdout_brier_league, holdout_logloss,
  calib_x, calib_y}` plus `_all`. Honest protocol: walk-forward player
  inputs; calibration fitted on 2025 W1-12, scored on W13-18 holdout.
  `calib_x`/`calib_y` (full-season fit) are the production calibration knots.
- `player_model`: constants + plain-English method + usage recipe (below).
- `note_2026`: why no 2026 weeks are graded yet.

### Player-level hit% (the Ridley rule: graded on his own games)

For a prop (player, stat, line):

1. `key = norm_name(player)` - lowercase, strip accents, remove `.` `'` and
   hyphens, drop name suffixes (jr/sr/ii/iii/iv/v), collapse spaces.
2. `[n, m, s] = payload.player_curves[stat][key]`; missing -> show "-".
3. `p = P(over) = normal_cdf((m - line) / s)` (Abramowitz-Stegun or erf).
4. Calibrate: linear-interp `calib_y` at `p` over `calib_x` (knots are
   pre-clamped to +/-0.20 swing and bounded 0.02-0.98). Stat has no calib
   entry -> use raw p bounded to 0.02-0.98.
5. Display max(p, 1-p) as the hit% with the matching side.

### `live_grades` (written by collect.py as weeks finish)

One row per graded edge: week, season, source, player, stat, line,
proj_source, proj_value, actual, side, hit. This is the real 2026 record;
it starts at go-live (no verified 2025 sportsbook line archive exists, so
no historical backfill - stated on the Accuracy page).
