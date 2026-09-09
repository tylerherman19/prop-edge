#!/usr/bin/env python3
"""prop-edge SumerSports pull: team efficiency + tendency tables (offense,
defense, formation, personnel; 2024 + 2025 seasons), no-auth public pages.
Publishes to the `sumersports` Supabase table, one row per dataset.
Run weekly (in-season pages update; off-season they just restate).
"""
import json, os, re, sys, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (prop-edge research collector)"}
PAGES = {
    "team_off":  "https://sumersports.com/teams/offensive/",
    "team_def":  "https://sumersports.com/teams/defensive/",
    "form_off":  "https://sumersports.com/teams/offensive/formation-tendency/",
    "form_def":  "https://sumersports.com/teams/defensive/formation-tendency/",
    "pers_off":  "https://sumersports.com/teams/offensive/personnel-tendency/",
    "pers_def":  "https://sumersports.com/teams/defensive/personnel-tendency/",
}
SEASONS = (2025, 2024)
TEAM_ABBR = {"Arizona Cardinals":"ARI","Atlanta Falcons":"ATL","Baltimore Ravens":"BAL",
 "Buffalo Bills":"BUF","Carolina Panthers":"CAR","Chicago Bears":"CHI","Cincinnati Bengals":"CIN",
 "Cleveland Browns":"CLE","Dallas Cowboys":"DAL","Denver Broncos":"DEN","Detroit Lions":"DET",
 "Green Bay Packers":"GB","Houston Texans":"HOU","Indianapolis Colts":"IND","Jacksonville Jaguars":"JAX",
 "Kansas City Chiefs":"KC","Las Vegas Raiders":"LV","Los Angeles Chargers":"LAC","Los Angeles Rams":"LAR",
 "Miami Dolphins":"MIA","Minnesota Vikings":"MIN","New England Patriots":"NE","New Orleans Saints":"NO",
 "New York Giants":"NYG","New York Jets":"NYJ","Philadelphia Eagles":"PHI","Pittsburgh Steelers":"PIT",
 "San Francisco 49ers":"SF","Seattle Seahawks":"SEA","Tampa Bay Buccaneers":"TB","Tennessee Titans":"TEN",
 "Washington Commanders":"WAS"}

def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode()

def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()

def parse_table(html):
    ths = [clean(t) for t in re.findall(r"<th[^>]*>(.*?)</th>", html, re.S)]
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)[1:]:
        tds = [clean(t) for t in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(tds) < len(ths): continue
        row = dict(zip(ths, tds))
        m = re.match(r"^(\d+)\.(.+)$", row.get("Team", ""))
        if m:
            row["rank"] = int(m.group(1))
            row["Team"] = m.group(2).strip()
        row["abbr"] = TEAM_ABBR.get(row.get("Team", ""))
        rows.append(row)
    return ths, rows

def main():
    out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "datasets": {}}
    for name, url in PAGES.items():
        seasons = {}
        headers = None
        for yr in SEASONS:
            try:
                html = fetch(url + ("" if yr == SEASONS[0] else f"?season={yr}"))
                headers, rows = parse_table(html)
                seasons[str(yr)] = rows
                print(f"  {name} {yr}: {len(rows)} rows", file=sys.stderr)
            except Exception as e:
                print(f"  {name} {yr} failed: {e}", file=sys.stderr)
            time.sleep(0.5)
        out["datasets"][name] = {"headers": headers, "seasons": seasons}
    os.makedirs("out", exist_ok=True)
    with open("out/sumersports.json", "w") as f:
        json.dump(out, f)
    print("wrote out/sumersports.json", file=sys.stderr)
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not os.environ.get("SUPABASE_URL") or not key:
        print("  supabase env not set; skipping publish", file=sys.stderr); return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from collect import sb
    for name, ds in out["datasets"].items():
        sb("POST", "sumersports", key,
           {"id": name, "payload": {"generated": out["generated"], "headers": ds["headers"],
                                    "seasons": ds["seasons"]}},
           prefer="resolution=merge-duplicates")
        print(f"  supabase: sumersports/{name} published", file=sys.stderr)

if __name__ == "__main__":
    main()
