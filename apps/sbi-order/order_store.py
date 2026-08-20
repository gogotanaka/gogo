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
    # 部分約定した株数（注文照会の約定明細から反映）。旧DBには無いので後付けする。
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN filled_qty INTEGER")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e):
            raise  # ロック等、列重複以外のエラーまで握りつぶさない
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


def abandon_stale_pending():
    """再起動時、キュー（メモリ）ごと消えた 'pending' の注文を error で打ち切る。

    「受け付けました」と返信済みなのに静かに消える事故を、少なくとも記録上
    見えるようにする。戻り値は打ち切った件数。
    """
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE orders SET status = 'error',"
            " error_message = '再起動によりキューから失われたため発注されていません'"
            " WHERE status = 'pending'")
        return cur.rowcount


def filled_qty_since(ticker, side, since_iso):
    """since_iso（UTC ISO）以降に発注した注文の約定株数合計。

    filled_qty（部分約定含む実測）があればそれを、無ければ全部約定した注文の
    qty を数える。買付サマリ表示用で、発注判断には使わない。
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT SUM(COALESCE(filled_qty,"
            "  CASE WHEN status = 'filled' THEN qty ELSE 0 END)) AS total"
            " FROM orders WHERE ticker = ? AND side = ? AND created_at >= ?",
            (str(ticker), side, since_iso)).fetchone()
        return row["total"] or 0


def get_order_by_sbi_id(sbi_order_id):
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE sbi_order_id = ? ORDER BY id DESC LIMIT 1",
            (str(sbi_order_id),)).fetchone()
        return dict(row) if row else None


def update_order_by_sbi_id(sbi_order_id, **fields):
    """SBI側の注文番号でローカル記録を更新する（clear all等、SBI側を先に
    操作してからローカルに反映するケース用）。該当レコードが無くても何もしない。"""
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as conn:
        conn.execute(f"UPDATE orders SET {cols} WHERE sbi_order_id = ?",
                     (*fields.values(), sbi_order_id))


def _now():
    return datetime.now(timezone.utc).isoformat()
