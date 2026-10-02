"""校準族群（記憶體／光通訊／被動元件，共 31 檔）的除權息事件抓取。

背景：TWSE 除權息端點（TWT49U/TWT48U）皆為未來預告式、不支援歷史查詢。
過渡方案：以 yfinance 抓取 31 檔校準股的股利／配股事件，但每一筆都必須通過
官方價格缺口驗證（除權息日的價格跳空 ≈ 股利金額）才寫入；未通過者列為人工覆核。
正式方案（v1.1）：FinMind TaiwanStockDividend（需 token）全市場補齊。

factor 定義與 adjust.py 一致：factor = 參考價/前收盤，僅作用於 ex_date 之前。
"""
import sqlite3
import sys
from datetime import date

sys.path.insert(0, '/home/hatch/workspace/.pylibs')
sys.path.insert(0, '/home/hatch/workspace/sector-detect/src')

DB = '/home/hatch/workspace/sector-detect/sector_detect.db'
THEMES = ['記憶體與控制晶片', '光通訊與 CPO', '被動元件 (MLCC/電感/電阻)']
SINCE = '2024-01-01'
TOL = 0.12  # 價格缺口驗證容忍（日波動）


def theme_stocks(con):
    out = {}
    for name in THEMES:
        rows = con.execute(
            "SELECT stock FROM sector_map WHERE sector_name=? AND version='v20260926'", (name,)).fetchall()
        out[name] = [r[0] for r in rows]
    return out


def prev_trading_close(con, stock, ex_date):
    r = con.execute('SELECT close FROM price_daily WHERE stock=? AND date < ? '
                    'ORDER BY date DESC LIMIT 1', (stock, ex_date)).fetchone()
    return r[0] if r and r[0] else None


def exday_close(con, stock, ex_date):
    r = con.execute('SELECT close FROM price_daily WHERE stock=? AND date=?',
                    (stock, ex_date)).fetchone()
    return r[0] if r and r[0] else None


def fetch_dividends(stocks):
    """yfinance 抓取股利＋配股。回傳 {stock: [(ex_date, kind, amount_or_ratio)]}。"""
    import yfinance as yf
    import time
    events = {}
    for i, code in enumerate(stocks):
        try:
            t = yf.Ticker(f'{code}.TW')
            evts = []
            for ts, amt in t.dividends.items():
                d = ts.strftime('%Y-%m-%d')
                if d >= SINCE and amt and amt > 0:
                    evts.append((d, '息', float(amt)))
            for ts, ratio in t.splits.items():
                d = ts.strftime('%Y-%m-%d')
                if d >= SINCE and ratio and ratio != 1.0:
                    evts.append((d, '權', float(ratio)))
            events[code] = sorted(evts)
        except Exception as e:
            print(f'  !! yf failed {code}: {str(e)[:80]}')
            events[code] = []
        if (i + 1) % 10 == 0:
            print(f'  yf {i + 1}/{len(stocks)}...', flush=True)
        time.sleep(0.5)
    return events


def validate_and_store(con, events):
    """價格缺口驗證 → 寫入 exdiv。回傳 (accepted, flagged)。"""
    cur = con.cursor()
    accepted, flagged = [], []
    for code, evts in events.items():
        for ex_date, kind, val in evts:
            prev = prev_trading_close(con, code, ex_date)
            exc = exday_close(con, code, ex_date)
            if prev is None or exc is None or prev <= 0:
                flagged.append((code, ex_date, kind, val, 'missing official price'))
                continue
            if kind == '息':
                if val / prev > 0.25:  # 殖利率異常 → 單位或資料錯誤
                    flagged.append((code, ex_date, kind, val, f'yield {val/prev:.1%} too high'))
                    continue
                raw_ret = exc / prev - 1
                # 除息日價格跳空應 ≈ -val/prev（容忍日波動）
                if abs(raw_ret + val / prev) > TOL:
                    flagged.append((code, ex_date, kind, val,
                                    f'gap mismatch: raw_ret={raw_ret:.2%} vs div={val/prev:.2%}'))
                    continue
                factor = (prev - val) / prev
            else:  # 權（配股）：ratio 如 1.1 表 10%
                if not (1.0 < val < 3.0):
                    flagged.append((code, ex_date, kind, val, f'ratio {val} out of range'))
                    continue
                raw_ret = exc / prev - 1
                if abs(raw_ret - (1 / val - 1)) > TOL:
                    flagged.append((code, ex_date, kind, val,
                                    f'gap mismatch: raw_ret={raw_ret:.2%} vs split={1/val-1:.2%}'))
                    continue
                factor = 1 / val
            cur.execute('INSERT OR REPLACE INTO exdiv VALUES (?,?,?,?,?,?,?)',
                        (ex_date.replace('-', ''), code, 'YF', prev, prev * factor, factor, kind))
            accepted.append((code, ex_date, kind, val, factor))
    con.commit()
    return accepted, flagged


def main():
    con = sqlite3.connect(DB)
    themes = theme_stocks(con)
    stocks = sorted({s for v in themes.values() for s in v})
    print(f'theme stocks: {len(stocks)}')
    for name, ss in themes.items():
        print(f'  {name}: {len(ss)}')
    events = fetch_dividends(stocks)
    n_evts = sum(len(v) for v in events.values())
    print(f'dividend/split events since {SINCE}: {n_evts}')
    accepted, flagged = validate_and_store(con, events)
    print(f'accepted: {len(accepted)}, flagged for review: {len(flagged)}')
    for f in flagged:
        print('  FLAG:', f)
    con.close()


if __name__ == '__main__':
    main()
