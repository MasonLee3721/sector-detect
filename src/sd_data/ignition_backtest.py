"""點火規則 v1 校準＋回測。

用法：
    python3 src/sd_data/ignition_backtest.py [--db PATH] [--out PATH]

流程：
1. 38 族群全歷史建 frame（compute_theme_frame）＋ SSS 序列。
2. 橫斷面 SSS 5 日平均排名。
3. 校準：在三個錨點波段起點（記憶體 20250918、光通訊 20260129、被動元件 20250924）
   上網格搜尋 thrust_x × thrust_win × cold_rank_cut，選「三波全中 ±3 天、
   且全歷史總點火數最少」的組合。
4. 回測：全歷史去重點火 → 前視 5/20 日超額報酬、命中率、與 S_early 重疊、
   86 波段召回率。
5. 輸出 ignition_report.json。

注意：除權息還原覆蓋不全（price_adj 部分），報酬結論標示 provisional。
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, '/home/hatch/workspace/sector-detect/src')
from sd_data.calibrate import load_matrices, load_benchmark
from sd_data.indicators import compute_theme_frame
from sd_data.ignition import sss_series, ignition_signals, dedupe_ignitions, IG_DEFAULTS

DB_DEFAULT = '/home/hatch/workspace/sector-detect/sector_detect.db'
ANCHORS = {  # sector_id -> wave start（點火錨點）
    'SEC_06': '20250918',   # 記憶體，波段峰值超額 +104.1%
    'SEC_10': '20260129',   # 光通訊，波段峰值超額 +147.3%
    'SEC_12': '20250924',   # 被動元件，波段峰值超額 +36.9%
}
COST = 0.006  # 來回成本（與 H1b 一致）


def build_all(con):
    """回傳 dict: sid -> dict(frame, sss, dates, sname, provisional)。"""
    sectors = [(r[0], r[1]) for r in con.execute(
        "SELECT DISTINCT sector_id, sector_name FROM sector_map ORDER BY sector_id")]
    mkt = pd.read_sql_query(
        "SELECT date, taiex_close FROM market_daily WHERE taiex_close IS NOT NULL", con)
    mkt = mkt.set_index('date')['taiex_close'].sort_index()
    all_dates = sorted(mkt.index)
    mkt_ret_full = mkt.pct_change()
    out = {}
    for si, (sid, sname) in enumerate(sectors):
        stocks = [r[0] for r in con.execute(
            "SELECT stock FROM sector_map WHERE sector_id=?", (sid,))]
        m = load_matrices(con, stocks, all_dates[0], all_dates[-1])
        bench = load_benchmark(con, all_dates[0], all_dates[-1])
        if m is None or bench is None or len(m['dates']) < 150:
            continue
        bench = bench.reindex(m['dates']).fillna(0)
        frame = compute_theme_frame(m['prices'], m['flow_amt'], m['amounts'], bench)
        sss = sss_series(frame)
        # provisional：該族群完全無 price_adj 覆蓋
        prov = con.execute(
            """SELECT COUNT(*) FROM price_adj a JOIN sector_map s ON a.stock=s.stock
               WHERE s.sector_id=?""", (sid,)).fetchone()[0] == 0
        out[sid] = {'frame': frame, 'sss': sss, 'dates': m['dates'],
                    'sname': sname, 'provisional': prov, 'stocks': stocks}
        print(f'[{si+1}/{len(sectors)}] {sid} {sname} frame ok', flush=True)
    return out, mkt_ret_full, all_dates


def cross_rank(data):
    """data: sid -> {'sss'}；回傳 DataFrame(date×sid) SSS 5日平均橫斷面排名（1=最強）。"""
    wide = pd.DataFrame({sid: d['sss']['sss'].rolling(5).mean() for sid, d in data.items()})
    return wide.rank(axis=1, ascending=False, method='min')


def fwd_excess(eq_index, mkt_ret_full, t, days):
    """t 日後 days 個交易日的超額報酬（sector eq − market）。"""
    idx = list(eq_index.index)
    try:
        i = idx.index(t)
    except ValueError:
        return np.nan
    if i + days >= len(idx):
        return np.nan
    t2 = idx[i + days]
    rs = eq_index.iloc[i + days] / eq_index.iloc[i] - 1
    mkt = mkt_ret_full.reindex(idx).fillna(0)
    rm = (1 + mkt).iloc[i + 1:i + days + 1].prod() - 1
    return float(rs - rm)


def calibrate(data, rank_wide, mkt_ret_full):
    """網格搜尋。回傳 (best_params, rows)。"""
    grid = []
    for tx in (0.01, 0.015, 0.02, 0.025, 0.03):
        for tw in (3, 5):
            for cc in (15, 19):
                grid.append((tx, tw, cc))
    rows = []
    for tx, tw, cc in grid:
        hits, details = 0, {}
        total_raw = 0
        for sid, start in ANCHORS.items():
            d = data[sid]
            sig = ignition_signals(d['frame'], rank_wide[sid], mkt_ret_full,
                                   thrust_x=tx, cold_rank_cut=cc, thrust_win=tw)
            total_raw += int(sig['ignite_raw'].sum())
            idx = list(sig.index)
            i0 = idx.index(start)
            lo, hi = idx[max(0, i0 - 10)], idx[min(len(idx) - 1, i0 + 10)]
            win = sig.loc[lo:hi]
            fired = win[win['ignite_raw']].index
            if len(fired):
                dt = (idx.index(fired[0]) - i0)
                hit = abs(dt) <= 3
                hits += hit
                details[sid] = {'fired': fired[0], 'delta_days': dt, 'hit': bool(hit)}
            else:
                details[sid] = {'fired': None, 'delta_days': None, 'hit': False}
        rows.append({'thrust_x': tx, 'thrust_win': tw, 'cold_rank_cut': cc,
                     'hits': hits, 'total_raw': total_raw, 'details': details})
    cands = [r for r in rows if r['hits'] == 3]
    best = min(cands, key=lambda r: r['total_raw']) if cands else \
        max(rows, key=lambda r: (r['hits'], -r['total_raw']))
    return best, rows


def backtest(data, rank_wide, mkt_ret_full, params):
    """全歷史去重點火，回傳事件表與統計。"""
    events = []
    for sid, d in data.items():
        sig = ignition_signals(d['frame'], rank_wide[sid], mkt_ret_full,
                               thrust_x=params['thrust_x'],
                               cold_rank_cut=params['cold_rank_cut'],
                               thrust_win=params['thrust_win'])
        ign = dedupe_ignitions(sig['ignite_raw'], cooldown=IG_DEFAULTS['cooldown'])
        frame = d['frame']
        for t in sig.index[ign]:
            e = {'date': t, 'sector_id': sid, 'sector_name': d['sname'],
                 'rank_5d': float(rank_wide[sid].loc[t]),
                 'thrust_3d': float(sig.loc[t, 'thrust_3d']),
                 'fwd5': fwd_excess(frame['eq_index'], mkt_ret_full, t, 5),
                 'fwd20': fwd_excess(frame['eq_index'], mkt_ret_full, t, 20),
                 'provisional': d['provisional']}
            # 與 S_early 重疊（±5 天內有 S_early）
            idx = list(frame.index)
            i = idx.index(t)
            lo, hi = max(0, i - 5), min(len(idx), i + 6)
            e['s_early_overlap'] = bool(frame['S_early'].iloc[lo:hi].any())
            events.append(e)
    ev = pd.DataFrame(events)
    f20 = ev['fwd20'].dropna()
    stats = {
        'n_ignitions': len(ev),
        'n_with_fwd20': int(f20.count()),
        'median_fwd20': float(f20.median()) if len(f20) else None,
        'mean_fwd20': float(f20.mean()) if len(f20) else None,
        'hit_rate_fwd20_pos': float((f20 > 0).mean()) if len(f20) else None,
        'strong_hit_fwd20_gt5pct': float((f20 > 0.05).mean()) if len(f20) else None,
        'false_fwd20_lt_neg3pct': float((f20 < -0.03).mean()) if len(f20) else None,
        'median_fwd20_costadj': float((f20 - COST).median()) if len(f20) else None,
        'cold_property_pct': float((ev['rank_5d'] > params['cold_rank_cut']).mean()) * 100,
        's_early_overlap_pct': float(ev['s_early_overlap'].mean()) * 100,
        'n_provisional_sectors': int(sum(1 for d in data.values() if d['provisional'])),
    }
    return ev, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DB_DEFAULT)
    ap.add_argument('--out', default='/home/hatch/workspace/sector-detect/ignition_report.json')
    ns = ap.parse_args()

    con = sqlite3.connect(ns.db)
    print('building frames...', flush=True)
    data, mkt_ret_full, all_dates = build_all(con)

    print('cross-sectional ranks...', flush=True)
    rank_wide = cross_rank(data)

    print('calibrating...', flush=True)
    best, grid_rows = calibrate(data, rank_wide, mkt_ret_full)
    print('BEST:', json.dumps({k: v for k, v in best.items() if k != 'details'},
                              ensure_ascii=False), flush=True)

    print('backtesting...', flush=True)
    ev, stats = backtest(data, rank_wide, mkt_ret_full, best)

    # 波段召回率（86 波段）
    waves = json.load(open('/home/hatch/workspace/sector-detect/wave_scan.json'))['waves']
    fired_by_sid = {}
    for sid, d in data.items():
        sig = ignition_signals(d['frame'], rank_wide[sid], mkt_ret_full,
                               thrust_x=best['thrust_x'],
                               cold_rank_cut=best['cold_rank_cut'],
                               thrust_win=best['thrust_win'])
        ign = dedupe_ignitions(sig['ignite_raw'], cooldown=IG_DEFAULTS['cooldown'])
        fired_by_sid[sid] = set(sig.index[ign])
    rec_hit, rec_tot = 0, 0
    for x in waves:
        sid, start = x['sector_id'], x['start']
        if sid not in fired_by_sid or sid not in data:
            continue
        idx = list(data[sid]['frame'].index)
        if start not in idx:
            continue
        i0 = idx.index(start)
        lo, hi = idx[max(0, i0 - 10)], idx[min(len(idx) - 1, i0 + 6)]
        window = set(idx[max(0, i0 - 10):min(len(idx), i0 + 6)])
        rec_tot += 1
        if fired_by_sid[sid] & window:
            rec_hit += 1

    report = {
        'generated_at': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S'),
        'db': ns.db,
        'n_sectors': len(data),
        'n_trading_days': len(all_dates),
        'date_range': [all_dates[0], all_dates[-1]],
        'anchors': ANCHORS,
        'best_params': {k: v for k, v in best.items() if k != 'details'},
        'calibration_detail': best['details'],
        'calibration_grid': [{k: v for k, v in r.items() if k != 'details'} for r in grid_rows],
        'backtest_stats': stats,
        'wave_recall': {'hit': rec_hit, 'total': rec_tot,
                        'rate': rec_hit / rec_tot if rec_tot else None},
        'events': ev.to_dict('records'),
        'limitations': [
            '除權息還原覆蓋不全（price_adj 19360 筆），報酬結論為 provisional（H1 §7）。',
            '錨點波段僅 3 個，閾值選擇自由度有限；須經樣本外驗證。',
            '前視報酬未計漲跌停／流動性限制；成本僅用 0.6% 來回估算。',
        ],
    }
    json.dump(report, open(ns.out, 'w'), ensure_ascii=False, indent=1, default=str)
    print('=== BACKTEST ===')
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"wave recall: {rec_hit}/{rec_tot}")
    print(f'written to {ns.out}')
    con.close()


if __name__ == '__main__':
    main()
