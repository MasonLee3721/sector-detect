"""除權息還原：向後還原（backward adjustment）。

對每檔股票：close_adj(t) = close(t) × Π factor(e)，其中 e 跑遍 ex_date > t 的所有除權息事件。
factor = 除權息參考價 / 除權息前收盤價（來自 TWT49U，含除息／除權／現金增資／減資）。
還原僅作用於 ex_date 之前的日期 → 不存在未來函數。
"""
from .schema import set_meta


def apply_adjustment(con, verbose=True):
    cur = con.cursor()
    stocks = [r[0] for r in cur.execute('SELECT DISTINCT stock FROM exdiv').fetchall()]
    total = 0
    for i, stock in enumerate(stocks):
        evts = cur.execute(
            'SELECT ex_date, factor FROM exdiv WHERE stock=? ORDER BY ex_date', (stock,)).fetchall()
        if not evts:
            continue
        prices = cur.execute(
            'SELECT date, close FROM price_daily WHERE stock=? ORDER BY date', (stock,)).fetchall()
        # 從右向左累乘
        cum = 1.0
        ei = len(evts) - 1
        rows = []
        for date, close in reversed(prices):
            while ei >= 0 and evts[ei][0] > date:
                cum *= evts[ei][1]
                ei -= 1
            if close is not None:
                rows.append((date, stock, close * cum))
        cur.executemany('INSERT OR REPLACE INTO price_adj VALUES (?,?,?)', rows)
        total += len(rows)
        if verbose and (i + 1) % 200 == 0:
            print(f'  adjusted {i + 1}/{len(stocks)} stocks...', flush=True)
    con.commit()
    set_meta(con, 'adjust_applied', '1')
    print(f'  price_adj rows: {total}', flush=True)
    return total


def check_adjustment(con, stock, ex_date):
    """驗證單一除權息事件：還原後跨除權息日的報酬應等於經濟報酬（ref基準）。"""
    cur = con.cursor()
    r = cur.execute('SELECT prev_close, ref_price, factor FROM exdiv WHERE stock=? AND ex_date=?',
                    (stock, ex_date)).fetchone()
    if not r:
        return {'ok': False, 'msg': 'no exdiv event'}
    prev_close, ref_price, factor = r
    a = cur.execute('SELECT close, close_adj FROM price_daily LEFT JOIN price_adj USING(date, stock)'
                    ' WHERE price_daily.stock=? AND date=?', (stock, ex_date)).fetchone()
    b = cur.execute('SELECT close_adj FROM price_adj WHERE stock=? AND date< ? ORDER BY date DESC LIMIT 1',
                    (stock, ex_date)).fetchone()
    if not a or not b or a[0] is None or a[1] is None or b[0] is None:
        return {'ok': False, 'msg': 'missing price data'}
    close_ex, adj_ex, adj_prev = a[0], a[1], b[0]
    # 經濟報酬（以參考價為基準）vs 還原報酬
    econ_ret = close_ex / ref_price - 1
    adj_ret = adj_ex / adj_prev - 1
    return {'ok': abs(econ_ret - adj_ret) < 1e-6,
            'econ_ret': econ_ret, 'adj_ret': adj_ret,
            'prev_close': prev_close, 'ref_price': ref_price, 'factor': factor}
