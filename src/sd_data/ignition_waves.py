"""點火規則 v2：系統性冷啟動波段枚舉＋特徵刻畫。

v1 的教訓（2026-10-04 診斷）：
1. 三個錨點波段的點火形態各異：被動元件是價格先行（9/18-22 thrust，資金 5 日暴增從未觸發）、
   光通訊是資金＋價格同動（1/28-29）、記憶體在波段起點時 SSS 已排名第 1（根本不是冷啟動）。
2. 同日 COLD（rank>19）與 FLOW_BURST 互斥：資金暴增本身會推高 SSS 排名。
   修正：WAS_COLD 用 t-5 日排名（因果）。

本腳本：
1. 枚舉 wave_scan.json 全部波段，定義冷啟動波段集：
   rank_5d(start-5) > 19 且 peak_excess > 0.15。
2. 對每個冷啟動波段，刻畫 [start-10, start+5] 內的訊號：
   thrust_3d 峰值、flow_rising 是否觸發、breadth 峰值、leader_accelerating 是否觸發。
3. 輸出 ignition_waves.json 供規則設計使用。
"""
import json
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, '/home/hatch/workspace/sector-detect/src')
from sd_data.calibrate import load_matrices, load_benchmark
from sd_data.indicators import compute_theme_frame
from sd_data.ignition import sss_series

DB = '/home/hatch/workspace/sector-detect/sector_detect.db'


def main():
    con = sqlite3.connect(DB)
    mkt = pd.read_sql_query(
        "SELECT date, taiex_close FROM market_daily WHERE taiex_close IS NOT NULL", con)
    mkt = mkt.set_index('date')['taiex_close'].sort_index()
    all_dates = sorted(mkt.index)
    mkt_ret = mkt.pct_change()
    mkt_idx = (1 + mkt_ret.fillna(0)).cumprod()

    sectors = [(r[0], r[1]) for r in con.execute(
        "SELECT DISTINCT sector_id, sector_name FROM sector_map ORDER BY sector_id")]
    frames, sss5 = {}, {}
    for si, (sid, sname) in enumerate(sectors):
        stocks = [r[0] for r in con.execute(
            "SELECT stock FROM sector_map WHERE sector_id=?", (sid,))]
        m = load_matrices(con, stocks, all_dates[0], all_dates[-1])
        bench = load_benchmark(con, all_dates[0], all_dates[-1])
        if m is None or bench is None or len(m['dates']) < 150:
            continue
        bench = bench.reindex(m['dates']).fillna(0)
        f = compute_theme_frame(m['prices'], m['flow_amt'], m['amounts'], bench)
        frames[sid] = (f, sname)
        sss5[sid] = sss_series(f)['sss'].rolling(5).mean()
        print(f'[{si+1}] {sid} ok', flush=True)
    rankw = pd.DataFrame(sss5).rank(axis=1, ascending=False, method='min')

    waves = json.load(open('/home/hatch/workspace/sector-detect/wave_scan.json'))['waves']
    cold_waves = []
    for x in waves:
        sid, start = x['sector_id'], x['start']
        if sid not in frames:
            continue
        f, sname = frames[sid]
        idx = list(f.index)
        if start not in idx:
            continue
        i0 = idx.index(start)
        if i0 < 15:
            continue
        r5 = rankw[sid].loc[idx[i0 - 5]]
        if not (r5 > 19 and x['peak_excess'] > 0.15):
            continue
        # 刻畫 [start-10, start+5]
        lo, hi = idx[i0 - 10], idx[min(len(idx) - 1, i0 + 5)]
        seg = f.loc[lo:hi]
        sec_idx = f['eq_index']
        thrust = (sec_idx / sec_idx.shift(3) - 1) - \
                 (mkt_idx.reindex(idx).ffill() / mkt_idx.reindex(idx).ffill().shift(3) - 1)
        tseg = thrust.loc[lo:hi]
        # 點火一週前排名（冷確認）
        cold_waves.append({
            'sector_id': sid, 'sector_name': sname,
            'start': start, 'end': x['end'],
            'peak_excess': round(x['peak_excess'], 3), 'days': x['days'],
            'rank_start': float(rankw[sid].loc[start]),
            'rank_start_m5': float(r5),
            'rank_min_pre10': float(rankw[sid].loc[idx[i0-10]:start].min()),
            'thrust3d_max': float(tseg.max()),
            'thrust3d_argmax': str(tseg.idxmax()),
            'flow_rising_any': bool(seg['flow_rising'].any()),
            'flow_rising_first': str(seg[seg['flow_rising']].index[0])
            if seg['flow_rising'].any() else None,
            'breadth_max': float(seg['breadth'].max()),
            'leader_any': bool(seg['leader_accelerating'].any()),
            'co_move_max': float(seg['co_move'].max()),
        })
    cold_waves.sort(key=lambda z: -z['peak_excess'])
    json.dump({'n_cold_waves': len(cold_waves), 'waves': cold_waves},
              open('/home/hatch/workspace/sector-detect/ignition_waves.json', 'w'),
              ensure_ascii=False, indent=1)
    print(f'\ncold-start waves: {len(cold_waves)}/{len(waves)}')
    for z in cold_waves:
        print(f"{z['sector_id']} {z['sector_name'][:14]:14s} {z['start']} peak {z['peak_excess']:+.2f} "
              f"rank_m5 {z['rank_start_m5']:.0f} thrust_max {z['thrust3d_max']:+.3f}@{z['thrust3d_argmax']} "
              f"flow {int(z['flow_rising_any'])} breadth {z['breadth_max']:.2f} leader {int(z['leader_any'])}")
    con.close()


if __name__ == '__main__':
    main()
