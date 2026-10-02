"""SQLite schema v1。"""
import sqlite3

SCHEMA_VERSION = '1'

DDL = '''
CREATE TABLE IF NOT EXISTS price_daily(
  date TEXT, stock TEXT, market TEXT,
  open REAL, high REAL, low REAL, close REAL,
  volume REAL, amount REAL,
  PRIMARY KEY(date, stock));
CREATE TABLE IF NOT EXISTS inst_flow(
  date TEXT, stock TEXT,
  f_buy REAL, f_sell REAL, f_net REAL,
  t_buy REAL, t_sell REAL, t_net REAL,
  d_buy REAL, d_sell REAL, d_net REAL,
  PRIMARY KEY(date, stock));
CREATE TABLE IF NOT EXISTS margin_daily(
  date TEXT, stock TEXT,
  fin_prev REAL, fin_today REAL,
  PRIMARY KEY(date, stock));
CREATE TABLE IF NOT EXISTS exdiv(
  ex_date TEXT, stock TEXT, market TEXT,
  prev_close REAL, ref_price REAL, factor REAL, kind TEXT,
  PRIMARY KEY(ex_date, stock));
CREATE TABLE IF NOT EXISTS price_adj(
  date TEXT, stock TEXT, close_adj REAL,
  PRIMARY KEY(date, stock));
CREATE TABLE IF NOT EXISTS sector_map(
  sector_id TEXT, sector_name TEXT, stock TEXT, version TEXT,
  PRIMARY KEY(sector_id, stock, version));
CREATE TABLE IF NOT EXISTS universe(
  stock TEXT PRIMARY KEY, market TEXT, name TEXT,
  first_date TEXT, last_date TEXT, is_common INTEGER);
CREATE TABLE IF NOT EXISTS market_daily(
  date TEXT PRIMARY KEY,
  f_net REAL, t_net REAL, d_net REAL,
  total_turnover REAL, up_count INTEGER, down_count INTEGER);
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_price_stock ON price_daily(stock, date);
CREATE INDEX IF NOT EXISTS idx_flow_stock ON inst_flow(stock, date);
'''


def init_db(path):
    con = sqlite3.connect(path)
    con.executescript(DDL)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    con.commit()
    return con


def get_meta(con, key):
    r = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return r[0] if r else None


def set_meta(con, key, value):
    con.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, str(value)))
    con.commit()
