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

def grade(proj, act, min_weeks=1):
    aidx = {}
    for a in act:
        aidx[(norm_name(a["player"]), a["week"])] = a
    errs = {s: [] for s in STATS}
    by_week = {}
    for pr in proj:
        a = aidx.get((norm_name(pr["player"]), pr["week"]))
        if not a: continue
        for s in STATS:
            pv, av = pr.get(s), a.get(s)
            if pv is None or av is None: continue
            if pv < 0.5: continue  # skip no-role players; keeps it honest and relevant
            errs[s].append(abs(pv - av))
            by_week.setdefault(pr["week"], {}).setdefault(s, []).append(abs(pv - av))
    out = {}
    for s, es in errs.items():
        if len(es) < 30: continue
        out[s] = {"n": len(es), "mae": round(sum(es)/len(es), 2), "med": round(median(es), 2)}
    wk = {str(w): {s: round(sum(v)/len(v), 2) for s, v in ss.items() if len(v) >= 20}
          for w, ss in sorted(by_week.items())}
    return out, wk

def main():
    os.makedirs("out", exist_ok=True); os.makedirs("cache", exist_ok=True)
    result = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seasons": {}}
    print("== sleeper 2025 ==", file=sys.stderr)
    sp, sa = sleeper_season(2025, range(1, 19))
    g, wk = grade(sp, sa)
    result["seasons"]["2025_sleeper"] = {"by_stat": g, "by_week_mae": wk,
        "note": "Sleeper weekly projections vs Sleeper actuals, 2025 regular season, players projected for a real role."}
    print("== espn 2025 ==", file=sys.stderr)
    ep, ea = espn_season(2025)
    g2, wk2 = grade(ep, ea)
    result["seasons"]["2025_espn"] = {"by_stat": g2, "by_week_mae": wk2,
        "note": "ESPN weekly projections vs ESPN actuals, 2025 regular season, players projected for a real role."}
    print("== 2026 finished weeks ==", file=sys.stderr)
    ep26, ea26 = espn_season(2026)
    # ESPN ships placeholder zero "actuals" for unplayed weeks; only weeks fully
    # before the current NFL week count. Weeks run Tue-Mon; W1 opened Sep 1 2026.
    from datetime import datetime, timezone
    anchor = datetime(2026, 9, 1, tzinfo=timezone.utc)
    cur = min(18, ((datetime.now(timezone.utc) - anchor).days // 7) + 1)
    done_weeks = [w for w in range(1, cur)]
    if done_weeks:
        g3, wk3 = grade([p for p in ep26 if p["week"] in done_weeks], ea26)
        result["seasons"]["2026_espn"] = {"by_stat": g3, "by_week_mae": wk3, "weeks": done_weeks,
            "note": "ESPN projections vs actuals, 2026 weeks " + ",".join(map(str, done_weeks)) + "."}
        sp26, _ = sleeper_season(2026, done_weeks)
        # grade sleeper 2026 against ESPN actuals by name+week
        g4, wk4 = grade(sp26, ea26)
        result["seasons"]["2026_sleeper"] = {"by_stat": g4, "by_week_mae": wk4, "weeks": done_weeks,
            "note": "Sleeper projections vs actuals (actuals via ESPN), 2026 weeks " + ",".join(map(str, done_weeks)) + "."}
    with open("out/backtest.json", "w") as f:
        json.dump(result, f)
    print("wrote out/backtest.json", file=sys.stderr)

if __name__ == "__main__":
    main()
