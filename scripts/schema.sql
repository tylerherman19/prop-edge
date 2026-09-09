-- prop-edge schema. Run once in the Supabase SQL editor on the NEW project.
create table if not exists runs (
  id bigint generated always as identity primary key,
  taken_at timestamptz not null default now(),
  season int not null,
  week int not null,
  counts jsonb,
  league text not null default 'nfl',   -- nfl | cfb
  context jsonb                        -- StatTree consensus line + matchup context (NFL player props only)
);
create table if not exists edges (
  id bigint generated always as identity primary key,
  run_id bigint references runs(id) on delete cascade,
  source text not null,           -- fanduel | prizepicks
  game text, kickoff timestamptz,
  player text not null, team text,
  stat text not null,             -- pass_yds, rec, rec_yds, ...
  line numeric not null,
  over_odds int, under_odds int,
  sleeper_proj numeric, espn_proj numeric,
  sleeper_edge numeric, espn_edge numeric,
  week int not null, season int not null,
  league text not null default 'nfl'   -- nfl | cfb
);
create index if not exists edges_run on edges(run_id);
create index if not exists edges_week on edges(season, week);
-- latest run per league (NFL and CFB publish independently)
create or replace view latest_board
with (security_invoker = on) as
  select e.league, e.source, e.game, e.kickoff, e.player, e.team, e.stat, e.line,
         e.over_odds, e.under_odds, e.sleeper_proj, e.espn_proj,
         e.sleeper_edge, e.espn_edge, e.week, e.season
  from edges e
  join (select league, max(id) as id from runs group by league) r on e.run_id = r.id;
create table if not exists backtest (
  id text primary key,
  payload jsonb not null,
  updated timestamptz not null default now()
);
create table if not exists stattree (
  id text primary key,                 -- 'latest'
  payload jsonb not null,              -- meta + nfl_games + cfb_games
  updated timestamptz not null default now()
);
create table if not exists live_grades (
  id bigint generated always as identity primary key,
  week int not null, season int not null,
  source text not null, player text not null, stat text not null,
  line numeric not null,
  proj_source text not null, proj_value numeric not null,
  actual numeric not null, side text not null, hit boolean not null,
  graded_at timestamptz not null default now(),
  unique (week, season, source, player, stat, proj_source)
);
-- public read-only; writes happen with the service key from the collector
alter table runs enable row level security;
alter table edges enable row level security;
alter table backtest enable row level security;
alter table live_grades enable row level security;
alter table stattree enable row level security;
create policy "public read runs" on runs for select to anon using (true);
create policy "public read edges" on edges for select to anon using (true);
create policy "public read backtest" on backtest for select to anon using (true);
create policy "public read grades" on live_grades for select to anon using (true);
create policy "public read stattree" on stattree for select to anon using (true);
