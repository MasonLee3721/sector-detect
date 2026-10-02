"""各官方端點的抓取＋解析，輸出正規化 records。欄位索引沿用 FundFlo 已驗證寫法。"""
import re
from .fetch import fetch, num, roc_date, POLITE_DELAY
import time

CODE_RE = re.compile(r'^[1-9]\d{3}$')  # 上市櫃普通股（含 TDR 91xx）


def _sleep():
    time.sleep(POLITE_DELAY)


# ---------- TWSE（上市） ----------

def twse_price(yyyymmdd):
    """MI_INDEX 每日收盤行情 → {code: {name, open, high, low, close, volume, amount}}"""
    d = fetch(f'https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={yyyymmdd}&type=ALLBUT0999')
    _sleep()
    if not d:
        return None
    out = {}
    for t in d.get('tables', []):
        if '每日收盤行情' in (t.get('title') or ''):
            for r in t.get('data', []):
                code = r[0].strip()
                if not CODE_RE.match(code):
                    continue
                out[code] = {'name': r[1].strip(), 'open': num(r[5]), 'high': num(r[6]),
                             'low': num(r[7]), 'close': num(r[8]),
                             'volume': num(r[2]), 'amount': num(r[4])}
            break
    return out


def twse_inst(yyyymmdd):
    """TWT38U/TWT44U/TWT43U → {code: {f_/t_/d_ buy/sell/net}}（股數）"""
    flows = {}
    d = fetch(f'https://www.twse.com.tw/rwd/zh/fund/TWT38U?response=json&date={yyyymmdd}')
    _sleep()
    if not (d and d.get('stat') == 'OK' and d.get('data')):
        return None
    for r in d['data']:
        c = r[1].strip()
        if not CODE_RE.match(c):
            continue
        flows[c] = {'f_buy': num(r[9]), 'f_sell': num(r[10]), 'f_net': num(r[11])}
    d = fetch(f'https://www.twse.com.tw/rwd/zh/fund/TWT44U?response=json&date={yyyymmdd}')
    _sleep()
    if d and d.get('data'):
        for r in d['data']:
            c = r[1].strip()
            if not CODE_RE.match(c):
                continue
            flows.setdefault(c, {}).update(
                {'t_buy': num(r[3]), 't_sell': num(r[4]), 't_net': num(r[5])})
    d = fetch(f'https://www.twse.com.tw/rwd/zh/fund/TWT43U?response=json&date={yyyymmdd}')
    _sleep()
    if d and d.get('data'):
        for r in d['data']:
            c = r[0].strip()
            if not CODE_RE.match(c):
                continue
            flows.setdefault(c, {}).update(
                {'d_buy': num(r[8]), 'd_sell': num(r[9]), 'd_net': num(r[10])})
    return flows


def twse_margin(yyyymmdd):
    """MI_MARGN 融資融券彙總 → {code: (fin_prev, fin_today)}（張）＋上市合計"""
    d = fetch(f'https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={yyyymmdd}&selectType=STOCK')
    _sleep()
    if not d:
        return None, None
    out, total = {}, None
    for t in d.get('tables', []):
        if '融資融券彙總' in (t.get('title') or ''):
            for r in t.get('data', []):
                code = r[0].strip()
                if code == '合計':
                    total = (num(r[4]), num(r[5]))
                elif CODE_RE.match(code):
                    out[code] = (num(r[4]), num(r[5]))
            break
    return out, total


def twse_exdiv(yyyymmdd):
    """TWT49U 除權息結果 → [(ex_date, code, prev_close, ref_price, factor, kind)]

    注意：此端點為「未來預告式」，r[0] 的除權息日通常晚於查詢日。
    必須使用 row 內的實際除權息日作為 ex_date（民國「115年10月05日」→ 西元），
    不可用查詢日，否則歷史回補會寫入錯誤的 ex_date。
    """
    import re
    d = fetch(f'https://www.twse.com.tw/rwd/zh/exRight/TWT49U?response=json&date={yyyymmdd}')
    _sleep()
    if not (d and d.get('data')):
        return []
    out = []
    for r in d['data']:
        code = r[1].strip()
        if not CODE_RE.match(code):
            continue
        m = re.match(r'(\d+)年(\d+)月(\d+)日', r[0].strip())
        if not m:
            continue
        ex_date = f'{int(m.group(1)) + 1911:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
        prev, ref = num(r[3]), num(r[4])
        if prev and ref and prev > 0 and ref > 0 and ref != prev:
            out.append((ex_date, code, prev, ref, ref / prev, r[6].strip()))
    return out


def twse_market(yyyymmdd):
    """BFI82U 市場法人合計＋MI_INDEX 大盤統計 → dict（億元、家數）"""
    d = fetch(f'https://www.twse.com.tw/fund/BFI82U?response=json&dayDate={yyyymmdd}&type=day')
    _sleep()
    f_m = t_m = dd_m = 0.0
    if d and d.get('data'):
        for r in d['data']:
            nm, v = r[0], (num(r[3]) or 0) / 1e8
            if nm in ('外資及陸資(不含外資自營商)', '外資自營商'):
                f_m += v
            elif nm == '投信':
                t_m += v
            elif nm in ('自營商(自行買賣)', '自營商(避險)'):
                dd_m += v
    mi = fetch(f'https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={yyyymmdd}&type=ALLBUT0999')
    _sleep()
    tot, up, down = None, None, None
    if mi:
        for t in mi.get('tables', []):
            ti = t.get('title') or ''
            if ti.endswith('大盤統計資訊'):
                for r in t['data']:
                    if r[0].strip() == '總計(1~15)':
                        tot = (num(r[1]) or 0) / 1e8
            elif ti == '漲跌證券數合計':
                for r in t['data']:
                    m = re.match(r'(\d+)', (r[2] or '').replace(',', ''))
                    if not m:
                        continue
                    if r[0].startswith('上漲'):
                        up = int(m.group(1))
                    elif r[0].startswith('下跌'):
                        down = int(m.group(1))
    return {'f_net': f_m, 't_net': t_m, 'd_net': dd_m,
            'total_turnover': tot, 'up_count': up, 'down_count': down}


# ---------- TPEx（上櫃） ----------

def _tpex_table(d, keyword):
    for t in d.get('tables', []):
        if keyword in (t.get('title') or ''):
            return t
    ts = d.get('tables', [])
    return ts[0] if len(ts) == 1 else None


def tpex_price(yyyymmdd):
    """舊版 stk_wn1430_result.php（支援歷史日期）→ {code: {...}}；金額統一為元"""
    rd = roc_date(yyyymmdd)
    d = fetch(f'https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={rd}&se=AL')
    _sleep()
    if not d:
        return None
    t = _tpex_table(d, '行情') or _tpex_table(d, '上櫃')
    if not t:
        return None
    fields = t.get('fields') or []
    def col(*keys):
        for i, f in enumerate(fields):
            if any(k in f for k in keys):
                return i
        return None
    i_code = col('代號')
    i_name = col('名稱')
    i_close = col('收盤')
    i_open = col('開盤')
    i_high = col('最高')
    i_low = col('最低')
    i_vol = col('成交股數')
    i_amt = col('成交金額')
    if i_code is None or i_close is None:
        return None
    amt_mult = 1000 if any('千元' in f for f in fields) else 1
    out = {}
    for r in t.get('data', []):
        code = (r[i_code] or '').strip()
        if not CODE_RE.match(code):
            continue
        out[code] = {'name': (r[i_name] or '').strip() if i_name is not None else '',
                     'open': num(r[i_open]) if i_open is not None else None,
                     'high': num(r[i_high]) if i_high is not None else None,
                     'low': num(r[i_low]) if i_low is not None else None,
                     'close': num(r[i_close]),
                     'volume': num(r[i_vol]) if i_vol is not None else None,
                     'amount': (num(r[i_amt]) * amt_mult) if i_amt is not None else None}
    return out


def tpex_inst(yyyymmdd):
    """3itrade_hedge_result.php → {code: {f_/t_/d_ buy/sell/net}}"""
    rd = roc_date(yyyymmdd)
    d = fetch(f'https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&se=EW&t=D&d={rd}')
    _sleep()
    if not d:
        return None
    out = {}
    for t in d.get('tables', []):
        for r in t.get('data', []):
            code = (r[0] or '').strip()
            if not CODE_RE.match(code):
                continue
            out[code] = {'f_buy': num(r[8]), 'f_sell': num(r[9]), 'f_net': num(r[10]),
                         't_buy': num(r[11]), 't_sell': num(r[12]), 't_net': num(r[13]),
                         'd_buy': num(r[20]), 'd_sell': num(r[21]), 'd_net': num(r[22])}
        break
    return out


def tpex_margin(yyyymmdd):
    """margin_bal_result.php → {code: (fin_prev, fin_today)}（張）"""
    rd = roc_date(yyyymmdd)
    d = fetch(f'https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&d={rd}&o=daily')
    _sleep()
    if not d:
        return None
    t = _tpex_table(d, '融資融券餘額')
    if not t:
        return None
    out = {}
    for r in t.get('data', []):
        code = (r[0] or '').strip()
        if not CODE_RE.match(code):
            continue
        out[code] = (num(r[2]), num(r[6]))
    return out
