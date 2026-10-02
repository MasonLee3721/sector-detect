"""資料品質驗證：覆蓋率、內部一致性、還原正確性。"""
import sqlite3
from .adjust import check_adjustment


def coverage(con, yyyymmdd):
    cur = con.cursor()
    p = cur.execute('SELECT COUNT(*), COUNT(DISTINCT stock) FROM price_daily WHERE date=?',
                    (yyyymmdd,)).fetchone()
    f = cur.execute('SELECT COUNT(DISTINCT stock) FROM inst_flow WHERE date=?', (yyyymmdd,)).fetchone()
    m = cur.execute('SELECT COUNT(DISTINCT stock) FROM margin_daily WHERE date=?', (yyyymmdd,)).fetchone()
    mk = cur.execute('SELECT up_count, down_count, total_turnover FROM market_daily WHERE date=?',
                     (yyyymmdd,)).fetchone()
    return {'price_rows': p[0], 'price_stocks': p[1], 'flow_stocks': f[0],
            'margin_stocks': m[0], 'market': mk}


def consistency(con, yyyymmdd, sample=2000):
    """抽檢 OHLC 邏輯：high>=max(open,close,low)，low<=min(...)。回傳違規數。"""
    cur = con.cursor()
    bad = 0
    for o, h, l, c in cur.execute(
            'SELECT open, high, low, close FROM price_daily WHERE date=? LIMIT ?', (yyyymmdd, sample)):
        if None in (o, h, l, c):
            continue
        if not (h >= max(o, c, l) - 1e-9 and l <= min(o, c, h) + 1e-9):
            bad += 1
    return bad


def adjustment_checks(con, limit=10):
    """對最近的 exdiv 事件逐一驗證還原正確性。"""
    cur = con.cursor()
    evts = cur.execute('SELECT ex_date, stock FROM exdiv ORDER BY ex_date DESC LIMIT ?', (limit,)).fetchall()
    results = []
    for ex_date, stock in evts:
        r = check_adjustment(con, stock, ex_date)
        r.update({'stock': stock, 'ex_date': ex_date})
        results.append(r)
    return results


def run_all(db_path, yyyymmdd):
    con = sqlite3.connect(db_path)
    print(f'## validate {yyyymmdd}')
    cov = coverage(con, yyyymmdd)
    print(' coverage:', cov)
    bad = consistency(con, yyyymmdd)
    print(f' ohlc violations: {bad}')
    adj = adjustment_checks(con)
    ok = sum(1 for r in adj if r['ok'])
    print(f' adjustment checks: {ok}/{len(adj)} ok')
    for r in adj:
        if not r['ok']:
            print('  FAIL:', r)
    con.close()
    return cov, bad, adj
