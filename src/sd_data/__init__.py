"""SectorDetect 資料層 v1：全市場（上市＋上櫃）日批量採集。

資料來源（皆為官方批量端點，一天約 10 個 request 覆蓋全市場）：
  上市 TWSE：MI_INDEX(價量) / TWT38U(外資) / TWT44U(投信) / TWT43U(自營合計)
            / MI_MARGN(融資) / TWT49U(除權息結果) / BFI82U(市場法人合計)
  上櫃 TPEx：stk_wn1430_result.php(價量，舊版端點，支援歷史日期)
            / 3itrade_hedge_result.php(三大法人) / margin_bal_result.php(融資)

已知限制（v1）：
  - 上櫃除權息官方批量端點尚未找到 → 上櫃成分股還原為近似（未調整），
    在校準階段標記；v1.1 規劃以 FinMind（需 token）補齊。
  - 上市宇宙以 ^[1-9]\\d{3}$ 篩選普通股（含 TDR 91xx，後續可剔除）。
"""
