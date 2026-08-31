#!/usr/bin/env python3
"""Common data layer for the fund dashboard project."""

import glob
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FUNDS_PATH = os.path.join(BASE_DIR, "funds.json")
FUNDS_JSON = FUNDS_PATH
HISTORY_PATH = os.path.join(BASE_DIR, "fund_history.json")
CACHE_PATH = os.path.join(BASE_DIR, "fund_cache.json")
HTML_PATH = os.path.join(BASE_DIR, "fund_dashboard.html")
DASHBOARD_HTML = HTML_PATH

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

# Common Chinese market holidays (simplified list; extend as needed)
HOLIDAYS = {
    "2026-01-01",  # New Year
    "2026-01-02",  # New Year
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",  # Spring Festival (est)
    "2026-04-06",  # Qingming (est)
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",  # Labor Day
    "2026-06-19", "2026-06-22",  # Dragon Boat (est)
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",  # National Day (est)
}


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    """Atomically replace a JSON file so an interrupted write cannot corrupt it."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix=".json-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def backup_history():
    """Backup history file before mutation."""
    if os.path.exists(HISTORY_PATH):
        suffix = datetime.now(TZ_SHANGHAI).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(BASE_DIR, f"fund_history.{suffix}.bak.json")
        shutil.copy2(HISTORY_PATH, backup_path)
        return backup_path
    return None


def load_funds():
    data = load_json(FUNDS_PATH)
    if not data:
        return {"funds": []}
    return data


def save_funds(data):
    save_json(FUNDS_PATH, data)


def load_history():
    return load_json(HISTORY_PATH, {"entries": []})


def save_history(data):
    backup_history()
    save_json(HISTORY_PATH, data)
    cleanup_old_backups(keep=7)


def cleanup_old_backups(keep=7):
    """Remove old history backup files, keeping the most recent `keep` files."""
    pattern = os.path.join(BASE_DIR, "fund_history.*.bak.json")
    files = sorted(glob.glob(pattern))
    if len(files) <= keep:
        return
    for f in files[:-keep]:
        os.remove(f)


def load_cache():
    return load_json(CACHE_PATH, {})


def save_cache(data):
    save_json(CACHE_PATH, data)


def is_trading_day(date_obj):
    """Return True if date_obj is likely a Chinese A-share trading day."""
    if date_obj.weekday() >= 5:
        return False
    date_str = date_obj.strftime("%Y-%m-%d")
    if date_str in HOLIDAYS:
        return False
    return True


def today_shanghai():
    return datetime.now(TZ_SHANGHAI)


def read_dashboard():
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()
