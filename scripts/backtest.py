#!/usr/bin/env python3
"""prop-edge backtest: grades Sleeper and ESPN weekly projections against actuals,
and fits the player-level model (per player-stat recency-weighted distribution
with shrinkage to the position baseline) used for the live board's hit%.

2025 season: full W1-18 for both sources. 2026: weeks with finished games feed
the live player curves. Writes out/backtest.json (published to Supabase as the
backtest payload, id=latest).
"""
import json, math, os, re, sys, time, unicodedata, urllib.request
from bisect import bisect_left
from statistics import median
from datetime import datetime, timezone

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

def cached(name, fn, ttl=6 * 3600):
    p = os.path.join("cache", name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
        return json.load(open(p))
    d = fn()
    with open(p, "w") as f:
        json.dump(d, f)
    return d

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
ALL_STATS = STATS + list(COMBO)
SL_STAT = {"pass_yd":"pass_yds","pass_td":"pass_tds","pass_att":"pass_att","pass_cmp":"pass_comp",
           "pass_int":"pass_int","rush_yd":"rush_yds","rush_td":"rush_tds","rush_att":"rush_att",
           "rec_yd":"rec_yds","rec":"rec","rec_td":"rec_tds","rec_tgt":"rec_tgt"}
ESPN_STAT = {"3":"pass_yds","4":"pass_tds","0":"pass_att","1":"pass_comp","19":"pass_int",
             "24":"rush_yds","25":"rush_tds","23":"rush_att","42":"rec_yds","53":"rec","43":"rec_tds","58":"rec_tgt"}
ESPN_POS = {1:"QB",2:"RB",3:"WR",4:"TE",5:"K"}
POS = ["QB","RB","WR","TE"]

# ---- player-level model constants ----
HALF_LIFE = 5      # games back at which a game's weight halves
SHRINK_K = 4       # prior strength, in games, of the position baseline
MIN_GAMES = 3      # below this the site shows "-" instead of a number
CALIB_CLAMP = 0.20 # max calibration swing, same guardrail as edge-nfl
CALIB_KNOT_N = 50  # training pairs near a knot for its full swing; below that the swing shrinks
MIN_PAIRS = 1000   # walk-forward pairs a stat needs before its curve is published at all.
                   # 21 isotonic knots fitted on <300 training pairs is ~14 points per knot:
                   # the clamp saturates on noise and manufactures 90-98% "locks". At 1000+
                   # pairs (600+ in the fit window, 30+ per knot) the curve is estimable.
                   # Every stat below this gate scored at or under chance on the 2025 holdout.
# Counting stats that are mostly 0/1/2 in a game: a normal CDF is the wrong shape for
# them (it cannot respect the integer support and floors the sd at a constant). Poisson
# on the player's own weighted mean is the minimal honest alternative.
COUNT_STATS = {"pass_tds", "pass_int", "rush_tds", "rec_tds", "any_td"}
P_FLOOR, P_CEIL = 0.02, 0.98
SIGMA_FLOOR = {"pass_yds":45,"pass_tds":0.55,"pass_att":5,"pass_comp":4,"pass_int":0.5,
               "rush_yds":16,"rush_tds":0.35,"rush_att":3,"rec_yds":16,"rec":1.4,
               "rec_tds":0.32,"rec_tgt":2,"rush_rec_yds":20,"pass_rush_yds":50,"any_td":0.4}

def sleeper_season(season, weeks):
    players = get("https://api.sleeper.app/v1/players/nfl")
    proj, act = [], []
    for w in weeks:
        for kind, url_t, sink in (("proj","https://api.sleeper.app/v1/projections/nfl/regular/%d/%d?position=%s", proj),
                                  ("act","https://api.sleeper.app/v1/stats/nfl/regular/%d/%d?position=%s", act)):
            seen = set()
            for pos in POS:
                try:
                    data = get(url_t % (season, w, pos))
                except Exception as e:
                    print(f"  sleeper {kind} {season} w{w} {pos}: {e}", file=sys.stderr); continue
                for pid, st in (data or {}).items():
                    if pid in seen: continue
                    seen.add(pid)
                    p = players.get(pid) or {}
                    name = p.get("full_name")
                    if not name or not isinstance(st, dict): continue
                    # the position= filter is ignored by the feed (returns all
                    # players every call), so take position from the players dump
                    row = {"player": name, "week": w, "pos": p.get("position") or pos, "season": season}
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
            r = {"player": name, "week": w, "pos": pos, "season": season}
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
        if not a: continue  # no row at all: bye or inactive, skip the week
        if not any(a.get(t) is not None for t in STATS): continue  # placeholder row, no played stats
        for s in STATS:
            pv = pr.get(s)
            if pv is None: continue
            av = a.get(s)
            if av is None: av = 0.0  # zero stats are omitted from the feed; played week + missing key = 0
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
        if not any(a.get(t) is not None for t in STATS): continue
        for cs, (c1, c2) in COMBO.items():
            p1, p2 = pr.get(c1), pr.get(c2)
            if p1 is None or p2 is None: continue
            a1 = a.get(c1); a1 = 0.0 if a1 is None else a1
            a2 = a.get(c2); a2 = 0.0 if a2 is None else a2
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
    """Average-of-models estimate error curves, plus the matched-sample
    head-to-head (only player-week-stats where BOTH models projected)."""
    aidx = {}
    for a in sa + ea:
        aidx.setdefault((norm_name(a["player"]), a["week"]), a)
    eidx = {}
    for pr in ep:
        eidx[(norm_name(pr["player"]), pr["week"])] = pr
    serrs = {s: [] for s in STATS}
    cerrs = {s: [] for s in COMBO}
    matched = {s: {"sl": [], "es": [], "av": []} for s in ALL_STATS}
    for pr in sp:
        a = aidx.get((norm_name(pr["player"]), pr["week"]))
        o = eidx.get((norm_name(pr["player"]), pr["week"]))
        if not a or not o: continue
        if not any(a.get(t) is not None for t in STATS): continue
        for s in STATS:
            v1, v2 = pr.get(s), o.get(s)
            if v1 is None or v2 is None: continue
            av = a.get(s)
            if av is None: av = 0.0
            pv = (v1 + v2) / 2
            if pv < 0.5: continue
            serrs[s].append(pv - av)
            matched[s]["sl"].append(v1 - av)
            matched[s]["es"].append(v2 - av)
            matched[s]["av"].append(pv - av)
        for cs, (c1, c2) in COMBO.items():
            v1a, v1b = pr.get(c1), pr.get(c2)
            v2a, v2b = o.get(c1), o.get(c2)
            if None in (v1a, v1b, v2a, v2b): continue
            a1 = a.get(c1); a1 = 0.0 if a1 is None else a1
            a2 = a.get(c2); a2 = 0.0 if a2 is None else a2
            pv = (v1a + v1b) / 2 + (v2a + v2b) / 2
            if pv < 0.5: continue
            cerrs[cs].append(pv - (a1 + a2))
            matched[cs]["sl"].append((v1a + v1b) - (a1 + a2))
            matched[cs]["es"].append((v2a + v2b) - (a1 + a2))
            matched[cs]["av"].append(pv - (a1 + a2))
    errq = {}
    for s, es in list(serrs.items()) + list(cerrs.items()):
        if len(es) < 30: continue
        errq[s] = {"n": len(es), "q": quantiles(es), "ae_q": quantiles([abs(e) for e in es])}
    msum = {}
    for s, d in matched.items():
        n = len(d["av"])
        if n < 30: continue
        ae = {k: [abs(x) for x in v] for k, v in d.items()}
        sl_w = sum(1 for i in range(n) if ae["sl"][i] < ae["es"][i])
        es_w = sum(1 for i in range(n) if ae["es"][i] < ae["sl"][i])
        msum[s] = {"n": n,
                   "sleeper_mae": round(sum(ae["sl"]) / n, 2), "espn_mae": round(sum(ae["es"]) / n, 2),
                   "avg_mae": round(sum(ae["av"]) / n, 2),
                   "sleeper_closer": sl_w, "espn_closer": es_w, "tied": n - sl_w - es_w}
    return errq, msum

# ================= player-level model =================

def build_logs(rows):
    """Per player: position + per-stat ordered series of (seq, value).
    A week counts when the feed returned any played stat for the player
    (placeholder rows are skipped). Inside a played week, a stat the player
    has recorded in THIS week or any EARLIER one but not this week is a 0.
    The prefix-only rule matters: keying the zero-fill off the full season
    would let a week-14 carry decide whether week 3 counts a rushing 0, and
    that pooled series feeds the position baselines. Combo stats zero-fill
    each component the same way. Weeks with no game simply do not appear
    (props are graded on games played)."""
    by_player = {}
    for a in rows:
        k = norm_name(a["player"])
        if not any(a.get(t) is not None for t in STATS): continue
        bp = by_player.setdefault(k, {"pos": None, "weeks": {}})
        if not bp["pos"] and a.get("pos"): bp["pos"] = a["pos"]
        seq = a.get("seq", a["season"] * 100 + a["week"])
        wk = bp["weeks"].setdefault(seq, {})
        for t in STATS:
            if a.get(t) is not None: wk[t] = a[t]
    logs = {}
    for k, bp in by_player.items():
        ever = set()
        stats = {}
        for seq, wk in sorted(bp["weeks"].items()):
            ever |= set(wk)
            for t in STATS:
                if t in ever:
                    stats.setdefault(t, {})[seq] = wk.get(t, 0.0)
            for cs, (c1, c2) in COMBO.items():
                if c1 in ever or c2 in ever:
                    stats.setdefault(cs, {})[seq] = wk.get(c1, 0.0) + wk.get(c2, 0.0)
        logs[k] = {"pos": bp["pos"] or "UNK",
                   "stats": {t: sorted(d.items()) for t, d in stats.items()},
                   "weeks": sorted(bp["weeks"].items())}  # raw, for honest walk-forward
    return logs

def wstats(vals):
    """Recency-weighted mean and sd; the latest game weighs ~2x a game
    HALF_LIFE games back."""
    n = len(vals)
    if n == 0: return 0.0, 1.0
    w = [0.5 ** ((n - 1 - i) / HALF_LIFE) for i in range(n)]
    wsum = sum(w)
    mean = sum(wi * v for wi, v in zip(w, vals)) / wsum
    var = sum(wi * (v - mean) ** 2 for wi, v in zip(w, vals)) / wsum
    return mean, math.sqrt(var) if var > 0 else 0.0

def p_over(mean, sigma, line):
    """P(X > line) under a normal, clamped to the band the site displays."""
    if sigma <= 0: p = 1.0 if mean > line else 0.0
    else: p = 0.5 * (1 + math.erf((mean - line) / (sigma * math.sqrt(2))))
    return max(P_FLOOR, min(P_CEIL, p))

def p_over_poisson(mean, line):
    """P(X > line) for a Poisson count. X > line means X >= ceil(line) when the
    line is fractional, and X >= line+1 when it is a whole number."""
    lam = max(mean, 1e-6)
    k = math.ceil(line) - 1 if line != int(line) else int(line)
    if k < 0: return P_CEIL
    if lam > 700 or k > 2000: return P_CEIL
    cdf, term = 0.0, math.exp(-lam)
    for i in range(k + 1):
        if i: term *= lam / i
        cdf += term
    return max(P_FLOOR, min(P_CEIL, 1.0 - cdf))

def p_hit(stat, mean, sigma, line):
    """The one place the over-probability is defined. index.html mirrors this;
    payload.player_model.dist tells the frontend which branch a stat takes."""
    if stat in COUNT_STATS: return p_over_poisson(mean, line)
    return p_over(mean, sigma, line)

class Baselines:
    """Pooled per-(pos, stat) distribution with prefix sums so a walk-forward
    query ('everything before seq X') is fast."""
    def __init__(self, logs):
        agg = {}
        for lg in logs.values():
            pos = lg["pos"]
            for t, series in lg["stats"].items():
                for seq, v in series:
                    for key in ((pos, t), ("ALL", t)):
                        a = agg.setdefault(key, {}).setdefault(seq, [0, 0.0, 0.0])
                        a[0] += 1; a[1] += v; a[2] += v * v
        self.pre = {}
        for key, seqs in agg.items():
            items = sorted(seqs.items())
            ks = [s for s, _ in items]
            c = [(0, 0.0, 0.0)]
            for _, (n, s, q) in items:
                pn, ps, pq = c[-1]
                c.append((pn + n, ps + s, pq + q))
            self.pre[key] = (ks, c)

    def query(self, pos, stat, before_seq=None):
        for key in ((pos, stat), ("ALL", stat)):
            if key not in self.pre: continue
            ks, c = self.pre[key]
            i = len(ks) if before_seq is None else bisect_left(ks, before_seq)
            n, s, q = c[i]
            if n < 20: continue
            m = s / n
            var = max(q / n - m * m, 1e-6)
            return n, m, math.sqrt(var)
        return None

def blend(vals, base, stat):
    """Shrink the player's own weighted mean/sd toward the position baseline.
    His own games take over as they pile up (weight n/(n+SHRINK_K))."""
    if base is None and not vals: return None
    if not vals: return base[1], max(base[2], SIGMA_FLOOR.get(stat, 1)), 0
    mp, sp = wstats(vals)
    n = len(vals)
    if base is None:
        return mp, max(sp, SIGMA_FLOOR.get(stat, 1)), n
    w = n / (n + SHRINK_K)
    m = w * mp + (1 - w) * base[1]
    s = math.sqrt(w * sp * sp + (1 - w) * base[2] * base[2])
    return m, max(s, SIGMA_FLOOR.get(stat, 1)), n

def isotonic_xy(pairs):
    """Pool adjacent violators; returns block (x, y) lists."""
    pairs = sorted(pairs)
    bx, by, bw = [], [], []
    for x, y in pairs:
        bx.append(x); by.append(y); bw.append(1)
        while len(by) >= 2 and by[-2] >= by[-1]:
            w = bw[-2] + bw[-1]
            y = (by[-2] * bw[-2] + by[-1] * bw[-1]) / w
            x = (bx[-2] * bw[-2] + bx[-1] * bw[-1]) / w
            bx[-2:] = [x]; by[-2:] = [y]; bw[-2:] = [w]
    return bx, by

def interp(x, y, p):
    if p <= x[0]: return y[0]
    if p >= x[-1]: return y[-1]
    lo, hi = 0, len(x) - 1
    while hi - lo > 1:
        mid = (lo + hi) >> 1
        if x[mid] <= p: lo = mid
        else: hi = mid
    span = x[hi] - x[lo] or 1
    return y[lo] + (p - x[lo]) / span * (y[hi] - y[lo])

def fit_calibration(pairs, min_pairs=None):
    """pairs: (raw_p, outcome 0/1). Returns fitted knots and in-fit diagnostics.
    Honest held-out diagnostics are computed separately by player_model().

    Two guardrails beyond the +/-0.20 swing clamp:
      * a knot's swing is scaled by how many training pairs actually sit near it
        (full swing at CALIB_KNOT_N, proportionally less below), so a knot backed
        by a handful of points cannot move the probability the full 20 points;
      * the result is forced monotone after shrinking.
    Without the first guardrail the 2025 fit put cal(0.75)=0.95 on four passing
    stats off ~15 points per knot - locks invented from noise."""
    if len(pairs) < (MIN_PAIRS if min_pairs is None else min_pairs): return None
    bx, by = isotonic_xy(pairs)
    knots_x = [P_FLOOR] + [i / 20 for i in range(1, 20)] + [P_CEIL]
    knots_y = []
    for kx in knots_x:
        iso = interp(bx, by, kx)
        swing = max(-CALIB_CLAMP, min(CALIB_CLAMP, iso - kx))
        near = sum(1 for p, _ in pairs if abs(p - kx) <= 0.05)
        swing *= min(1.0, near / CALIB_KNOT_N)
        knots_y.append(round(max(P_FLOOR, min(P_CEIL, kx + swing)), 3))
    for i in range(1, len(knots_y)):
        if knots_y[i] < knots_y[i - 1]: knots_y[i] = knots_y[i - 1]
    def cal(p): return interp(knots_x, knots_y, p)
    brier = sum((cal(p) - o) ** 2 for p, o in pairs) / len(pairs)
    ll = sum(-(o * math.log(max(cal(p), 1e-9)) + (1 - o) * math.log(max(1 - cal(p), 1e-9)))
             for p, o in pairs) / len(pairs)
    return {"x": [round(v, 3) for v in knots_x], "y": knots_y,
            "brier": round(brier, 4), "logloss": round(ll, 4)}

def auc(pairs):
    """Rank discrimination: P(a random over-hit scored higher than a random miss).
    0.50 is a coin flip. This is the number the pair-count gate protects - a
    Brier that only looks good because the base rate was learned is not skill."""
    ps = sorted(range(len(pairs)), key=lambda i: pairs[i][0])
    ranks = [0.0] * len(pairs); i = 0
    while i < len(ps):
        j = i
        while j + 1 < len(ps) and pairs[ps[j + 1]][0] == pairs[ps[i]][0]: j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1): ranks[ps[k]] = r
        i = j + 1
    n1 = sum(1 for _, o in pairs if o == 1); n0 = len(pairs) - n1
    if not n1 or not n0: return None
    rsum = sum(ranks[i] for i, (_, o) in enumerate(pairs) if o == 1)
    return round((rsum - n1 * (n1 + 1) / 2) / (n1 * n0), 4)

def brier_const(train, test):
    """The only honest null for a market this lopsided: predict the training
    period's base rate for everything. Beating a constant 0.5, or beating the
    position-baseline model, is not evidence the player curves know anything."""
    if not train or not test: return None
    b = sum(o for _, o in train) / len(train)
    return round(sum((b - o) ** 2 for _, o in test) / len(test), 4), round(b, 4)

def player_model(logs, sp):
    """Walk-forward grade on 2025 (line = Sleeper projection as the market
    proxy) + live curves from each player's full log."""
    bl = Baselines(logs)
    sp_idx = {}
    for pr in sp:
        if pr.get("season") != 2025: continue
        sp_idx[(norm_name(pr["player"]), 202500 + pr["week"])] = pr
    pairs = {}   # stat -> [(raw_p, outcome)]
    pairs_lg = {}
    for k, lg in logs.items():
        pos = lg["pos"]
        weeks = lg["weeks"]  # sorted [(seq, raw)] ; ever_pre uses strictly earlier weeks
        ever_pre = set()     # so no future game decides what counts as a 0
        for wi, (seq, raw) in enumerate(weeks):
            if 202500 <= seq < 202600 and wi >= MIN_GAMES and ever_pre:
                pr = sp_idx.get((k, seq))
                if pr:
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
                        if line < 0.5: continue
                        if val == line: continue  # push, drop from calibration
                        out = 1 if val > line else 0
                        base = bl.query(pos, t, before_seq=seq)
                        m, s, _ = blend(prior, base, t)
                        pairs.setdefault(t, []).append((seq, p_hit(t, m, s, line), out))
                        if base is not None:
                            pairs_lg.setdefault(t, []).append((seq, p_hit(t, base[1], max(base[2], SIGMA_FLOOR.get(t, 1)), line), out))
            ever_pre |= set(raw)
    grade = {}
    all_pairs, all_lg = [], []
    published = []
    for t in ALL_STATS:
        trip = pairs.get(t) or []
        if len(trip) < 150: continue   # below this there is nothing to report at all
        lgtrip = pairs_lg.get(t) or []
        prs = [(p, o) for _, p, o in trip]
        lg = [(p, o) for _, p, o in lgtrip]
        # A stat is published (curve + calibration shipped to the board) only with
        # enough walk-forward pairs to estimate a 21-knot curve and test it.
        pub = len(trip) >= MIN_PAIRS
        brier_raw = sum((p - o) ** 2 for p, o in prs) / len(prs)
        entry = {"n": len(prs), "brier_raw": round(brier_raw, 4), "published": pub,
                 "dist": "poisson" if t in COUNT_STATS else "normal"}
        if lg:
            entry["brier_league"] = round(sum((p - o) ** 2 for p, o in lg) / len(lg), 4)
        # Honest temporal holdout: fit calibration on W1-12, score it on W13-18.
        train = [(p, o) for seq, p, o in trip if seq <= 202512]
        test = [(p, o) for seq, p, o in trip if seq >= 202513]
        lgtest = [(p, o) for seq, p, o in lgtrip if seq >= 202513]
        hfit = fit_calibration(train, min_pairs=150)
        if hfit and len(test) >= 30:
            cp = [interp(hfit["x"], hfit["y"], p) for p, _ in test]
            entry.update({"holdout_n": len(test),
                          "holdout_brier_raw": round(sum((p-o)**2 for p,o in test)/len(test), 4),
                          "holdout_brier": round(sum((p-o)**2 for p,(_,o) in zip(cp,test))/len(test), 4),
                          "holdout_logloss": round(sum(-(o*math.log(max(p,1e-9))+(1-o)*math.log(max(1-p,1e-9))) for p,(_,o) in zip(cp,test))/len(test), 4),
                          "holdout_auc": auc(test)})
            bc = brier_const(train, test)
            if bc:
                entry["holdout_brier_base_rate"], entry["train_over_rate"] = bc
                entry["holdout_skill_vs_base_rate"] = round((1 - entry["holdout_brier"] / bc[0]) * 100, 2)
            if lgtest:
                entry["holdout_brier_league"] = round(sum((p-o)**2 for p,o in lgtest)/len(lgtest), 4)
        fit = fit_calibration(prs) if pub else None   # full-season curve, production calibrator
        if fit:
            entry.update({"calib_x": fit["x"], "calib_y": fit["y"]})
        grade[t] = entry
        if pub: published.append(t)
        all_pairs += trip; all_lg += lgtrip
    if all_pairs:
        prs = [(p, y) for _, p, y in all_pairs]
        lgs = [(p, y) for _, p, y in all_lg]
        o = {"n": len(prs), "brier_raw": round(sum((p-y)**2 for p,y in prs)/len(prs), 4)}
        if lgs: o["brier_league"] = round(sum((p-y)**2 for p,y in lgs)/len(lgs), 4)
        train = [(p,y) for seq,p,y in all_pairs if seq <= 202512]
        test = [(p,y) for seq,p,y in all_pairs if seq >= 202513]
        lgtest = [(p,y) for seq,p,y in all_lg if seq >= 202513]
        hfit = fit_calibration(train)
        if hfit and test:
            cp = [interp(hfit["x"], hfit["y"], p) for p,_ in test]
            o.update({"holdout_n":len(test),
                      "holdout_brier_raw":round(sum((p-y)**2 for p,y in test)/len(test),4),
                      "holdout_brier":round(sum((p-y)**2 for p,(_,y) in zip(cp,test))/len(test),4),
                      "holdout_logloss":round(sum(-(y*math.log(max(p,1e-9))+(1-y)*math.log(max(1-p,1e-9))) for p,(_,y) in zip(cp,test))/len(test),4)})
            if lgtest: o["holdout_brier_league"] = round(sum((p-y)**2 for p,y in lgtest)/len(lgtest),4)
            o["holdout_auc"] = auc(test)
            bc = brier_const(train, test)
            if bc:
                o["holdout_brier_base_rate"], o["train_over_rate"] = bc
                o["holdout_skill_vs_base_rate"] = round((1 - o["holdout_brier"] / bc[0]) * 100, 2)
        grade["_all"] = o
    # live curves from full logs
    curves = {}
    bases = {}
    for t in ALL_STATS:
        if t not in published: continue   # unvalidated stat: ship no curve, board shows "-"
        cstat = {}
        for k, lg in logs.items():
            series = lg["stats"].get(t)
            if not series or len(series) < MIN_GAMES: continue
            base = bl.query(lg["pos"], t)
            m, s, n = blend([v for _, v in series], base, t)
            cstat[k] = [n, round(m, 2), round(s, 2)]
        if cstat: curves[t] = cstat
        bstat = {}
        for pos in POS + ["ALL"]:
            b = bl.query(pos, t)
            if b: bstat[pos] = [b[0], round(b[1], 2), round(b[2], 2)]
        if bstat: bases[t] = bstat
    return grade, curves, bases, published

# 2026 week math (mirrors collect.py): feeds label the Sep 9-14 slate week 2.
def finished_feed_weeks_2026():
    now = datetime.now(timezone.utc)
    opener = datetime(2026, 9, 9, tzinfo=timezone.utc)
    if now < opener: return []
    site_w = min(18, (now - opener).days // 7 + 1)
    return list(range(2, site_w + 1))  # finished site weeks 1..W-1 => feed weeks 2..W

def main():
    os.makedirs("out", exist_ok=True); os.makedirs("cache", exist_ok=True)
    result = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seasons": {}}
    print("== sleeper 2025 ==", file=sys.stderr)
    sp, sa = cached("sleeper_2025.json", lambda: sleeper_season(2025, range(1, 19)))
    print("== espn 2025 ==", file=sys.stderr)
    ep, ea = cached("espn_2025.json", lambda: espn_season(2025))
    g, wk, eq = grade(sp, sa)
    result["seasons"]["2025_sleeper"] = {"by_stat": g, "by_week_mae": wk,
        "note": "Sleeper weekly projections vs Sleeper actuals, 2025 regular season, players projected for a real role."}
    g2, wk2, eq2 = grade(ep, ea)
    result["seasons"]["2025_espn"] = {"by_stat": g2, "by_week_mae": wk2,
        "note": "ESPN weekly projections vs ESPN actuals, 2025 regular season, players projected for a real role."}
    eqa, matched = grade_avg(sp, sa, ep, ea)
    result["errors"] = {"sleeper": eq, "espn": eq2, "avg": eqa,
        "note": "Signed error (projection minus actual) percentiles per stat, 0-100 in steps of 1, 2025 season. ae_q is the absolute miss. avg pairs weeks where both models projected the player."}
    result["errors_matched"] = {"stats": matched,
        "note": "Head-to-head on the matched sample only: player-week-stats where BOTH Sleeper and ESPN published a projection, graded against the same actuals. sleeper_closer/espn_closer count how often each model's miss was smaller."}
    # 2026 actuals for live curves (finished weeks only)
    fw = finished_feed_weeks_2026()
    sa26 = []
    if fw:
        print(f"== sleeper 2026 actuals weeks {fw} ==", file=sys.stderr)
        try:
            _, sa26 = sleeper_season(2026, fw)
        except Exception as e:
            print(f"  sleeper 2026 actuals failed: {e}", file=sys.stderr)
    logs = build_logs(sa + sa26)
    print(f"== player model: {len(logs)} players ==", file=sys.stderr)
    pgrade, curves, bases, published = player_model(logs, sp)
    result["player_curves"] = curves
    result["player_baselines"] = bases
    result["player_model_grade"] = pgrade
    result["player_model_published"] = published
    result["player_model"] = {
        "method": ("A player's over/under chance comes from his own game log: the recency-weighted "
                   "average and spread of his weekly numbers (a game five weeks back counts about half "
                   "of last week), blended toward his position's league average when he has played few "
                   "games - his own games take over as they pile up. Under 3 recorded games: no number, "
                   "shown as -. The raw chance is then corrected by a per-stat calibration fitted on "
                   "2025 results, with the correction capped at 20 points and further shrunk where a "
                   "knot has little data behind it. A stat is only published when its 2025 walk-forward "
                   "sample is big enough to fit and test that curve; the rest show - on the board."),
        "half_life_games": HALF_LIFE, "shrink_prior_games": SHRINK_K, "min_games": MIN_GAMES,
        "calib_clamp": CALIB_CLAMP, "calib_knot_n": CALIB_KNOT_N, "min_pairs": MIN_PAIRS,
        "p_floor": P_FLOOR, "p_ceil": P_CEIL,
        "published_stats": published,
        "dist": {t: ("poisson" if t in COUNT_STATS else "normal") for t in ALL_STATS},
        "backtest_line_source": "Sleeper weekly projection used as the market-line proxy (no verified 2025 sportsbook line archive found yet).",
        "line_proxy_caveat": ("The calibration is fitted against Sleeper projections, not sportsbook lines, "
                              "and the two are not the same market. Against Sleeper's 2025 rushing-yards "
                              "projections the over hit only 32% of the time, so the fitted curve maps a "
                              "raw 50% to about 33%. A real sportsbook line is priced near 50/50, so that "
                              "shift is a property of Sleeper's bias, not of the player. Treat every "
                              "published probability as calibrated to the proxy until the 2026 live record "
                              "(live_grades) is deep enough to refit against real market lines."),
        "grade_protocol": ("Walk-forward player inputs (only earlier games, and the zero-fill for a week "
                           "uses only that week and earlier ones); calibration temporal holdout fits W1-12 "
                           "and scores W13-18. Full-season calibration knots are for 2026 production. "
                           "holdout_brier_base_rate is the null that matters: a constant equal to the "
                           "training period's over-rate. holdout_auc under 0.50 means no rank skill at all."),
        "usage": ("hit% for a prop = calib_interp(p_hit(stat, m, s, line)) where [n, m, s] = "
                  "player_curves[stat][norm_name(player)] and p_hit is the normal CDF, or a Poisson CDF "
                  "for stats listed as poisson in dist, clamped to 0.02-0.98. "
                  "norm_name: NFKD-normalize, drop every non-ASCII character, lowercase, remove . ' and "
                  "hyphens, drop suffixes jr/sr/ii/iii/iv/v, collapse spaces. "
                  "A stat absent from player_curves is not published - show - and say the model is not "
                  "validated for it. Player absent from a published stat: show - (under 3 games).")}
    result["note_2026"] = ("The 2026 season opens Sep 9, so no 2026 weeks are graded here. "
        "This season's record builds week by week in the live-grades table as games finish.")
    with open("out/backtest.json", "w") as f:
        json.dump(result, f)
    print("wrote out/backtest.json", file=sys.stderr)
    sz = os.path.getsize("out/backtest.json")
    print(f"payload bytes: {sz}", file=sys.stderr)

if __name__ == "__main__":
    main()
