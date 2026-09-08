#!/usr/bin/env python3
"""prop-edge collector: pulls NFL prop market lines (FanDuel, PrizePicks) and
weekly projections (Sleeper, ESPN), joins them, computes edges, writes JSON.

Runs locally for testing (writes ./out/*.json) or in GitHub Actions (POSTs to
Supabase REST with service key). No auth needed for any source.
"""
import json, math, os, re, sys, time, urllib.request, unicodedata
from datetime import datetime, timezone, timedelta

FANDUEL_AK = "FhMFpcPWXMeyZxOx"
FD_LEAGUE = f"https://sbapi.md.sportsbook.fanduel.com/api/content-managed-page?page=CUSTOM&customPageId=nfl&_ak={FANDUEL_AK}"
FD_EVENT  = f"https://sbapi.md.sportsbook.fanduel.com/api/event-page?_ak={FANDUEL_AK}&eventId={{eid}}&tab={{tab}}"
PP_URL = "https://partner-api.prizepicks.com/projections?league_id=9&per_page=250&single_stat=true"
SLEEPER_PROJ = "https://api.sleeper.app/v1/projections/nfl/regular/{season}/{week}?position={pos}"
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
ESPN_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leaguedefaults/1?view=kona_player_info"

UA = {"User-Agent": "Mozilla/5.0 (prop-edge research collector)"}

def get(url, headers=None, tries=3):
    h = dict(UA); h.update(headers or {})
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == tries - 1: raise
            time.sleep(2 * (i + 1))

# ---------- week math ----------
def current_nfl_week(now=None):
    now = now or datetime.now(timezone.utc)
    # NFL weeks run Tue-Mon; W1 of 2026 opened Tue Sep 1 (games Sep 3-8).
    anchor = datetime(2026, 9, 1, tzinfo=timezone.utc)
    if now < anchor: return 1
    return min(18, ((now - anchor).days // 7) + 1)

def week_window(week, season=2026):
    anchor = datetime(season, 9, 1, tzinfo=timezone.utc)
    start = anchor + timedelta(days=7 * (week - 1))
    return start, start + timedelta(days=7)

# ---------- name normalization ----------
SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.I)
def norm_name(s):
    if not s: return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace(".", "").replace("'", "").replace("-", " ")
    s = SUFFIX.sub("", s)
    return re.sub(r"\s+", " ", s).strip()

# ---------- FanDuel ----------
FD_STAT = {
    "Passing Yds": "pass_yds", "Passing TDs": "pass_tds", "Passing Attempts": "pass_att",
    "Pass Completions": "pass_comp", "Passing Interceptions": "pass_int",
    "Rushing Yds": "rush_yds", "Rushing TDs": "rush_tds", "Rushing Attempts": "rush_att",
    "Receiving Yds": "rec_yds", "Receptions": "rec", "Receiving TDs": "rec_tds",
    "Rush + Rec Yds": "rush_rec_yds", "Pass + Rush Yds": "pass_rush_yds",
    "Longest Reception": "long_rec", "Longest Rush": "long_rush", "Longest Completion": "long_comp",
}
FD_PLAYER_RE = re.compile(r"^(.*?) - (Alt )?(.+)$")

def fanduel_collect(week, season, days_ahead=8):
    out = []
    league = get(FD_LEAGUE)
    events = (league.get("attachments") or {}).get("events") or {}
    now = datetime.now(timezone.utc)
    games = []
    for ev in events.values():
        name = ev.get("name") or ""
        if " @ " not in name: continue
        try:
            od = datetime.fromisoformat(ev["openDate"].replace("Z", "+00:00"))
        except Exception:
            continue
        if od < now - timedelta(hours=6) or od > now + timedelta(days=days_ahead):
            continue
        games.append({"id": ev["eventId"], "name": name, "open": ev["openDate"]})
    games.sort(key=lambda g: g["open"])
    tabs = ["passing-props", "rushing-props", "receiving-props"]
    for g in games:
        seen_players = set()
        for tab in tabs:
            try:
                data = get(FD_EVENT.format(eid=g["id"], tab=tab))
            except Exception as e:
                print(f"  FD event {g['id']} tab {tab} failed: {e}", file=sys.stderr); continue
            markets = (data.get("attachments") or {}).get("markets") or {}
            for m in markets.values():
                mn = m.get("marketName") or ""
                match = FD_PLAYER_RE.match(mn)
                if not match: continue
                player, alt, stat_name = match.group(1), match.group(2), match.group(3)
                if alt: continue  # skip alt ladders, keep the main line
                stat = FD_STAT.get(stat_name)
                if not stat: continue
                runners = m.get("runners") or []
                over = next((r for r in runners if r.get("resultType") == "OVER" or " Over" in (r.get("runnerName") or "")), None)
                under = next((r for r in runners if r.get("resultType") == "UNDER" or " Under" in (r.get("runnerName") or "")), None)
                if not over or not under: continue
                line = over.get("handicap")
                if line is None: continue
                def am(r):
                    return (((r.get("winRunnerOdds") or {}).get("americanDisplayOdds") or {}).get("americanOdds"))
                out.append({
                    "source": "fanduel", "game": g["name"], "kickoff": g["open"],
                    "player": player, "stat": stat, "line": line,
                    "over_odds": am(over), "under_odds": am(under),
                    "week": week, "season": season,
                })
        time.sleep(0.3)
    return out, games

# ---------- PrizePicks ----------
PP_STAT = {
    "Pass Yards": "pass_yds", "Pass TDs": "pass_tds", "Pass Attempts": "pass_att",
    "Pass Completions": "pass_comp", "INT": "pass_int",
    "Rush Yards": "rush_yds", "Rush TDs": "rush_tds", "Rush Attempts": "rush_att",
    "Receiving Yards": "rec_yds", "Receptions": "rec", "Rec Targets": "rec_tgt",
    "Pass+Rush Yds": "pass_rush_yds", "Rush+Rec Yds": "rush_rec_yds",
    "Longest Reception": "long_rec", "Longest Rush": "long_rush", "Longest Completion": "long_comp",
    "Player Touchdowns": "any_td",
}
def prizepicks_collect(week, season):
    out = []
    data = get(PP_URL)
    included = {}
    for inc in data.get("included") or []:
        included[(inc.get("type"), str(inc.get("id")))] = inc.get("attributes") or {}
    for row in data.get("data") or []:
        a = row.get("attributes") or {}
        stat = PP_STAT.get(a.get("stat_type"))
        if not stat: continue
        if a.get("is_promo") or a.get("in_game") or a.get("is_live"): continue
        if a.get("odds_type") != "standard": continue
        try:
            st = datetime.fromisoformat((a.get("start_time") or "").replace("Z", "+00:00"))
            w0, w1 = week_window(week, season)
            if not (w0 - timedelta(days=2) <= st < w1 + timedelta(days=2)): continue
        except Exception:
            pass
        rel = row.get("relationships") or {}
        pkey = rel.get("new_player", {}).get("data") or {}
        player = included.get(("new_player", str(pkey.get("id")))) or {}
        name = player.get("display_name") or player.get("name") or a.get("description")
        grel = rel.get("game", {}).get("data") or {}
        game = included.get(("game", str(grel.get("id")))) or {}
        out.append({
            "source": "prizepicks", "game": game.get("name") or "", "kickoff": a.get("start_time"),
            "player": name, "team": player.get("team"), "stat": stat, "line": a.get("line_score"),
            "over_odds": None, "under_odds": None, "week": week, "season": season,
        })
    return out

# ---------- Sleeper ----------
SL_STAT = {"pass_yd":"pass_yds","pass_td":"pass_tds","pass_att":"pass_att","pass_cmp":"pass_comp",
           "pass_int":"pass_int","rush_yd":"rush_yds","rush_td":"rush_tds","rush_att":"rush_att",
           "rec_yd":"rec_yds","rec":"rec","rec_td":"rec_tds","rec_tgt":"rec_tgt"}
def sleeper_collect(week, season):
    players = get(SLEEPER_PLAYERS)
    out = []
    for pos in ["QB", "RB", "WR", "TE"]:
        try:
            data = get(SLEEPER_PROJ.format(season=season, week=week, pos=pos))
        except Exception as e:
            print(f"  sleeper {pos} w{week} failed: {e}", file=sys.stderr); continue
        for pid, stats in (data or {}).items():
            p = players.get(pid) or {}
            name = p.get("full_name")
            if not name or not isinstance(stats, dict): continue
            for sk, canon in SL_STAT.items():
                v = stats.get(sk)
                if v is None: continue
                out.append({"source": "sleeper", "player": name, "team": p.get("team"),
                            "pos": pos, "stat": canon, "value": v, "week": week, "season": season})
            # derived combos
            ry, cy = stats.get("rush_yd"), stats.get("rec_yd")
            py = stats.get("pass_yd")
            if ry is not None and cy is not None:
                out.append({"source":"sleeper","player":name,"team":p.get("team"),"pos":pos,
                            "stat":"rush_rec_yds","value":round(ry+cy,2),"week":week,"season":season})
            if py is not None and ry is not None:
                out.append({"source":"sleeper","player":name,"team":p.get("team"),"pos":pos,
                            "stat":"pass_rush_yds","value":round(py+ry,2),"week":week,"season":season})
    return out

# ---------- ESPN ----------
ESPN_TEAM = {1:"ATL",2:"BUF",3:"CHI",4:"CIN",5:"CLE",6:"DAL",7:"DEN",8:"DET",9:"GB",10:"TEN",
             11:"IND",12:"KC",13:"LV",14:"LAR",15:"MIA",16:"MIN",17:"NE",18:"NO",19:"NYG",20:"NYJ",
             21:"PHI",22:"ARI",23:"PIT",24:"LAC",25:"SF",26:"SEA",27:"TB",28:"WAS",29:"CAR",30:"JAX",
             33:"BAL",34:"HOU"}
ESPN_STAT = {"3":"pass_yds","4":"pass_tds","0":"pass_att","1":"pass_comp","19":"pass_int",
             "24":"rush_yds","25":"rush_tds","23":"rush_att","42":"rec_yds","53":"rec","43":"rec_tds","58":"rec_tgt"}
ESPN_POS = {1:"QB",2:"RB",3:"WR",4:"TE",5:"K"}
def espn_collect(week, season, limit=2000):
    out = []
    filt = json.dumps({"players":{"limit":limit,"sortPercOwned":{"sortPriority":1,"sortAsc":False}}})
    data = get(ESPN_URL.format(season=season), headers={"X-Fantasy-Filter": filt})
    for row in (data or {}).get("players") or []:
        p = row.get("player") or {}
        name = p.get("fullName")
        if not name: continue
        team = ESPN_TEAM.get(p.get("proTeamId"))
        pos = ESPN_POS.get(p.get("defaultPositionId"))
        entry = next((s for s in (p.get("stats") or [])
                      if s.get("scoringPeriodId") == week and s.get("statSourceId") == 1), None)
        if not entry: continue
        smap = entry.get("stats") or {}
        vals = {}
        for sid, canon in ESPN_STAT.items():
            v = smap.get(sid)
            if v is not None: vals[canon] = v
        if "rush_yd" if False else False: pass
        for canon, v in vals.items():
            out.append({"source":"espn","player":name,"team":team,"pos":pos,
                        "stat":canon,"value":round(v,2),"week":week,"season":season})
        if "rush_yds" in vals and "rec_yds" in vals:
            out.append({"source":"espn","player":name,"team":team,"pos":pos,
                        "stat":"rush_rec_yds","value":round(vals["rush_yds"]+vals["rec_yds"],2),"week":week,"season":season})
        if "pass_yds" in vals and "rush_yds" in vals:
            out.append({"source":"espn","player":name,"team":team,"pos":pos,
                        "stat":"pass_rush_yds","value":round(vals["pass_yds"]+vals["rush_yds"],2),"week":week,"season":season})
    return out

# ---------- edges ----------
STAT_LABEL = {
 "pass_yds":"Passing yards","pass_tds":"Passing TDs","pass_att":"Pass attempts","pass_comp":"Completions",
 "pass_int":"Interceptions","rush_yds":"Rushing yards","rush_tds":"Rushing TDs","rush_att":"Rush attempts",
 "rec_yds":"Receiving yards","rec":"Receptions","rec_tds":"Receiving TDs","rec_tgt":"Targets",
 "rush_rec_yds":"Rush + receiving yards","pass_rush_yds":"Pass + rush yards",
 "long_rec":"Longest reception","long_rush":"Longest rush","long_comp":"Longest completion","any_td":"Touchdowns",
}
def build_edges(lines, projections):
    proj = {}
    for p in projections:
        proj[(p["source"], norm_name(p["player"]), p["stat"])] = p
    edges = []
    for ln in lines:
        key_player = norm_name(ln["player"])
        row = dict(ln)
        row["name_key"] = key_player
        for src in ("sleeper", "espn"):
            pr = proj.get((src, key_player, ln["stat"]))
            row[f"{src}_proj"] = pr["value"] if pr else None
            row[f"{src}_pos"] = (pr.get("pos") if pr else None)
            row[f"{src}_team"] = (pr.get("team") if pr else None)
            if pr:
                row[f"{src}_edge"] = round(pr["value"] - ln["line"], 2)
                row[f"{src}_edge_pct"] = round((pr["value"] - ln["line"]) / ln["line"] * 100, 1) if ln["line"] else None
            else:
                row[f"{src}_edge"] = None; row[f"{src}_edge_pct"] = None
        edges.append(row)
    return edges

def main():
    season = 2026
    week = int(os.environ.get("PROP_WEEK") or current_nfl_week())
    print(f"collecting season={season} week={week}", file=sys.stderr)
    fd_lines, games = fanduel_collect(week, season)
    print(f"  fanduel: {len(fd_lines)} lines across {len(games)} games", file=sys.stderr)
    pp_lines = prizepicks_collect(week, season)
    print(f"  prizepicks: {len(pp_lines)} lines", file=sys.stderr)
    sl = sleeper_collect(week, season)
    print(f"  sleeper: {len(sl)} projections", file=sys.stderr)
    try:
        es = espn_collect(week, season)
    except Exception as e:
        print(f"  espn failed: {e}", file=sys.stderr); es = []
    print(f"  espn: {len(es)} projections", file=sys.stderr)
    lines = fd_lines + pp_lines
    projections = sl + es
    edges = build_edges(lines, projections)
    joined_sl = sum(1 for e in edges if e["sleeper_proj"] is not None)
    joined_es = sum(1 for e in edges if e["espn_proj"] is not None)
    print(f"  edges: {len(edges)} rows; sleeper joined {joined_sl}, espn joined {joined_es}", file=sys.stderr)
    payload = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "season": season, "week": week, "games": games,
        "counts": {"lines": len(lines), "fanduel": len(fd_lines), "prizepicks": len(pp_lines),
                   "sleeper_proj": len(sl), "espn_proj": len(es),
                   "edges": len(edges), "joined_sleeper": joined_sl, "joined_espn": joined_es},
        "edges": edges,
    }
    os.makedirs("out", exist_ok=True)
    with open("out/edges.json", "w") as f:
        json.dump(payload, f)
    print("wrote out/edges.json", file=sys.stderr)
    supabase_publish(payload, projections)


# ---------- Supabase write + grading (only when env keys are set) ----------
def sb(method, path, key, body=None, prefer=None):
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("apikey", key); req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    if prefer: req.add_header("Prefer", prefer)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else None

def supabase_publish(payload, projections):
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not os.environ.get("SUPABASE_URL") or not key:
        print("  supabase env not set; skipping publish", file=sys.stderr); return
    run = sb("POST", "runs", key, {"season": payload["season"], "week": payload["week"],
             "taken_at": payload["taken_at"], "counts": payload["counts"]}, prefer="return=representation")[0]
    rid = run["id"]
    rows = []
    for e in payload["edges"]:
        rows.append({"run_id": rid, "source": e["source"], "game": e["game"], "kickoff": e["kickoff"],
                     "player": e["player"], "team": e.get("team") or e.get("sleeper_team") or e.get("espn_team"),
                     "stat": e["stat"], "line": e["line"], "over_odds": e["over_odds"], "under_odds": e["under_odds"],
                     "sleeper_proj": e["sleeper_proj"], "espn_proj": e["espn_proj"],
                     "sleeper_edge": e["sleeper_edge"], "espn_edge": e["espn_edge"],
                     "week": e["week"], "season": e["season"]})
    for i in range(0, len(rows), 500):
        sb("POST", "edges", key, rows[i:i+500])
    print(f"  supabase: run {rid}, {len(rows)} edges", file=sys.stderr)
    # backtest payload if present
    try:
        bt = json.load(open("out/backtest.json"))
        sb("POST", "backtest", key, {"id": "latest", "payload": bt, "updated": payload["taken_at"]},
           prefer="resolution=merge-duplicates")
        print("  supabase: backtest published", file=sys.stderr)
    except FileNotFoundError:
        pass
    grade_finished_weeks(key, payload["season"], payload["week"])

def grade_finished_weeks(key, season, cur_week):
    """Grade stored edges for finished weeks against ESPN actuals."""
    for w in range(1, cur_week):
        done = sb("GET", f"live_grades?select=id&week=eq.{w}&season=eq.{season}&limit=1", key)
        if done: continue
        edges = sb("GET", f"edges?select=*&week=eq.{w}&season=eq.{season}&order=run_id.desc&limit=3000", key) or []
        if not edges: continue
        # keep only the latest run's rows
        latest = edges[0]["run_id"]; edges = [e for e in edges if e["run_id"] == latest]
        try:
            _, act = espn_actuals_only(season, w)
        except Exception as ex:
            print(f"  grade w{w}: actuals failed: {ex}", file=sys.stderr); continue
        aidx = {norm_name(a["player"]): a for a in act}
        out = []
        for e in edges:
            a = aidx.get(norm_name(e["player"]))
            if not a: continue
            av = a.get(e["stat"])
            if av is None: continue
            for src in ("sleeper", "espn"):
                pv = e.get(f"{src}_proj")
                if pv is None: continue
                edge = pv - e["line"]
                if abs(edge) < 1.0: continue  # only grade real disagreements
                side = "over" if edge > 0 else "under"
                hit = (av > e["line"]) if side == "over" else (av < e["line"])
                out.append({"week": w, "season": season, "source": e["source"], "player": e["player"],
                            "stat": e["stat"], "line": e["line"], "proj_source": src, "proj_value": pv,
                            "actual": av, "side": side, "hit": hit})
        if out:
            sb("POST", "live_grades", key, out, prefer="resolution=merge-duplicates")
        print(f"  grade w{w}: {len(out)} edges graded", file=sys.stderr)

def espn_actuals_only(season, week):
    filt = json.dumps({"players":{"limit":2500,"sortPercOwned":{"sortPriority":1,"sortAsc":False}}})
    data = get(ESPN_URL.format(season=season), headers={"X-Fantasy-Filter": filt})
    act = []
    for row in (data or {}).get("players") or []:
        p = row.get("player") or {}
        name = p.get("fullName")
        if not name: continue
        entry = next((s for s in (p.get("stats") or [])
                      if s.get("scoringPeriodId") == week and s.get("statSourceId") == 0), None)
        if not entry: continue
        smap = entry.get("stats") or {}
        r = {"player": name}
        for sid, canon in ESPN_STAT.items():
            v = smap.get(sid)
            if v is not None: r[canon] = v
        act.append(r)
    return None, act

if __name__ == "__main__":
    main()
