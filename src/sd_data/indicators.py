"""族群性三維度指標（D1/D2/D3）＋分層訊號。

公式依據 docs/H1_可證偽假設規格_v0.1.md。所有閾值皆為待校準起點。
輸入皆為因果（causal）計算：t 日的指標只用 ≤ t 的資料，無未來函數。

D1 同動性：橫斷面報酬標準差 σ(t)、20 日均 σ̄(t)、上漲家數比 B(t)、等權指數 I(t)
D2 資金集中度：5 日法人淨流入（金額）之 HHI、CR3、正流入家數比
D3 領先落後：動態領頭股（當日 5 日法人資金淨流入前 N，僅正流入）之相對強度斜率。
  刻意不用動能選股，避免 RS 機械性向上偏誤；閾值採歷史相對分位（自我校準）。
"""
import numpy as np
import pandas as pd

PARAMS = {
    'disp_window': 20,       # σ̄ 移動平均窗口
    'disp_drop_pct': 0.25,   # 自高位下降比例視為收斂（待校準）
    'disp_lookback': 60,     # 「高位」回看窗口（待校準）
    'breadth_thresh': 0.6,   # B(t) 門檻
    'flow_fast': 5,          # 資金變化觀察窗口（日）：快
    'flow_slow': 20,         # 資金水位窗口（日）：穩；合成實驗顯示 5 日 HHI 訊噪比不足
    'hhi_hist': 120,         # HHI 歷史分位回看（日）
    'hhi_pct': 0.85,         # HHI 高位分位（待校準）
    'leader_n': 3,           # 領頭股檔數
    'mom_window': 20,        # 動能窗口（日）
    'rs_window': 20,         # RS 計算窗口（日）
    'accel_window': 5,       # RS 加速觀察窗口（日）
    'ma_short': 20, 'ma_long': 60,
    'vol_mult': 1.5,         # S_confirm 成交額倍數（待校準）
    'early_lookback': 20,    # S_mid 回看 S_early 的窗口（日）
}


def daily_returns(prices):
    """prices: DataFrame(date×stock) 調整後收盤 → 日報酬。"""
    return prices.pct_change()


def d1_metrics(returns, p=PARAMS):
    """D1 同動性。returns: DataFrame(date×stock) 日報酬。

    設計（v0.3）：「收斂」不用離散度絕對值（共同因子出現時離散度不一定下降，
    取決於 loading 差異），改用共動比率 co_move = 1 − cs_disp/ts_vol：
    完全同動→1，各自獨立→0。族群性 = 共動比率上升。
    """
    cs_disp = returns.std(axis=1, skipna=True)
    ts_vol = returns.rolling(p['disp_window'], min_periods=p['disp_window']).std().mean(axis=1)
    co_move = (1 - cs_disp / ts_vol.replace(0, np.nan)).clip(-1, 1)
    co_move = co_move.fillna(0)
    co_q = co_move.rolling(60, min_periods=60).quantile(0.75)
    converging = co_move > co_q
    breadth = (returns > 0).sum(axis=1) / returns.notna().sum(axis=1)
    eq_index = (1 + returns.mean(axis=1, skipna=True)).cumprod()
    return pd.DataFrame({
        'cs_disp': cs_disp, 'ts_vol': ts_vol, 'co_move': co_move,
        'breadth': breadth, 'eq_index': eq_index, 'converging': converging.fillna(False),
    })


def d2_metrics(flow_amt, p=PARAMS):
    """D2 資金集中度。flow_amt: DataFrame(date×stock) 日法人淨流入（金額，可正負）。

    設計（v0.3，合成實驗結論）：
    - 「錢進來了」用 5 日總流入的 90 分位偵測（快、乾淨）；
    - 「錢集中」用 20 日 HHI 超過歷史中位數確認（穩）；
    - 兩者相乘：沒有總流入暴增時，集中度高可能是正流入家數少的虛胖。
    5 日 HHI 本身訊噪比不足，不直接用。
    """
    f5 = flow_amt.rolling(p['flow_fast'], min_periods=p['flow_fast']).sum()
    f20 = flow_amt.rolling(p['flow_slow'], min_periods=p['flow_slow']).sum()

    def _hhi(fsum):
        pos = fsum.clip(lower=0)
        tot = pos.sum(axis=1)
        share = pos.div(tot.replace(0, np.nan), axis=0).fillna(0)
        return (share ** 2).sum(axis=1), tot

    hhi20, tot20 = _hhi(f20)
    _, tot5 = _hhi(f5)
    cr3 = f20.clip(lower=0).apply(
        lambda r: r.nlargest(3).sum() / r.sum() if r.sum() > 0 else 0, axis=1)
    pos_ratio = (f20 > 0).sum(axis=1) / f20.notna().sum(axis=1)

    hist = p['hhi_hist']
    surge = tot5 > tot5.rolling(hist, min_periods=60).quantile(0.90)
    conc = hhi20 > hhi20.rolling(hist, min_periods=60).quantile(0.50)
    hhi_chg = hhi20.diff(p['flow_fast'])
    accel = hhi_chg > hhi_chg.rolling(60, min_periods=60).std() * 1.5
    rising = (surge & (conc | accel.fillna(False))).fillna(False)
    return pd.DataFrame({
        'hhi': hhi20, 'cr3': cr3, 'pos_ratio': pos_ratio,
        'inflow_tot': tot20, 'inflow_tot_5d': tot5,
        'hhi_chg': hhi_chg, 'flow_rising': rising,
    })


def _leader_mask(flow_amt, date, p=PARAMS):
    """當日動態領頭股 mask：僅用 5 日法人資金淨流入排名前 N。

    刻意不用價格動能排名——以動能選股會機械性地保證領頭股過去報酬高，
    使 RS 斜率先天向上偏誤。只用資金排名，則「領頭股 RS 加速」才是
    「資金先進、價格跟上」的乾淨檢驗（呼應 H1a）。
    """
    fl = flow_amt.loc[date]
    leaders = fl.nlargest(p['leader_n']).index
    # 只取正流入者；若無正流入則無領頭股
    leaders = [c for c in leaders if fl[c] > 0]
    return flow_amt.columns.isin(leaders)


def d3_metrics(returns, flow_amt, market_returns, p=PARAMS):
    """D3 領先落後。market_returns: Series 大盤日報酬。"""
    dates = returns.index
    leader_ret = pd.Series(index=dates, dtype=float)
    for d in dates:
        win = returns.loc[:d].tail(p['mom_window'])
        if len(win) < p['mom_window']:
            continue
        mask = _leader_mask(flow_amt, d, p)
        if not mask.any():
            continue
        lr = returns.loc[d, mask]
        leader_ret[d] = lr.mean(skipna=True)
    # 領頭相對強度：20 日累積領頭報酬 − 大盤報酬，再取 5 日變化為「加速」
    rs = (1 + leader_ret).rolling(p['rs_window']).apply(lambda x: np.prod(x) - 1, raw=True) \
        - (1 + market_returns).rolling(p['rs_window']).apply(lambda x: np.prod(x) - 1, raw=True)
    accel = rs.diff(p['accel_window'])
    # 歷史相對閾值：加速處於過去 120 日前 20% 高位
    accel_q = accel.rolling(120, min_periods=60).quantile(0.80)
    return pd.DataFrame({'leader_rs': rs, 'leader_accel': accel,
                         'leader_accelerating': (accel > accel_q).fillna(False)})


def signal_state(frame, p=PARAMS):
    """分層訊號（因果）。frame 需含 d1/d2/d3 欄位。回傳每層布林。"""
    d = frame
    s_early = d['flow_rising'] & d['leader_accelerating']
    ma_s = d['eq_index'].rolling(p['ma_short']).mean()
    s_mid = (s_early.rolling(p['early_lookback'], min_periods=1).max() > 0) \
        & (d['breadth'] > p['breadth_thresh']) \
        & (d['eq_index'] > ma_s) & d['converging']
    s_early = s_early & ~s_mid
    ma_l = d['eq_index'].rolling(p['ma_long']).mean()
    # 確認層的量能是「配合」（vs 60 日均量墊高），不是「突波」（vs 20 日均量只抓轉折）
    vol_ma60 = d['volume'].rolling(p['ma_long']).mean()
    # S_confirm 必須建立在 S_mid 之上（累積確信度：early→mid→confirm 嚴格序列化）；
    # 且趨勢已「持續」：MA20>MA60 在過去 20 天中至少 18 天（非剛交叉）。
    was_mid = (s_mid.rolling(40, min_periods=1).max() > 0)
    trend_held = (ma_s > ma_l).rolling(20, min_periods=20).sum() >= 18
    s_confirm = trend_held.fillna(False) & (d['volume'] > vol_ma60 * 1.2) & was_mid
    s_confirm = s_confirm.fillna(False)
    return pd.DataFrame({'S_early': s_early.fillna(False), 'S_mid': s_mid.fillna(False),
                         'S_confirm': s_confirm})


def compute_theme_frame(prices, flow_amt, volumes, market_returns, p=PARAMS):
    """一站式：輸入價格／資金／成交額／大盤 → D1+D2+D3+訊號層。

    flow_amt 為日法人淨流入（金額）；各維度內部自行做 rolling。
    """
    rets = daily_returns(prices)
    d1 = d1_metrics(rets, p)
    d2 = d2_metrics(flow_amt, p)
    flow_slow = flow_amt.rolling(p['flow_slow'], min_periods=p['flow_slow']).sum()
    d3 = d3_metrics(rets, flow_slow, market_returns, p)
    frame = pd.concat([d1, d2, d3], axis=1)
    frame['volume'] = volumes.sum(axis=1)
    sig = signal_state(frame, p)
    return pd.concat([frame, sig], axis=1)
