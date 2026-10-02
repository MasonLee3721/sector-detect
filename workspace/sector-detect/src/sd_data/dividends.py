"""校準族群除權息事件：FinMind TaiwanStockDividend（正式方案 v1.1）。

背景：TWSE 除權息端點（TWT49U/TWT48U）皆為未來預告式、不支援歷史查詢，
故歷史除權息改用 FinMind。注意 FinMind 註冊級別擋「整市場批量」查詢
（不帶 data_id 會 400），必須逐檔查詢（帶 data_id 正常）。

流程：
1. 逐檔抓 TaiwanStockDividend（2024-01-01 起）
2. 解析現金／股票股利事件，以 CashExDividendTradingDate / StockExDividendTradingDate 為準
3. 官方價格缺口驗證：理論參考價 (prev_close - cash_d)/(1 + stock_s) vs
   除權息日實際開盤價，誤差 <= 3% 才接受
4. 通過者寫入 exdiv（ex_date=實際除權息日，factor=參考價/前收），供 adjust.py 向後還原

factor 定義與 adjust.py 一致：僅作用於 ex_date 之前的日期，無未來函數。

用法：
  python3 src/sd_data/dividends.py            # 31 檔校準股
  python3 src/sd_data/dividends.py --stocks 2330,2317
  python3 src/sd_data/dividends.py --universe  # 全市場（背景執行，約 1975 檔）
  python3 src/sd_data/dividends.py --db <path> [同上]  # 指定 DB（worker DB 專用）
"""
import json
import sqlite3
import subprocess
import sys
import time

FM_CLI = '/home/hatch/workspace/skills/finmind/bin/fm_api.py'
DB = '/home/hatch/workspace/sector-detect/sector_detect.db'
SINCE = '2024-01-01'
UNTIL = '2026-10-02'
TOL = 0.03          # 參考價驗證容忍
DELAY = 1.0         # FinMind 註冊級別節流（秒）

THEMES = ['記憶體與控制晶片', '光通訊與 CPO', '被動元件 (MLCC/電感/電阻)']


def theme_stocks(con):
    out = []
    for name in THEMES:
        rows = con.execute(
            "SELECT stock FROM sector_map WHERE sector_name=? AND version='v20260926'",
            (name,)).fetchall()
        out += [r[0] for r in rows]
    return sorted(set(out))


def fm_fetch(stock, delay=DELAY, retries=3):
    """逐檔抓 FinMind TaiwanStockDividend，429/402 時退避重試。"""
    for attempt in range(retries):
        p = subprocess.run(
            [sys.executable, FM_CLI, 'data', '--dataset', 'TaiwanStockDividend',
             '--data-id', stock, '--start-date', SINCE, '--end-date', UNTIL],
            capture_output=True, text=True, timeout=120)
        if p.returncode == 0:
            time.sleep(delay)
            return json.loads(p.stdout or '[]')
        err = p.stderr.strip()[-300:]
        if '402' in err or '429' in err or 'rate' in err.lower():
            wait = 60 * (attempt + 1)
            print(f'  {stock}: rate limited, wait {wait}s', flush=True)
            time.sleep(wait)
            continue
        raise RuntimeError(f'{stock}: {err}')
    raise RuntimeError(f'{stock}: rate limited after {retries} retries')


def fnum(v):
    try:
        return float(v) if v not in (None, '') else 0.0
    except (TypeError, ValueError):
        return 0.0


def parse_events(stock, rows):
    """rows -> {(ex_date): {'cash_d': x, 'stock_s': y}}；現金＋股票同日合併。"""
    evts = {}
    for r in rows:
        cash_d = fnum(r.get('CashEarningsDistribution')) + fnum(r.get('CashStatutorySurplus'))
        stock_s = fnum(r.get('StockEarningsDistribution')) + fnum(r.get('StockStatutorySurplus'))
        for ex_date, amt, key in ((r.get('CashExDividendTradingDate'), cash_d, 'cash_d'),
                                  (r.get('StockExDividendTradingDate'), stock_s, 'stock_s')):
            if ex_date and amt > 0:
                e = evts.setdefault(ex_date, {'cash_d': 0.0, 'stock_s': 0.0})
                e[key] += amt
    return evts


def validate(con, stock, ex_date, cash_d, stock_s):
    """價格缺口驗證。回傳 (ok, info)。"""
    prev = con.execute(
        'SELECT close FROM price_daily WHERE stock=? AND date < ? AND close IS NOT NULL'
        ' ORDER BY date DESC LIMIT 1', (stock, ex_date)).fetchone()
    op = con.execute(
        'SELECT open FROM price_daily WHERE stock=? AND date=?', (stock, ex_date)).fetchone()
    if not prev or not op or op[0] is None:
        return False, {'reason': 'no_price_data'}
    prev_close, open_px = prev[0], op[0]
    ref = (prev_close - cash_d) / (1 + stock_s)
    if ref <= 0:
        return False, {'reason': 'bad_ref', 'prev_close': prev_close}
    dev = abs(open_px - ref) / ref
    info = {'prev_close': prev_close, 'ref': ref, 'open': open_px, 'dev': dev,
            'cash_d': cash_d, 'stock_s': stock_s}
    return dev <= TOL, info


def market_of(con, stock):
    r = con.execute('SELECT market FROM universe WHERE stock=?', (stock,)).fetchone()
    return r[0] if r else ''


def run(stocks, con):
    accepted, flagged, pending = [], [], []
    for i, stock in enumerate(stocks):
        try:
            rows = fm_fetch(stock)
        except Exception as e:
            flagged.append((stock, '', 0, 0, {'reason': f'fetch_error: {e}'}))
            continue
        evts = parse_events(stock, rows)
        for ex_date in sorted(evts):
            e = evts[ex_date]
            ok, info = validate(con, stock, ex_date, e['cash_d'], e['stock_s'])
            rec = (stock, ex_date, e['cash_d'], e['stock_s'], info)
            if info.get('reason') == 'no_price_data':
                pending.append(rec)
            elif ok:
                accepted.append(rec)
            else:
                flagged.append(rec)
        if (i + 1) % 10 == 0:
            print(f'  {i + 1}/{len(stocks)} stocks...', flush=True)
    # 寫入 exdiv
    mkt_cache = {}
    for stock, ex_date, cash_d, stock_s, info in accepted:
        if stock not in mkt_cache:
            mkt_cache[stock] = market_of(con, stock)
        kind = 'both' if (cash_d > 0 and stock_s > 0) else ('cash' if cash_d > 0 else 'stock')
        con.execute('INSERT OR REPLACE INTO exdiv VALUES (?,?,?,?,?,?,?)',
                    (ex_date, stock, mkt_cache[stock], info['prev_close'],
                     info['ref'], info['ref'] / info['prev_close'], kind))
    con.commit()
    return accepted, flagged, pending


def main():
    args = sys.argv[1:]
    db = DB
    if args[:1] == ['--db']:
        db = args[1]
        args = args[2:]
    con = sqlite3.connect(db)
    if args[:1] == ['--stocks']:
        stocks = args[1].split(',')
    elif args[:1] == ['--universe']:
        stocks = [r[0] for r in con.execute(
            "SELECT stock FROM universe WHERE is_common=1").fetchall()]
    else:
        stocks = theme_stocks(con)
    print(f'fetching dividends for {len(stocks)} stocks from FinMind...', flush=True)
    accepted, flagged, pending = run(stocks, con)
    print(f'\naccepted={len(accepted)} flagged={len(flagged)} pending(no price yet)={len(pending)}')
    for stock, ex_date, cash_d, stock_s, info in flagged:
        print(f'  FLAG {stock} {ex_date} cash={cash_d} stock={stock_s} {info}')
    for stock, ex_date, cash_d, stock_s, info in pending:
        print(f'  PENDING {stock} {ex_date} cash={cash_d} stock={stock_s}')
    con.close()


if __name__ == '__main__':
    main()
