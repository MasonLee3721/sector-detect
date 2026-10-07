"""SectorDetect 每日掃描：更新最新交易日資料 + 計算 38 族群 SSS 排名。

用法：
    python3 src/sd_data/daily_scan.py [--db PATH] [--out DIR]

流程：
1. 找出 DB 最後日期，往後補到最新交易日（呼叫 backfill，冪等）。
2. 對 38 族群計算當日 SSS（D1/D2/D3 各自 120 日標準化後加總）。
3. 輸出 JSON + 文字摘要。
"""
import sqlite3
import sys
import json
import argparse
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, '/home/hatch/workspace/sector-detect/src')
import pandas as pd
import numpy as np
from sd_data.backfill import backfill
from sd_data.calibrate import load_matrices, load_benchmark
from sd_data.indicators import compute_theme_frame
from sd_data.ignition import sss_series, ignition_v2_signals, IG_V2_DEFAULTS

DB_DEFAULT = '/home/hatch/workspace/sector-detect/sector_detect.db'
IGNITION_STATE = '/home/hatch/workspace/sector-detect/ignition_state.json'
# 點火規則 v1（2026-10-04 校準）：IGNITE = was_cold(t-5排名後半) & 3日超額>2% & 廣度>0.6
# 26 個冷啟動波段召回率 85%，中位領先波段起點 2.5 天；屬召回型 watchlist，非買入訊號。
THRUST_X = 0.02


def update_to_latest(db_path):
    """補資料到最新交易日。回傳補的天數。"""
    con = sqlite3.connect(db_path)
    last = con.execute("SELECT MAX(date) FROM price_daily").fetchone()[0]
    con.close()
    today = date.today().strftime('%Y%m%d')
    if last >= today:
        return 0
    # backfill 從 last+1 補到 today（內部會跳過休市日）
    d = (datetime.strptime(last, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
    backfill(d, today, db_path)
    con = sqlite3.connect(db_path)
    new_last = con.execute("SELECT MAX(date) FROM price_daily").fetchone()[0]
    con.close()
    return 1 if new_last > last else 0


def compute_daily_sss(con):
    """計算 38 族群最新一日的 SSS。回傳 (rows, latest, ignitions)。

    rows: list of dict（按 5 日平均 SSS 排序，含 rank/level）。
    ignitions: 當日點火候選（冷啟動）list，已經 20 日去重。
    """
    sectors = [(r[0], r[1]) for r in con.execute(
        "SELECT DISTINCT sector_id, sector_name FROM sector_map ORDER BY sector_id")]
    mkt = pd.read_sql_query(
        "SELECT date, taiex_close FROM market_daily WHERE taiex_close IS NOT NULL", con)
    mkt = mkt.set_index('date')['taiex_close'].sort_index()
    all_dates = sorted(mkt.index)
    latest = all_dates[-1]
    mkt_ret = mkt.pct_change()

    rows = []
    sss5, frames = {}, {}
    for sid, sname in sectors:
        stocks = [r[0] for r in con.execute(
            "SELECT stock FROM sector_map WHERE sector_id=?", (sid,))]
        m = load_matrices(con, stocks, all_dates[0], latest)
        bench = load_benchmark(con, all_dates[0], latest)
        if m is None or bench is None:
            continue
        bench = bench.reindex(m['dates']).fillna(0)
        f = compute_theme_frame(m['prices'], m['flow_amt'], m['amounts'], bench)
        frames[sid] = f
        sss5[sid] = sss_series(f)['sss'].rolling(5).mean()
        for col, zc in (('hhi', 'z_hhi'), ('inflow_tot_5d', 'z_tot5')):
            r = f[col].rolling(120, min_periods=60)
            f[zc] = ((f[col] - r.mean()) / r.std()).fillna(0)
        d1 = f['co_move'].fillna(0)
        d2 = f['z_hhi'] + f['z_tot5']
        d3 = f['leader_accel'].fillna(0)
        z1 = (d1 - d1.rolling(120, min_periods=60).mean()) / d1.rolling(120, min_periods=60).std()
        z2 = (d2 - d2.rolling(120, min_periods=60).mean()) / d2.rolling(120, min_periods=60).std()
        z3 = (d3 - d3.rolling(120, min_periods=60).mean()) / d3.rolling(120, min_periods=60).std()
        sss = z1.fillna(0) + z2.fillna(0) + z3.fillna(0)
        rows.append({
            'sector_id': sid, 'sector_name': sname,
            'sss_today': float(sss.iloc[-1]),
            'sss_5d': float(sss.tail(5).mean()),
            'z_d1': float(z1.fillna(0).iloc[-1]),
            'z_d2': float(z2.fillna(0).iloc[-1]),
            'z_d3': float(z3.fillna(0).iloc[-1]),
        })
    rows.sort(key=lambda x: -x['sss_5d'])
    for i, r in enumerate(rows, 1):
        r['rank'] = i
        s = r['sss_5d']
        r['level'] = '強' if s > 3 else ('中' if s > 1 else ('弱' if s < -1 else '平'))

    # ---- 點火（冷啟動）偵測 ----
    rankw = pd.DataFrame(sss5).rank(axis=1, ascending=False, method='min')
    name_of = {r['sector_id']: r['sector_name'] for r in rows}
    ignitions = compute_ignitions(frames, rankw, mkt_ret, latest, all_dates, name_of)
    return rows, latest, ignitions


def compute_ignitions(frames, rankw, mkt_ret, latest, all_dates, name_of):
    """當日點火候選。經 ignition_state.json 做 20 交易日去重。"""
    import json as _json
    import os
    state = {}
    if os.path.exists(IGNITION_STATE):
        try:
            state = _json.load(open(IGNITION_STATE))
        except Exception:
            state = {}
    out = []
    for sid, f in frames.items():
        try:
            if sid not in rankw.columns or latest not in rankw[sid].index:
                continue
            sig = ignition_v2_signals(f, rankw[sid], mkt_ret, thrust_x=THRUST_X,
                                      cold_rank_cut=IG_V2_DEFAULTS['cold_rank_cut'])
            if not bool(sig['ignite_raw'].iloc[-1]):
                continue
            # 去重：20 交易日內點過就跳過；但同一天重跑要保留（冪等）
            last = state.get(sid)
            if last and last in all_dates and last != latest:
                if all_dates.index(latest) - all_dates.index(last) < IG_V2_DEFAULTS['cooldown']:
                    continue
            out.append({
                'sector_id': sid,
                'sector_name': name_of.get(sid, sid),
                'rank_5d': int(rankw[sid].loc[latest]),
                'thrust_3d': round(float(sig['thrust_3d'].iloc[-1]), 4),
                'breadth': round(float(f['breadth'].iloc[-1]), 2),
                'money_confirmed': bool(sig['money_confirmed'].iloc[-1]),
                'leader_led': bool(sig['leader_led'].iloc[-1]),
            })
            state[sid] = latest
        except Exception:
            continue
    # 只有實際輸出點火才寫回 state（避免空跑覆蓋）
    if out:
        _json.dump(state, open(IGNITION_STATE, 'w'), ensure_ascii=False, indent=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DB_DEFAULT)
    ap.add_argument('--out', default='/home/hatch/workspace/sector-detect/reports')
    ap.add_argument('--no-update', action='store_true', help='跳過資料更新，只算分數')
    ns = ap.parse_args()

    if not ns.no_update:
        n = update_to_latest(ns.db)
        print(f'data updated: {n} new day(s)', flush=True)

    con = sqlite3.connect(ns.db)
    rows, latest, ignitions = compute_daily_sss(con)

    outdir = Path(ns.out) / latest
    outdir.mkdir(parents=True, exist_ok=True)
    json.dump({'date': latest, 'sectors': rows, 'ignitions': ignitions,
               'ignition_params': {'thrust_x': THRUST_X,
                                   'cold_rank_cut': IG_V2_DEFAULTS['cold_rank_cut'],
                                   'cooldown': IG_V2_DEFAULTS['cooldown']}},
              open(outdir / 'sss.json', 'w'), ensure_ascii=False, indent=1)

    # 文字摘要
    lines = [f'SectorDetect 族群強度掃描 {latest}（38族群 SSS 排名）', '']
    lines.append(f"{'排名':<4s} {'族群':22s} {'5日SSS':>7s} {'今日':>7s} {'強度':<4s} D1/D2/D3")
    for r in rows:
        lines.append(f"{r['rank']:<4d} {r['sector_name']:22s} {r['sss_5d']:+7.2f} "
                     f"{r['sss_today']:+7.2f} {r['level']:<4s} "
                     f"{r['z_d1']:+.1f}/{r['z_d2']:+.1f}/{r['z_d3']:+.1f}")

    # 點火候選（冷啟動）：排名後半＋3日超額>2%＋廣度>0.6，20日去重
    lines += ['', '─' * 60, '點火候選（冷啟動，召回型 watchlist，非買入訊號）', '']
    if ignitions:
        for g in ignitions:
            tags = []
            if g['money_confirmed']:
                tags.append('資金確認')
            if g['leader_led']:
                tags.append('領頭加速')
            tag = '＋'.join(tags) if tags else '無加權'
            lines.append(f"  [點火] {g['sector_name'][:20]:20s} 排名#{g['rank_5d']} "
                         f"3日超額 {g['thrust_3d']:+.2%} 廣度 {g['breadth']:.2f} [{tag}]")
    else:
        lines.append('  （今日無點火）')

    # 昨日排名 vs 今日實際對帳
    lines += ['', '─' * 60, '昨日排名 vs 今日實際（紙上驗證）', '']
    recon_data = None
    try:
        recon_lines, recon_data = reconcile_yesterday(con, ns.out, latest, rows)
        lines += recon_lines
    except Exception as e:
        lines.append(f'對帳跳過：{e}')
    con.close()

    summary = '\n'.join(lines)
    (outdir / 'sss.txt').write_text(summary, encoding='utf-8')
    # 把對帳結構化資料補進 sss.json（供 HTML 報表用）
    if recon_data:
        sss_path = outdir / 'sss.json'
        jd = json.load(open(sss_path, encoding='utf-8'))
        jd['reconciliation'] = recon_data
        json.dump(jd, open(sss_path, 'w'), ensure_ascii=False, indent=1)
    print(summary)
    print(f'\nwritten to {outdir}')


def reconcile_yesterday(con, out_dir, latest, rows):
    """讀昨日 sss.json 的排名，對比今日各族群實際報酬。

    回傳 (文字行 list, 結構化 dict)。"""
    import json as _json
    out_dir = Path(out_dir)
    # 找最近一個有 sss.json 的歷史日期
    prev = None
    for d in sorted(out_dir.iterdir(), reverse=True):
        if d.name < latest and (d / 'sss.json').exists():
            prev = d.name
            break
    if not prev:
        return ['（無昨日排名資料，跳過對帳）'], None
    ydata = _json.load(open(out_dir / prev / 'sss.json'))
    yrank = {s['sector_name']: (s['rank'], s['sss_5d']) for s in ydata['sectors']}

    # 今日各族群等權報酬
    px = pd.read_sql_query(
        """SELECT p.date AS date, p.stock AS stock, COALESCE(a.close_adj, p.close) AS px
           FROM price_daily p LEFT JOIN price_adj a ON a.date=p.date AND a.stock=p.stock
           WHERE p.date IN (?, ?)""", con, params=(prev, latest))
    piv = px.pivot(index='date', columns='stock', values='px')
    day_ret = piv.pct_change().loc[latest]
    mkt = pd.read_sql_query(
        "SELECT date, taiex_close FROM market_daily WHERE date IN (?, ?)", con, params=(prev, latest))
    mkt = mkt.set_index('date')['taiex_close']
    mkt_ret = float(mkt.loc[latest] / mkt.loc[prev] - 1)

    lines = [f'昨日({prev})排名 → 今日({latest})實際，大盤 {mkt_ret:+.2%}：', '']
    # 前 8 與後 3
    ordered = sorted(rows, key=lambda r: yrank.get(r['sector_name'], (99, 0))[0])
    picks = ordered[:8] + ordered[-3:]
    hits = 0
    items = []
    for r in picks:
        yr, ys = yrank.get(r['sector_name'], (None, None))
        if yr is None:
            continue
        stocks = [x[0] for x in con.execute(
            "SELECT stock FROM sector_map WHERE sector_id=?", (r['sector_id'],))]
        sr = float(day_ret[day_ret.index.isin(stocks)].mean(skipna=True))
        ex = sr - mkt_ret
        mark = '✓' if (yr <= 8 and ex > 0) or (yr > 30 and ex < 0) else ('✗' if abs(ex) > 0.02 else '~')
        if mark == '✓':
            hits += 1
        tag = '前段' if yr <= 8 else '後段'
        lines.append(f'  [{tag}#{yr}] {r["sector_name"][:18]:18s} 昨日SSS {ys:+.2f} → 今日 {sr:+.2%} (超額 {ex:+.2%}) {mark}')
        items.append({'sector_name': r['sector_name'], 'tag': tag, 'y_rank': yr,
                      'y_sss': round(ys, 2), 'ret': round(sr, 4),
                      'excess': round(ex, 4), 'mark': mark})
    lines.append('')
    lines.append(f'命中：{hits}/{len(picks)}')
    data = {'prev_date': prev, 'date': latest, 'mkt_ret': round(mkt_ret, 4),
            'hits': hits, 'total': len(items), 'items': items}
    return lines, data


if __name__ == '__main__':
    main()
