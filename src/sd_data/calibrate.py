"""三族群真實歷史校準：H1a（資金領先價格）＋ H1b（S_early 後 20 日成本後超額報酬）。

用法：
  python3 src/sd_data/calibrate.py --db sector_detect.db [--out report.json]

輸入（DB）：
- price_daily(date YYYYMMDD, stock, close, amount)、price_adj(date, stock, close_adj)
- inst_flow(date, stock, f_net, t_net, d_net)   # 股數
- sector_map(sector_id, sector_name, stock, version)

方法：
1. 價格 = COALESCE(price_adj.close_adj, price_daily.close)。
   若某族群完全無 price_adj，該族群結果標示 provisional（未還原）。
2. flow_amt = (f_net + t_net + d_net) * close（股數 → 金額）。
3. benchmark = 全市場等權日報酬。注意：market_daily 無加權指數收盤，
   此為 proxy；正式版應接入加權指數日報酬（見 limitations）。
4. compute_theme_frame → D1/D2/D3 ＋ S_early/S_mid/S_confirm（warmup 130 交易日）。
5. H1a：波段內首次 flow_rising / leader_accelerating / converging 的日期差。
6. H1b：S_early 叢集首日後 20 個交易日，族群等權報酬 − benchmark − 成本。
   成本：台股現股來回約 0.6%（手續費 0.1425%×2＋賣出交易稅 0.3%）。
7. LOO 穩定性：每次剔除一波段，重算領先天數中位數。

否證（依 H1 規格）：三波段中 < 2 個呈現 D3 正領先（lead_d3 > 0）→ H1a 被否證；
H1b 樣本外（LOO）成本後超額報酬不穩定 → H1b 被否證。
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, '/home/hatch/workspace/sector-detect/src')
from sd_data.indicators import compute_theme_frame, PARAMS

VERSION = 'v20260926'
WARMUP = 130          # 交易日；涵蓋 hhi_hist=120 等最長窗口
FWD = 20              # H1b 前視窗口（交易日）
COST = 0.006          # 來回成本 0.6%

WAVES = {
    # (sector關鍵字, 波段起, 波段迄)；起迄依 H1 規格 §4，為待驗證起點
    'memory': ('記憶體', '20240701', '20250630'),
    'optical': ('光通訊', '20240101', '20250630'),
    'passive': ('被動元件', '20250101', '20251231'),
}


def qdf(con, sql, params=()):
    return pd.read_sql_query(sql, con, params=params)


def resolve_sector(con, keyword):
    rows = qdf(con, "SELECT DISTINCT sector_id, sector_name FROM sector_map WHERE version=?",
               (VERSION,))
    hit = rows[rows['sector_name'].str.contains(keyword)]
    if hit.empty:
        raise ValueError(f'sector keyword not found: {keyword}')
    r = hit.iloc[0]
    stocks = qdf(con, "SELECT stock FROM sector_map WHERE sector_id=? AND version=?",
                 (r['sector_id'], VERSION))['stock'].tolist()
    return r['sector_id'], r['sector_name'], sorted(set(stocks))


def load_matrices(con, stocks, start, end):
    """回傳 dict(dates, prices, flow_amt, amounts)。價格為還原價（有才用）。"""
    ph = ','.join('?' * len(stocks))
    px = qdf(con, f"""
        SELECT p.date, p.stock, COALESCE(a.close_adj, p.close) AS px, p.amount,
               (COALESCE(f.f_net,0)+COALESCE(f.t_net,0)+COALESCE(f.d_net,0)) AS net_sh,
               p.close AS raw_close
        FROM price_daily p
        LEFT JOIN price_adj a ON a.date=p.date AND a.stock=p.stock
        LEFT JOIN inst_flow f ON f.date=p.date AND f.stock=p.stock
        WHERE p.stock IN ({ph}) AND p.date BETWEEN ? AND ?
        ORDER BY p.date""", (*stocks, start, end))
    if px.empty:
        return None
    dates = sorted(px['date'].unique())
    prices = px.pivot(index='date', columns='stock', values='px').reindex(dates)
    amounts = px.pivot(index='date', columns='stock', values='amount').reindex(dates).fillna(0)
    net_sh = px.pivot(index='date', columns='stock', values='net_sh').reindex(dates).fillna(0)
    raw = px.pivot(index='date', columns='stock', values='raw_close').reindex(dates)
    flow_amt = net_sh * raw  # 股數 × 收盤 = 金額
    return {'dates': dates, 'prices': prices, 'flow_amt': flow_amt, 'amounts': amounts}


def load_benchmark(con, start, end):
    """全市場等權日報酬（proxy；正式版應為加權指數）。"""
    px = qdf(con, """
        SELECT p.date AS date, p.stock AS stock, COALESCE(a.close_adj, p.close) AS px
        FROM price_daily p LEFT JOIN price_adj a ON a.date=p.date AND a.stock=p.stock
        WHERE p.date BETWEEN ? AND ?""", (start, end))
    if px.empty:
        return None
    piv = px.pivot(index='date', columns='stock', values='px').sort_index()
    rets = piv.pct_change()
    return rets.mean(axis=1, skipna=True)


def first_true(s):
    t = s[s.fillna(False)]
    return t.index[0] if len(t) else None


def first_persistent(s, k=5):
    """首次「連續 k 個交易日為真」的首日；無則 None。

    相對閾值（分位數）在噪音中本來就會零星觸發（75 分位→約 25% 天數），
    取首次觸發會抓到噪音。用持續性要求定位機制轉換（regime change）的起點。
    僅作輔助診斷；H1a 主檢定用下方 lead-lag 分佈法。
    """
    b = s.fillna(False).astype(bool)
    run = b.rolling(k, min_periods=k).sum() >= k
    t = run[run]
    if len(t) == 0:
        return None
    idx = list(b.index)
    return idx[idx.index(t.index[0]) - k + 1]


def lead_lag_days(x, y, max_lag=20):
    """x 領先 y 的天數：corr(x[t], y[t+L]) 在 L∈[-max_lag, +max_lag] 的 argmax。

    正值 = x 領先 y。回傳 (lead_days, max_corr)。全部因果：只用同期與過去資料。
    """
    best_L, best_c = 0, float('-inf')
    for L in range(-max_lag, max_lag + 1):
        c = x.corr(y.shift(-L))
        if pd.notna(c) and c > best_c:
            best_c, best_L = c, L
    return best_L, best_c


def wave_lead_dist(frame, xcol, ycol, w0, w1, win=60, step=10, max_lag=20,
                   min_corr=0.15):
    """波段內滾動窗口的領先天數分佈。回傳 list of int（空表示無有效估計）。"""
    sub = frame.loc[w0:w1]
    leads = []
    for s in range(0, len(sub) - win + 1, step):
        w = sub.iloc[s:s + win]
        L, c = lead_lag_days(w[xcol], w[ycol], max_lag)
        if c > min_corr:
            leads.append(L)
    return leads


def early_clusters(sig, min_gap=FWD):
    """S_early 連續日成叢，取每叢首日；叢間至少隔 min_gap 個交易日（前視不重疊）。"""
    idx = sig.index[sig.fillna(False)]
    if len(idx) == 0:
        return []
    clusters, cur = [], [idx[0]]
    pos = {d: i for i, d in enumerate(sig.index)}
    for d in idx[1:]:
        if pos[d] - pos[cur[-1]] <= 2:
            cur.append(d)
        else:
            clusters.append(cur[0])
            cur = [d]
    clusters.append(cur[0])
    out, last_p = [], -10 ** 9
    for d in clusters:
        if pos[d] - last_p >= min_gap:
            out.append(d)
            last_p = pos[d]
    return out


def run_wave(con, key, keyword, w0, w1):
    sid, sname, stocks = resolve_sector(con, keyword)
    # warmup：往前取足夠交易日
    cal = qdf(con, "SELECT DISTINCT date FROM price_daily WHERE date < ? ORDER BY date DESC LIMIT ?",
              (w0, WARMUP))
    start = cal['date'].min() if not cal.empty else w0
    m = load_matrices(con, stocks, start, w1)
    bench = load_benchmark(con, start, w1)
    if m is None or bench is None:
        return {'sector_id': sid, 'sector_name': sname, 'stocks': stocks,
                'error': 'no data in window'}
    bench = bench.reindex(m['dates']).fillna(0)
    frame = compute_theme_frame(m['prices'], m['flow_amt'], m['amounts'], bench)

    # H1a 主檢定：連續強度序列的 lead-lag 分佈（滾動 60 日窗口，step 10）
    # D2 強度 = 標準化(HHI20) + 標準化(5日總流入)；D3 = 領頭 RS 加速；D1 = 共動比率
    f = frame.copy()
    for col, zc in (('hhi', 'z_hhi'), ('inflow_tot_5d', 'z_tot5')):
        r = f[col].rolling(120, min_periods=60)
        f[zc] = ((f[col] - r.mean()) / r.std()).fillna(0)
    f['d2_strength'] = f['z_hhi'] + f['z_tot5']
    f['d3_strength'] = f['leader_accel'].fillna(0)
    f['d1_comove'] = f['co_move'].fillna(0)
    leads_d2 = wave_lead_dist(f, 'd2_strength', 'd1_comove', w0, w1)
    leads_d3 = wave_lead_dist(f, 'd3_strength', 'd1_comove', w0, w1)

    win = frame.loc[w0:w1]
    if win.empty:
        return {'sector_id': sid, 'sector_name': sname, 'stocks': stocks,
                'error': 'empty wave window'}

    # 輔助診斷：首次持續訊號（事件定年法，較脆弱，僅參考）
    t_flow = first_persistent(win['flow_rising'])
    t_acc = first_persistent(win['leader_accelerating'])
    t_conv = first_persistent(win['converging'])
    pos = {d: i for i, d in enumerate(frame.index)}
    def _td(a, b):
        return (pos[b] - pos[a]) if (a and b) else None
    lead_d2_evt = _td(t_flow, t_conv)
    lead_d3_evt = _td(t_acc, t_conv)

    # H1b：S_early 叢集 → 20 日成本後超額報酬
    rets = m['prices'].pct_change()
    eq_ret = rets.mean(axis=1, skipna=True)
    trades = []
    for d in early_clusters(win['S_early']):
        i = pos[d]
        if i + FWD >= len(frame.index):
            continue
        r_sec = float((1 + eq_ret.iloc[i + 1:i + 1 + FWD]).prod() - 1)
        r_mkt = float((1 + bench.iloc[i + 1:i + 1 + FWD]).prod() - 1)
        trades.append({'entry': d, 'excess_net': r_sec - r_mkt - COST,
                       'sec_ret': r_sec, 'mkt_ret': r_mkt})
    xs = [t['excess_net'] for t in trades]
    h1b = {'n': len(trades), 'trades': trades}
    if xs:
        h1b.update({'mean': float(np.mean(xs)), 'median': float(np.median(xs)),
                    'hit_rate': float(np.mean([x > 0 for x in xs])),
                    'min': float(np.min(xs)), 'max': float(np.max(xs))})

    # 還原覆蓋率
    adj_cov = qdf(con, """SELECT COUNT(DISTINCT a.stock) c FROM price_adj a
                          WHERE a.stock IN (%s)""" % ','.join('?' * len(stocks)),
                  (*stocks,)).iloc[0]['c']
    return {
        'sector_id': sid, 'sector_name': sname, 'stocks': stocks,
        'window': [w0, w1], 'n_trading_days': len(win),
        'provisional': bool(adj_cov == 0),
        'first_signals': {'flow_rising': t_flow, 'leader_accelerating': t_acc,
                          'converging': t_conv},
        'lead_days': {
            'd2_vs_d1_median': float(np.median(leads_d2)) if leads_d2 else None,
            'd3_vs_d1_median': float(np.median(leads_d3)) if leads_d3 else None,
            'd2_vs_d1_dist': leads_d2, 'd3_vs_d1_dist': leads_d3,
            'd2_vs_d1_event': lead_d2_evt, 'd3_vs_d1_event': lead_d3_evt},
        'n_signals': {'early_days': int(win['S_early'].sum()),
                      'early_clusters': len(trades),
                      'mid_days': int(win['S_mid'].sum()),
                      'confirm_days': int(win['S_confirm'].sum())},
        'h1b': h1b,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', default='')
    ns = ap.parse_args()
    con = sqlite3.connect(ns.db)
    rep = {'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'db': ns.db, 'waves': {}, 'limitations': []}

    # 籃子一致性檢查
    try:
        import json as _json
        bj = _json.load(open('/home/hatch/workspace/fundflo/basket.json'))
        nb = sum(len(s['stocks']) for s in bj['sectors'])
        nd = qdf(con, "SELECT COUNT(*) c FROM sector_map WHERE version=?",
                 (VERSION,)).iloc[0]['c']
        rep['basket_check'] = {'basket_json_rows': nb, 'db_sector_map_rows': int(nd)}
        if nb != nd:
            rep['limitations'].append(f'籃子筆數不一致：basket.json={nb} vs DB={nd}，待核對')
    except Exception as e:
        rep['limitations'].append(f'basket check skipped: {e}')

    for key, (kw, w0, w1) in WAVES.items():
        print(f'wave {key} ...', flush=True)
        try:
            rep['waves'][key] = run_wave(con, key, kw, w0, w1)
        except Exception as e:
            rep['waves'][key] = {'error': str(e)}

    # H1a 彙總＋否證：以每波段「領先天數分佈的中位數」為單位
    med3 = [w['lead_days']['d3_vs_d1_median'] for w in rep['waves'].values()
            if w.get('lead_days', {}).get('d3_vs_d1_median') is not None]
    med2 = [w['lead_days']['d2_vs_d1_median'] for w in rep['waves'].values()
            if w.get('lead_days', {}).get('d2_vs_d1_median') is not None]
    n_pos3 = sum(1 for x in med3 if x > 0)
    h1a = {'per_wave_median_lead_d3': med3, 'per_wave_median_lead_d2': med2,
           'median_of_medians_d3': float(np.median(med3)) if med3 else None,
           'median_of_medians_d2': float(np.median(med2)) if med2 else None,
           'waves_positive_d3': n_pos3}
    h1a['falsified'] = (n_pos3 < 2)  # <2 波段呈現 D3 正領先 → 否證
    # LOO 穩定性：每次剔除一波段，重算中位數的中位數
    loo = {}
    keys = list(rep['waves'].keys())
    for k in keys:
        rest = [w['lead_days']['d3_vs_d1_median'] for kk, w in rep['waves'].items()
                if kk != k and w.get('lead_days', {}).get('d3_vs_d1_median') is not None]
        loo[f'leave_{k}_out'] = float(np.median(rest)) if rest else None
    h1a['loo_median_of_medians_d3'] = loo
    rep['h1a'] = h1a

    # H1b 彙總
    allx = [t['excess_net'] for w in rep['waves'].values()
            for t in w.get('h1b', {}).get('trades', [])]
    h1b = {'n_trades': len(allx)}
    if allx:
        h1b.update({'pooled_mean': float(np.mean(allx)),
                    'pooled_median': float(np.median(allx)),
                    'hit_rate': float(np.mean([x > 0 for x in allx]))})
    rep['h1b'] = h1b
    rep['limitations'].extend([
        'benchmark 為全市場等權報酬 proxy，非加權指數；正式版應接入指數日報酬。',
        '波段起迄為 H1 規格建議起點，錨點應以資料驅動精煉（v0.2）。',
        '閾值為 PARAMS 固定值；threshold 優化與 walk-forward 為 v0.2。',
    ])
    con.close()

    out = ns.out or '/home/hatch/workspace/sector-detect/calibration_report.json'
    json.dump(rep, open(out, 'w'), ensure_ascii=False, indent=1)
    print(json.dumps({'h1a': h1a, 'h1b': h1b,
                      'provisional_waves': [k for k, w in rep['waves'].items()
                                            if w.get('provisional')]},
                     ensure_ascii=False, indent=1))
    print(f'report: {out}')


if __name__ == '__main__':
    main()
