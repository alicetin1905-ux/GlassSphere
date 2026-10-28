"""Sends the GlassSphere alerts to ntfy, one message per coin with something to report. Runs on every update of the
site (the hourly one and those after a push; GitHub runs the hourly one only every few hours at busy times), and
reports what happened since the last run: each event once.

Usage: glasssphere_notify.py SITE_DIR BASE_URL [--now HH:MM] [--test]
  --test   also send one test message, to check the topic and the phone

What was checked and sent is kept in SITE_DIR/glasssphere-alerts.json, carried forward from BASE_URL:
  {coin: {last: {t, up, dn, lab}, sent: {event key: ms}}}   (glasssphere_alert.check gives the state and the keys)

Besides the check of glasssphere_alert.py, each coin's new Buy / Sell signals and their outcomes from
SITE_DIR/glasssphere-signals.json (glasssphere_signals.py) are sent once: they are marked sent in that file, which is
written back (so run this before the site is uploaded). Signals and outcomes over 12 hours old are marked without
sending; older than 90 minutes they say how long ago.

Settings are in alerts.json at the repo root:
  - "munten": coin -> true (all alerts), "signalen" (only new Buy / Sell signals and their outcome) or false (none);
    a coin left out sends all
  - "stil": quiet hours "van" - "tot" in "tijdzone"; inside them nothing is sent, except liquidation waves when
    "behalve_liquidatiegolf" is true

The topic comes from the NTFY_TOPIC environment variable; without it the messages are only printed (dry run).
Messages go through ntfy's JSON publishing, so the title, emoji tags, priority, the tap target (the coin's tab) and an
"Open BTC" button all survive in UTF-8.
"""
import datetime as dt
import json
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import glasssphere_alert as AL      # noqa: E402
import glasssphere_snapshot as SN   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ntfy emoji tags, most important kind first; the first kind present sets the leading emoji
KIND_TAG = {'signal': 'rotating_light', 'wave': 'boom', 'score': 'white_check_mark', 'hit': 'dart',
            'switch': 'arrows_counterclockwise', 'near': 'eyes'}
STALE = 12 * 3600000   # signals and outcomes older than this are not sent any more
# how long an event of the check is not sent again (hours); a wave has its own hour in the key
AGAIN = {'near': 6, 'hit': 12, 'switch': 3, 'wave': 48}
VERDICT_TAG = {'up': 'chart_with_upwards_trend', 'down': 'chart_with_downwards_trend', 'neutral': 'left_right_arrow'}


def minutes(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def quiet(cfg, now=None):
    """True inside the quiet hours (a window over midnight, 23:00 - 07:00, works too)."""
    s = cfg.get('stil')
    if not s:
        return False
    if now is None:
        t = dt.datetime.now(ZoneInfo(s.get('tijdzone', 'Europe/Amsterdam')))
        now = t.hour * 60 + t.minute
    a, b = minutes(s['van']), minutes(s['tot'])
    return a <= now < b if a <= b else now >= a or now < b


def message(r, key, base):
    """The ntfy JSON for one coin's check result."""
    name = SN.ASSETS[key]['name']
    kinds = [k for k in KIND_TAG if any(a[0] == k for a in r['alerts'])]
    tags = [KIND_TAG[k] for k in kinds] + ([VERDICT_TAG[r['verdict']]] if r['verdict'] else [])
    url = f'{base.rstrip("/")}/#{key}'
    return {
        'title': f'GlassSphere {name}',
        'message': r['status'] + '\n' + '\n'.join('• ' + a[1] for a in r['alerts']),
        'tags': tags,
        'priority': 4 if kinds and kinds[0] in ('signal', 'wave', 'hit') else 3,
        'click': url,
        'icon': f'{base.rstrip("/")}/icons/coins/{key}.png',
        'actions': [{'action': 'view', 'label': f'Open {name}', 'url': url, 'clear': True}],
    }


def pc(x):
    return f'{x * 100:+.1f}%'.replace('.', ',').replace('-', '−')


def when(ms, cfg):
    tz = ZoneInfo(cfg.get('stil', {}).get('tijdzone', 'Europe/Amsterdam'))
    return dt.datetime.fromtimestamp(ms / 1000, tz).strftime('%d-%m %H:%M')


def ago(ms, now):
    h = (now - ms) / 3600000
    return f' ({round(h)} uur geleden)' if h >= 1.5 else ''


def signal_alerts(coin, cfg, now, send):
    """(kind, text) for the coin's unsent signals and outcomes, marking them sent; stale ones are marked only.
    With send False (quiet hours, coin off) the fresh ones are left for a later run."""
    out = []
    for s in (coin or {}).get('list', []):
        side = s['side'].upper()
        if not s.get('sent'):
            if now - s['t'] > STALE:
                s['sent'] = 1
            elif send:
                out.append(('signal', f'nieuw {side}-signaal {s["pct"]}% (1 dag){ago(s["t"], now)}: doel {AL.fmt(s["tp"])} '
                                      f'({pc(s["tp"] / s["entry"] - 1)}), stop {AL.fmt(s["sl"])} ({pc(s["sl"] / s["entry"] - 1)}), '
                                      f'instap {AL.fmt(s["entry"])}', None))
                s['sent'] = 1
        if 'out' in s and not s.get('sentOut'):
            if now - s['tOut'] > STALE:
                s['sentOut'] = 1
            elif send:
                what = {'doel': 'doel geraakt', 'stop': 'stop geraakt', 'geen': 'geen doel binnen 24 uur'}[s['out']]
                out.append(('score', f'{side} van {when(s["t"], cfg)}: {what}, {pc(s["res"])}', None))
                s['sentOut'] = 1
    return out


def main():
    site, base = sys.argv[1], sys.argv[2]
    now = minutes(sys.argv[sys.argv.index('--now') + 1]) if '--now' in sys.argv else None
    cfg = json.load(open(os.path.join(ROOT, 'alerts.json')))
    on = cfg.get('munten', {})
    is_quiet = quiet(cfg, now)
    only_waves = is_quiet and cfg['stil'].get('behalve_liquidatiegolf', True)
    topic = os.environ.get('NTFY_TOPIC')
    print('stille uren' + (' (alleen liquidatiegolven)' if only_waves else '') if is_quiet else 'geen stille uren')
    sig_path = os.path.join(site, 'glasssphere-signals.json')
    try:
        signals = json.load(open(sig_path))
    except (OSError, ValueError):
        signals = {'coins': {}}
    now_ms = int(dt.datetime.now().timestamp() * 1000)
    try:
        memo = SN.get(base.rstrip('/') + '/glasssphere-alerts.json')
    except Exception as e:
        print(f'geen vorige check: {e}')
        memo = {}
    if '--test' in sys.argv:
        post(topic, {'title': 'GlassSphere testmelding', 'message': 'Meldingen komen aan. Je krijgt bericht bij een nieuw '
                     'Buy/Sell-signaal en zijn uitkomst, een geraakt doel, een wissel van kant en een liquidatiegolf.',
                     'tags': ['white_check_mark'], 'priority': 3, 'click': base})
    for key, A in SN.ASSETS.items():
        path = os.path.join(site, A['file'])
        if not os.path.exists(path):
            continue
        m = memo.get(key) or {}
        try:
            r = AL.check(json.load(open(path)), m.get('last'))
        except Exception as e:   # one broken file must not stop the other coins
            print(f'== {A["name"]}: fout {e}')
            continue
        sent = {k: t for k, t in (m.get('sent') or {}).items() if now_ms - t < 48 * 3600000}
        memo[key] = {'last': r['state'], 'sent': sent}
        # each event once: drop what was sent within its own wait
        r['alerts'] = [a for a in r['alerts'] if not (a[2] in sent and now_ms - sent[a[2]] < AGAIN[a[0]] * 3600000)]
        sig = signal_alerts(signals['coins'].get(key), cfg, now_ms, on.get(key, True) and not is_quiet)
        r['alerts'] = sig + r['alerts']
        print(f'== {A["name"]}: {r["status"]}')
        for a in r['alerts']:
            print('   MELDING: ' + a[1])
        if not on.get(key, True):
            print('   (meldingen uit voor deze munt)')
            continue
        if on.get(key, True) == 'signalen':
            r['alerts'] = [a for a in r['alerts'] if a[0] in ('signal', 'score')]
        if is_quiet:
            r['alerts'] = [a for a in r['alerts'] if a[0] == 'wave'] if only_waves else []
        if not r['alerts']:
            continue
        if post(topic, message(r, key, base)):
            for a in r['alerts']:
                if a[2]:
                    sent[a[2]] = now_ms
    json.dump(signals, open(sig_path, 'w'), separators=(',', ':'))   # what was sent, carried forward by the site
    json.dump(memo, open(os.path.join(site, 'glasssphere-alerts.json'), 'w'), separators=(',', ':'))


def post(topic, msg):
    """Send one message; without a topic only print it. True when sent."""
    if not topic:
        print('   droog: ' + json.dumps(msg, ensure_ascii=False))
        return True
    req = urllib.request.Request('https://ntfy.sh', data=json.dumps(dict(msg, topic=topic)).encode(),
                                 headers={'Content-Type': 'application/json'})
    for i in range(3):
        try:
            urllib.request.urlopen(req, timeout=20).read()
            print('   verstuurd')
            return True
        except Exception as e:
            print(f'   versturen mislukt: {e}')
    return False


if __name__ == '__main__':
    main()
