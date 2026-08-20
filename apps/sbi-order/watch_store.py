#!/usr/bin/env python3
"""watch / watch-open 設定の永続化（config/watches.json）。

- watch <銘柄> <平均株数> <上限価格> <平均間隔秒>:
  場中、間隔（±30%ランダム）ごとに rebid（その銘柄の未約定注文を取消して
  最良買気配と同値の買い指値。株数も平均±30%ランダム）を続ける常設設定。
  目標株数は無い（止めるまで買い続ける）。unwatch で解除。
- watch-open buy <銘柄> <株数> <上限価格>:
  毎営業日の8:59〜9:05、20秒ごとに rebid する寄り付き用の常設設定。
  unwatch-open で解除。

どちらも銘柄ごとに1つ（同じ銘柄への再設定は置き換え）。設定は web.py の
再起動をまたいで残る。このモジュールは Playwright に触らないので、
HTTPスレッドと _sbi_loop の両方から使ってよい（ファイルアクセスはロックで
直列化し、書き込みは tmp + rename で原子的に行う）。

updated_at は設定・置き換えのたびに更新され、_sbi_loop が「設定が変わった」
ことを検知して即時ティックする合図と、ティック中の置き換え検知（発注直前に
再読みして違っていたら発注しない）の両方に使う。
"""
import json
import os
import sys
import threading
from datetime import datetime, timezone

from config import CONF_DIR

WATCHES_PATH = os.path.join(CONF_DIR, "watches.json")
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load_nolock():
    try:
        with open(WATCHES_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"想定外の型: {type(data).__name__}")
    except FileNotFoundError:
        data = {}
    except ValueError as e:
        # 壊れたJSONを「全watch解除」と同一視しない: 空扱いにはするが（発注は
        # 止まる=安全側）、.corrupt へ退避して修復材料を保全する（退避しないと
        # 次の設定コマンドが壊れた本体を上書きし、他の設定が失われる）。
        corrupt = WATCHES_PATH + ".corrupt"
        try:
            os.replace(WATCHES_PATH, corrupt)
        except OSError:
            corrupt = "(退避失敗)"
        print(f"[watch_store] {WATCHES_PATH} が壊れています（{e}）。"
              f"{corrupt} へ退避し、全watchを無効扱いにしています。"
              f"必要なら内容を確認して再設定してください。",
              file=sys.stderr)
        data = {}
    data.setdefault("watches", {})
    data.setdefault("watch_opens", {})
    return data


def _save_nolock(data):
    tmp = WATCHES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())  # 電源断での空ファイル化（=全watch消失）を防ぐ
    os.replace(tmp, WATCHES_PATH)
    try:
        os.chmod(WATCHES_PATH, 0o600)
    except OSError:
        pass


def load():
    with _lock:
        return _load_nolock()


def set_watch(ticker, avg_qty, price_cap, avg_interval_sec):
    with _lock:
        data = _load_nolock()
        prev = data["watches"].get(str(ticker))
        data["watches"][str(ticker)] = {
            "avg_qty": int(avg_qty),
            "price_cap": float(price_cap),
            "avg_interval_sec": int(avg_interval_sec),
            "created_at": prev["created_at"] if prev else _now(),
            "updated_at": _now(),
        }
        _save_nolock(data)
        return prev


def remove_watch(ticker):
    with _lock:
        data = _load_nolock()
        prev = data["watches"].pop(str(ticker), None)
        if prev is not None:
            _save_nolock(data)
        return prev


def get_watch(ticker):
    with _lock:
        return _load_nolock()["watches"].get(str(ticker))


def set_watch_open(ticker, side, qty, price_cap):
    with _lock:
        data = _load_nolock()
        prev = data["watch_opens"].get(str(ticker))
        data["watch_opens"][str(ticker)] = {
            "side": side,
            "qty": int(qty),
            "price_cap": float(price_cap),
            "created_at": prev["created_at"] if prev else _now(),
            "updated_at": _now(),
        }
        _save_nolock(data)
        return prev


def remove_watch_open(ticker):
    with _lock:
        data = _load_nolock()
        prev = data["watch_opens"].pop(str(ticker), None)
        if prev is not None:
            _save_nolock(data)
        return prev


def get_watch_open(ticker):
    with _lock:
        return _load_nolock()["watch_opens"].get(str(ticker))
