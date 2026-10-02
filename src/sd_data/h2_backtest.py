"""H2 回測：族群強度分數（SSS）的偵測有效性（H2a）與動能延續性（H2b）。

H2a：SSS 高分期（>=90分位）的 D1 是否顯著高於低分期（<50分位）。
H2b：SSS 突破 90 分位後，20 天成本後超額報酬是否為正。
"""
import sqlite3
import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime
from scipy import stats

sys.path.insert(0, '/home/hatch/workspace/sector-detect/src')
from sd_data.calibrate import load_matrices, load_benchmark
from sd_data.indicators import compute_theme_frame

DB = '/home/hatch/workspace/sector-detect/sector_detect.db'
OUT = '/home/hatch/workspace/sector-detect/h2_report.json'
COST = 0.006
FWD = 20


def compute_sss(con, sid, stocks, all_dates):
    """計算單一族群的每日 SSS。回傳 DataFrame [date, d1, d2, d3, sss]。"""
    # 取全歷史（warmup 130 天）
    start = all_dates[0]
    m = load_matrices(con, stocks, start, all_dates[-1])
    bench = load_benchmark(con, start, all_dates[-1])
    if m is None or bench is None:
        return None
    bench = bench.reindex(m['dates']).fillna(0)
    f = compute_theme_frame(m['prices'], m['flow_amt'], m['amounts'], bench)
    # D2/D3 強度（同 calibrate.py）
    for col, zc in (('hhi', 'z_hhi'), ('inflow_tot_5d', 'z_tot5')):
        r = f[col].rolling(120, min_periods=60)
        f[zc] = ((f[col] - r.mean()) / r.std()).fillna(0)
    d1 = f['co_move'].fillna(0)
    d2 = f['z_hhi'] + f['z_tot5']
    d3 = f['leader_accel'].fillna(0)
    # 各自 120 日標準化
    z1 = (d1 - d1.rolling(120, min_periods=60).mean()) / d1.rolling(120, min_periods=60).std()
    z2 = (d2 - d2.rolling(120, min_periods=60).mean()) / d2.rolling(120, min_periods=60).std()
    z3 = (d3 - d3.rolling(120, min_periods=60).mean()) / d3.rolling(120, min_periods=60).std()
    sss = z1.fillna(0) + z2.fillna(0) + z3.fillna(0)
    out = pd.DataFrame({'date': f.index, 'd1': d1.values, 'd2': d2.values,
                        'd3': d3.values, 'sss': sss.values})
    # 族群等權報酬（還原價）
    rets = m['prices'].pct_change().mean(axis=1, skipna=True)
    out['sec_ret'] = rets.reindex(f.index).values
    return out.dropna(subset=['sss'])


def main():
    con = sqlite3.connect(DB)
    sectors = [(r[0], r[1]) for r in con.execute(
        "SELECT DISTINCT sector_id, sector_name FROM sector_map ORDER BY sector_id")]
    mkt = pd.read_sql_query(
        "SELECT date, taiex_close FROM market_daily WHERE taiex_close IS NOT NULL", con)
    mkt = mkt.set_index('date')['taiex_close'].sort_index()
    mkt_ret = mkt.pct_change()
    all_dates = sorted(mkt.index)

    h2a_high_d1, h2a_low_d1 = [], []
    h2b_trades = []
    per_sector = {}

    for si, (sid, sname) in enumerate(sectors):
        stocks = [r[0] for r in con.execute(
            "SELECT stock FROM sector_map WHERE sector_id=?", (sid,))]
        df = compute_sss(con, sid, stocks, all_dates)
        if df is None or len(df) < 200:
            continue
        df = df.set_index('date').sort_index()
        # H2a：高分期 vs 低分期 的 D1
        q90 = df['sss'].quantile(0.90)
        q50 = df['sss'].quantile(0.50)
        hi = df[df['sss'] >= q90]['d1']
        lo = df[df['sss'] < q50]['d1']
        h2a_high_d1.extend(hi.tolist())
        h2a_low_d1.extend(lo.tolist())
        # H2b：SSS 突破 90 分位 → 20 天超額報酬
        above = df['sss'] >= q90
        # 首次突破：今天在上、過去20天不在上
        entries = []
        for i in range(20, len(df) - FWD):
            if above.iloc[i] and not above.iloc[i-20:i].any():
                entries.append(df.index[i])
        bench_aligned = mkt_ret.reindex(df.index).fillna(0)
        for d in entries:
            i = df.index.get_loc(d)
            r_sec = float((1 + df['sec_ret'].iloc[i+1:i+1+FWD]).prod() - 1)
            r_mkt = float((1 + bench_aligned.iloc[i+1:i+1+FWD]).prod() - 1)
            h2b_trades.append({'sector': sid, 'entry': d,
                               'excess_net': r_sec - r_mkt - COST,
                               'sec_ret': r_sec, 'mkt_ret': r_mkt})
        per_sector[sid] = {'n_entries': len(entries)}
        if (si + 1) % 10 == 0:
            print(f'[{si+1}/{len(sectors)}] done', flush=True)

    con.close()

    # H2a 檢定
    h, l = np.array(h2a_high_d1), np.array(h2a_low_d1)
    t, p = stats.ttest_ind(h, l, equal_var=False)
    h2a = {'n_high': len(h), 'n_low': len(l),
           'mean_d1_high': float(np.mean(h)), 'mean_d1_low': float(np.mean(l)),
           't_stat': float(t), 'p_value': float(p),
           'passed': bool(p < 0.05 and np.mean(h) > np.mean(l))}
    # H2b 檢定
    xs = np.array([t['excess_net'] for t in h2b_trades])
    h2b = {'n_trades': len(xs)}
    if len(xs):
        h2b.update({'mean': float(np.mean(xs)), 'median': float(np.median(xs)),
                    'hit_rate': float(np.mean(xs > 0)),
                    'passed': bool(np.mean(xs) > 0 and np.mean(xs > 0) > 0.5)})
    rep = {'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'h2a': h2a, 'h2b': h2b, 'per_sector': per_sector}
    json.dump(rep, open(OUT, 'w'), ensure_ascii=False, indent=1)
    print(json.dumps({'h2a': h2a, 'h2b': h2b}, ensure_ascii=False, indent=1))
    print(f'report: {OUT}')


if __name__ == '__main__':
    main()
