#!/usr/bin/env python3
"""macOS 通知 (osascript display notification)。"""
import subprocess


def notify(title, message):
    script = (
        f'display notification "{_esc(message)}"'
        f' with title "{_esc(title)}" sound name "Glass"'
    )
    subprocess.run(["osascript", "-e", script], check=False)


def _esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')
