"""回補驅動：日期區間 → 逐日 etl_day（冪等＋斷點續跑）→ 全量還原。"""
import json
import sqlite3
import sys
from datetime import date, timedelta

sys.path.insert(0, '/home/hatch/workspace/sector-detect/src')
from sd_data.schema import init_db, get_meta, set_meta
from sd_data.etl import etl_day
from sd_data.adjust import apply_adjustment

DB = '/home/hatch/workspace/sector-detect/sector_detect.db'


def daterange(start, end):
    d = start
    while d <= end:
        yield d.strftime('%Y%m%d')
        d += timedelta(days=1)


def load_sector_map(con, basket_path, version):
    b = json.load(open(basket_path, encoding='utf-8'))
    rows = []
    for s in b['sectors']:
        sid = s.get('id') or s.get('sector_id') or s.get('name')
        sname = s.get('name') or sid
        for st in s['stocks']:
            rows.append((str(sid), sname, st['code'], version))
    con.executemany('INSERT OR REPLACE INTO sector_map VALUES (?,?,?,?)', rows)
    con.commit()
    print(f'  sector_map: {len(rows)} rows, version={version}')


def backfill(start_yyyymmdd, end_yyyymmdd=None, db_path=DB):
    con = init_db(db_path)
    end = end_yyyymmdd or date.today().strftime('%Y%m%d')
    start_d = date(int(start_yyyymmdd[:4]), int(start_yyyymmdd[4:6]), int(start_yyyymmdd[6:8]))
    end_d = date(int(end[:4]), int(end[4:6]), int(end[6:8]))
    done = ok = skipped = 0
    for ymd in daterange(start_d, end_d):
        if ymd < start_yyyymmdd:
            continue
        # 斷點續跑：當日已有價量資料則跳過
        r = con.execute('SELECT 1 FROM price_daily WHERE date=? LIMIT 1', (ymd,)).fetchone()
        if r:
            skipped += 1
            continue
        try:
            if etl_day(con, ymd):
                ok += 1
            else:
                skipped += 1  # 休市或無資料
        except Exception as e:
            print(f'  !! ETL exception {ymd}: {e}', file=sys.stderr)
            skipped += 1
        done += 1
    print(f'backfill done: {ok} ok, {skipped} skipped/holiday')
    print('applying backward adjustment...')
    apply_adjustment(con)
    set_meta(con, 'backfill_until', end)
    con.close()


if __name__ == '__main__':
    start = sys.argv[1] if len(sys.argv) > 1 else '20240102'
    end = sys.argv[2] if len(sys.argv) > 2 else None
    basket = sys.argv[3] if len(sys.argv) > 3 else '/home/hatch/workspace/fundflo/basket.json'
    con = init_db(DB)
    load_sector_map(con, basket, 'v20260926')
    con.close()
    backfill(start, end)
