"""點火規則 v1：冷啟動反轉偵測。

動機：SSS 是 120 日標準化後的平滑強度排名；排名墊底但突然點火的族群，
要等 5 日平均 SSS 爬升才會被看見。點火規則專門捕捉「冷＋拐點」。

定義（全因果，只用 ≤ t 資料）：
  IGNITE(t) = COLD(t) & FLOW_BURST(t) & LEADER(t) & THRUST(t)
  - COLD(t)：SSS 5 日平均排名在後半（rank > cold_rank_cut，預設 19/38）
  - FLOW_BURST(t)：D2 flow_rising（5日總流入 > 120日 90分位 × 集中度確認）
  - LEADER(t)：D3 leader_accelerating（領頭 RS 加速在 120 日前 20%）
  - THRUST(t)：族群等權指數 thrust_win 日超額報酬（vs 加權）> thrust_x

去重：同一族群點火後 cooldown 個交易日內不重複計。

與 S_early 的區別：S_early = flow_rising & leader_accelerating（不限排名、
不限價格）；點火多加了「冷排名」與「價格動能確認」，專門抓冷啟動。
"""
import numpy as np
import pandas as pd

IG_DEFAULTS = {
    'cold_rank_cut': 19,   # SSS 5日平均排名 > 此值視為冷（38 族群之後半）
    'thrust_x': 0.02,      # 動能確認門檻（待校準）
    'thrust_win': 3,       # 動能確認窗口（日，待校準）
    'cooldown': 20,        # 同族群去重冷卻（交易日）
    'z_win': 120,          # SSS 標準化窗口（與 daily_scan 一致）
    'z_min': 60,
}


def sss_series(frame, z_win=120, z_min=60):
    """與 daily_scan.compute_daily_sss 相同的 z-score 邏輯，輸出全序列。"""
    f = frame
    out = pd.DataFrame(index=f.index)
    for col, zc in (('hhi', 'z_hhi'), ('inflow_tot_5d', 'z_tot5')):
        r = f[col].rolling(z_win, min_periods=z_min)
        out[zc] = ((f[col] - r.mean()) / r.std()).fillna(0)
    d1 = f['co_move'].fillna(0)
    d2 = out['z_hhi'] + out['z_tot5']
    d3 = f['leader_accel'].fillna(0)
    for d, zc in ((d1, 'z1'), (d2, 'z2'), (d3, 'z3')):
        r = d.rolling(z_win, min_periods=z_min)
        out[zc] = ((d - r.mean()) / r.std()).fillna(0)
    out['sss'] = out['z1'] + out['z2'] + out['z3']
    return out


def ignition_signals(frame, rank_5d, market_returns, thrust_x=0.02,
                     cold_rank_cut=19, thrust_win=3):
    """回傳 DataFrame：cold/flow_burst/leader/thrust/thrust_3d/ignite_raw。

    frame: compute_theme_frame 輸出；rank_5d: 該族群 SSS 5日平均的橫斷面排名
    （1=最強）；market_returns: 大盤日報酬 Series（與 frame 同 index）。
    """
    idx = frame.index
    mkt_idx = (1 + market_returns.reindex(idx).fillna(0)).cumprod()
    sec_idx = frame['eq_index']
    thrust_3d = (sec_idx / sec_idx.shift(thrust_win) - 1) - \
                (mkt_idx / mkt_idx.shift(thrust_win) - 1)
    cold = rank_5d.reindex(idx).fillna(99) > cold_rank_cut
    out = pd.DataFrame(index=idx)
    out['cold'] = cold.fillna(False)
    out['flow_burst'] = frame['flow_rising'].fillna(False)
    out['leader'] = frame['leader_accelerating'].fillna(False)
    out['thrust_3d'] = thrust_3d
    out['thrust'] = (thrust_3d > thrust_x).fillna(False)
    out['ignite_raw'] = out['cold'] & out['flow_burst'] & out['leader'] & out['thrust']
    return out.fillna(False)


def dedupe_ignitions(ignite_raw, cooldown=20):
    """同一族群點火後冷卻期內不重複計。回傳去重後的布林 Series。"""
    s = ignite_raw.fillna(False).astype(bool)
    out = pd.Series(False, index=s.index)
    last = -10 ** 9
    dates = list(s.index)
    for i, d in enumerate(dates):
        if s.iloc[i] and i - last >= cooldown:
            out.iloc[i] = True
            last = i
    return out


# ---------------------------------------------------------------------------
# v2（2026-10-04）：實證重設計
# 26 個冷啟動波段的特徵刻畫顯示：thrust_3d 與 breadth 在 100% 波段的點火窗內
# 出現強訊號；flow_rising 僅 46%、leader_accelerating 僅 65%。
# 因此 v2 以「價格動能＋廣度」為主觸發，資金／領頭改為信心加權（tag），
# 且 COLD 改為 t-5 日排名（同日排名與資金暴增互斥）。
# ---------------------------------------------------------------------------

IG_V2_DEFAULTS = {
    'cold_rank_cut': 19,   # t-5 日 SSS 5日平均排名 > 此值視為冷
    'cold_lag': 5,         # 冷確認回看（交易日）
    'thrust_x': 0.04,      # 3 日超額報酬門檻（待校準）
    'thrust_win': 3,
    'breadth_x': 0.6,      # 廣度門檻
    'cooldown': 20,
}


def ignition_v2_signals(frame, rank_5d, market_returns, thrust_x=0.04,
                        cold_rank_cut=19, cold_lag=5, breadth_x=0.6, thrust_win=3):
    """v2 點火訊號。回傳 DataFrame（含 was_cold/thrust/breadth_ok/ignite_raw/
    money_confirmed/leader_led/thrust_3d）。

    frame: compute_theme_frame 輸出；rank_5d: SSS 5日平均橫斷面排名（1=最強）；
    market_returns: 大盤日報酬 Series。
    全因果：t 日訊號只用 ≤ t 資料（was_cold 用 t-cold_lag 排名）。
    """
    idx = frame.index
    mkt_idx = (1 + market_returns.reindex(idx).fillna(0)).cumprod()
    sec_idx = frame['eq_index']
    thrust = (sec_idx / sec_idx.shift(thrust_win) - 1) - \
             (mkt_idx / mkt_idx.shift(thrust_win) - 1)
    was_cold = rank_5d.reindex(idx).shift(cold_lag).fillna(99) > cold_rank_cut
    out = pd.DataFrame(index=idx)
    out['was_cold'] = was_cold.fillna(False)
    out['thrust_3d'] = thrust
    out['thrust'] = (thrust > thrust_x).fillna(False)
    out['breadth_ok'] = (frame['breadth'] > breadth_x).fillna(False)
    out['money_confirmed'] = frame['flow_rising'].fillna(False)
    out['leader_led'] = frame['leader_accelerating'].fillna(False)
    out['ignite_raw'] = out['was_cold'] & out['thrust'] & out['breadth_ok']
    # 信心分級：money/leader 加權（僅標記，不影響觸發）
    out['confidence'] = (out['money_confirmed'].astype(int)
                         + out['leader_led'].astype(int)).astype(int)
    return out.fillna(False)
