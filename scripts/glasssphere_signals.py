"""The Buy / Sell signals of index.html kept as a live log with their outcome, for the scoreboard on the page and the
signal alerts.

Each closed hour of each coin: the 1-day reading (the calibrated chance, glasssphere_model.chance) from the data as it
was at the close of that hour, the same calculation as the page's signals(). A Buy starts at 80% or more for "eerst
omhoog", a Sell at 80% or more for "eerst omlaag"; it runs until the other one starts. Each signal is scored as a trade
from the close of its hour: take profit at the more likely target, stop at the other one, 24 hours:
  doel  the take profit was reached first
  stop  the stop was reached first (or both in the same candle)
  geen  neither within 24 hours: the result is the move to the close 24 hours later
(15-minute candles where the file has them, 1-hour candles before that.)

Writes DIR/glasssphere-signals.json:
  ts     when written
  since  when the live log began (the first run); the signals before it were worked out afterwards from the hours
         in the data files of that first run
  coins  {coin: {cur: 'buy' / 'sell' / null, last: close ms of the last hour worked out,
                 list: [{t, side, pct, entry, tp, sl, out?, res?, tOut?, sent?, sentOut?}, ...]}}   (last 100)
  t is the close of the signal's hour (ms), res the result as a fraction of the entry price, without costs; sent / sentOut
  are set by glasssphere_notify.py once the signal / its outcome was sent as an alert.
Without a previous file (the first run, or a new coin) the hours of the data file are worked out from the start, and
those signals count as sent, so the first run sends nothing.

    python3 scripts/glasssphere_signals.py DIR [--prev URL]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import glasssphere_model as M       # noqa: E402
import glasssphere_snapshot as SN   # noqa: E402

DAYS, MIN, KEEP, H = 1, 80, 100, 3600000
FROM = 48   # first bar worked out without a previous file, as on the page


def chances(candles, oi, taker, todo):
    """{close ms: calibrated P(up first)} for the bars whose index is in todo (closed hours)."""
    book = M.LiqBook('taker')
    out = {}
    for k, c in enumerate(candles):
        book.step(k, c, oi.get(c[0]), oi.get(c[0] + H), taker.get(c[0]))
        if k not in todo:
            continue
        win, P = candles[:k + 1], c[4]
        pl = M.pools(M.estimate_stops(win, P), [(q[0], q[1], q[2]) for q in book.pos], P, rng=0.15)
        up, dn, p = M.first_targets(pl, P, M.r24_of(win), days=DAYS)
        out[c[0] + H] = (M.chance(p, M.flow_of(win, taker), DAYS), up and up[0], dn and dn[0], P)
    return out


def score(sig, c1, c15, now):
    """Outcome of an open signal from the candles after its close, or None while it is still running."""
    end = sig['t'] + DAYS * 24 * H
    bars = [c for c in c15 if sig['t'] <= c[0] < end] if c15 and c15[0][0] <= sig['t'] else \
        [c for c in c1 if sig['t'] <= c[0] < end]
    buy = sig['side'] == 'buy'
    for c in bars:
        hit_tp = c[2] >= sig['tp'] if buy else c[3] <= sig['tp']
        hit_sl = c[3] <= sig['sl'] if buy else c[2] >= sig['sl']
        if hit_sl:
            return 'stop', sig['sl'], c[0]
        if hit_tp:
            return 'doel', sig['tp'], c[0]
    if now >= end and bars:
        return 'geen', bars[-1][4], end
    return None


def update(prev, snap, now):
    candles, c15 = snap['candles'], snap.get('candles15') or []
    oi, taker = {t: v for t, v in snap['oi']}, {t: v for t, v in snap['taker']}
    closed = [k for k, c in enumerate(candles) if c[0] + H <= now]
    st = {'cur': None, 'last': 0, 'list': []} if not prev else dict(prev, list=[dict(s) for s in prev['list']])
    fresh = not prev
    todo = {k for k in closed if k >= FROM and candles[k][0] + H > st['last']}
    for t, (p, up, dn, P) in sorted(chances(candles, oi, taker, todo).items()):
        if p is not None:
            pct = {'buy': round(p * 100), 'sell': round((1 - p) * 100)}
            for side in ('buy', 'sell'):
                if side != st['cur'] and pct[side] >= MIN:
                    st['cur'] = side
                    tp, sl = (up, dn) if side == 'buy' else (dn, up)
                    st['list'].append({'t': t, 'side': side, 'pct': pct[side], 'entry': P, 'tp': tp, 'sl': sl,
                                       **({'sent': 1, 'sentOut': 1} if fresh else {})})
                    break
        st['last'] = t
    for s in st['list']:
        if 'out' not in s:
            r = score(s, candles, c15, now)
            if r:
                s['out'], px, s['tOut'] = r
                s['res'] = round((px / s['entry'] - 1) * (1 if s['side'] == 'buy' else -1), 5)
                if fresh:
                    s['sentOut'] = 1
    st['list'] = st['list'][-KEEP:]
    return st


def main():
    out_dir = sys.argv[1]
    prev, now = {}, int(time.time() * 1000)
    since = now
    if '--prev' in sys.argv:
        try:
            p = SN.get(sys.argv[sys.argv.index('--prev') + 1])
            prev, since = p.get('coins', {}), p.get('since', now)
        except Exception as e:
            print(f'no previous signals: {e}')
    coins = {}
    for key, A in SN.ASSETS.items():
        try:
            snap = json.load(open(os.path.join(out_dir, A['file'])))
            coins[key] = update(prev.get(key), snap, now)
        except Exception as e:   # keep what there was for a coin whose file is missing or broken
            print(f'{key:5s} skipped: {e}')
            if key in prev:
                coins[key] = prev[key]
            continue
        s = coins[key]
        done = [x for x in s['list'] if 'out' in x]
        print(f'{key:5s} now {s["cur"] or "-":4s} {len(s["list"])} signals, ' +
              ', '.join(f'{o} {sum(x["out"] == o for x in done)}' for o in ('doel', 'stop', 'geen')))
    path = os.path.join(out_dir, 'glasssphere-signals.json')
    json.dump({'ts': now, 'since': since, 'coins': coins}, open(path, 'w'), separators=(',', ':'))
    print(f'wrote {path} ({os.path.getsize(path) // 1024} KB)')


if __name__ == '__main__':
    main()
