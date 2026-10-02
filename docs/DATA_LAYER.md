# 資料層設計（v1）

## 目標

全市場（上市＋上櫃）每日批量採集：還原價量、三大法人、融資餘額、除權息事件、38 族群映射。
一天約 10 個 HTTP request 覆蓋全市場（非逐檔），支援 2024-01 起歷史回補。

## 端點清單（2026-10-02 實測）

| 資料 | 端點 | 歷史日期參數 | 備註 |
|---|---|---|---|
| 上市價量 OHLCV | TWSE `exchangeReport/MI_INDEX?type=ALLBUT0999` | ✅ | 宇宙篩選 `^[1-9]\d{3}$` → 約 1001 檔普通股 |
| 上市外資 | TWSE `rwd/zh/fund/TWT38U` | ✅ | cols 9/10/11 |
| 上市投信 | TWSE `rwd/zh/fund/TWT44U` | ✅ | cols 3/4/5 |
| 上市自營商合計 | TWSE `rwd/zh/fund/TWT43U` | ✅ | cols 8/9/10 |
| 上市融資 | TWSE `rwd/zh/marginTrading/MI_MARGN?selectType=STOCK` | ✅ | cols 4/5（前日／今日餘額，張） |
| 上市除權息 | TWSE `rwd/zh/exRight/TWT49U` | ✅ | 前收盤／參考價 → factor |
| 市場法人合計 | TWSE `fund/BFI82U`（舊版，日期參數有效） | ✅ | 注意新版 `/rwd/zh/fund/BFI82U` 忽略日期 |
| 上櫃價量 OHLCV | TPEx `stk_wn1430_result.php`（**舊版**端點） | ✅ | 新版 `stk_quote_result.php` **忽略日期參數**，不可用於歷史 |
| 上櫃三大法人 | TPEx `3itrade_hedge_result.php` | ✅ | 外資 8-10／投信 11-13／自營合計 20-22 |
| 上櫃融資 | TPEx `margin_bal_result.php` | ✅ | cols 2/6（前日／今日餘額，張） |

## 還原方法

- 上市：`factor = 除權息參考價 / 除權息前收盤價`（TWT49U 直接給，無需拆解現金／股票股利；
  現金增資、減資同樣適用）。
- 向後還原：`close_adj(t) = close(t) × Π factor(e)`，`e` 跑遍 `ex_date > t` 的事件。
  還原僅作用於除權息日**之前**的日期 → 無未來函數。
- ⚠️ **TWSE 除權息端點皆為未來預告式**：實測 TWT49U／TWT48U（新版／舊版路徑）
  皆忽略日期參數、只回傳未來即將除權息的資料 → **無法用於歷史回補**。
- 過渡方案（校準用）：`src/sd_data/dividends.py` 以 yfinance 抓取 31 檔校準股
  （記憶體／光通訊／被動元件）的股利＋配股事件，但每一筆必須通過官方價格
  缺口驗證（除權息日跳空 ≈ 股利金額，容忍 12% 日波動）才寫入 `exdiv`；
  未通過者列為人工覆核，不靜默採用。
- 正式方案（v1.1）：FinMind `TaiwanStockDividend`（需 token）全市場補齊；
  上櫃除權息官方批量端點尚未找到，同列 v1.1。

## Schema

見 `src/sd_data/schema.py`：`price_daily`、`inst_flow`、`margin_daily`、`exdiv`、
`price_adj`、`sector_map`、`universe`、`market_daily`、`meta`。缺值存 NULL，
絕不靜默填零；`INSERT OR REPLACE` 保證冪等。

## 驗證

`src/sd_data/validate.py`：
- 覆蓋率（每日價量／法人／融資檔數、市場合計）。
- OHLC 內部一致性抽檢（high ≥ max(open, close, low)）。
- 還原正確性：對每個除權息事件驗證 `還原報酬 == 以參考價為基準的經濟報酬`。

## 偵察過程中的坑（已踩過）

1. TPEx 新版 `stk_quote_result.php` 忽略日期參數（實測要 113/01/02 回 20261002）→ 改用舊版 `stk_wn1430_result.php`。
2. TPEx 逐檔 `st43_result.php` 已下架（404）。
3. Yahoo `query1` 直接打會 404（需 crumb）→ 不採用。
4. `BFI82U` 新版忽略日期 → 用舊版 `/fund/BFI82U?dayDate=&type=day`。
5. 舊版上櫃行情回傳量大（~1MB+），抓取需重試機制。
