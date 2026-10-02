"""38 族群全掃描：波段偵測 + 每波段 lead-lag 分析。

目的：檢驗條件式假設——「急漲波中 D3 領先，緩漲波中 D3 落後」。
每波段記錄：族群、波段起迄、峰值超額報酬、天數、銳度(峰值/天數)、
D3/D2 領先天數中位數、估計品質（邊界命中率、樣本數）。
"""
import sqlite3
import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime

sys.path.insert(0, '/home/hatch/workspace/sector-detect/src')
from sd_data.calibrate import (load_matrices, load_benchmark, resolve_sector,
                               wave_lead_dist, WARMUP)
from sd_data.indicators import compute_theme_frame

DB = '/home/hatch/workspace/sector-detect/sector_detect.db'
OUT = '/home/hatch/workspace/sector-detect/wave_scan.json'


def detect_waves(ex60, thresh, min_len=30, merge_gap=15):
    hi = (ex60 > thresh).values
    idx = list(ex60.index)
    segs, s = [], None
    for i, v in enumerate(hi):
        if v and s is None:
            s = i
        if not v and s is not None:
            segs.append((s, i - 1))
            s = None
    if s is not None:
        segs.append((s, len(hi) - 1))
    merged = []
    for a, b in segs:
        if merged and a - merged[-1][1] <= merge_gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    out = []
    for a, b in merged:
        if b - a + 1 < min_len:
            continue
        seg = ex60.iloc[a:b + 1]
        out.append({'start': idx[a], 'end': idx[b],
                    'peak_date': seg.idxmax(), 'peak': float(seg.max()),
                    'days': b - a + 1})
    return out


def wave_lead_median(f, col_x, col_d1, w0, w1):
    """回傳 (median, n, edge_rate, pos_rate)。"""
    dist = wave_lead_dist(f, col_x, col_d1, w0, w1)
    if not dist:
        return None, 0, None, None
    arr = np.array(dist)
    return (float(np.median(arr)), len(arr),
            float(np.mean(np.abs(arr) >= 19)), float(np.mean(arr > 0)))


def main():
    con = sqlite3.connect(DB)
    sectors = [(r[0], r[1]) for r in con.execute(
        "SELECT DISTINCT sector_id, sector_name FROM sector_map ORDER BY sector_id")]
    print(f'{len(sectors)} sectors', flush=True)

    # 全市場價格 + benchmark（一次載入）
    px = pd.read_sql_query(
        """SELECT p.date AS date, p.stock AS stock, COALESCE(a.close_adj, p.close) AS px
           FROM price_daily p LEFT JOIN price_adj a
           ON a.date=p.date AND a.stock=p.stock""", con)
    mkt = pd.read_sql_query(
        "SELECT date, taiex_close FROM market_daily WHERE taiex_close IS NOT NULL", con)
    mkt = mkt.set_index('date')['taiex_close'].sort_index()
    mkt_ret = mkt.pct_change()
    all_dates = sorted(mkt.index)

    results = []
    for si, (sid, sname) in enumerate(sectors):
        stocks = [r[0] for r in con.execute(
            "SELECT stock FROM sector_map WHERE sector_id=?", (sid,))]
        piv = px[px.stock.isin(stocks)].pivot(
            index='date', columns='stock', values='px').sort_index()
        if piv.empty:
            continue
        sec_ret = piv.pct_change().mean(axis=1, skipna=True)
        aligned = pd.DataFrame({'sec': sec_ret, 'mkt': mkt_ret}).dropna()
        if len(aligned) < 120:
            continue
        ex60 = ((1 + aligned['sec']).rolling(60).apply(np.prod, raw=True) -
                (1 + aligned['mkt']).rolling(60).apply(np.prod, raw=True)).dropna()
        thresh = ex60.quantile(0.75)
        waves = detect_waves(ex60, thresh)
        print(f'[{si+1}/{len(sectors)}] {sid} {sname}: {len(waves)} waves', flush=True)

        for wi, wv in enumerate(waves):
            # 校準窗口：波段起往前 60 交易日
            si0 = all_dates.index(wv['start'])
            w0 = all_dates[max(0, si0 - 60)]
            w1 = wv['end']
            try:
                cal = pd.read_sql_query(
                    "SELECT DISTINCT date FROM price_daily WHERE date < ? "
                    "ORDER BY date DESC LIMIT ?", con, params=(w0, WARMUP))
                start = cal['date'].min() if not cal.empty else w0
                m = load_matrices(con, stocks, start, w1)
                bench = load_benchmark(con, start, w1)
                if m is None or bench is None:
                    continue
                bench = bench.reindex(m['dates']).fillna(0)
                f = compute_theme_frame(m['prices'], m['flow_amt'], m['amounts'], bench)
                for col, zc in (('hhi', 'z_hhi'), ('inflow_tot_5d', 'z_tot5')):
                    r = f[col].rolling(120, min_periods=60)
                    f[zc] = ((f[col] - r.mean()) / r.std()).fillna(0)
                f['d2_strength'] = f['z_hhi'] + f['z_tot5']
                f['d3_strength'] = f['leader_accel'].fillna(0)
                f['d1_comove'] = f['co_move'].fillna(0)
                d3_med, d3_n, d3_edge, d3_pos = wave_lead_median(
                    f, 'd3_strength', 'd1_comove', w0, w1)
                d2_med, d2_n, d2_edge, d2_pos = wave_lead_median(
                    f, 'd2_strength', 'd1_comove', w0, w1)
                results.append({
                    'sector_id': sid, 'sector_name': sname,
                    'wave': wi + 1, 'start': wv['start'], 'end': wv['end'],
                    'peak_date': wv['peak_date'], 'peak_excess': wv['peak'],
                    'days': wv['days'],
                    'sharpness': wv['peak'] / wv['days'],  # 日均超額報酬
                    'd3_lead_med': d3_med, 'd3_n': d3_n,
                    'd3_edge_rate': d3_edge, 'd3_pos_rate': d3_pos,
                    'd2_lead_med': d2_med, 'd2_n': d2_n,
                    'd2_edge_rate': d2_edge, 'd2_pos_rate': d2_pos,
                })
            except Exception as e:
                print(f'  wave {wi+1} error: {e}', flush=True)
                continue

    con.close()
    rep = {'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'n_waves': len(results), 'waves': results}
    json.dump(rep, open(OUT, 'w'), ensure_ascii=False, indent=1)
    print(f'done: {len(results)} waves -> {OUT}')


if __name__ == '__main__':
    main()
