"""合併 3 個 worker DB → 主 DB（冪等，可重跑）。"""
import sqlite3
import sys
from datetime import date

sys.path.insert(0, '/home/hatch/workspace/sector-detect/src')

MAIN = '/home/hatch/workspace/sector-detect/sector_detect.db'
WORKERS = [f'/home/hatch/workspace/sector-detect/sector_detect_w{i}.db' for i in (1, 2, 3)]
TABLES = ['price_daily', 'inst_flow', 'margin_daily', 'exdiv', 'market_daily']


def workers_done():
    """3 個 worker 行程是否都已結束（用 log 結尾判斷）。"""
    import os
    for i in (1, 2, 3):
        log = f'/home/hatch/workspace/sector-detect/backfill_w{i}.log'
        if not os.path.exists(log):
            return False
        with open(log) as f:
            tail = f.read()[-500:]
        if 'backfill done' not in tail:
            return False
    return True


def merge():
    main = sqlite3.connect(MAIN)
    for pdb in WORKERS:
        main.execute(f"ATTACH DATABASE '{pdb}' AS w")
        for tbl in TABLES:
            n = main.execute(f'INSERT OR REPLACE INTO {tbl} SELECT * FROM w.{tbl}').rowcount
            print(f'{pdb.split("/")[-1]}:{tbl} +{n}', flush=True)
        # 注意：SELECT FROM w.<attached> 時 ON CONFLICT 會觸發 SQLite 解析 bug，改用 INSERT OR IGNORE
        main.execute('''INSERT OR IGNORE INTO universe(stock,market,name,first_date,last_date,is_common)
                        SELECT stock,market,name,first_date,last_date,is_common FROM w.universe''')
        rows = main.execute(
            'SELECT stock,market,name,first_date,last_date,is_common FROM w.universe').fetchall()
        for (stock, market, name, fd, ld, ic) in rows:
            main.execute('''UPDATE universe SET first_date = min(first_date, ?),
                            last_date = max(last_date, ?) WHERE stock = ?''', (fd, ld, stock))
        main.commit()  # INSERT/UPDATE 未 commit 前 DETACH 會報 database is locked
        main.execute('DETACH DATABASE w')
    main.commit()
    print('merged. recomputing adjustment...', flush=True)
    from sd_data.adjust import apply_adjustment
    from sd_data.schema import set_meta
    apply_adjustment(main)
    set_meta(main, 'backfill_until', date.today().strftime('%Y%m%d'))
    # 覆蓋率摘要
    n = main.execute('SELECT COUNT(DISTINCT date), COUNT(*), COUNT(DISTINCT stock) FROM price_daily').fetchone()
    print(f'price_daily: {n[0]} dates, {n[1]} rows, {n[2]} stocks')
    main.close()
    print('merge done')


if __name__ == '__main__':
    if not workers_done() and '--force' not in sys.argv:
        print('workers not all done yet; use --force to override')
        sys.exit(1)
    merge()
