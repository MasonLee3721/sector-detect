"""點火規則 v2 校準＋回測。

用法：
    python3 src/sd_data/ignition_backtest_v2.py [--db PATH] [--out PATH]

v2 規則：IGNITE(t) = was_cold(t) & thrust(t) & breadth_ok(t)
  - was_cold: t-5 日 SSS 5日平均排名 > 19（後半）
  - thrust: 3 日超額報酬（族群等權 − 加權）> thrust_x
  - breadth_ok: 上漲家數比 > 0.6
money_confirmed / leader_led 僅做信心加權標記。

校準：在 26 個冷啟動波段（ignition_waves.json）上網格搜尋 thrust_x，
以「波段召回率（[start-10, start+3] 內首觸發）」為主、總觸發數為輔選擇。
回測：全歷史去重點火 → 前視報酬、命中率、信心分級表現、null 基線對照。
"""
import argparse
import json
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, '/home/hatch/workspace/sector-detect/src')
from sd_data.calibrate import load_matrices, load_benchmark
from sd_data.indicators import compute_theme_frame
from sd_data.ignition import (sss_series, ignition_v2_signals, dedupe_ignitions,
                              IG_V2_DEFAULTS)

DB_DEFAULT = '/home/hatch/workspace/sector-detect/sector_detect.db'
COST = 0.006


def build_all(con):
    mkt = pd.read_sql_query(
        "SELECT date, taiex_close FROM market_daily WHERE taiex_close IS NOT NULL", con)
    mkt = mkt.set_index('date')['taiex_close'].sort_index()
    all_dates = sorted(mkt.index)
    mkt_ret = mkt.pct_change()
    sectors = [(r[0], r[1]) for r in con.execute(
        "SELECT DISTINCT sector_id, sector_name FROM sector_map ORDER BY sector_id")]
    data, sss5 = {}, {}
    for si, (sid, sname) in enumerate(sectors):
        stocks = [r[0] for r in con.execute(
            "SELECT stock FROM sector_map WHERE sector_id=?", (sid,))]
        m = load_matrices(con, stocks, all_dates[0], all_dates[-1])
        bench = load_benchmark(con, all_dates[0], all_dates[-1])
        if m is None or bench is None or len(m['dates']) < 150:
            continue
        bench = bench.reindex(m['dates']).fillna(0)
        f = compute_theme_frame(m['prices'], m['flow_amt'], m['amounts'], bench)
        prov = con.execute(
            """SELECT COUNT(*) FROM price_adj a JOIN sector_map s ON a.stock=s.stock
               WHERE s.sector_id=?""", (sid,)).fetchone()[0] == 0
        data[sid] = {'frame': f, 'sname': sname, 'provisional': prov}
        sss5[sid] = sss_series(f)['sss'].rolling(5).mean()
        print(f'[{si+1}/{len(sectors)}] {sid} ok', flush=True)
    rankw = pd.DataFrame(sss5).rank(axis=1, ascending=False, method='min')
    return data, rankw, mkt_ret, all_dates


def fwd_excess(eq_index, mkt_ret_full, t, days):
    idx = list(eq_index.index)
    try:
        i = idx.index(t)
    except ValueError:
        return np.nan
    if i + days >= len(idx):
        return np.nan
    rs = eq_index.iloc[i + days] / eq_index.iloc[i] - 1
    mkt = mkt_ret_full.reindex(idx).fillna(0)
    rm = (1 + mkt).iloc[i + 1:i + days + 1].prod() - 1
    return float(rs - rm)


def calibrate(data, rankw, mkt_ret, cold_waves):
    """回傳 (best_x, cal_rows)。"""
    cal_rows = []
    for tx in (0.02, 0.03, 0.04, 0.05, 0.06):
        rec, leads, tot_trig = 0, [], 0
        det = []
        for wv in cold_waves:
            sid, start = wv['sector_id'], wv['start']
            d = data[sid]
            sig = ignition_v2_signals(d['frame'], rankw[sid], mkt_ret, thrust_x=tx)
            tot_trig += int(sig['ignite_raw'].sum())
            idx = list(sig.index)
            i0 = idx.index(start)
            lo, hi = idx[max(0, i0 - 10)], idx[min(len(idx) - 1, i0 + 10)]
            win = sig.loc[lo:hi]
            fired = win[win['ignite_raw']].index
            if len(fired) and idx.index(fired[0]) <= i0 + 3:
                rec += 1
                leads.append(idx.index(fired[0]) - i0)
                det.append({'wave': f"{sid}@{start}", 'fired': fired[0],
                            'lead': idx.index(fired[0]) - i0})
            else:
                det.append({'wave': f"{sid}@{start}", 'fired': None, 'lead': None})
        cal_rows.append({'thrust_x': tx, 'recall': rec, 'n': len(cold_waves),
                         'recall_rate': rec / len(cold_waves),
                         'median_lead': float(np.median(leads)) if leads else None,
                         'total_raw_triggers': tot_trig, 'detail': det})
    # 選 recall 最高；平手選總觸發少
    best = max(cal_rows, key=lambda r: (r['recall'], -r['total_raw_triggers']))
    return best, cal_rows


def backtest(data, rankw, mkt_ret, tx):
    events = []
    for sid, d in data.items():
        sig = ignition_v2_signals(d['frame'], rankw[sid], mkt_ret, thrust_x=tx)
        ign = dedupe_ignitions(sig['ignite_raw'], cooldown=IG_V2_DEFAULTS['cooldown'])
        frame = d['frame']
        for t in sig.index[ign]:
            events.append({
                'date': t, 'sector_id': sid, 'sector_name': d['sname'],
                'rank_5d_t': float(rankw[sid].loc[t]),
                'rank_5d_tm5': float(rankw[sid].shift(5).loc[t]),
                'thrust_3d': float(sig.loc[t, 'thrust_3d']),
                'confidence': int(sig.loc[t, 'confidence']),
                'money_confirmed': bool(sig.loc[t, 'money_confirmed']),
                'leader_led': bool(sig.loc[t, 'leader_led']),
                'fwd5': fwd_excess(frame['eq_index'], mkt_ret, t, 5),
                'fwd20': fwd_excess(frame['eq_index'], mkt_ret, t, 20),
                'provisional': d['provisional'],
            })
    ev = pd.DataFrame(events)
    f20 = ev['fwd20'].dropna()
    f5 = ev['fwd5'].dropna()
    # null 基線：全部 sector-day 的前視 20 日超額報酬
    nulls = []
    for sid, d in data.items():
        idx = list(d['frame'].index)
        for t in idx[130:-20:20]:  # 每 20 天抽樣一日，降計算量
            nulls.append(fwd_excess(d['frame']['eq_index'], mkt_ret, t, 20))
    nulls = pd.Series(nulls).dropna()
    stats = {
        'n_ignitions': len(ev),
        'n_with_fwd20': int(f20.count()),
        'median_fwd20': float(f20.median()) if len(f20) else None,
        'mean_fwd20': float(f20.mean()) if len(f20) else None,
        'hit_fwd20_pos': float((f20 > 0).mean()) if len(f20) else None,
        'strong_fwd20_gt5': float((f20 > 0.05).mean()) if len(f20) else None,
        'false_fwd20_lt_m3': float((f20 < -0.03).mean()) if len(f20) else None,
        'median_fwd20_costadj': float((f20 - COST).median()) if len(f20) else None,
        'median_fwd5': float(f5.median()) if len(f5) else None,
        'null_median_fwd20': float(nulls.median()) if len(nulls) else None,
        'null_hit_fwd20_pos': float((nulls > 0).mean()) if len(nulls) else None,
        'n_provisional_sectors': int(sum(1 for d in data.values() if d['provisional'])),
    }
    # 信心分級
    for c in (0, 1, 2):
        s = ev[ev['confidence'] == c]['fwd20'].dropna()
        stats[f'conf{c}_n'] = int(len(s))
        stats[f'conf{c}_median_fwd20'] = float(s.median()) if len(s) else None
        stats[f'conf{c}_hit'] = float((s > 0).mean()) if len(s) else None
    return ev, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DB_DEFAULT)
    ap.add_argument('--out', default='/home/hatch/workspace/sector-detect/ignition_report_v2.json')
    ns = ap.parse_args()
    con = sqlite3.connect(ns.db)
    print('building frames...', flush=True)
    data, rankw, mkt_ret, all_dates = build_all(con)
    cold_waves = json.load(open('/home/hatch/workspace/sector-detect/ignition_waves.json'))['waves']
    print(f'calibrating on {len(cold_waves)} cold-start waves...', flush=True)
    best, cal_rows = calibrate(data, rankw, mkt_ret, cold_waves)
    for r in cal_rows:
        print(f"  x={r['thrust_x']}: recall {r['recall']}/{r['n']} "
              f"({r['recall_rate']:.0%}) median_lead {r['median_lead']} "
              f"total_raw {r['total_raw_triggers']}", flush=True)
    print('BEST thrust_x =', best['thrust_x'], flush=True)
    print('backtesting...', flush=True)
    ev, stats = backtest(data, rankw, mkt_ret, best['thrust_x'])
    report = {
        'generated_at': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S'),
        'rule': 'IGNITE(t) = was_cold(t) & thrust_3d(t) > x & breadth(t) > 0.6; '
                'was_cold = rank_5d(t-5) > 19',
        'n_sectors': len(data), 'n_trading_days': len(all_dates),
        'date_range': [all_dates[0], all_dates[-1]],
        'n_cold_waves': len(cold_waves),
        'calibration': cal_rows,
        'best_thrust_x': best['thrust_x'],
        'backtest_stats': stats,
        'events': ev.to_dict('records'),
        'limitations': [
            '除權息還原覆蓋不全，報酬結論為 provisional（H1 §7）。',
            '冷啟動波段集由 wave_scan 波段定義衍生（60日超額>75分位），定義依賴。',
            'thrust_x 在 26 波段上選擇，有過擬合風險；須樣本外驗證。',
            '前視報酬未計漲跌停／流動性限制；成本僅 0.6% 估算。',
        ],
    }
    json.dump(report, open(ns.out, 'w'), ensure_ascii=False, indent=1, default=str)
    print('=== BACKTEST ===')
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print('wrote', ns.out)
    con.close()


if __name__ == '__main__':
    main()
