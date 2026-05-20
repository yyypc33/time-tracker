#!/usr/bin/env python3
"""
iPhone Screen Time Sync
從 macOS Biome 讀取 iPhone App.InFocus 資料 → 輸出 data/today.js
用法: python3 sync.py [--days N]
"""

import os, sys, re, struct, glob, json, argparse
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from collections import defaultdict

# ── 設定 ─────────────────────────────────────────────────
IPHONE_UUID  = 'E2A05AC8-01E9-474A-9169-32C3677BB4EA'
BIOME_PATH   = os.path.expanduser(
    '~/Library/Biome/streams/restricted/App.InFocus/remote/'
)
OUTPUT_DIR   = Path(__file__).parent / 'data'
OUTPUT_JS    = OUTPUT_DIR / 'today.js'
HISTORY_JS   = OUTPUT_DIR / 'history.js'

SESSION_CAP  = 10 * 60   # 超過 10 分鐘的空白截斷
MIN_SESSION  = 3          # 小於 3 秒不算

APPLE_EPOCH  = datetime(2001, 1, 1, tzinfo=timezone.utc)
TS_MIN       = 599616000.0   # 2020-01-01
TS_MAX       = 820454400.0   # 2027-01-01

# ── App 分類表 ────────────────────────────────────────────
# (顯示名稱, 維度, 是否正面)
# 維度: 工作 / 健康 / 財務 / 社交 / 心理 / None=忽略
# 正面: True=加分, False=扣分, None=依使用時間判斷(超1hr算負面)
BUNDLE_MAP = {
    # 工作 / 學習
    'com.microsoft.Office.Word':        ('Word',          '工作', True),
    'com.microsoft.Office.Excel':       ('Excel',         '工作', True),
    'com.microsoft.Office.Powerpoint':  ('PowerPoint',    '工作', True),
    'com.microsoft.Outlook':            ('Outlook',       '工作', True),
    'com.microsoft.teams':              ('Teams',         '工作', True),
    'com.apple.mobilemail':             ('Mail',          '工作', True),
    'com.apple.mobilecal':              ('Calendar',      '工作', True),
    'com.apple.Notes':                  ('Notes',         '工作', True),
    'com.notion.id':                    ('Notion',        '工作', True),
    'com.apple.iBooks':                 ('Books',         '工作', True),
    'com.amazon.kindle':                ('Kindle',        '工作', True),
    'com.apple.Safari':                 ('Safari',        '工作', None),
    'com.google.chrome.ios':            ('Chrome',        '工作', None),
    'com.apple.reminders':              ('Reminders',     '工作', True),

    # 健康
    'com.apple.Health':                 ('Health',        '健康', True),
    'com.apple.Fitness':                ('Fitness',       '健康', True),
    'com.nike.nikeplus-gps':            ('Nike Run',      '健康', True),
    'com.strava.ios':                   ('Strava',        '健康', True),
    'com.myfitnesspal.MFPiPhone':       ('MyFitnessPal',  '健康', True),

    # 財務
    'com.dbs.cardplus.tw':              ('DBS Card',      '財務', True),
    'com.apple.Passbook':               ('Wallet',        '財務', True),
    'com.taobao.taobao4iphone':         ('Taobao',        '財務', False),

    # 社交
    'com.burbn.instagram':              ('Instagram',     '社交', None),
    'com.facebook.Facebook':            ('Facebook',      '社交', None),
    'com.burbn.barcelona':              ('Threads',       '社交', None),
    'com.hammerandchisel.discord':      ('Discord',       '社交', True),
    'com.apple.MobilePhone':            ('Phone',         '社交', True),
    'com.apple.MobileSMS':              ('Messages',      '社交', True),
    'com.apple.facetime':               ('FaceTime',      '社交', True),
    'com.tencent.xin':                  ('WeChat',        '社交', True),

    # 休閒（娛樂 / 遊戲）
    'com.google.ios.youtube':           ('YouTube',       '休閒', None),
    'com.netflix.Netflix':              ('Netflix',       '休閒', None),
    'com.netmarble.tskgb':              ('遊戲',           '休閒', None),
    'com.garena.game.kgtw':             ('手遊',           '休閒', None),

    # 心理（音樂 / 冥想 / 放鬆）
    'com.google.ios.youtubemusic':      ('YouTube Music', '心理', True),
    'com.spotify.client':               ('Spotify',       '心理', True),
    'com.apple.Music':                  ('Apple Music',   '心理', True),
    'com.apple.Podcasts':               ('Podcasts',      '心理', True),

    # 工作（AI / 學習 / 生產力）
    'com.openai.chat':                  ('ChatGPT',       '工作', True),
    'com.anthropic.claude':             ('Claude',        '工作', True),
    'com.duolingo.DuolingoMobile':      ('Duolingo',      '工作', True),
    'com.brave.ios.browser':            ('Brave',         '工作', None),
    'com.google.Gmail':                 ('Gmail',         '工作', True),
    'com.forestapp.Forest':             ('Forest',        '工作', True),
    'com.microsoft.to-do':              ('To Do',         '工作', True),
    'com.timeleft.app':                 ('TimeLeft',      '工作', True),

    # 忽略（系統 UI，不計時）
    'com.apple.mobileslideshow':        ('Photos',        None, None),
    'com.apple.AppStore':               ('App Store',     None, None),
    'com.apple.Preferences':            ('Settings',      None, None),
    'com.apple.camera':                 ('Camera',        None, None),
}

SKIP_KEYWORDS = [
    'springboard', 'control-center', 'posterboard', 'carousel',
    'sleeplockscreen', 'transitionreason', 'today-view', 'app-library',
    'backlightinactive',
]

DIMS = ['工作', '健康', '財務', '社交', '休閒', '心理']

# ── SEGB 解析 ─────────────────────────────────────────────
def parse_segb(path: str) -> list[tuple[float, str]]:
    with open(path, 'rb') as f:
        data = f.read()
    if data[:4] != b'SEGB':
        return []

    events, prev_i = [], -1
    for i in range(24, len(data) - 20, 1):
        try:
            val = struct.unpack_from('<d', data, i)[0]
            if not (TS_MIN < val < TS_MAX):
                continue
            if i - prev_i < 8:
                continue
            chunk = data[i: i + 200]
            bundles = re.findall(rb'com\.[a-zA-Z0-9._-]{4,50}', chunk)
            if bundles:
                bundle = bundles[0].decode().rstrip('Jh\x10\x18\x08')
                if any(k in bundle.lower() for k in SKIP_KEYWORDS):
                    prev_i = i + 7
                    continue
                events.append((val, bundle))
                prev_i = i + 7
        except Exception:
            pass
    return events

def load_events(uuid: str, days: int = 7) -> list[tuple[float, str]]:
    base = os.path.join(BIOME_PATH, uuid)
    if not os.path.exists(base):
        sys.exit(f'[錯誤] 找不到裝置目錄: {base}')

    raw = []
    for f in glob.glob(f'{base}/[0-9]*'):
        raw.extend(parse_segb(f))

    # 去重 + 排序
    seen, deduped = set(), []
    for ts, bundle in sorted(raw):
        key = (round(ts), bundle)
        if key not in seen:
            seen.add(key)
            deduped.append((ts, bundle))

    # 只保留最近 N 天
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days))
    cutoff_apple = (cutoff - APPLE_EPOCH).total_seconds()
    return [(ts, b) for ts, b in deduped if ts >= cutoff_apple]

def compute_sessions(events: list) -> list[dict]:
    sessions = []
    for i, (ts, bundle) in enumerate(events):
        duration = min(events[i+1][0] - ts, SESSION_CAP) if i+1 < len(events) else 60
        if duration >= MIN_SESSION:
            sessions.append({'ts': ts, 'bundle': bundle, 'duration': round(duration)})
    return sessions

def group_by_day(sessions: list) -> dict[str, dict[str, float]]:
    daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for s in sessions:
        dt_local = (APPLE_EPOCH + timedelta(seconds=s['ts'])).astimezone()
        day = dt_local.strftime('%Y-%m-%d')
        daily[day][s['bundle']] += s['duration']
    return daily

# 明確忽略的系統 App（不輸出到 JS，讓 HTML 設定介面更乾淨）
IGNORE_BUNDLES = {
    # 系統 App
    'com.apple.mobileslideshow',        # Photos
    'com.apple.AppStore',
    'com.apple.Preferences',
    'com.apple.camera',
    'com.apple.SleepLockScreen',
    'com.apple.S',                      # Siri
    'com.apple.ClockAngel',             # 系統時鐘服務
    'com.apple.HeadphoneProxService',   # 耳機服務
    'com.apple.PassbookUIService',      # Wallet UI 服務
    'com.apple.AuthKitUIService',       # 驗證 UI 服務
    'com.apple.ScreenshotServicesService',
    'com.apple.LocalAuthenticationUIService',
    'com.apple.InCallService',          # 通話中系統疊層
    'com.apple.webapp',                 # 系統
    'com.apple.mobiletimer',            # 時鐘 App（系統）
    'com.apple.calculator',             # 計算機
    'com.apple.mobilesafari',           # Safari（已由 com.apple.Safari 追蹤）
    'com.apple.mobilephone',            # Phone（已由 com.apple.MobilePhone 追蹤）
    'com.apple.stocks',                 # 股市 App
    'com.nordvpn.NordVPN',              # VPN 工具
    # 交通（不計分）
    'com.grabtaxi.iphone',              # Grab
    'com.google.Maps',                  # Google Maps
}

# ── 分類 ──────────────────────────────────────────────────
def classify(bundle: str) -> tuple[str, str | None, bool | None]:
    info = BUNDLE_MAP.get(bundle)
    if info:
        name, dim, pos = info
        return name, dim, pos          # pos=None 的中性 App，由 HTML 依上限判斷
    name = bundle.split('.')[-1].replace('-', ' ').title()
    return name, None, None            # 未知 App：dimension=None，讓使用者在 app 裡設定

# ── 輸出 ──────────────────────────────────────────────────
def build_day_output(day: str, bundle_secs: dict) -> dict:
    apps = []
    for bundle, secs in sorted(bundle_secs.items(), key=lambda x: -x[1]):
        if bundle in IGNORE_BUNDLES:
            continue
        # 在 BUNDLE_MAP 裡但 dimension=None 的（Photos/Settings 等）也跳過
        if bundle in BUNDLE_MAP and BUNDLE_MAP[bundle][1] is None:
            continue
        name, dim, pos = classify(bundle)
        apps.append({
            'bundle':    bundle,
            'name':      name,
            'dimension': dim,    # 可能是 None（未知 app），由 HTML 使用者設定
            'positive':  pos,    # 可能是 None（中性），由 HTML 依上限判斷
            'seconds':   round(secs),
            'minutes':   round(secs / 60, 1),
        })

    summary = {}
    for dim in DIMS:
        dim_apps = [a for a in apps if a['dimension'] == dim]
        summary[dim] = {
            'total':    round(sum(a['seconds'] for a in dim_apps)),
            'positive': round(sum(a['seconds'] for a in dim_apps if a['positive'])),
            'negative': round(sum(a['seconds'] for a in dim_apps if a['positive'] is False)),
        }

    return {'date': day, 'apps': apps, 'summary': summary}

def write_js(path: Path, var_name: str, data: object, comment: str = ''):
    path.parent.mkdir(exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        if comment:
            f.write(f'// {comment}\n')
        f.write(f'window.{var_name} = {json.dumps(data, ensure_ascii=False, indent=2)};\n')

# ── 主程式 ────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=7, help='讀取最近幾天（預設 7）')
    args = parser.parse_args()

    print(f'[1/4] 讀取 Biome 資料（最近 {args.days} 天）...')
    events = load_events(IPHONE_UUID, args.days)
    print(f'      找到 {len(events)} 筆事件')

    print('[2/4] 計算使用時段...')
    sessions = compute_sessions(events)
    daily    = group_by_day(sessions)

    today_str = date.today().strftime('%Y-%m-%d')
    print(f'[3/4] 輸出今日資料 ({today_str})...')
    today_out = build_day_output(today_str, daily.get(today_str, {}))
    today_out['generated'] = datetime.now().isoformat()
    write_js(OUTPUT_JS, 'SCREEN_TIME_DATA', today_out,
             f'Generated: {today_out["generated"]}')

    print('[4/4] 輸出歷史資料（最近 7 天）...')
    history = [build_day_output(d, daily[d]) for d in sorted(daily)]
    write_js(HISTORY_JS, 'SCREEN_TIME_HISTORY', history,
             f'Generated: {datetime.now().isoformat()}')

    print('\n── 今日統計 ──────────────────────')
    total_all = 0
    for dim in DIMS:
        t = today_out['summary'][dim]['total']
        total_all += t
        if t:
            sign = '+' if today_out['summary'][dim]['positive'] >= t * 0.5 else '⚠'
            print(f'  {sign} {dim}: {t//3600}h {(t%3600)//60}m')
    print(f'  ─ 總計: {total_all//3600}h {(total_all%3600)//60}m')
    print(f'\n✓ 已輸出到 {OUTPUT_DIR}')
    print('  用瀏覽器開啟 index.html 查看完整結果')

if __name__ == '__main__':
    main()
