"""SQLite-хранилище: лоты, техспеки, история цен, расчёты пользователя."""
import json
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "zakup.db"
_lock = threading.Lock()


def conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS lots(id INTEGER PRIMARY KEY, data TEXT, category TEXT,
        first_seen REAL, updated REAL);
    CREATE TABLE IF NOT EXISTS techspec(lot_id INTEGER PRIMARY KEY, data TEXT, fetched REAL);
    CREATE TABLE IF NOT EXISTS history(enstru_code TEXT PRIMARY KEY, data TEXT, fetched REAL);
    CREATE TABLE IF NOT EXISTS auto(lot_id INTEGER PRIMARY KEY, data TEXT, updated REAL);
    CREATE TABLE IF NOT EXISTS calc(lot_id INTEGER PRIMARY KEY, data TEXT, updated REAL);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS supplier_products(article INTEGER PRIMARY KEY, name TEXT, norm TEXT,
        category INTEGER, brand TEXT, article_pn TEXT, price REAL, final_price REAL, quantity INTEGER,
        image TEXT, updated REAL);
    CREATE INDEX IF NOT EXISTS sp_norm ON supplier_products(norm);
    CREATE INDEX IF NOT EXISTS sp_cat ON supplier_products(category);
    """)
    return c


DB = conn()


def q(sql, args=()):
    with _lock:
        return DB.execute(sql, args).fetchall()


def x(sql, args=()):
    with _lock:
        DB.execute(sql, args)
        DB.commit()


def put_json(table, key_col, key, data):
    x(f"INSERT OR REPLACE INTO {table}({key_col}, data, {'fetched' if table in ('techspec','history') else 'updated'}) VALUES(?,?,?)",
      (key, json.dumps(data, ensure_ascii=False), time.time()))


def get_json(table, key_col, key):
    r = q(f"SELECT * FROM {table} WHERE {key_col}=?", (key,))
    if not r:
        return None, None
    row = dict(r[0])
    return json.loads(row["data"]), row.get("fetched") or row.get("updated")


def meta_set(k, v):
    x("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))


def meta_get(k, default=None):
    r = q("SELECT v FROM meta WHERE k=?", (k,))
    return json.loads(r[0]["v"]) if r else default
