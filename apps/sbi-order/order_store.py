#!/usr/bin/env python3
"""発注記録の永続化 (SQLite)。config/orders.db, .gitignore 済み。"""
import os
import sqlite3
from datetime import datetime, timezone

CONF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
DB_PATH = os.path.join(CONF_DIR, "orders.db")


def _conn():
    os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
    existed = os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS orders ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ticker TEXT NOT NULL,"
        " side TEXT NOT NULL,"
        " qty INTEGER NOT NULL,"
        " price REAL NOT NULL,"
        " sbi_order_id TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " error_message TEXT,"
        " created_at TEXT NOT NULL,"
        " submitted_at TEXT,"
        " filled_at TEXT,"
        " notified_at TEXT"
        ")")
    if not existed:
        os.chmod(DB_PATH, 0o600)
    return conn


def create_order(ticker, side, qty, price):
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO orders (ticker, side, qty, price, status, created_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?)",
            (ticker, side, qty, price, _now()))
        return cur.lastrowid


def update_order(order_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as conn:
        conn.execute(f"UPDATE orders SET {cols} WHERE id = ?",
                     (*fields.values(), order_id))


def get_order(order_id):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


def list_orders(limit=50):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def pending_watch_orders():
    """ポーラーが約定チェックすべき注文（submitted で未約定のもの）。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status = 'submitted'").fetchall()
        return [dict(r) for r in rows]


def _now():
    return datetime.now(timezone.utc).isoformat()
