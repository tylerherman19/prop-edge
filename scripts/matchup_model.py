#!/usr/bin/env python3
"""prop-edge phase 2: does SumerSports opponent strength find edges better?

Three variants on the IDENTICAL walk-forward protocol as backtest.py
player_model (line = Sleeper weekly projection as market proxy, calibration
fit W1-12, temporal holdout W13-18, same zero-fill and MIN_GAMES rules):

  baseline        current model: player-log mean shrunk toward position baseline
  matchup_only    position baseline x opponent multiplier (SumerSports def rank)
  matchup_primary matchup-adjusted center + player-log deviation as corrector
                  (the user's 9/9 steering: SumerSports primary, log corrects)

Opponent strength: per-team defensive EPA/Pass and EPA/Rush from the
sumersports Supabase table, ranked across the 32 teams. 2024 ranks are the
leakage-free prior for the 2025 holdout; 2025 ranks run as a labeled
sensitivity (they leak holdout outcomes). The multiplier scale (beta) is
fitted on train weeks only, per stat family.
"""
import csv, json, math, os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest import (cached, sleeper_season, build_logs, Baselines, blend, wstats,
                      p_hit, fit_calibration, interp, auc, norm_name,
                      ALL_STATS, COMBO, MIN_GAMES, SHRINK_K, SIGMA_FLOOR)

BETA_CAP = 0.60          # max opponent swing on the expectation (fraction)
FAM = {"pass_yds":"pass","pass_tds":"pass","pass_att":"pass","pass_comp":"pass",
       "pass_int":"pass_int","rush_yds":"rush","rush_tds":"rush","rush_att":"rush",
       "rec_yds":"pass","rec":"pass","rec_tds":"pass","rec_tgt":"pass",
       "rush_rec_yds":"mixed","pass_rush_yds":"pass","any_td":"posdep"}

def load_def_ranks():
    key = os.environ["SUPABASE_SERVICE_KEY"]; base = os.environ["SUPABASE_URL"].rstrip("/")
    req = urllib.request.Request(base + "/rest/v1/sumersports?id=eq.team_def&select=payload",
                                 headers={"apikey": key, "Authorization": "Bearer " + key})
    pl = json.loads(urllib.request.urlopen(req, timeout=60).read())[0]["payload"]
    out = {}
    for season, rows in pl["seasons"].items():
        ranks = {}
        for field, tag in (("EPA/Pass", "pass"), ("EPA/Rush", "rush")):
            vals = []
            for r in rows:
                try: vals.append((float(r[field]), r["abbr"]))
                except (KeyError, ValueError, TypeError): pass
            vals.sort()  # most negative EPA allowed = best defense = rank 1
            for i, (_, ab) in enumerate(vals):
                ranks.setdefault(ab, {})[tag] = i + 1
        out[season] = ranks
    return out

ALIAS = {"LA": "LAR", "LAR": "LA"}
def load_opp_map():
    """(norm_name, week) -> opponent abbr, 2025 REG, from nflverse weekly."""
    m = {}
    with open("cache/nflverse_week_2025.csv") as f:
        for r in csv.DictReader(f):
            if r.get("season_type") != "REG": continue
            k = (norm_name(r["player_display_name"]), int(r["week"]))
            if r.get("opponent_team"): m[k] = ALIAS.get(r["opponent_team"], r["opponent_team"])
    return m

def z_of(rank):  # rank 1 (best def) -> ~-0.97 ; rank 32 (worst) -> ~+0.94
    return (rank - 16.5) / 16.5

def opp_z(stat, pos, opp, ranks):
    if opp is None or opp not in ranks: return None
    fam = FAM[stat]
    rr = ranks[opp]
    if fam == "pass": return z_of(rr["pass"])
    if fam == "rush": return z_of(rr["rush"])
    if fam == "pass_int": return -z_of(rr["pass"])   # weak def -> fewer INTs
    if fam == "mixed": return (z_of(rr["pass"]) + z_of(rr["rush"])) / 2
    # posdep (any_td): QBs score on the ground, WR/TE through the air, RB rush
    return z_of(rr["pass" if pos in ("WR", "TE") else "rush"])

def variant_pairs(logs, sp_idx, bl, opp_map, ranks, mode):
    """Replicates player_model's pair loop; mean/sigma per variant."""
    pairs = {}
    beta_num, beta_den = {}, {}
    for k, lg in logs.items():
        pos = lg["pos"]
        weeks = lg["weeks"]
        ever_pre = set()
        for wi, (seq, raw) in enumerate(weeks):
            if 202500 <= seq < 202600 and wi >= MIN_GAMES and ever_pre:
                pr = sp_idx.get((k, seq))
                if pr:
                    wk = seq - 202500
                    opp = opp_map.get((k, wk))
                    for t in ALL_STATS:
                        if t in COMBO:
                            c1, c2 = COMBO[t]
                            if c1 not in ever_pre and c2 not in ever_pre: continue
                            v1, v2 = pr.get(c1), pr.get(c2)
                            if v1 is None or v2 is None: continue
                            line = v1 + v2
                            val = raw.get(c1, 0.0) + raw.get(c2, 0.0)
                            prior = [w.get(c1, 0.0) + w.get(c2, 0.0) for _, w in weeks[:wi]]
                        else:
                            if t not in ever_pre: continue
                            line = pr.get(t)
                            if line is None: continue
                            val = raw.get(t, 0.0)
                            prior = [w.get(t, 0.0) for _, w in weeks[:wi]]
                        if line < 0.5 or val == line: continue
                        out = 1 if val > line else 0
                        base = bl.query(pos, t, before_seq=seq)
                        if mode == "baseline":
                            m, s, _ = blend(prior, base, t)
                        else:
                            if base is None: continue
                            z = opp_z(t, pos, opp, ranks)
                            if z is None: continue
                            mb = base[1]
                            if mb < 0.5: continue
                            fam = FAM[t]
                            if mode == "fit_beta":
                                if seq <= 202512:
                                    beta_num[fam] = beta_num.get(fam, 0) + z * (val - mb) / mb
                                    beta_den[fam] = beta_den.get(fam, 0) + z * z
                                continue
                            beta = BETA[fam]
                            m_adj = mb * (1 + beta * z)
                            if mode == "matchup_only":
                                m, s = m_adj, max(base[2], SIGMA_FLOOR.get(t, 1))
                            else:  # matchup_primary: player deviation corrects the adjusted center
                                mp, _sp = wstats(prior)
                                n = len(prior)
                                w = n / (n + SHRINK_K)
                                m = m_adj + w * (mp - mb)
                                sbase = max(base[2], SIGMA_FLOOR.get(t, 1))
                                s = math.sqrt(w * max(_sp, 0)**2 + (1 - w) * sbase**2)
                                s = max(s, SIGMA_FLOOR.get(t, 1))
                        pairs.setdefault(t, []).append((seq, p_hit(t, m, s, line), out))
            ever_pre |= set(raw)
    if mode == "fit_beta":
        return {fam: max(-BETA_CAP, min(BETA_CAP, beta_num.get(fam, 0) / beta_den[fam]))
                for fam in beta_den if beta_den[fam] > 0}
    return pairs

def holdout_metrics(trip, min_train=150):
    prs = [(p, o) for _, p, o in trip]
    if len(prs) < min_train: return None
    train = [(p, o) for seq, p, o in trip if seq <= 202512]
    test = [(p, o) for seq, p, o in trip if seq >= 202513]
    if len(train) < min_train or len(test) < 30: return None
    hfit = fit_calibration(train, min_pairs=150)
    res = {"n": len(prs), "holdout_n": len(test),
           "holdout_brier_raw": round(sum((p-o)**2 for p, o in test)/len(test), 4),
           "holdout_auc": auc(test)}
    b = sum(o for _, o in train) / len(train)
    res["holdout_brier_base_rate"] = round(sum((b-o)**2 for _, o in test)/len(test), 4)
    if hfit:
        cp = [interp(hfit["x"], hfit["y"], p) for p, _ in test]
        res["holdout_brier"] = round(sum((p-o)**2 for p, (_, o) in zip(cp, test))/len(test), 4)
        res["holdout_logloss"] = round(sum(-(o*math.log(max(p,1e-9))+(1-o)*math.log(max(1-p,1e-9)))
                                           for p, (_, o) in zip(cp, test))/len(test), 4)
        # confident-flag record on the holdout
        fl = [(p, o) for p, (_, o) in zip(cp, test) if p >= 0.62 or p <= 0.38]
        if fl:
            wins = sum(1 for p, o in fl if (p >= 0.62 and o == 1) or (p <= 0.38 and o == 0))
            res["flagged"] = {"n": len(fl), "wins": wins, "win_pct": round(100*wins/len(fl), 1)}
    return res

def main():
    global BETA
    ranks_all = load_def_ranks()
    opp_map = load_opp_map()
    print("opp map entries:", len(opp_map), file=sys.stderr)
    sp, sa = cached("sleeper_2025.json", lambda: sleeper_season(2025, range(1, 19)))
    logs = build_logs(sa)
    bl = Baselines(logs)
    sp_idx = {}
    for pr in sp:
        sp_idx[(norm_name(pr["player"]), 202500 + pr["week"])] = pr
    print(f"players: {len(logs)}", file=sys.stderr)

    report = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seasons": {}}
    for rank_season in ("2024", "2025"):
        ranks = ranks_all.get(rank_season, {})
        BETA = variant_pairs(logs, sp_idx, bl, opp_map, ranks, "fit_beta")
        print(f"[{rank_season} ranks] fitted beta: {BETA}", file=sys.stderr)
        run = {"ranks_used": f"SumerSports {rank_season} defensive EPA ranks",
               "leakage": "none (prior-season ranks)" if rank_season == "2024"
                          else "LEAKY: 2025 ranks include holdout games - sensitivity only",
               "beta": BETA, "variants": {}}
        for mode in ("baseline", "matchup_only", "matchup_primary"):
            pairs = variant_pairs(logs, sp_idx, bl, opp_map, ranks, mode)
            vres = {}
            pooled = []
            for t in ALL_STATS:
                trip = pairs.get(t) or []
                mres = holdout_metrics(trip)
                if mres: vres[t] = mres; pooled += trip
            if pooled:
                vres["_pooled"] = holdout_metrics(pooled)
            run["variants"][mode] = vres
            print(f"[{rank_season}] {mode}: pooled {vres.get('_pooled')}", file=sys.stderr)
        report["seasons"][rank_season] = run
    os.makedirs("out", exist_ok=True)
    with open("out/matchup_eval.json", "w") as f:
        json.dump(report, f)
    print("wrote out/matchup_eval.json", file=sys.stderr)

if __name__ == "__main__":
    main()
