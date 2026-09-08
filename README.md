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
- `scripts/backtest.py` - grades Sleeper and ESPN projections vs actuals
  (2025 full season + finished 2026 weeks), writes out/backtest.json
- `scripts/schema.sql` - one-time database setup

Data sources are anonymous public endpoints; no keys needed for reads.
