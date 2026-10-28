"""Does anything beyond the distances tell the direction? The 1-day reading on the Binance archive, 2022 (or the
listing) - Sep 2026, every 6 hours, with what was known at that hour:

  weight   size of the target pool above vs below (log ratio)
  flow     net taker buying over 24h as a share of volume
  oi       open interest change over 24h, signed by the 24h price move (rising OI with the move = conviction)
  funding  the last funding rate (8h)
  trend    price vs its 7-day average
  vol      targets from the average daily range of the last 14 (the page), 7 or 3 days

and what followed: which target was reached first within the horizon, and which of a band of ±0.5 x the expected range
(a test of direction alone, without the pools).

    python3 scripts/glasssphere_edge_test.py [cache_dir] --symbol BTCUSDT [--days 1|3|7] --json OUT   # one coin
    python3 scripts/glasssphere_edge_test.py --analyse OUT1 OUT2 ...                      # the tables
Fitted on 2022 - 2024, judged on 2025 - Sep 2026.
"""
import bisect
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DAYS = int(sys.argv[sys.argv.index('--days') + 1]) if '--days' in sys.argv else 1     # the reading's horizon
STEP, LO = 6, 0.3
VOLS = (14, 7, 3)                 # days of daily ranges behind the expected range
SPLIT = 1735689600                # 2025-01-01: fit before, judge after
FEES = (0.001, 0.0004)            # round trip: taker in and out, maker in and out


def r_of(candles, blocks):
    rngs, e = [], len(candles)
    while e >= 24 and len(rngs) < blocks:
        w = candles[e - 24:e]
        rngs.append(max(x[2] for x in w) - min(x[3] for x in w))
        e -= 24
    return sum(rngs) / len(rngs)


def first_touch(path, up, dn):
    for x in path:
        if x[2] >= up or x[3] <= dn:
            return 'both' if x[2] >= up and x[3] <= dn else 'up' if x[2] >= up else 'down'
    return None


def points():
    import glasssphere_backtest_long as L
    import glasssphere_model as M
    candles, stats, funding = L.load()
    ft = [f[0] for f in funding]
    book = M.LiqBook('taker')
    out, n, H = [], len(candles), 24 * DAYS
    for k, c in enumerate(candles):
        t = c[0] // 1000
        a, b = stats.get(str(t)), stats.get(str(t + 3600))
        book.step(k, c, a and a['oi'], b and b['oi'], a['taker'] / (1 + a['taker']) if a else None)
        if k < M.WINDOW or k % STEP or k + H >= n:
            continue
        win = candles[k - M.WINDOW + 1:k + 1]
        P = c[4]
        pl = M.pools(M.estimate_stops(win, P), [(q[0], q[1], q[2]) for q in book.pos], P, rng=0.15)
        path, end = candles[k + 1:k + 1 + H], candles[k + H][4] / P - 1
        # what was known at the close of hour k
        day = candles[k - 23:k + 1]
        vt = sum(x[5] for x in day)
        sh = lambda x: (lambda s: s['taker'] / (1 + s['taker']) if s else 0.5)(stats.get(str(x[0] // 1000)))
        flow = sum(x[5] * (2 * sh(x) - 1) for x in day) / vt if vt else 0
        o0, o1 = stats.get(str(candles[k - 24][0] // 1000 + 3600)), stats.get(str(t + 3600))
        ret24 = P / candles[k - 24][4] - 1
        oi = o1['oi'] / o0['oi'] - 1 if o0 and o1 else None
        i = bisect.bisect_right(ft, t + 3600) - 1
        fund = funding[i][1] if i >= 0 and t + 3600 - funding[i][0] < 86400 else None
        trend = P / (sum(x[4] for x in candles[k - 167:k + 1]) / 168) - 1
        pt = {'t': t, 'flow': round(flow, 4), 'oi': None if oi is None else round(oi, 4), 'ret24': round(ret24, 4),
              'fund': fund, 'trend': round(trend, 4), 'end': round(end, 5)}
        for v in VOLS:
            rh = r_of(win, v) * DAYS ** 0.5
            ups = [q for q in pl if P + LO * rh < q[0] <= P + rh]
            dns = [q for q in pl if P - rh <= q[0] < P - LO * rh]
            band = first_touch(path, P + 0.5 * rh, P - 0.5 * rh)
            row = {'band': band, 'bd': round(0.5 * rh / P, 5)}
            if ups and dns:
                tu, td = max(ups, key=lambda q: q[1]), max(dns, key=lambda q: q[1])
                row.update({'u': round(tu[0] / P - 1, 5), 'd': round(1 - td[0] / P, 5), 'wu': round(tu[1], 3),
                            'wd': round(td[1], 3), 'first': first_touch(path, tu[0], td[0])})
            pt[str(v)] = row
        out.append(pt)
    return L.SYM, out


# ---------- analysis ----------

def pnl_targets(r, side, fee):
    """Trade toward the upper (side 1) or lower (-1) target, stop at the other one, closed at the horizon if neither."""
    v, f = r['v'], r['v']['first']
    tgt, stp = (v['u'], v['d']) if side > 0 else (v['d'], v['u'])
    if f is None:
        g = r['end'] * side
    elif f == 'both':
        g = -stp
    else:
        g = tgt if (f == 'up') == (side > 0) else -stp
    return g - fee


def pnl_band(r, side, fee):
    v, f = r['v'], r['v']['band']
    if f is None:
        g = r['end'] * side
    elif f == 'both':
        g = -v['bd']
    else:
        g = v['bd'] if (f == 'up') == (side > 0) else -v['bd']
    return g - fee


def feats(r):
    v = r['v']
    oi = 0.0 if r['oi'] is None else math.copysign(1, r['ret24'] or 1e-9) * math.tanh(r['oi'] / 0.03)
    fund = 0.0 if r['fund'] is None else math.tanh((r['fund'] - 0.0001) / 0.0003)
    w = math.log(max(v['wu'], 1e-3) / max(v['wd'], 1e-3)) if 'wu' in v else 0.0
    return {'weight': w, 'flow': math.tanh(r['flow'] / 0.1), 'oi': oi, 'funding': fund, 'trend': math.tanh(r['trend'] / 0.05)}


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def fit(rows, names, l2=1.0, iters=300):
    """Logistic regression of 'upper target first' on the features, with the distance chance as a fixed offset:
    a weight of 0 means the feature adds nothing to the distances."""
    w = [0.0] * (len(names) + 1)
    data = [(logit(r['p']), [1.0] + [r['f'][k] for k in names], 1.0 if r['v']['first'] == 'up' else 0.0) for r in rows]
    for _ in range(iters):                     # Newton steps
        g = [0.0] * len(w)
        Hs = [[0.0] * len(w) for _ in w]
        for off, x, y in data:
            z = off + sum(a * b for a, b in zip(w, x))
            p = 1 / (1 + math.exp(-z))
            for i in range(len(w)):
                g[i] += (y - p) * x[i]
                for j in range(len(w)):
                    Hs[i][j] += p * (1 - p) * x[i] * x[j]
        for i in range(len(w)):
            g[i] -= l2 * w[i]
            Hs[i][i] += l2
        step = solve(Hs, g)
        w = [a + b for a, b in zip(w, step)]
        if max(abs(s) for s in step) < 1e-7:
            break
    return w


def solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(M[r][i]))
        M[i], M[piv] = M[piv], M[i]
        for r in range(n):
            if r != i and M[i][i]:
                f = M[r][i] / M[i][i]
                M[r] = [a - f * c for a, c in zip(M[r], M[i])]
    return [M[i][n] / M[i][i] if M[i][i] else 0.0 for i in range(n)]


def p_adj(r, w, names):
    z = logit(r['p']) + w[0] + sum(a * r['f'][k] for a, k in zip(w[1:], names))
    return 1 / (1 + math.exp(-z))


def logloss(rows, pf):
    s = 0.0
    for r in rows:
        p = min(max(pf(r), 1e-4), 1 - 1e-4)
        s -= math.log(p) if r['v']['first'] == 'up' else math.log(1 - p)
    return s / len(rows)


def summary(trades):
    if not trades:
        return '    -'
    m = sum(trades) / len(trades)
    sd = (sum((x - m) ** 2 for x in trades) / len(trades)) ** 0.5
    # one independent trade per horizon per coin, about: 4 a day when every 6 hours
    t = m / sd * (len(trades) * STEP / (24 * DAYS)) ** 0.5 if sd else 0
    return f'n {len(trades):6d}  {m:+.3%}  win {sum(x > 0 for x in trades) / len(trades):4.0%}  t {t:+5.1f}'


def analyse(files):
    pts = []
    for fn in files:
        sym, rows = json.load(open(fn))
        for r in rows:
            r['sym'] = sym
            pts.append(r)
    print(f'{len(pts)} points, {len(files)} coins, horizon {DAYS} day\n')
    res = {}
    for v in map(str, VOLS):
        rows = []
        for r in pts:
            x = dict(r, v=r[v])
            if 'u' in x['v']:
                x['p'] = x['v']['d'] / (x['v']['u'] + x['v']['d'])     # (P - tDn) / (tUp - tDn)
                x['f'] = feats(x)
                rows.append(x)
        ins = [r for r in rows if r['t'] < SPLIT]
        oos = [r for r in rows if r['t'] >= SPLIT]
        hit = lambda rs: [r for r in rs if r['v']['first'] in ('up', 'down')]
        print(f'== expected range from {v} days ==')
        # 5: does the window change the calibration or the result of the plain reading?
        for lo in (60, 70):
            sel = [r for r in rows if max(r['p'], 1 - r['p']) * 100 >= lo]
            h = hit(sel)
            right = sum((r['v']['first'] == 'up') == (r['p'] >= 0.5) for r in h) / max(len(h), 1)
            shown = sum(max(r['p'], 1 - r['p']) for r in sel) / max(len(sel), 1)
            for fee in FEES:
                tr = [pnl_targets(r, 1 if r['p'] >= 0.5 else -1, fee) for r in sel]
                print(f'  reading >= {lo}%  shown {shown:.0%}  came true {right:.0%}  fee {fee:.2%}  {summary(tr)}')
        names = ['weight', 'flow', 'oi', 'funding', 'trend']
        h_in, h_out = hit(ins), hit(oos)
        base_ll = logloss(h_out, lambda r: r['p'])
        print(f'  out of sample log loss, distance only: {base_ll:.4f}')
        fits = {}
        for nm in [[k] for k in names] + [names]:
            w = fit(h_in, nm)
            ll = logloss(h_out, lambda r, w=w, nm=nm: p_adj(r, w, nm))
            fits['+'.join(nm)] = (w, nm)
            print(f'  {"+".join(nm):28s} weights {" ".join(f"{a:+.2f}" for a in w)}  log loss {ll:.4f} ({ll - base_ll:+.4f})')
        # trades out of sample: toward the target the features favour, only when the adjusted chance beats the distance
        # chance by a margin (that is where the trade has an edge: with the stop at the other target it breaks even at p)
        for key in ['weight', 'flow', 'oi', 'funding', 'trend', '+'.join(names)]:
            w, nm = fits[key]
            for m in (0.05, 0.10):
                for fee in FEES:
                    tr = []
                    for r in oos:
                        q = p_adj(r, w, nm)
                        if q - r['p'] >= m:
                            tr.append(pnl_targets(r, 1, fee))
                        elif r['p'] - q >= m:
                            tr.append(pnl_targets(r, -1, fee))
                    print(f'  trade {key:28s} edge >= {m:.0%}  fee {fee:.2%}  {summary(tr)}')
        # 2, 3, 4 as simple rules on the band (direction alone): with or against
        if v == '14':
            print('  band ±0.5 x range, out of sample (2025-2026), each rule alone')
            rules = {
                'flow: with net buying': lambda r: math.copysign(1, r['flow']),
                'oi: rising OI with the move': lambda r: (math.copysign(1, r['ret24']) if r['oi'] is not None and r['oi'] > 0.01 else 0),
                'funding: against crowded side': lambda r: (-1 if r['fund'] is not None and r['fund'] > 0.0003 else 1 if r['fund'] is not None and r['fund'] < 0 else 0),
                'trend: with the 7-day trend': lambda r: math.copysign(1, r['trend']),
                'trend: against the 7-day trend': lambda r: -math.copysign(1, r['trend']),
                'reading + trend agree': lambda r: (1 if r['p'] >= 0.7 and r['trend'] > 0 else -1 if r['p'] <= 0.3 and r['trend'] < 0 else 0),
            }
            for nm, f in rules.items():
                for fee in FEES:
                    tr = [pnl_band(r, f(r), fee) for r in oos if f(r)]
                    print(f'    {nm:32s} fee {fee:.2%}  {summary(tr)}')
            # the reading at 70% with the trend filter, on the targets (as the Buy / Sell signals trade)
            for fee in FEES:
                for nm, keep in (('signal >= 70%', lambda r: True), ('signal >= 70% with the trend', lambda r: (r['p'] >= 0.5) == (r['trend'] > 0))):
                    tr = [pnl_targets(r, 1 if r['p'] >= 0.5 else -1, fee) for r in oos if max(r['p'], 1 - r['p']) >= 0.7 and keep(r)]
                    print(f'    {nm:32s} fee {fee:.2%}  {summary(tr)}')
        res[v] = {k: w for k, (w, _) in fits.items()}
        print()
    return res


if __name__ == '__main__':
    if '--analyse' in sys.argv:
        out = analyse([a for a in sys.argv[sys.argv.index('--analyse') + 1:] if not a.startswith('--')])
    else:
        sym, rows = points()
        json.dump([sym, rows], open(sys.argv[sys.argv.index('--json') + 1], 'w'))
        print(sym, len(rows), 'points')
