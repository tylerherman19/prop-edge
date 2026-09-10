#!/usr/bin/env python3
"""Phase-2 ship: additive matchup blocks into the backtest payload + matchup matrix.
- backtest id=latest gains "matchup_model_grade" (both rank seasons, all three
  variants, fitted betas, verdict) and "player_curves_matchup" (week-1 2026
  curves adjusted by opponent SumerSports 2025 defensive rank; same
  [n, m, s] shape as player_curves). Nothing existing is overwritten.
- backtest id="matchups_latest" = the weekly matchup matrix (both teams'
  key SumerSports ranks per game) + green/yellow/red reads for the two
  Sleeper rosters of record.
"""
import json, math, os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest import norm_name, get
from matchup_model import FAM, opp_z, BETA_CAP

KEY = os.environ["SUPABASE_SERVICE_KEY"]; BASE = os.environ["SUPABASE_URL"].rstrip("/")
def rest(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + "/rest/v1/" + path, data=data, method=method,
        headers={"apikey": KEY, "Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"})
    return urllib.request.urlopen(req, timeout=180).read().decode()

def ranks_from(rows, field):
    vals = []
    for r in rows:
        try: vals.append((float(r[field]), r["abbr"]))
        except (KeyError, ValueError, TypeError): pass
    vals.sort()
    return {ab: i + 1 for i, (_, ab) in enumerate(vals)}

def getrow(rid):
    return json.loads(rest(f"sumersports?id=eq.{rid}&select=payload"))[0]["payload"]

def verdict_text(ev):
    h = ev["seasons"]["2024"]["variants"]
    b, mo, mp = h["baseline"]["_pooled"], h["matchup_only"]["_pooled"], h["matchup_primary"]["_pooled"]
    return (f"SumerSports-primary does NOT beat the player-log baseline on the honest holdout "
            f"(2024 ranks, no leakage): calibrated Brier {mp['holdout_brier']} vs {b['holdout_brier']}, "
            f"rank skill AUC {mp['holdout_auc']} vs {b['holdout_auc']}, confident-flag record "
            f"{mp['flagged']['win_pct']}% vs {b['flagged']['win_pct']}% (n={mp['flagged']['n']} vs {b['flagged']['n']}). "
            f"Matchup-only is clearly worst (Brier {mo['holdout_brier']}, AUC {mo['holdout_auc']}). "
            f"With leaky 2025 ranks the gap narrows but the protocol is contaminated. "
            f"Shipped as additive blocks per the user's instruction; the player-log model remains "
            f"the board's engine and this block is the honest record.")

def main():
    ev = json.load(open("out/matchup_eval.json"))
    beta = ev["seasons"]["2024"]["beta"]  # honest fit (no leakage)
    tdef, toff = getrow("team_def"), getrow("team_off")
    d25, o25 = tdef["seasons"]["2025"], toff["seasons"]["2025"]
    d24 = tdef["seasons"]["2024"]
    # rank tables, 2025 (production prior) and 2024
    rk = {}
    for tag, rows, src in (("def_pass", d25, None), ("def_rush", d25, None)):
        pass
    ranks25 = {}
    for ab, rr in ((r["abbr"], r) for r in d25): ranks25.setdefault(ab, {})
    def_pass25 = ranks_from(d25, "EPA/Pass"); def_rush25 = ranks_from(d25, "EPA/Rush")
    off_pass25 = ranks_from([{**r, "EPA/Pass": str(-float(r["EPA/Pass"]))} for r in o25 if r.get("EPA/Pass") not in (None,"")], "EPA/Pass")
    off_rush25 = ranks_from([{**r, "EPA/Rush": str(-float(r["EPA/Rush"]))} for r in o25 if r.get("EPA/Rush") not in (None,"")], "EPA/Rush")
    # offense ranks: higher EPA is better, so rank descending = negate then ascending sort
    ranks_def25 = {ab: {"pass": def_pass25.get(ab), "rush": def_rush25.get(ab)} for ab in def_pass25}
    def_p2024 = ranks_from(d24, "EPA/Pass"); def_r2024 = ranks_from(d24, "EPA/Rush")
    ranks_def24 = {ab: {"pass": def_p2024.get(ab), "rush": def_r2024.get(ab)} for ab in def_p2024}
    off25 = {r["abbr"]: r for r in o25}
    def25 = {r["abbr"]: r for r in d25}

    # ---- 2026 week 1 games ----
    import csv
    games = []
    with open("cache/nflverse_games.csv") as f:
        for r in csv.DictReader(f):
            if r["season"] == "2026" and r["game_type"] == "REG" and r["week"] == "1":
                al = {"LA": "LAR"}
                games.append({"away": al.get(r["away_team"], r["away_team"]),
                              "home": al.get(r["home_team"], r["home_team"]), "date": r["gameday"]})
    print(f"week1 games: {len(games)}", file=sys.stderr)
    opp26 = {}  # games already carry SumerSports-style abbrevs (LA->LAR applied above)
    for g in games:
        opp26[g["away"]] = g["home"]; opp26[g["home"]] = g["away"]

    # ---- player team/pos map (Sleeper dump, current teams) ----
    players = get("https://api.sleeper.app/v1/players/nfl")
    team_of, pos_of = {}, {}
    for p in players.values():
        nm = norm_name(p.get("full_name") or "")
        if nm:
            if p.get("team"): team_of[nm] = p["team"]
            if p.get("position"): pos_of[nm] = p["position"]

    def z16(rank):  # rank 1 (best def) -> -0.97 ; rank 32 -> +0.94
        return (rank - 16.5) / 16.5

    def adj_z(stat, pos, opp):
        if opp not in def_pass25: return None
        fam = FAM[stat]
        zp, zr = z16(def_pass25[opp]), z16(def_rush25[opp])
        if fam == "pass": return zp
        if fam == "rush": return zr
        if fam == "pass_int": return -zp
        if fam == "mixed": return (zp + zr) / 2
        return zp if pos in ("WR", "TE") else zr

    # ---- load backtest payload, add blocks ----
    pl = json.loads(rest("backtest?id=eq.latest&select=payload"))[0]["payload"]
    pl["matchup_model_grade"] = {
        "generated": ev["generated"],
        "protocol": ("Identical walk-forward as player_model_grade (line = Sleeper projection proxy, "
                     "calibration fit W1-12, holdout W13-18). Opponent = nflverse 2025 weekly. "
                     "Opponent strength = SumerSports defensive EPA/Pass + EPA/Rush ranks. "
                     "PRIMARY evaluation uses 2024 ranks (no leakage); 2025-rank run is a labeled "
                     "sensitivity (2025 ranks leak holdout games). Multiplier scale (beta) fitted "
                     "on train weeks only."),
        "beta_2024fit": beta,
        "user_steering": "9/9: SumerSports weighted HIGHER (primary signal), player log as corrector; measure honestly.",
        "results_2024ranks_honest": ev["seasons"]["2024"]["variants"],
        "results_2025ranks_leaky": ev["seasons"]["2025"]["variants"],
        "verdict": verdict_text(ev)}
    curves_m = {}
    n_adj = n_tot = 0
    for stat, cstat in (pl.get("player_curves") or {}).items():
        if stat not in FAM:
            continue
        fb = beta.get(FAM[stat], 0.0)
        out = {}
        for k, (n, m, s) in cstat.items():
            n_tot += 1
            z = adj_z(stat, pos_of.get(k), opp26.get(team_of.get(k, ""), ""))
            if z is None:
                out[k] = [n, m, s]
            else:
                n_adj += 1
                out[k] = [n, round(m * (1 + fb * z), 2), s]
        curves_m[stat] = out
    pl["player_curves_matchup"] = curves_m
    pl["player_curves_matchup_note"] = (f"Week 1 2026 curves with SumerSports-primary opponent adjustment "
        f"(2025 defensive EPA ranks, beta fitted on 2025 W1-12 with 2024 ranks: {beta}). "
        f"Adjusted {n_adj} of {n_tot} curve entries; unadjusted when no week-1 opponent was found. "
        f"Additive only - index.html still reads player_curves.")
    rest("backtest", method="POST", body={"id": "latest", "payload": pl})
    print("backtest/latest updated", file=sys.stderr)

    # ---- matchup matrix ----
    def teamcard(ab):
        o, d = off25.get(ab, {}), def25.get(ab, {})
        return {"team": ab,
                "off_epa_pass_rank": off_pass25.get(ab), "off_epa_rush_rank": off_rush25.get(ab),
                "def_epa_pass_rank": def_pass25.get(ab), "def_epa_rush_rank": def_rush25.get(ab),
                "off_epa_play": o.get("EPA/Play"), "def_epa_play": d.get("EPA/Play"),
                "off_success": o.get("Success %"), "def_success": d.get("Success %"),
                "off_adot": o.get("ADoT")}
    matrix = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "week": 1, "season": 2026, "ranks_season": 2025,
              "note": "Ranks out of 32. Defense rank 1 = fewest EPA allowed (tough matchup). Offense rank 1 = most EPA.",
              "games": [{"date": g["date"], "away": g["away"], "home": g["home"],
                          "away_card": teamcard(g["away"]), "home_card": teamcard(g["home"])} for g in games]}

    # ---- fantasy roster reads ----
    def roster(league_id, roster_id):
        rs = get(f"https://api.sleeper.app/v1/league/{league_id}/rosters")
        for r in rs:
            if r.get("roster_id") == roster_id:
                return r
    def pname(pid):
        p = players.get(pid) or {}
        return p.get("full_name"), p.get("position"), p.get("team")
    def light(pos, opp):
        if not opp: return "yellow", "opponent unknown"
        dr = def_pass25.get(opp, 16.5) if pos in ("QB", "WR", "TE") else def_rush25.get(opp, 16.5)
        if pos == "QB":  # both matter; weight pass
            dr = 0.7 * def_pass25.get(opp, 16.5) + 0.3 * def_rush25.get(opp, 16.5)
        if dr <= 10: return "red", f"{opp} is a top-10 defense vs this spot (rank {dr:.0f})"
        if dr >= 23: return "green", f"{opp} is a bottom-10 defense vs this spot (rank {dr:.0f})"
        return "yellow", f"{opp} mid-pack (rank {dr:.0f})"
    fantasy = []
    for label, lid, rid, whose in (("Badger Dynasty - The Innocence Project (Tyler)", 1312207312923918336, 3, "tyler"),
                                   ("DYNASTATION - 2017 Browns (opponent scout)", 1312077150911733760, 1, "opponent")):
        try:
            r = roster(lid, rid)
            if not r: fantasy.append({"league": label, "error": "roster not found"}); continue
            rows = []
            for pid in (r.get("starters") or []):
                nm, pos, team = pname(pid)
                if not nm or pos not in ("QB", "RB", "WR", "TE"): continue  # K/DEF/IDP: no matchup read
                opp = opp26.get(team)
                lg, why = light(pos, opp)
                rows.append({"player": nm, "pos": pos, "team": team, "opp": opp,
                             "light": lg, "why": why})
            fantasy.append({"league": label, "whose": whose, "starters": rows})
        except Exception as e:
            fantasy.append({"league": label, "error": str(e)})
    matrix["fantasy"] = fantasy
    rest("backtest", method="POST", body={"id": "matchups_latest", "payload": matrix})
    print("matchups_latest published", file=sys.stderr)

    # verify
    chk = json.loads(rest("backtest?id=eq.latest&select=payload"))[0]["payload"]
    print("verify latest keys:", sorted(chk.keys()))
    print("matchup_model_grade present:", "matchup_model_grade" in chk,
          "| curves_matchup stats:", len(chk.get("player_curves_matchup", {})))
    chk2 = json.loads(rest("backtest?id=eq.matchups_latest&select=payload"))[0]["payload"]
    print("matchups games:", len(chk2["games"]), "| fantasy leagues:", len(chk2["fantasy"]))

if __name__ == "__main__":
    main()
