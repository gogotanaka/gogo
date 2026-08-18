#!/usr/bin/env python3
"""config/.env の読み込み（sbi_client.py / web.py で共有）。"""
import os

CONF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")


def _load_env():
    env = dict(os.environ)
    path = os.path.join(CONF_DIR, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


ENV = _load_env()
