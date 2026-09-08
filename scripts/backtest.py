#!/usr/bin/env python3
"""prop-edge backtest: grades Sleeper and ESPN weekly projections against actuals.
2025 season: full W1-18 for both sources. 2026: weeks with finished games.
Writes out/backtest.json.
"""
import json, os, re, sys, time, unicodedata, urllib.request
from statistics import median

UA = {"User-Agent": "Mozilla/5.0 (prop-edge research collector)"}
def get(url, headers=None, tries=3):
    h = dict(UA); h.update(headers or {})
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == tries - 1: raise
            time.sleep(2 * (i + 1))

SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.I)
def norm_name(s):
    if not s: return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace(".", "").replace("'", "").replace("-", " ")
    s = SUFFIX.sub("", s)
    return re.sub(r"\s+", " ", s).strip()

STATS = ["pass_yds","pass_tds","pass_att","pass_comp","pass_int","rush_yds","rush_tds","rush_att",
         "rec_yds","rec","rec_tds","rec_tgt"]
COMBO = {"rush_rec_yds": ("rush_yds","rec_yds"), "pass_rush_yds": ("pass_yds","rush_yds"),
         "any_td": ("rush_tds","rec_tds")}
SL_STAT = {"pass_yd":"pass_yds","pass_td":"pass_tds","pass_att":"pass_att","pass_cmp":"pass_comp",
           "pass_int":"pass_int","rush_yd":"rush_yds","rush_td":"rush_tds","rush_att":"rush_att",
           "rec_yd":"rec_yds","rec":"rec","rec_td":"rec_tds","rec_tgt":"rec_tgt"}
ESPN_STAT = {"3":"pass_yds","4":"pass_tds","0":"pass_att","1":"pass_comp","19":"pass_int",
             "24":"rush_yds","25":"rush_tds","23":"rush_att","42":"rec_yds","53":"rec","43":"rec_tds","58":"rec_tgt"}
ESPN_POS = {1:"QB",2:"RB",3:"WR",4:"TE",5:"K"}
POS = ["QB","RB","WR","TE"]

def sleeper_season(season, weeks):
    players = get("https://api.sleeper.app/v1/players/nfl")
    proj, act = [], []
    for w in weeks:
        for kind, url_t, sink in (("proj","https://api.sleeper.app/v1/projections/nfl/regular/%d/%d?position=%s", proj),
                                  ("act","https://api.sleeper.app/v1/stats/nfl/regular/%d/%d?position=%s", act)):
            for pos in POS:
                try:
                    data = get(url_t % (season, w, pos))
                except Exception as e:
                    print(f"  sleeper {kind} {season} w{w} {pos}: {e}", file=sys.stderr); continue
                for pid, st in (data or {}).items():
                    p = players.get(pid) or {}
                    name = p.get("full_name")
                    if not name or not isinstance(st, dict): continue
                    row = {"player": name, "week": w, "pos": pos}
                    for sk, canon in SL_STAT.items():
                        if st.get(sk) is not None: row[canon] = st[sk]
                    sink.append(row)
        print(f"  sleeper {season} w{w} done", file=sys.stderr)
    return proj, act

def espn_season(season):
    filt = json.dumps({"players":{"limit":2500,"sortPercOwned":{"sortPriority":1,"sortAsc":False}}})
    data = get(f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leaguedefaults/1?view=kona_player_info",
               headers={"X-Fantasy-Filter": filt})
    proj, act = [], []
    for row in (data or {}).get("players") or []:
        p = row.get("player") or {}
        name = p.get("fullName")
        if not name: continue
        pos = ESPN_POS.get(p.get("defaultPositionId"))
        for st in p.get("stats") or []:
            w = st.get("scoringPeriodId")
            src = st.get("statSourceId")
            if not w or w < 1 or w > 18: continue
            r = {"player": name, "week": w, "pos": pos}
            for sid, canon in ESPN_STAT.items():
                v = (st.get("stats") or {}).get(sid)
                if v is not None: r[canon] = v
            if src == 1: proj.append(r)
            elif src == 0: act.append(r)
    return proj, act

def quantiles(xs, points=101):
    xs = sorted(xs)
    n = len(xs)
    if n == 0: return []
    return [round(xs[min(n - 1, int(p / 100 * (n - 1) + 0.5))], 3) for p in range(points)]

def grade(proj, act, min_weeks=1):
    aidx = {}
    for a in act:
        aidx[(norm_name(a["player"]), a["week"])] = a
    errs = {s: [] for s in STATS}
    serrs = {s: [] for s in STATS}
    by_week = {}
    for pr in proj:
        a = aidx.get((norm_name(pr["player"]), pr["week"]))
        if not a: continue
        for s in STATS:
            pv, av = pr.get(s), a.get(s)
            if pv is None or av is None: continue
            if pv < 0.5: continue  # skip no-role players; keeps it honest and relevant
            errs[s].append(abs(pv - av))
            serrs[s].append(pv - av)
            by_week.setdefault(pr["week"], {}).setdefault(s, []).append(abs(pv - av))
    # combo stats: rush+rec yards etc., paired per player-week
    cerrs = {s: [] for s in COMBO}
    pr_idx = {}
    for pr in proj:
        pr_idx.setdefault((norm_name(pr["player"]), pr["week"]), pr)
    for key, pr in pr_idx.items():
        a = aidx.get(key)
        if not a: continue
        for cs, (c1, c2) in COMBO.items():
            p1, p2, a1, a2 = pr.get(c1), pr.get(c2), a.get(c1), a.get(c2)
            if None in (p1, p2, a1, a2): continue
            pv = p1 + p2
            if pv < 0.5: continue
            cerrs[cs].append(pv - (a1 + a2))
    out = {}
    errq = {}
    for s, es in errs.items():
        if len(es) < 30: continue
        out[s] = {"n": len(es), "mae": round(sum(es)/len(es), 2), "med": round(median(es), 2)}
        errq[s] = {"n": len(es), "q": quantiles(serrs[s]), "ae_q": quantiles(es)}
    for s, es in cerrs.items():
        if len(es) < 30: continue
        errq[s] = {"n": len(es), "q": quantiles(es), "ae_q": quantiles([abs(e) for e in es])}
    wk = {str(w): {s: round(sum(v)/len(v), 2) for s, v in ss.items() if len(v) >= 20}
          for w, ss in sorted(by_week.items())}
    return out, wk, errq

def grade_avg(sp, sa, ep, ea):
    """Errors for the average-of-models estimate, paired per player-week-stat."""
    aidx = {}
    for a in sa + ea:
        aidx.setdefault((norm_name(a["player"]), a["week"]), a)
    eidx = {}
    for pr in ep:
        eidx[(norm_name(pr["player"]), pr["week"])] = pr
    serrs = {s: [] for s in STATS}
    cerrs = {s: [] for s in COMBO}
    for pr in sp:
        a = aidx.get((norm_name(pr["player"]), pr["week"]))
        o = eidx.get((norm_name(pr["player"]), pr["week"]))
        if not a or not o: continue
        for s in STATS:
            v1, v2, av = pr.get(s), o.get(s), a.get(s)
            if v1 is None or v2 is None or av is None: continue
            pv = (v1 + v2) / 2
            if pv < 0.5: continue
            serrs[s].append(pv - av)
        for cs, (c1, c2) in COMBO.items():
            v1a, v1b = pr.get(c1), pr.get(c2)
            v2a, v2b = o.get(c1), o.get(c2)
            a1, a2 = a.get(c1), a.get(c2)
            if None in (v1a, v1b, v2a, v2b, a1, a2): continue
            pv = (v1a + v1b) / 2 + (v2a + v2b) / 2
            if pv < 0.5: continue
            cerrs[cs].append(pv - (a1 + a2))
    errq = {}
    for s, es in list(serrs.items()) + list(cerrs.items()):
        if len(es) < 30: continue
        errq[s] = {"n": len(es), "q": quantiles(es), "ae_q": quantiles([abs(e) for e in es])}
    return errq

def main():
    os.makedirs("out", exist_ok=True); os.makedirs("cache", exist_ok=True)
    result = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seasons": {}}
    print("== sleeper 2025 ==", file=sys.stderr)
    sp, sa = sleeper_season(2025, range(1, 19))
    g, wk, eq = grade(sp, sa)
    result["seasons"]["2025_sleeper"] = {"by_stat": g, "by_week_mae": wk,
        "note": "Sleeper weekly projections vs Sleeper actuals, 2025 regular season, players projected for a real role."}
    print("== espn 2025 ==", file=sys.stderr)
    ep, ea = espn_season(2025)
    g2, wk2, eq2 = grade(ep, ea)
    result["seasons"]["2025_espn"] = {"by_stat": g2, "by_week_mae": wk2,
        "note": "ESPN weekly projections vs ESPN actuals, 2025 regular season, players projected for a real role."}
    eqa = grade_avg(sp, sa, ep, ea)
    result["errors"] = {"sleeper": eq, "espn": eq2, "avg": eqa,
        "note": "Signed error (projection minus actual) percentiles per stat, 0-100 in steps of 1, 2025 season. ae_q is the absolute miss. avg pairs weeks where both models projected the player."}
    result["note_2026"] = ("The 2026 season opens Sep 9, so no 2026 weeks are graded here. "
        "This season's record builds week by week in the live-grades table as games finish.")
    with open("out/backtest.json", "w") as f:
        json.dump(result, f)
    print("wrote out/backtest.json", file=sys.stderr)

if __name__ == "__main__":
    main()
