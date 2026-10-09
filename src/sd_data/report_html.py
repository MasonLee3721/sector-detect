"""SectorDetect 每日 SSS 掃描 HTML 報表產生器。

用法：
    python3 src/sd_data/report_html.py [reports/<日期>]
預設處理最新日期的 sss.json，輸出同目錄的 SSS.html。
"""
import json
import sys
import sqlite3
from pathlib import Path

REPORTS = Path('/home/hatch/workspace/sector-detect/reports')
DB = '/home/hatch/workspace/sector-detect/sector_detect.db'

CSS = """body{font-family:-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif;max-width:980px;margin:0 auto;padding:20px 16px 48px;background:#fafafa;color:#222;line-height:1.6}
h1{font-size:1.35rem;margin:0 0 4px}
.meta{color:#888;font-size:.8rem;margin:0 0 12px}
.lead{background:#fff;border-left:4px solid #1a56db;padding:10px 14px;margin:0 0 18px;border-radius:0 6px 6px 0;box-shadow:0 1px 2px rgba(0,0,0,.06)}
h2{font-size:1.05rem;margin:22px 0 8px;padding-bottom:4px;border-bottom:2px solid #e0e0e0}
.tbl{overflow-x:auto;margin:0 0 12px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:.85rem;box-shadow:0 1px 2px rgba(0,0,0,.05)}
th,td{border:1px solid #e2e2e2;padding:7px 10px;white-space:nowrap}
th{background:#f2f2f2;font-weight:700;text-align:center}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.pos{color:#c0392b;font-weight:600}
td.neg{color:#1e8449;font-weight:600}
a.tk{color:#1a56db;text-decoration:none;border-bottom:1px dotted #1a56db}
a.tk:hover{color:#c0392b;border-bottom-style:solid}
a.tk::after{content:" ↗";font-size:.72em;color:#888}
.leader{font-size:.78rem;line-height:1.7;white-space:nowrap}
.leader .ex{font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:.75rem;font-weight:700}
.b-strong{background:#c0392b;color:#fff}
.b-mid{background:#f39c12;color:#fff}
.b-flat{background:#bdc3c7;color:#fff}
.b-weak{background:#7f8c8d;color:#fff}
.ignite{background:#fef9e7;border:1px solid #f9e79f;border-radius:6px;padding:10px 14px;margin:0 0 12px}
p{font-size:.9rem}
.note{color:#888;font-size:.8rem}
.disclaimer{margin-top:26px;color:#999;font-size:.8rem;text-align:center}"""

LEVEL_BADGE = {'強': 'b-strong', '中': 'b-mid', '平': 'b-flat', '弱': 'b-weak'}


def cls(v):
    return 'pos' if v > 0 else ('neg' if v < 0 else '')


def fmt(v, pct=False):
    s = f'{v:+.2%}' if pct else f'{v:+.2f}'
    return f'<td class="num {cls(v)}">{s}</td>'


def load_leaders(date, sectors):
    """對 5 日 SSS > 0 的族群，取近 5 日超額報酬前 3 名為代表股。

    回傳 {sector_id: [(代號, 名稱, 超額報酬), ...]}。"""
    import pandas as pd
    pos = [s for s in sectors if s['sss_5d'] > 0]
    if not pos:
        return {}
    con = sqlite3.connect(DB)
    try:
        # 往前取 8 個交易日，確保能抓到 5 日前價格
        dates = [r[0] for r in con.execute(
            "SELECT date FROM market_daily WHERE date <= ? AND taiex_close IS NOT NULL "
            "ORDER BY date DESC LIMIT 8", (date,))]
        if len(dates) < 6:
            return {}
        d0, d1 = dates[0], dates[5]
        mkt = con.execute(
            "SELECT taiex_close FROM market_daily WHERE date IN (?, ?)", (d0, d1)).fetchall()
        if len(mkt) < 2 or not all(m[0] for m in mkt):
            return {}
        mkt_ret = mkt[0][0] / mkt[1][0] - 1
        names = {r[0]: r[1] for r in con.execute("SELECT stock, name FROM universe")}
        out = {}
        for s in pos:
            sid = s['sector_id']
            stocks = [r[0] for r in con.execute(
                "SELECT stock FROM sector_map WHERE sector_id=?", (sid,))]
            if not stocks:
                continue
            q = ("SELECT p.stock AS stock, p.date AS date, p.close AS close, a.close_adj AS close_adj "
                 "FROM price_daily p "
                 "LEFT JOIN price_adj a ON a.date=p.date AND a.stock=p.stock "
                 "WHERE p.date IN (?, ?) AND p.stock IN (%s)" % ','.join('?' * len(stocks)))
            df = pd.read_sql_query(q, con, params=(d0, d1, *stocks))
            df['px'] = df['close_adj'].fillna(df['close'])
            rets = []
            for stk in stocks:
                sub = df[df['stock'] == stk].set_index('date')['px']
                if d0 in sub.index and d1 in sub.index:
                    p0, p1 = sub.loc[d0], sub.loc[d1]
                    if p0 and p1 and p1 > 0:
                        rets.append((stk, p0 / p1 - 1 - mkt_ret))
            rets.sort(key=lambda x: -x[1])
            out[sid] = [(stk, names.get(stk, ''), ex) for stk, ex in rets[:3]]
        return out
    finally:
        con.close()


def leader_cell(leaders):
    """代表股儲存格：代號名稱＋超額報酬，附玩股網技術線圖連結。"""
    if not leaders:
        return '<td class="note">—</td>'
    parts = []
    for stk, name, ex in leaders:
        c = cls(ex)
        parts.append(
            f'<a class="tk" href="https://www.wantgoo.com/stock/{stk}/technical-chart" '
            f'target="_blank" rel="noopener">{stk}{name}</a>'
            f' <span class="ex {c}">{ex:+.1%}</span>')
    return '<td><div class="leader">' + '<br>'.join(parts) + '</div></td>'


def build(date_dir: Path) -> Path:
    d = json.load(open(date_dir / 'sss.json', encoding='utf-8'))
    date = d['date']
    sectors = d['sectors']
    ignitions = d.get('ignitions', [])
    recon = d.get('reconciliation')
    params = d.get('ignition_params', {})

    top = sectors[0]
    n_strong = sum(1 for s in sectors if s['level'] == '強')

    # 一句話摘要
    lead = (f'一句話：{date} 38 族群中強度最高為{top["sector_name"]}'
            f'（5 日 SSS {top["sss_5d"]:+.2f}）；'
            f'「強」級別共 {n_strong} 個；'
            f'點火候選 {len(ignitions)} 個。')

    # SSS 排名表（5 日 SSS > 0 的族群加掛代表股：近 5 日超額報酬前 3 名）
    leaders = load_leaders(date, sectors)
    rows = []
    for s in sectors:
        badge = LEVEL_BADGE.get(s['level'], 'b-flat')
        rows.append(
            f'<tr><td class="num">{s["rank"]}</td><td>{s["sector_name"]}</td>'
            f'{fmt(s["sss_5d"])}{fmt(s["sss_today"])}'
            f'<td><span class="badge {badge}">{s["level"]}</span></td>'
            f'{fmt(s["z_d1"])}{fmt(s["z_d2"])}{fmt(s["z_d3"])}'
            f'{leader_cell(leaders.get(s["sector_id"]))}</tr>')
    rank_tbl = ('<div class="tbl"><table><thead><tr><th>#</th><th>族群</th>'
                '<th>5日SSS</th><th>今日SSS</th><th>強度</th>'
                '<th>D1 同動</th><th>D2 資金</th><th>D3 領頭</th>'
                '<th>代表股（5日領漲前3，附技術線圖）</th></tr></thead><tbody>'
                + ''.join(rows) + '</tbody></table></div>'
                '<p class="note">代表股：該族群近 5 日相對大盤超額報酬前 3 名，僅 5 日 SSS &gt; 0 的族群列出；'
                '點代號開啟玩股網技術線圖。</p>')

    # 點火區
    if ignitions:
        ig_rows = []
        for g in ignitions:
            tags = []
            if g.get('money_confirmed'):
                tags.append('資金確認')
            if g.get('leader_led'):
                tags.append('領頭加速')
            tag = '＋'.join(tags) if tags else '無加權'
            ig_rows.append(
                f'<tr><td>{g["sector_name"]}</td><td class="num">#{g["rank_5d"]}</td>'
                f'{fmt(g["thrust_3d"], pct=True)}'
                f'<td class="num">{g["breadth"]:.2f}</td><td>{tag}</td></tr>')
        ig_sec = ('<h2>二、點火候選（冷啟動，召回型 watchlist，非買入訊號）</h2>'
                  '<div class="ignite">點火規則：5 日前 SSS 排名後半（冷）＋ 3 日超額報酬 &gt; 2% ＋ '
                  '上漲家數比 &gt; 0.6；同族群 20 日去重。</div>'
                  '<div class="tbl"><table><thead><tr><th>族群</th><th>排名</th>'
                  '<th>3日超額</th><th>廣度</th><th>加權確認</th></tr></thead><tbody>'
                  + ''.join(ig_rows) + '</tbody></table></div>')
    else:
        ig_sec = ('<h2>二、點火候選（冷啟動，召回型 watchlist，非買入訊號）</h2>'
                  '<p>今日無點火。</p>')

    # 對帳表
    if recon and recon.get('items'):
        r = recon
        rrows = []
        for it in r['items']:
            rrows.append(
                f'<tr><td>{it["tag"]}#{it["y_rank"]}</td><td>{it["sector_name"]}</td>'
                f'{fmt(it["y_sss"])}{fmt(it["ret"], pct=True)}{fmt(it["excess"], pct=True)}'
                f'<td style="text-align:center">{it["mark"]}</td></tr>')
        recon_sec = (
            f'<h2>三、昨日排名 vs 今日實際（紙上驗證）</h2>'
            f'<p>昨日（{r["prev_date"]}）排名 → 今日（{r["date"]}）實際，大盤 {r["mkt_ret"]:+.2%}；'
            f'命中 {r["hits"]}/{r["total"]}。</p>'
            '<div class="tbl"><table><thead><tr><th>昨日段位</th><th>族群</th>'
            '<th>昨日SSS</th><th>今日報酬</th><th>超額</th><th>命中</th></tr></thead><tbody>'
            + ''.join(rrows) + '</tbody></table></div>')
    else:
        recon_sec = ('<h2>三、昨日排名 vs 今日實際（紙上驗證）</h2>'
                     '<p class="note">無昨日排名資料，跳過對帳。</p>')

    method = ('<h2>四、方法說明</h2>'
              '<p>SSS（族群強度分數）＝ z(D1 同動性) ＋ z(D2 資金集中度) ＋ z(D3 領頭股加速)，'
              '各維度相對自身過去 120 交易日標準化。排名使用 5 日平均 SSS。'
              '強度分級：&gt;3 強、&gt;1 中、-1～1 平、&lt;-1 弱。'
              'SSS 是偵測／確認量表，不是買進訊號。</p>'
              f'<p class="note">點火參數：thrust &gt; {params.get("thrust_x", 0.02):.0%}、'
              f'冷排名後 {params.get("cold_rank_cut", 19)} 名、去重 {params.get("cooldown", 20)} 日。'
              '資料來源：TWSE／TPEx 公開資料。</p>')

    html = (f'<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>族群強度掃描 {date}｜SectorDetect</title>'
            f'<style>\n{CSS}\n</style></head><body>'
            f'<h1>族群強度掃描｜SectorDetect</h1>'
            f'<p class="meta">台股 as_of {date}｜38 族群 SSS 排名｜來源：TWSE／TPEx 公開資料</p>'
            f'<div class="lead">{lead}</div>'
            f'<h2>一、38 族群 SSS 排名</h2>{rank_tbl}'
            f'{ig_sec}{recon_sec}{method}'
            f'<p class="disclaimer">以上為研究整理，非買賣建議</p></body></html>')

    out = date_dir / 'SSS.html'
    out.write_text(html, encoding='utf-8')
    return out


def main():
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        subs = sorted([p for p in REPORTS.iterdir()
                       if p.is_dir() and (p / 'sss.json').exists()])
        if not subs:
            print('no reports found')
            sys.exit(1)
        target = subs[-1]
    out = build(target)
    print(f'wrote {out}')


if __name__ == '__main__':
    main()
