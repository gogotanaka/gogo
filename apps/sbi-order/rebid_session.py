#!/usr/bin/env python3
"""リビッドセッション（`start` メンションコマンド）の永続化。

セッション = 「銘柄 X を、上限 P 円で、合計 N 株買い付けるまで、営業日の
8:59〜場中はリビッド（未約定注文を全取消して最良買気配に残量の買い指値）を
続ける」という宣言。web.py の再起動や日またぎでも継続するよう
config/rebid_session.json に置く（config/ は .gitignore 済み）。

買付済み株数は注文単位で orders に持つ:
  {"orders": {"527": {"qty": 600, "filled": 200}, ...}}
filled は「注文照会の 注文株数（未約定） セル」（例: '600 (400)' → 約定200株）と
約定ポーラーの検知の両方から更新する。約定株数は減らないので常に max でマージする。
filled を過小に数えると残量を多く見積もって買い過ぎ、過大に数えると買い逃しになる。
セルが読めないときは更新しない（過小方向）だが、読めないのは全部約定
（ポーラーが別途拾う）などの過渡的なケースで、各ティックの再読みで収束する。

このモジュールは Playwright に触らないので、HTTPスレッド（start/stop受付）と
_sbi_loop（ティック実行）の両方から使ってよい。ファイルアクセスはロックで
直列化し、書き込みは tmp + rename で原子的に行う。
"""
import json
import os
import threading
from datetime import datetime, timezone

from config import CONF_DIR

SESSION_PATH = os.path.join(CONF_DIR, "rebid_session.json")
_lock = threading.Lock()


def _load_nolock():
    try:
        with open(SESSION_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def _save_nolock(sess):
    tmp = SESSION_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sess, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SESSION_PATH)
    try:
        os.chmod(SESSION_PATH, 0o600)
    except OSError:
        pass


def load():
    with _lock:
        return _load_nolock()


def exists():
    return os.path.exists(SESSION_PATH)


def start(ticker, target_qty, price_cap, begins_on):
    """セッションを予約/開始する。既存セッションがあれば置き換える。

    begins_on: 'YYYY-MM-DD'（JST）。この日以降の営業日8:59からティックが走る。
    """
    sess = {
        "ticker": str(ticker),
        "target_qty": int(target_qty),
        "price_cap": float(price_cap),
        "begins_on": str(begins_on),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "orders": {},
    }
    with _lock:
        _save_nolock(sess)
    return sess


def clear():
    with _lock:
        try:
            os.remove(SESSION_PATH)
        except FileNotFoundError:
            pass


def add_order(sbi_order_id, qty):
    """このセッションで発注した注文を記録する。"""
    with _lock:
        sess = _load_nolock()
        if not sess:
            return
        sess["orders"][str(sbi_order_id)] = {"qty": int(qty), "filled": 0}
        _save_nolock(sess)


def record_fill(sbi_order_id, filled_qty):
    """約定検知（ポーラー等）からの反映。セッション外の注文なら何もしない。"""
    with _lock:
        sess = _load_nolock()
        if not sess:
            return
        rec = sess["orders"].get(str(sbi_order_id))
        if not rec:
            return
        rec["filled"] = max(rec["filled"], min(int(filled_qty), rec["qty"]))
        _save_nolock(sess)


def update_fills_from_rows(rows):
    """注文照会の読み取り結果（sbi_client.read_order_table()の戻り値）から
    セッション注文の約定株数を更新し、最新のセッションを返す。

    - 注文株数（未約定）が読めた行: filled = qty - unfilled
    - 読めないが状態に「約定」を含み「取消」を含まない行: 全部約定とみなす
    - それ以外: 更新しない（前回の値を保持）
    """
    with _lock:
        sess = _load_nolock()
        if not sess:
            return None
        changed = False
        for row in rows:
            rec = sess["orders"].get(str(row.get("order_id")))
            if not rec:
                continue
            filled = None
            if row.get("qty") is not None and row.get("unfilled") is not None:
                filled = row["qty"] - row["unfilled"]
            elif "約定" in row.get("status", "") and "取消" not in row.get("status", ""):
                filled = rec["qty"]
            if filled is not None:
                filled = max(rec["filled"], min(int(filled), rec["qty"]))
                if filled != rec["filled"]:
                    rec["filled"] = filled
                    changed = True
        if changed:
            _save_nolock(sess)
        return sess


def total_filled(sess):
    return sum(o["filled"] for o in sess["orders"].values())
