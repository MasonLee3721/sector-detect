"""單日 ETL：抓取上市＋上櫃批量端點，正規化後寫入 SQLite（冪等，可重跑）。"""
import sqlite3
from . import sources as S


def _upsert_universe(cur, code, name, market, date):
    cur.execute('''INSERT INTO universe(stock, market, name, first_date, last_date, is_common)
                   VALUES (?,?,?,?,?,1)
                   ON CONFLICT(stock) DO UPDATE SET
                     last_date=excluded.last_date,
                     name=COALESCE(NULLIF(excluded.name,''), universe.name),
                     market=excluded.market''',
                (code, market, name or '', date, date))


def etl_day(con, yyyymmdd):
    """回傳 True=成功寫入；False=當日無資料（休市或尚未公布）→ caller 跳過。"""
    print(f'== ETL {yyyymmdd}', flush=True)
    cur = con.cursor()

    tw_p = S.twse_price(yyyymmdd)
    tw_f = S.twse_inst(yyyymmdd)
    if tw_f is None:
        print(f'  skip {yyyymmdd}: no TWSE inst data (holiday or not published)')
        return False
    tw_m, tw_mtot = S.twse_margin(yyyymmdd)
    tw_x = S.twse_exdiv(yyyymmdd)
    tw_mkt = S.twse_market(yyyymmdd)

    tp_p = S.tpex_price(yyyymmdd)
    tp_f = S.tpex_inst(yyyymmdd)
    tp_m = S.tpex_margin(yyyymmdd)

    n_price = n_flow = n_marg = n_xdiv = 0

    for code, q in (tw_p or {}).items():
        cur.execute('INSERT OR REPLACE INTO price_daily VALUES (?,?,?,?,?,?,?,?,?)',
                    (yyyymmdd, code, 'TWSE', q['open'], q['high'], q['low'],
                     q['close'], q['volume'], q['amount']))
        _upsert_universe(cur, code, q['name'], 'TWSE', yyyymmdd)
        n_price += 1
    for code, q in (tp_p or {}).items():
        if code in (tw_p or {}):
            continue
        cur.execute('INSERT OR REPLACE INTO price_daily VALUES (?,?,?,?,?,?,?,?,?)',
                    (yyyymmdd, code, 'TPEx', q['open'], q['high'], q['low'],
                     q['close'], q['volume'], q['amount']))
        _upsert_universe(cur, code, q['name'], 'TPEx', yyyymmdd)
        n_price += 1

    for code, fl in tw_f.items():
        cur.execute('INSERT OR REPLACE INTO inst_flow VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (yyyymmdd, code,
                     fl.get('f_buy'), fl.get('f_sell'), fl.get('f_net'),
                     fl.get('t_buy'), fl.get('t_sell'), fl.get('t_net'),
                     fl.get('d_buy'), fl.get('d_sell'), fl.get('d_net')))
        n_flow += 1
    for code, fl in (tp_f or {}).items():
        if code in tw_f:
            continue
        cur.execute('INSERT OR REPLACE INTO inst_flow VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (yyyymmdd, code,
                     fl.get('f_buy'), fl.get('f_sell'), fl.get('f_net'),
                     fl.get('t_buy'), fl.get('t_sell'), fl.get('t_net'),
                     fl.get('d_buy'), fl.get('d_sell'), fl.get('d_net')))
        n_flow += 1

    for code, (prev, today) in (tw_m or {}).items():
        cur.execute('INSERT OR REPLACE INTO margin_daily VALUES (?,?,?,?)',
                    (yyyymmdd, code, prev, today))
        n_marg += 1
    for code, (prev, today) in (tp_m or {}).items():
        cur.execute('''INSERT OR REPLACE INTO margin_daily VALUES (?,?,?,?)''',
                    (yyyymmdd, code, prev, today))
        n_marg += 1

    for ex_date, code, prev, ref, factor, kind in tw_x:
        cur.execute('INSERT OR REPLACE INTO exdiv VALUES (?,?,?,?,?,?,?)',
                    (ex_date, code, 'TWSE', prev, ref, factor, kind))
        n_xdiv += 1

    cur.execute('''INSERT OR REPLACE INTO market_daily
                   (date, f_net, t_net, d_net, total_turnover, up_count, down_count, taiex_close)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (yyyymmdd, tw_mkt['f_net'], tw_mkt['t_net'], tw_mkt['d_net'],
                 tw_mkt['total_turnover'], tw_mkt['up_count'], tw_mkt['down_count'],
                 tw_mkt.get('taiex_close')))

    con.commit()
    print(f'  price={n_price} flow={n_flow} margin={n_marg} exdiv={n_xdiv} '
          f"mkt f={tw_mkt['f_net']:.1f}億", flush=True)
    return True
