"""HTTP 抓取基礎：重試、禮貌延遲、數字清洗。"""
import json
import sys
import time
import urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
TIMEOUT = 60
POLITE_DELAY = 0.8  # 官方站點之間請求間隔（秒）


def fetch(url, retries=3):
    """GET JSON with retries. Returns parsed object or None (never raises)."""
    for attempt in (1, 2, 3, 4)[:retries]:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            if attempt == retries:
                print(f'  !! fetch failed: {url[:90]} -> {str(e)[:100]}', file=sys.stderr)
                return None
            time.sleep(3 * attempt)
    return None


def num(s):
    """官方表格數字清洗：'--'/'' 視為缺值 → None（絕不靜默填零）。"""
    if s is None:
        return None
    s = str(s).replace(',', '').strip()
    if s in ('', '--', '---', 'NULL'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_date(yyyymmdd):
    return f'{int(yyyymmdd[:4]) - 1911}/{yyyymmdd[4:6]}/{yyyymmdd[6:8]}'
