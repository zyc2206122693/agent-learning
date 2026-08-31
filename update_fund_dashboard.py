#!/usr/bin/env python3
"""Daily fund dashboard updater.

Fetches NAV data from Sina, updates a local history file, and generates a
self-contained HTML dashboard.
"""

import html
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from urllib.request import Request, urlopen

from fund_data import (
    BASE_DIR,
    FUNDS_PATH,
    HISTORY_PATH,
    CACHE_PATH,
    HTML_PATH,
    TZ_SHANGHAI,
    load_json,
    save_json,
    load_funds,
    save_funds,
    load_history,
    load_cache,
    save_history,
    save_cache,
    is_trading_day,
    today_shanghai,
)

SINA_URL = "https://hq.sinajs.cn/list=f_{code}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_money(value):
    return f"{value:,.2f}"


def format_nav(value):
    return f"{value:.4f}"


def format_pct(value):
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def cn_color(value):
    """Tailwind color class: red = up, green = down (Chinese convention)."""
    if value >= 0:
        return "text-red-500"
    return "text-emerald-500"


def cn_bg(value):
    """Background tint for up/down badges."""
    if value >= 0:
        return "bg-red-50 dark:bg-red-900/20"
    return "bg-emerald-50 dark:bg-emerald-900/20"


def fetch_fund(code):
    """Fetch and parse Sina fund data for a single fund code."""
    url = SINA_URL.format(code=code)
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("gbk")
    except Exception as e:
        return {"error": f"Network error: {e}"}

    prefix = f'var hq_str_f_{code}="'
    start = raw.find(prefix)
    if start == -1:
        return {"error": "Unexpected response format"}
    start += len(prefix)
    end = raw.find('"', start)
    if end == -1:
        return {"error": "Unexpected response format"}

    fields = raw[start:end].split(",")
    if len(fields) < 5:
        return {"error": "Unexpected response format"}

    name = fields[0].strip()
    try:
        nav_today = parse_float(fields[1], 0.0)
        nav_prev = parse_float(fields[3], 0.0)
    except (TypeError, ValueError) as e:
        return {"error": f"Invalid numeric data: {e}"}

    if nav_prev <= 0:
        return {"error": "Invalid previous NAV"}

    change_pct = round((nav_today - nav_prev) / nav_prev * 100, 2)
    date_str = fields[4].strip()

    return {
        "fundcode": code,
        "name": name,
        "dwjz": str(nav_prev),
        "gsz": str(nav_today),
        "gszzl": str(change_pct),
        "jzrq": date_str,
        "gztime": f"{date_str} 15:00",
    }


def fetch_fund_cached(code, cache):
    """Fetch fund data, falling back to cache on failure."""
    fresh = fetch_fund(code)
    if "error" not in fresh:
        cache[code] = fresh
        return fresh, True
    if code in cache:
        cached = cache[code].copy()
        cached["_from_cache"] = True
        return cached, False
    return fresh, False


# ---------------------------------------------------------------------------
# Portfolio calculations
# ---------------------------------------------------------------------------
def calculate_portfolio(funds_config, fetched_data, prev_nav_map=None):
    total_value = 0.0
    total_cost = 0.0
    total_invested = 0.0
    details = []

    for fund in funds_config["funds"]:
        code = fund["code"]
        raw = fetched_data.get(code, {})
        amount = float(fund["amount"])
        invested = float(fund.get("total_invested", amount))

        if "error" in raw:
            details.append({
                "code": code,
                "name": fund["name"],
                "platform": fund["platform"],
                "category": fund["category"],
                "type": fund["type"],
                "risk": fund["risk"],
                "theme": fund.get("theme", "未分类"),
                "investment_status": fund.get("investment_status", ""),
                "amount": amount,
                "total_invested": invested,
                "error": raw["error"],
            })
            total_cost += amount
            total_invested += invested
            continue

        dwjz = parse_float(raw.get("dwjz"), 0.0)
        gsz = parse_float(raw.get("gsz"), 0.0)
        gszzl = parse_float(raw.get("gszzl"), 0.0)

        # Use stored shares if available (stable), otherwise compute from amount.
        # Shares are the source of truth — they don't drift across runs.
        # Detect a USER manual edit of "amount" by comparing it against the
        # value it was synced to at the PREVIOUS run's NAV (prev_nav_map, read
        # from cache before this run's fetch). Comparing against the *current*
        # dwjz would misfire whenever the NAV itself moved >2% in a day
        # (routine for equity funds) and wrongly rewrite shares; the amount is
        # re-synced to shares*dwjz on every run, so a large deviation from
        # shares*prev_dwjz can only mean the user edited the amount.
        stored_shares = fund.get("shares")
        if stored_shares is not None and float(stored_shares) > 0 and dwjz > 0:
            shares = float(stored_shares)
            prev_dwjz = parse_float((prev_nav_map or {}).get(code, 0.0), 0.0)
            if prev_dwjz > 0:
                expected_amount = shares * prev_dwjz
                user_edited = expected_amount > 0 and abs(amount - expected_amount) / expected_amount > 0.02
            else:
                # No previous NAV on record (first run for this fund): fall
                # back to comparing against the current computed amount.
                computed_amount = shares * dwjz
                user_edited = computed_amount > 0 and abs(amount - computed_amount) / computed_amount > 0.02
            if user_edited:
                # User manually changed amount → recalculate shares at current NAV
                shares = amount / dwjz
            else:
                amount = shares * dwjz
        elif dwjz > 0 and amount > 0:
            shares = amount / dwjz
        else:
            shares = 0.0

        if shares > 0 and gsz > 0:
            current_value = shares * gsz
        else:
            current_value = amount

        daily_pnl = current_value - amount
        hold_pnl = current_value - invested
        hold_return_pct = (hold_pnl / invested * 100) if invested > 0 else 0.0

        total_value += current_value
        total_cost += amount
        total_invested += invested

        details.append({
            "code": code,
            "name": raw.get("name") or fund["name"],
            "platform": fund["platform"],
            "category": fund["category"],
            "type": fund["type"],
            "risk": fund["risk"],
            "theme": fund.get("theme", "未分类"),
            "investment_status": fund.get("investment_status", ""),
            "amount": amount,
            "total_invested": invested,
            "nav": dwjz,
            "estimate": gsz,
            "change_pct": gszzl,
            "current_value": current_value,
            "daily_pnl": daily_pnl,
            "hold_pnl": hold_pnl,
            "hold_return_pct": hold_return_pct,
            "nav_date": raw.get("jzrq", ""),
            "estimate_time": raw.get("gztime", ""),
            "_shares": shares,
        })

    total_change_pct = (total_value / total_cost - 1) * 100 if total_cost > 0 else 0.0
    total_hold_pnl = total_value - total_invested
    total_hold_return_pct = (total_hold_pnl / total_invested * 100) if total_invested > 0 else 0.0

    return {
        "total_value": total_value,
        "total_cost": total_cost,
        "total_invested": total_invested,
        "total_pnl": total_value - total_cost,
        "total_change_pct": total_change_pct,
        "total_hold_pnl": total_hold_pnl,
        "total_hold_return_pct": total_hold_return_pct,
        "details": details,
    }


# ---------------------------------------------------------------------------
# History management
# ---------------------------------------------------------------------------
def update_history(history, today, portfolio):
    if history is None:
        history = {"entries": []}

    entries = history.get("entries", [])
    funds_snapshot = {}
    for d in portfolio["details"]:
        if "error" in d:
            continue
        funds_snapshot[d["code"]] = {
            "nav": d["nav"],
            "estimate": d["estimate"],
            "change_pct": d["change_pct"],
        }

    new_entry = {
        "date": today,
        "funds": funds_snapshot,
        "total_value": round(portfolio["total_value"], 2),
        # Cumulative invested amount for this date; lets the daily-P&L table
        # separate real gains from new deposits (定投/单笔买入/补录).
        "total_invested": round(portfolio["total_invested"], 2),
    }

    if entries and entries[-1]["date"] == today:
        entries[-1] = new_entry
    else:
        entries.append(new_entry)

    history["entries"] = entries
    return history


# ---------------------------------------------------------------------------
# Analysis generation
# ---------------------------------------------------------------------------
def generate_analysis(config, portfolio, history, today):
    details = portfolio["details"]
    total_pnl = portfolio["total_pnl"]
    total_change_pct = portfolio["total_change_pct"]
    total_value = portfolio["total_value"]
    total_hold_pnl = portfolio["total_hold_pnl"]
    total_hold_return_pct = portfolio["total_hold_return_pct"]

    conservative_types = {"债券型-中长债", "混合型-偏债"}
    growth_types = {"指数型-股票被动", "混合型-灵活配置"}

    conservative_value = sum(d["current_value"] for d in details if d["type"] in conservative_types)
    growth_value = sum(d["current_value"] for d in details if d["type"] in growth_types)
    locked_value = sum(
        d["current_value"] for d in details
        if "1年持有" in d.get("investment_status", "") or d["category"] == "锁定仓位"
    )

    conservative_pct = conservative_value / total_value * 100 if total_value > 0 else 0
    growth_pct = growth_value / total_value * 100 if total_value > 0 else 0
    locked_pct = locked_value / total_value * 100 if total_value > 0 else 0

    if total_change_pct >= 1:
        tone = "up_strong"
        market_summary = f"今日表现强势，总市值上涨 {format_pct(total_change_pct)}，浮盈 {format_money(total_pnl)} 元。"
    elif total_change_pct > 0:
        tone = "up"
        market_summary = f"今日小幅上涨 {format_pct(total_change_pct)}，浮盈 {format_money(total_pnl)} 元。"
    elif total_change_pct <= -2:
        tone = "down_strong"
        market_summary = f"今日市场明显回调，总市值下跌 {abs(total_change_pct):.2f}%，浮亏 {format_money(abs(total_pnl))} 元。"
    elif total_change_pct < 0:
        tone = "down"
        market_summary = f"今日小幅回调 {abs(total_change_pct):.2f}%，浮亏 {format_money(abs(total_pnl))} 元。"
    else:
        tone = "flat"
        market_summary = "今日基本持平，属于正常震荡。"

    highlights = []
    sorted_details = sorted(
        [d for d in details if "error" not in d],
        key=lambda x: x["change_pct"],
        reverse=True,
    )
    if sorted_details:
        top_gainer = sorted_details[0]
        top_loser = sorted_details[-1]
        if top_gainer["change_pct"] > 0:
            highlights.append(f"今日最强：{top_gainer['name']}（+{top_gainer['change_pct']:.2f}%）")
        if top_loser["change_pct"] < 0 and top_loser["code"] != top_gainer["code"]:
            highlights.append(f"今日最弱：{top_loser['name']}（{top_loser['change_pct']:.2f}%）")

    # SIP reminder
    today_obj = datetime.strptime(today, "%Y-%m-%d")
    sip_today = []
    for f in config["funds"]:
        status = f.get("investment_status", "")
        if "每月" in status and "日" in status:
            m = re.search(r'(\d+)\s*日', status)
            if m and int(m.group(1)) == today_obj.day:
                sip_today.append(f)
    if sip_today:
        names = [f["name"] for f in sip_today]
        highlights.append(f"今日有定投扣款：{', '.join(names)}，记得确保账户余额充足。")

    # Allocation drift check
    drift_notes = []
    if abs(conservative_pct - 71) > 8:
        drift_notes.append(f"稳健部分当前 {conservative_pct:.0f}%，偏离目标 71% 较多。")
    if abs(growth_pct - 25) > 5:
        drift_notes.append(f"进攻部分当前 {growth_pct:.0f}%，偏离目标 25% 较多。")
    if abs(locked_pct - 4) > 3:
        drift_notes.append(f"锁定部分当前 {locked_pct:.0f}%，偏离目标 4% 较多。")
    if drift_notes:
        highlights.append("配置偏离提醒：" + " ".join(drift_notes))

    # Hold period comment
    if total_hold_return_pct > 0:
        highlights.append(f"持有期整体盈利 {format_pct(total_hold_return_pct)}，累计收益 {format_money(total_hold_pnl)} 元。")
    elif total_hold_return_pct < 0:
        highlights.append(f"持有期暂时浮亏 {abs(total_hold_return_pct):.2f}%，累计亏损 {format_money(abs(total_hold_pnl))} 元，坚持定投可摊薄成本。")

    action_map = {
        "up_strong": "持有 / 不追高",
        "up": "持有 / 继续定投",
        "down_strong": "持有 / 逢低定投",
        "down": "持有 / 坚持定投",
        "flat": "持有 / 观望",
    }
    action = action_map[tone]

    recommendation_map = {
        "up_strong": "市场情绪较好，但不建议因单日大涨追高加仓。继续按计划定投，保持纪律。",
        "up": "温和上涨，保持现有节奏。有定投计划的基金继续执行即可。",
        "down_strong": "市场明显回调，正是定投摊低成本的好时机。若有余力，可在定投基础上小幅加仓。",
        "down": "小幅回调，无需紧张。坚持定投，不要因短期波动停止扣款。",
        "flat": "今天波动不大，正常观望。按计划定投，不追涨杀跌。",
    }
    recommendation = recommendation_map[tone]

    encouragement_map = {
        "up_strong": "涨得好的时候，保持冷静比赚钱更重要。纪律才是真正的护城河。",
        "up": "小步前进也是前进，坚持定投会让时间站在你这边。",
        "down_strong": "市场大幅回调时最考验心态。你已经设好了计划，按计划执行就是胜利。",
        "down": "短期波动是市场的常态，定投就是在波动中悄悄收集便宜的筹码。",
        "flat": "不急不躁，不追不杀。定投的乐趣就在于把复杂决策交给时间和纪律。",
    }
    encouragement = encouragement_map[tone]

    return {
        "market_summary": market_summary,
        "highlights": highlights,
        "recommendation": recommendation,
        "action": action,
        "encouragement": encouragement,
        "allocation": {
            "conservative": round(conservative_pct, 1),
            "growth": round(growth_pct, 1),
            "locked": round(locked_pct, 1),
        },
    }


# ---------------------------------------------------------------------------
# HTML rendering (split into focused helpers)
# ---------------------------------------------------------------------------
def _esc(s):
    return html.escape(str(s))


def render_header(today, status_note=""):
    note_html = f'<p class="text-amber-600 dark:text-amber-400 mt-1">{_esc(status_note)}</p>' if status_note else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>基金每日看板</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="chart.js.min.js"></script>
<script>if(typeof Chart==='undefined'){{document.write('<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"><\\/script>');}}</script>
<style>
  .kpi-label {{ font-size: 0.75rem; color: #64748b; }}
  .dark .kpi-label {{ color: #94a3b8; }}
</style>
</head>
<body class="bg-slate-50 text-slate-800 dark:bg-slate-900 dark:text-slate-100">
<div class="max-w-7xl mx-auto p-4 md:p-8">
  <header class="mb-8">
    <h1 class="text-3xl font-bold mb-2">📈 基金每日看板</h1>
    <p class="text-slate-500 dark:text-slate-400">数据更新于 {_esc(today)}</p>
    {note_html}
  </header>
"""


def render_kpi_cards(portfolio):
    return f"""
  <!-- KPI Cards -->
  <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
    <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow">
      <p class="kpi-label">总市值</p>
      <p class="text-2xl font-bold">¥{format_money(portfolio['total_value'])}</p>
    </div>
    <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow">
      <p class="kpi-label">今日盈亏</p>
      <p class="text-2xl font-bold {cn_color(portfolio['total_pnl'])}">
        {'+' if portfolio['total_pnl'] >= 0 else ''}{format_money(portfolio['total_pnl'])}
      </p>
    </div>
    <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow">
      <p class="kpi-label">今日涨跌幅</p>
      <p class="text-2xl font-bold {cn_color(portfolio['total_change_pct'])}">
        {format_pct(portfolio['total_change_pct'])}
      </p>
    </div>
    <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow">
      <p class="kpi-label">累计投入</p>
      <p class="text-2xl font-bold">¥{format_money(portfolio['total_invested'])}</p>
    </div>
    <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow">
      <p class="kpi-label">累计盈亏</p>
      <p class="text-2xl font-bold {cn_color(portfolio['total_hold_pnl'])}">
        {'+' if portfolio['total_hold_pnl'] >= 0 else ''}{format_money(portfolio['total_hold_pnl'])}
      </p>
    </div>
    <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow">
      <p class="kpi-label">持有期收益率</p>
      <p class="text-2xl font-bold {cn_color(portfolio['total_hold_return_pct'])}">
        {format_pct(portfolio['total_hold_return_pct'])}
      </p>
    </div>
  </div>
"""


def render_analysis(analysis):
    if not analysis:
        return ""
    highlights = "".join(f"<li>{_esc(h)}</li>" for h in analysis["highlights"])
    return f"""
  <!-- Daily Analysis -->
  <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow mb-8">
    <h2 class="text-lg font-bold mb-4">💡 每日分析</h2>
    <p class="mb-3">{_esc(analysis['market_summary'])}</p>
    <ul class="list-disc list-inside mb-4 text-sm text-slate-600 dark:text-slate-300">
      {highlights}
    </ul>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
      <div class="bg-slate-50 dark:bg-slate-700/50 rounded-xl p-4">
        <p class="text-xs text-slate-500 dark:text-slate-400">操作建议</p>
        <p class="font-bold text-lg">{_esc(analysis['action'])}</p>
      </div>
      <div class="bg-slate-50 dark:bg-slate-700/50 rounded-xl p-4">
        <p class="text-xs text-slate-500 dark:text-slate-400">当前配置</p>
        <p class="text-sm">稳健 {analysis['allocation']['conservative']}% · 进攻 {analysis['allocation']['growth']}% · 锁定/观察 {analysis['allocation']['locked']}%</p>
      </div>
    </div>
    <div class="border-l-4 border-blue-500 pl-4 py-2 bg-blue-50 dark:bg-blue-900/20 rounded-r-xl">
      <p class="text-sm font-medium text-blue-800 dark:text-blue-200">{_esc(analysis['recommendation'])}</p>
    </div>
    <div class="mt-4 text-sm text-slate-500 dark:text-slate-400 italic">
      “{_esc(analysis['encouragement'])}”
    </div>
  </div>
"""


def render_charts(category_labels, category_data, trend_labels, trend_values, trend_invested, platform_labels, platform_data, fund_line_datasets, benchmark=None, profit_data=None):
    js_category_labels = json.dumps(category_labels, ensure_ascii=False)
    js_category_data = json.dumps(category_data)
    js_trend_labels = json.dumps(trend_labels, ensure_ascii=False)
    js_trend_values = json.dumps(trend_values)
    js_trend_invested = json.dumps(trend_invested, ensure_ascii=False)
    js_platform_labels = json.dumps(platform_labels, ensure_ascii=False)
    js_platform_data = json.dumps(platform_data)
    js_fund_datasets = json.dumps(fund_line_datasets, ensure_ascii=False)
    js_benchmark = "null" if not benchmark else json.dumps(benchmark, ensure_ascii=False)
    js_profit_data = json.dumps(profit_data or [], ensure_ascii=False)

    benchmark_html = ""
    if benchmark:
        benchmark_html = f"""
  <!-- Benchmark comparison -->
  <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow mb-8">
    <h2 class="text-lg font-bold mb-4">📉 基准对比（归一化，起始 = 100）</h2>
    <p class="text-sm text-slate-500 dark:text-slate-400 mb-4">组合总市值 vs 沪深300 vs 中证全债 —— 看组合有没有跑赢大盘</p>
    <div style="height: 320px; position: relative;">
      <canvas id="benchmarkChart" style="display: block; width: 100%; height: 100%;"></canvas>
    </div>
  </div>
"""
    profit_html = ""
    if profit_data:
        total_pnl = sum(p["pnl"] for p in profit_data)
        profit_html = f"""
  <!-- Per-fund profit contribution -->
  <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow mb-8">
    <h2 class="text-lg font-bold mb-4">💰 单只基金收益贡献</h2>
    <p class="text-sm text-slate-500 dark:text-slate-400 mb-4">各基金持有盈亏（元），合计 {format_money(total_pnl)} 元（红涨绿跌）</p>
    <div style="height: 300px; position: relative;">
      <canvas id="profitChart" style="display: block; width: 100%; height: 100%;"></canvas>
    </div>
  </div>
"""

    palette = "['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4']"
    platform_palette = "['#3b82f6', '#10b981', '#f59e0b', '#ef4444']"

    return f"""
  <!-- Charts -->
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-8">
    <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow">
      <h2 class="text-lg font-bold mb-4">配置结构</h2>
      <div style="height: 256px; position: relative;">
        <canvas id="allocationChart" style="display: block; width: 100%; height: 100%;"></canvas>
      </div>
    </div>
    <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow">
      <h2 class="text-lg font-bold mb-4">平台分布</h2>
      <div style="height: 256px; position: relative;">
        <canvas id="platformChart" style="display: block; width: 100%; height: 100%;"></canvas>
      </div>
    </div>
    <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow">
      <h2 class="text-lg font-bold mb-4">总市值走势</h2>
      <div style="height: 256px; position: relative;">
        <canvas id="trendChart" style="display: block; width: 100%; height: 100%;"></canvas>
      </div>
    </div>
  </div>

  <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow mb-8">
    <h2 class="text-lg font-bold mb-4">单只基金净值走势</h2>
    <div style="height: 320px; position: relative;">
      <canvas id="fundTrendChart" style="display: block; width: 100%; height: 100%;"></canvas>
    </div>
  </div>

  {benchmark_html}
  {profit_html}

  <!-- 收益明细 -->
  <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow mb-8">
    <h2 class="text-lg font-bold mb-4">📊 收益明细（最近1个月）</h2>
    <p class="text-sm text-slate-500 dark:text-slate-400 mb-4">当日盈亏 = 市值变化 − 当日新增投入（定投 / 单笔买入 / 补录），反映真实收益</p>
    <div style="height: 288px; position: relative;">
      <canvas id="dailyPnLChart" style="display: block; width: 100%; height: 100%;"></canvas>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full text-left text-sm">
        <thead>
          <tr class="border-b border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-400">
            <th class="py-2">日期</th>
            <th class="py-2 text-right">当日总市值</th>
            <th class="py-2 text-right">当日盈亏</th>
            <th class="py-2 text-right">涨跌幅</th>
            <th class="py-2 text-right">新增投入</th>
          </tr>
        </thead>
        <tbody id="dailyPnLTable"></tbody>
      </table>
    </div>
  </div>

  <script>
    (function() {{
      const palette = {palette};
      const platformPalette = {platform_palette};

      function safeChart(id, factory) {{
        try {{
          if (typeof Chart !== 'undefined') factory();
          else console.warn('Chart.js not loaded, skipping chart #' + id);
        }} catch(e) {{
          console.warn('Chart #' + id + ' failed:', e.message);
        }}
      }}

      safeChart('allocationChart', () => new Chart(document.getElementById('allocationChart'), {{
      type: 'doughnut',
      data: {{
        labels: {js_category_labels},
        datasets: [{{
          data: {js_category_data},
          backgroundColor: palette,
          borderWidth: 0
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: {{ position: 'bottom' }} }}
      }}
    }}));

      safeChart('platformChart', () => new Chart(document.getElementById('platformChart'), {{
      type: 'pie',
      data: {{
        labels: {js_platform_labels},
        datasets: [{{
          data: {js_platform_data},
          backgroundColor: platformPalette,
          borderWidth: 0
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: {{ position: 'bottom' }} }}
      }}
    }}));

      safeChart('trendChart', () => new Chart(document.getElementById('trendChart'), {{
      type: 'line',
      data: {{
        labels: {js_trend_labels},
        datasets: [{{
          label: '总市值',
          data: {js_trend_values},
          borderColor: '#3b82f6',
          backgroundColor: 'rgba(59, 130, 246, 0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: 3
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        scales: {{ y: {{ beginAtZero: false }} }},
        plugins: {{ legend: {{ display: false }} }}
      }}
    }}));

    const fundDatasets = {js_fund_datasets};
    if (fundDatasets.length > 0) {{
        safeChart('fundTrendChart', () => {{
          const labels = fundDatasets[0].dates;
          const datasets = fundDatasets.map((ds, idx) => ({{
            label: ds.name,
            data: ds.values,
            borderColor: palette[idx % palette.length],
            backgroundColor: 'transparent',
            tension: 0.3,
            pointRadius: 2,
            borderWidth: 2
          }}));
          new Chart(document.getElementById('fundTrendChart'), {{
        type: 'line',
        data: {{ labels, datasets }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          interaction: {{ mode: 'index', intersect: false }},
          scales: {{ y: {{ beginAtZero: false }} }},
          plugins: {{ legend: {{ position: 'bottom' }} }}
        }}
      }});
        }});
    }}

    // 基准对比：组合 vs 沪深300 vs 中证全债（归一化）
    const benchmark = {js_benchmark};
    if (benchmark && benchmark.labels && benchmark.labels.length > 1) {{
      safeChart('benchmarkChart', () => new Chart(document.getElementById('benchmarkChart'), {{
        type: 'line',
        data: {{
          labels: benchmark.labels,
          datasets: [
            {{ label: '我的组合', data: benchmark.portfolio, borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.08)', fill: true, tension: 0.3, pointRadius: 2, borderWidth: 2.5 }},
            {{ label: '沪深300', data: benchmark.hs300, borderColor: '#f59e0b', backgroundColor: 'transparent', tension: 0.3, pointRadius: 2, borderWidth: 1.5 }},
            {{ label: '中证全债', data: benchmark.zhzq, borderColor: '#10b981', backgroundColor: 'transparent', tension: 0.3, pointRadius: 2, borderWidth: 1.5 }}
          ]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          interaction: {{ mode: 'index', intersect: false }},
          scales: {{ y: {{ beginAtZero: false }} }},
          plugins: {{ legend: {{ position: 'bottom' }} }}
        }}
      }}));
    }}

    // 单只基金收益贡献
    const profitData = {js_profit_data};
    if (profitData && profitData.length > 0) {{
      safeChart('profitChart', () => new Chart(document.getElementById('profitChart'), {{
        type: 'bar',
        data: {{
          labels: profitData.map(d => d.name),
          datasets: [{{
            label: '持有收益（元）',
            data: profitData.map(d => d.pnl),
            backgroundColor: profitData.map(d => d.pnl >= 0 ? 'rgba(239,68,68,0.7)' : 'rgba(16,185,129,0.7)'),
            borderRadius: 4,
            borderSkipped: false
          }}]
        }},
        options: {{
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          plugins: {{
            legend: {{ display: false }},
            tooltip: {{
              callbacks: {{
                label: function(ctx) {{
                  const val = ctx.raw;
                  return (val >= 0 ? '+' : '') + val.toFixed(2) + ' 元';
                }}
              }}
            }}
          }},
          scales: {{
            x: {{ grid: {{ color: 'rgba(148, 163, 184, 0.2)' }}, ticks: {{ callback: function(v) {{ return v.toFixed(0); }} }} }},
            y: {{ grid: {{ display: false }}, ticks: {{ callback: function(val) {{ return val.length > 14 ? val.slice(0, 14) + '…' : val; }} }} }}
          }}
        }}
      }}));
    }}

    // 收益明细：基于总市值走势计算每日盈亏
    (function() {{
      const labels = {js_trend_labels};
      const values = {js_trend_values};

      const pnlLabels = [];
      const pnlValues = [];
      const pnlColors = [];
      let tableHTML = '';
      let totalProfit = 0;
      let profitDays = 0;
      let lossDays = 0;

      // 当日盈亏 = 市值变化 − 新增投入（定投/单笔买入/补录），避免把存入的
      // 本金算成收益。历史旧数据缺少 total_invested 字段时，用最近的已知值
      // 向后回填（倒序扫描），保证相邻两天口径一致。
      const investedRaw = {js_trend_invested};
      const invested = [];
      let nextInv = null;
      for (let i = investedRaw.length - 1; i >= 0; i--) {{
        if (investedRaw[i] != null) nextInv = investedRaw[i];
        invested[i] = nextInv;
      }}

      for (let i = 1; i < values.length; i++) {{
        const inflow = (invested[i] != null && invested[i - 1] != null)
          ? invested[i] - invested[i - 1]
          : 0;
        const change = values[i] - values[i - 1] - inflow;
        const pct = values[i - 1] === 0 ? 0 : (change / values[i - 1] * 100);
        const colorClass = change >= 0 ? 'text-red-500' : 'text-emerald-500';
        const sign = change >= 0 ? '+' : '';
        const bgColor = change >= 0 ? 'rgba(239, 68, 68, 0.7)' : 'rgba(16, 185, 129, 0.7)';

        if (change >= 0) {{ profitDays++; }} else {{ lossDays++; }}
        totalProfit += change;

        pnlLabels.push(labels[i]);
        pnlValues.push(change);
        pnlColors.push(bgColor);

        const investNote = inflow > 0.005
          ? '+' + inflow.toFixed(2)
          : '—';

        tableHTML += `<tr class="border-b border-slate-100 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-700/50">
          <td class="py-2">${{labels[i]}}</td>
          <td class="py-2 text-right">${{values[i].toFixed(2)}}</td>
          <td class="py-2 text-right ${{colorClass}}">${{sign}}${{change.toFixed(2)}}</td>
          <td class="py-2 text-right ${{colorClass}}">${{sign}}${{pct.toFixed(2)}}%</td>
          <td class="py-2 text-right">${{investNote}}</td>
        </tr>`;
      }}

      const tableEl = document.getElementById('dailyPnLTable');
      if (tableEl) tableEl.innerHTML = tableHTML;

      const summaryDiv = document.createElement('div');
      summaryDiv.className = 'grid grid-cols-1 md:grid-cols-3 gap-4 mb-4';
      summaryDiv.innerHTML = `
        <div class="bg-slate-50 dark:bg-slate-700/50 rounded-xl p-4">
          <p class="text-xs text-slate-500 dark:text-slate-400">近1月累计盈亏</p>
          <p class="text-xl font-bold ${{totalProfit >= 0 ? 'text-red-500' : 'text-emerald-500'}}">${{totalProfit >= 0 ? '+' : ''}}${{totalProfit.toFixed(2)}}</p>
        </div>
        <div class="bg-slate-50 dark:bg-slate-700/50 rounded-xl p-4">
          <p class="text-xs text-slate-500 dark:text-slate-400">盈利天数 / 亏损天数</p>
          <p class="text-xl font-bold">${{profitDays}} / ${{lossDays}}</p>
        </div>
        <div class="bg-slate-50 dark:bg-slate-700/50 rounded-xl p-4">
          <p class="text-xs text-slate-500 dark:text-slate-400">胜率</p>
          <p class="text-xl font-bold">${{pnlValues.length > 0 ? Math.round(profitDays / pnlValues.length * 100) : 0}}%</p>
        </div>
      `;
      const container = document.getElementById('dailyPnLTable').parentElement.parentElement;
      container.insertBefore(summaryDiv, container.children[2]);

      safeChart('dailyPnLChart', () => new Chart(document.getElementById('dailyPnLChart'), {{
        type: 'bar',
        data: {{
          labels: pnlLabels,
          datasets: [{{
            label: '当日盈亏',
            data: pnlValues,
            backgroundColor: pnlColors,
            borderRadius: 4,
            borderSkipped: false
          }}]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          plugins: {{
            legend: {{ display: false }},
            tooltip: {{
              callbacks: {{
                label: function(ctx) {{
                  const val = ctx.raw;
                  return (val >= 0 ? '+' : '') + val.toFixed(2) + ' 元';
                }}
              }}
            }}
          }},
          scales: {{
            x: {{ grid: {{ display: false }} }},
            y: {{
              grid: {{ color: 'rgba(148, 163, 184, 0.2)' }},
              ticks: {{
                callback: function(v) {{ return v.toFixed(0); }}
              }}
            }}
          }}
        }}
      }}));
    }})();
    }})();
  </script>
"""


FUND_GROUP_JS = """
<script>
/* Grouped holdings table: dimension switching (方向/类型/平台/类别/全部), live
   search, and per-group sorting. Single state + applyState() rebuild — headers
   are moved (never cloned) from a pool collected at init; their array order is
   the canonical group order rendered by the server. */
(function () {
  var table = document.getElementById('fund-table');
  if (!table) return;
  var tbody = table.querySelector('tbody');
  var input = document.getElementById('fund-search');
  var btns = document.getElementById('fund-group-btns');
  var ths = Array.prototype.slice.call(table.querySelectorAll('thead th'));

  var DIMS = ['theme', 'type', 'platform', 'category'];
  var KEY_OF = { theme: 'gkeyTheme', type: 'gkeyType', platform: 'gkeyPlatform', category: 'gkeyCategory' };
  var state = { dim: 'theme', sortIdx: null, sortDir: null, query: '' };

  /* Header node pool per dimension (order = canonical group order). */
  var headers = {};
  DIMS.forEach(function (d) { headers[d] = []; });
  Array.prototype.slice.call(tbody.querySelectorAll('tr[data-gkind]')).forEach(function (h) {
    var k = h.getAttribute('data-gkind');
    if (headers[k]) headers[k].push(h);
  });

  function cellValue(row, idx) {
    var td = row.cells[idx];
    if (!td) return null;
    var text = td.textContent.trim();
    if ((ths[idx].getAttribute('data-sort') || 'text') === 'num') {
      var n = parseFloat(text.replace(/[,%+\\s¥]/g, ''));
      return isNaN(n) ? null : n;   // null = unparsable ("-"), sinks to bottom
    }
    return text;
  }

  function applyState() {
    /* 1) Pull every header row out of the tbody back into its pool. */
    DIMS.forEach(function (d) {
      headers[d].forEach(function (h) { if (h.parentNode === tbody) tbody.removeChild(h); });
    });

    /* 2) Bucket fund rows by the current dimension (single bucket for 'all'). */
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr[data-code]'));
    var buckets = {};
    var order = [];
    if (state.dim === 'all') {
      buckets[''] = rows;
      order.push('');
    } else {
      rows.forEach(function (r) {
        var k = r.dataset[KEY_OF[state.dim]] || '未分类';
        if (!buckets[k]) { buckets[k] = []; order.push(k); }
        buckets[k].push(r);
      });
      /* Canonical order: headers pool order first, then any unknown keys. */
      var poolKeys = headers[state.dim].map(function (h) { return h.getAttribute('data-gkey'); });
      order.sort(function (a, b) {
        var ia = poolKeys.indexOf(a), ib = poolKeys.indexOf(b);
        if (ia === -1 && ib === -1) return 0;
        if (ia === -1) return 1;
        if (ib === -1) return -1;
        return ia - ib;
      });
    }

    /* 3) Sort within each bucket (group headers never participate). */
    if (state.sortIdx !== null) {
      var si = state.sortIdx, sd = state.sortDir;
      Object.keys(buckets).forEach(function (k) {
        buckets[k].sort(function (a, b) {
          var av = cellValue(a, si), bv = cellValue(b, si);
          if (av === null && bv === null) return 0;
          if (av === null) return 1;
          if (bv === null) return -1;
          if (typeof av === 'number') return sd === 'asc' ? av - bv : bv - av;
          var c = String(av).localeCompare(String(bv), 'zh');
          return sd === 'asc' ? c : -c;
        });
      });
    }

    /* 4) Rebuild tbody as header + its rows blocks (move, never clone). */
    var frag = document.createDocumentFragment();
    if (state.dim === 'all') {
      buckets[''].forEach(function (r) { frag.appendChild(r); });
    } else {
      order.forEach(function (k) {
        var header = null;
        headers[state.dim].forEach(function (h) {
          if (h.getAttribute('data-gkey') === k) header = h;
        });
        if (header) frag.appendChild(header);
        (buckets[k] || []).forEach(function (r) { frag.appendChild(r); });
      });
    }
    tbody.appendChild(frag);

    /* 5) Search filter: rows match text; a header stays visible only if its
          group still has a matching row (headers themselves never match). */
    var q = state.query;
    var groupMatch = {};
    rows.forEach(function (r) {
      var ok = !q || r.textContent.toLowerCase().indexOf(q) !== -1;
      if (ok && state.dim !== 'all') {
        var k = r.dataset[KEY_OF[state.dim]] || '未分类';
        groupMatch[k] = (groupMatch[k] || 0) + 1;
      }
      r.style.display = ok ? '' : 'none';
    });
    if (state.dim !== 'all') {
      headers[state.dim].forEach(function (h) {
        var k = h.getAttribute('data-gkey');
        var shown = (groupMatch[k] || 0) > 0;
        h.style.display = shown ? '' : 'none';
        var gc = h.querySelector('.gcount');
        if (gc) {
          var total = (buckets[k] || []).length;
          gc.textContent = q ? (groupMatch[k] + '/' + total + ' 只') : total + ' 只';
        }
      });
    } else {
      DIMS.forEach(function (d) { headers[d].forEach(function (h) { h.style.display = 'none'; }); });
    }
  }

  /* Dimension switch buttons. */
  if (btns) {
    var btnEls = Array.prototype.slice.call(btns.querySelectorAll('button[data-gdim]'));
    btnEls.forEach(function (btn) {
      btn.addEventListener('click', function () {
        state.dim = btn.getAttribute('data-gdim');
        btnEls.forEach(function (b) {
          var on = b === btn;
          b.setAttribute('aria-pressed', on ? 'true' : 'false');
          b.className = on
            ? 'px-3 py-1.5 rounded-full text-sm font-medium bg-blue-600 text-white'
            : 'px-3 py-1.5 rounded-full text-sm font-medium bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300';
        });
        applyState();
      });
    });
  }

  /* Live search. */
  if (input) {
    input.addEventListener('input', function () {
      state.query = input.value.trim().toLowerCase();
      applyState();
    });
  }

  /* Click-to-sort: click a column header, click again to reverse. Sorting
     always stays within the current grouping. */
  ths.forEach(function (th, idx) {
    th.style.cursor = 'pointer';
    th.style.userSelect = 'none';
    th.addEventListener('click', function () {
      var dir = th.getAttribute('data-dir') === 'asc' ? 'desc' : 'asc';
      ths.forEach(function (t) { t.removeAttribute('data-dir'); });
      th.setAttribute('data-dir', dir);
      state.sortIdx = idx;
      state.sortDir = dir;

      ths.forEach(function (t) {
        var s = t.querySelector('.sort-arrow');
        if (s) s.parentNode.removeChild(s);
      });
      var arrow = document.createElement('span');
      arrow.className = 'sort-arrow';
      arrow.textContent = dir === 'asc' ? ' ▲' : ' ▼';
      th.appendChild(arrow);
      applyState();
    });
  });

  /* Normalize the server-rendered default view (idempotent). */
  applyState();
})();
</script>
"""


# ---------------------------------------------------------------------------
# Theme (investment direction) grouping — 持仓模块化
# ---------------------------------------------------------------------------
THEME_ORDER = ["纳指100", "A股科技", "稳健固收", "观察停投"]
THEME_META = {
    "纳指100": ("🇺🇸", "blue"),
    "A股科技": ("🚀", "rose"),
    "稳健固收": ("🛡️", "emerald"),
    "观察停投": ("⏸️", "slate"),
}
BADGE_CLS = {
    "blue": "bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-300",
    "rose": "bg-rose-50 dark:bg-rose-900/20 text-rose-600 dark:text-rose-300",
    "emerald": "bg-emerald-50 dark:bg-emerald-900/20 text-emerald-600 dark:text-emerald-300",
    "slate": "bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300",
}


def monthly_sip_amount(status):
    """Parse investment_status into an approximate monthly amount (元).
    每月X日Y元 → Y；每周X定投Y元 → Y×4.3；每日定投Y元 → Y×30；否则 0."""
    m = re.search(r"每月\s*(\d+)\s*日\s*([\d.]+)\s*元", status or "")
    if m:
        return round(float(m.group(2)), 2)
    m = re.search(r"每周[一二三四五六日]\s*定投\s*([\d.]+)\s*元", status or "")
    if m:
        return round(float(m.group(1)) * 4.3, 2)
    m = re.search(r"每日定投\s*([\d.]+)\s*元", status or "")
    if m:
        return round(float(m.group(1)) * 30, 2)
    return 0.0


def build_theme_summary(details):
    """Aggregate per-theme: fund count / market value / monthly SIP."""
    groups = {}
    for d in details:
        key = d.get("theme") or "未分类"
        g = groups.setdefault(key, {"count": 0, "value": 0.0, "sip": 0.0})
        g["count"] += 1
        g["sip"] += monthly_sip_amount(d.get("investment_status", ""))
        if "error" not in d:
            g["value"] += d.get("current_value", 0.0)
    ordered = [k for k in THEME_ORDER if k in groups] + [k for k in groups if k not in THEME_ORDER]
    return [(k, groups[k]) for k in ordered]


def render_theme_cards(details, total_value):
    """Direction summary cards shown above the holdings table."""
    cards = []
    for key, g in build_theme_summary(details):
        emoji, color = THEME_META.get(key, ("🧩", "slate"))
        badge = BADGE_CLS.get(color, BADGE_CLS["slate"])
        pct = g["value"] / total_value * 100 if total_value > 0 else 0.0
        cards.append(f"""
    <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow">
      <div class="flex items-center justify-between mb-2">
        <p class="font-medium text-base">{emoji} {_esc(key)}</p>
        <span class="px-2 py-1 rounded-full text-xs {badge}">{g['count']} 只</span>
      </div>
      <p class="text-2xl font-bold">{format_money(g['value'])}</p>
      <p class="mt-2 text-xs text-slate-500 dark:text-slate-400">占总市值 {pct:.1f}%</p>
      <p class="text-xs text-slate-500 dark:text-slate-400">月定投 约 {format_money(g['sip'])}</p>
    </div>""")
    return f"""
  <!-- Theme summary cards -->
  <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
    {''.join(cards)}
  </div>
"""


def render_table(portfolio):
    details = portfolio["details"]
    total_value = portfolio["total_value"]

    def gkey(d, dim):
        v = {"theme": d.get("theme"), "type": d.get("type"),
             "platform": d.get("platform"), "category": d.get("category")}[dim]
        return v or "未分类"

    def group_stats(dim, key):
        members = [d for d in details if gkey(d, dim) == key]
        value = sum(d.get("current_value", 0.0) for d in members if "error" not in d)
        sip = sum(monthly_sip_amount(d.get("investment_status", "")) for d in members)
        return len(members), value, sip

    def group_header_html(dim, key, hidden=False):
        n, value, sip = group_stats(dim, key)
        pct = value / total_value * 100 if total_value > 0 else 0.0
        emoji, _ = THEME_META.get(key, ("", "slate"))
        label = f"{emoji} {key}" if dim == "theme" and emoji else key
        hidden_attr = ' style="display:none"' if hidden else ""
        return f"""
        <tr class="group-header bg-slate-50 dark:bg-slate-700/40" data-gkind="{dim}" data-gkey="{_esc(key)}"{hidden_attr}>
          <td colspan="13" class="px-3 py-2">
            <span class="font-semibold text-slate-700 dark:text-slate-200">{_esc(label)}</span>
            <span class="ml-2 text-xs text-slate-400"><span class="gcount">{n} 只</span> · 市值 {format_money(value)} · 占 {pct:.1f}% · 月定投 约 {format_money(sip)}</span>
          </td>
        </tr>"""

    def row_html(d):
        attrs = ('data-code="{code}" data-gkey-theme="{gtheme}" data-gkey-type="{gtype}" '
                 'data-gkey-platform="{gplatform}" data-gkey-category="{gcategory}"').format(
            code=_esc(d["code"]), gtheme=_esc(gkey(d, "theme")), gtype=_esc(gkey(d, "type")),
            gplatform=_esc(gkey(d, "platform")), gcategory=_esc(gkey(d, "category")))
        if "error" in d:
            pct_of_total = d["amount"] / total_value * 100 if total_value > 0 else 0.0
            return f"""
        <tr class="border-b border-slate-100 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-700/50" {attrs}>
          <td class="py-3 font-medium">{_esc(d['name'])}</td>
          <td class="py-3 font-mono text-xs">{_esc(d['code'])}</td>
          <td class="py-3">{_esc(d['platform'])}</td>
          <td class="py-3"><span class="px-2 py-1 rounded-full bg-slate-100 dark:bg-slate-700 text-xs">{_esc(d['category'])}</span></td>
          <td class="py-3 text-right">{format_money(d['total_invested'])}</td>
          <td class="py-3 text-right">{format_money(d['amount'])}</td>
          <td class="py-3 text-right text-slate-400">-</td>
          <td class="py-3 text-right text-slate-400">-</td>
          <td class="py-3 text-right text-slate-400">-</td>
          <td class="py-3 text-right text-slate-400">-</td>
          <td class="py-3 text-right text-slate-400">-</td>
          <td class="py-3 text-right">{pct_of_total:.1f}%</td>
          <td class="py-3 text-right text-xs text-red-400">{_esc(d['error'])}</td>
        </tr>"""
        pct_of_total = d["current_value"] / total_value * 100 if total_value > 0 else 0.0
        return f"""
        <tr class="border-b border-slate-100 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-700/50" {attrs}>
          <td class="py-3 font-medium">{_esc(d['name'])}</td>
          <td class="py-3 font-mono text-xs">{_esc(d['code'])}</td>
          <td class="py-3">{_esc(d['platform'])}</td>
          <td class="py-3"><span class="px-2 py-1 rounded-full bg-slate-100 dark:bg-slate-700 text-xs">{_esc(d['category'])}</span></td>
          <td class="py-3 text-right">{format_money(d['total_invested'])}</td>
          <td class="py-3 text-right">{format_money(d['amount'])}</td>
          <td class="py-3 text-right">{format_nav(d['nav'])}</td>
          <td class="py-3 text-right">{format_nav(d['estimate'])}</td>
          <td class="py-3 text-right {cn_color(d['change_pct'])}">{format_pct(d['change_pct'])}</td>
          <td class="py-3 text-right {cn_color(d['daily_pnl'])}">{'+' if d['daily_pnl'] >= 0 else ''}{format_money(d['daily_pnl'])}</td>
          <td class="py-3 text-right {cn_color(d['hold_return_pct'])}">{format_pct(d['hold_return_pct'])}</td>
          <td class="py-3 text-right font-medium">{format_money(d['current_value'])}</td>
          <td class="py-3 text-right">{pct_of_total:.1f}%</td>
        </tr>"""

    # Server-rendered default view: rows grouped by theme with a header before
    # each group (readable without JS). Headers for the other three dimensions
    # are appended hidden at the end as the JS node pool.
    present_themes = {gkey(d, "theme") for d in details}
    extra_themes = []
    for d in details:
        k = gkey(d, "theme")
        if k not in THEME_ORDER and k not in extra_themes:
            extra_themes.append(k)
    theme_keys = [k for k in THEME_ORDER if k in present_themes] + extra_themes

    rows_by_theme = {}
    for d in details:
        rows_by_theme.setdefault(gkey(d, "theme"), []).append(d)

    body_rows = []
    for k in theme_keys:
        body_rows.append(group_header_html("theme", k))
        for d in rows_by_theme[k]:
            body_rows.append(row_html(d))

    for dim in ("type", "platform", "category"):
        keys = []
        for d in details:
            k = gkey(d, dim)
            if k not in keys:
                keys.append(k)
        for k in keys:
            body_rows.append(group_header_html(dim, k, hidden=True))

    rows_html = "\n".join(body_rows)

    return f"""
  {render_theme_cards(details, total_value)}
  <!-- Fund table -->
  <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow mb-8 overflow-x-auto">
    <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
      <h2 class="text-lg font-bold">持仓明细</h2>
      <div class="flex flex-wrap gap-2" id="fund-group-btns" role="group" aria-label="分组维度">
        <button type="button" data-gdim="theme" class="px-3 py-1.5 rounded-full text-sm font-medium bg-blue-600 text-white" aria-pressed="true">方向</button>
        <button type="button" data-gdim="type" class="px-3 py-1.5 rounded-full text-sm font-medium bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300" aria-pressed="false">类型</button>
        <button type="button" data-gdim="platform" class="px-3 py-1.5 rounded-full text-sm font-medium bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300" aria-pressed="false">平台</button>
        <button type="button" data-gdim="category" class="px-3 py-1.5 rounded-full text-sm font-medium bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300" aria-pressed="false">类别</button>
        <button type="button" data-gdim="all" class="px-3 py-1.5 rounded-full text-sm font-medium bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300" aria-pressed="false">全部</button>
      </div>
      <input id="fund-search" type="search" placeholder="🔍 搜索基金（名称 / 代码 / 平台 / 类别 / 方向）"
             class="w-full md:w-80 px-4 py-2 rounded-lg border border-slate-200 dark:border-slate-600 bg-slate-50 dark:bg-slate-700 text-sm outline-none focus:border-blue-400" />
    </div>
    <table class="w-full text-left text-sm" id="fund-table">
      <thead>
        <tr class="border-b border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-400">
          <th class="py-2 cursor-pointer select-none" data-sort="text">基金名称</th>
          <th class="py-2 cursor-pointer select-none" data-sort="text">代码</th>
          <th class="py-2 cursor-pointer select-none" data-sort="text">平台</th>
          <th class="py-2 cursor-pointer select-none" data-sort="text">类别</th>
          <th class="py-2 text-right cursor-pointer select-none" data-sort="num">累计投入</th>
          <th class="py-2 text-right cursor-pointer select-none" data-sort="num">持仓金额</th>
          <th class="py-2 text-right cursor-pointer select-none" data-sort="num">净值</th>
          <th class="py-2 text-right cursor-pointer select-none" data-sort="num">估算</th>
          <th class="py-2 text-right cursor-pointer select-none" data-sort="num">涨跌幅</th>
          <th class="py-2 text-right cursor-pointer select-none" data-sort="num">当日盈亏</th>
          <th class="py-2 text-right cursor-pointer select-none" data-sort="num">持有收益</th>
          <th class="py-2 text-right cursor-pointer select-none" data-sort="num">当前市值</th>
          <th class="py-2 text-right cursor-pointer select-none" data-sort="num">占比</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>
""" + FUND_GROUP_JS


def render_sip_reminder(funds, today_obj):
    days_to_deduct = []
    for f in funds:
        status = f.get("investment_status", "")
        if "每月" in status and "日" in status:
            m = re.search(r'(\d+)\s*日', status)
            if m:
                day = int(m.group(1))
                if day == today_obj.day or day > today_obj.day:
                    days_to_deduct.append(f)
    if not days_to_deduct:
        return ""
    items = "".join(f"<li>{_esc(f['name'])}（{_esc(f['platform'])}）— {_esc(f['investment_status'])}</li>\n" for f in days_to_deduct)
    return f"""
  <div class="bg-blue-50 dark:bg-blue-900/20 border border-blue-100 dark:border-blue-800 rounded-2xl p-6 mb-8">
    <h3 class="font-bold mb-2">🗓️ 定投提醒</h3>
    <ul class="list-disc list-inside text-sm">
      {items}
    </ul>
  </div>
"""


def render_footer():
    return """
</div>
</body>
</html>
"""


def build_fund_line_datasets(history, fund_codes):
    """Build per-fund NAV trend datasets from history."""
    entries = history.get("entries", [])
    if len(entries) < 2:
        return []

    datasets = []
    for code in fund_codes:
        dates = []
        values = []
        for e in entries:
            if code in e.get("funds", {}):
                dates.append(e["date"])
                values.append(e["funds"][code].get("estimate", e["funds"][code].get("nav", 0)))
        if len(dates) >= 2:
            # Find fund name from latest entry or fallback
            name = code
            datasets.append({"name": name, "dates": dates, "values": values})
    return datasets


# ---------------------------------------------------------------------------
# Benchmark, risk metrics, XIRR
# ---------------------------------------------------------------------------
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,{start},{end},{count},qfq"
BENCHMARK_INDEXES = {"hs300": "sh000300", "zhzq": "sh000012"}


def fetch_benchmark(cache, history):
    """Fetch daily closes for benchmark indexes (沪深300 / 中证全债), cached in fund_cache.json."""
    entries = history.get("entries", [])
    if not entries:
        return {"dates": {}}
    first_date = entries[0]["date"]
    last_date = entries[-1]["date"]

    cached = (cache.get("benchmark") or {}).get("dates", {})
    if cached and min(cached.keys()) <= first_date and max(cached.keys()) >= last_date:
        return {"dates": cached}

    start = (datetime.strptime(first_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    end = (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")
    dates = {}
    for key, code in BENCHMARK_INDEXES.items():
        url = TENCENT_KLINE_URL.format(code=code, start=start, end=end, count=90)
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"})
        try:
            with urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            rows = (payload.get("data") or {}).get(code, {}).get("day") or []
            for row in rows:
                if len(row) >= 3:
                    dates.setdefault(row[0], {})[key] = float(row[2])
        except Exception as e:
            print(f"[WARN] Benchmark {key} fetch failed: {e}")
    if dates:
        cache["benchmark"] = {"dates": dates}
    return {"dates": dates}


def build_benchmark_series(history, benchmark):
    """Align benchmark closes with history dates and normalize all series to 100 at start."""
    entries = history.get("entries", [])
    dates = benchmark.get("dates", {})
    aligned = []
    for e in entries:
        d = e["date"]
        hs = dates.get(d, {}).get("hs300")
        zz = dates.get(d, {}).get("zhzq")
        if hs is not None and zz is not None:
            aligned.append((d, e["total_value"], hs, zz))
    if len(aligned) < 2:
        return None
    p0, h0, z0 = aligned[0][1], aligned[0][2], aligned[0][3]
    return {
        "labels": [a[0] for a in aligned],
        "portfolio": [round(a[1] / p0 * 100, 2) for a in aligned],
        "hs300": [round(a[2] / h0 * 100, 2) for a in aligned],
        "zhzq": [round(a[3] / z0 * 100, 2) for a in aligned],
    }


def compute_risk_metrics(labels, values):
    """Max drawdown, current drawdown and longest underwater duration from a value series."""
    if len(values) < 2:
        return None
    peak = values[0]
    peak_date = labels[0]
    max_dd = 0.0
    max_dd_peak_date = labels[0]
    max_dd_trough_date = labels[0]
    current_dd = 0.0
    underwater_since = None
    longest_dd_days = 0

    for i, v in enumerate(values):
        if v >= peak:
            if underwater_since is not None:
                d0 = datetime.strptime(underwater_since, "%Y-%m-%d").date()
                d1 = datetime.strptime(labels[i], "%Y-%m-%d").date()
                longest_dd_days = max(longest_dd_days, (d1 - d0).days)
                underwater_since = None
            peak, peak_date = v, labels[i]
        else:
            dd = (v / peak - 1) * 100
            if underwater_since is None:
                underwater_since = labels[i]
            if dd < current_dd:
                current_dd = dd
            if dd < max_dd:
                max_dd = dd
                max_dd_peak_date = peak_date
                max_dd_trough_date = labels[i]

    if underwater_since is not None:
        d0 = datetime.strptime(underwater_since, "%Y-%m-%d").date()
        d1 = datetime.strptime(labels[-1], "%Y-%m-%d").date()
        longest_dd_days = max(longest_dd_days, (d1 - d0).days)

    return {
        "max_dd": max_dd,
        "max_dd_peak_date": max_dd_peak_date,
        "max_dd_trough_date": max_dd_trough_date,
        "current_dd": current_dd,
        "longest_dd_days": longest_dd_days,
        "peak_value": peak,
        "peak_date": peak_date,
    }


def compute_xirr(history, config, portfolio):
    """Estimate annualized return (XIRR) from the DCA plan schedule and current value.

    Cash flows are reconstructed: initial capital at the first recorded date,
    plan-based DCA payments inside the window, and the final portfolio value.
    """
    entries = history.get("entries", [])
    if len(entries) < 2:
        return None
    start_date = date.fromisoformat(entries[0]["date"])
    end_date = date.fromisoformat(entries[-1]["date"])

    flows = []  # (date, amount); negative = money in, positive = money out
    sip_total = 0.0
    first_flow_date = start_date

    def add_flow(d, amt):
        flows.append((d, amt))

    # DCA payments from the plan within (start, end]
    for f in config["funds"]:
        m = re.search(r"每月\s*(\d+)\s*日\s*([\d,]+)\s*元", f.get("investment_status", ""))
        if not m:
            continue
        day = int(m.group(1))
        amount = float(m.group(2).replace(",", ""))
        y, mo = start_date.year, start_date.month
        while (y, mo) <= (end_date.year, end_date.month):
            try:
                d = date(y, mo, day)
            except ValueError:
                y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
                continue
            if start_date < d <= end_date:
                add_flow(d, -amount)
                sip_total += amount
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)

    initial = portfolio["total_invested"] - sip_total
    if initial <= 0:
        return None
    add_flow(start_date, -initial)
    add_flow(end_date, portfolio["total_value"])

    # Newton-Raphson for XIRR (rate r such that sum(flow / (1+r)^t) = 0)
    days = max((end_date - start_date).days, 1)
    rate = max((portfolio["total_value"] / initial - 1) * 365 / days, 0.01)
    rate = min(rate, 0.5)
    for _ in range(200):
        total = 0.0
        deriv = 0.0
        for d, amt in flows:
            t = (d - first_flow_date).days / 365.0
            denom = (1 + rate) ** t
            total += amt / denom
            deriv += -t * amt / (denom * (1 + rate))
        if deriv == 0 or not all(abs(x) < 1e300 for x in (total, deriv)):
            break
        new_rate = rate - total / deriv
        if abs(new_rate - rate) < 1e-8:
            rate = new_rate
            break
        rate = new_rate
        if rate <= -0.999:
            rate = None
            break
    return rate * 100 if rate is not None else None


def render_eval_section(metrics, xirr_rate):
    """Risk & return stat cards: XIRR, max drawdown, current drawdown, longest underwater period."""
    if metrics is None:
        return ""
    if xirr_rate is None:
        xirr_html = '<p class="text-2xl font-bold text-slate-400">—</p>'
    else:
        xirr_html = f'<p class="text-2xl font-bold {cn_color(xirr_rate)}">{format_pct(xirr_rate)}</p>'
    return f"""
  <!-- Risk & return evaluation -->
  <div class="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow mb-8">
    <h2 class="text-lg font-bold mb-4">📐 组合评估</h2>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
      <div class="bg-slate-50 dark:bg-slate-700/50 rounded-xl p-4">
        <p class="text-xs text-slate-500 dark:text-slate-400">年化收益（XIRR 估算）</p>
        {xirr_html}
        <p class="text-xs text-slate-400 mt-1">按定投计划 + 当前市值估算</p>
      </div>
      <div class="bg-slate-50 dark:bg-slate-700/50 rounded-xl p-4">
        <p class="text-xs text-slate-500 dark:text-slate-400">历史最大回撤</p>
        <p class="text-2xl font-bold text-emerald-500">{format_pct(metrics['max_dd'])}</p>
        <p class="text-xs text-slate-400 mt-1">{_esc(metrics['max_dd_peak_date'])} → {_esc(metrics['max_dd_trough_date'])}</p>
      </div>
      <div class="bg-slate-50 dark:bg-slate-700/50 rounded-xl p-4">
        <p class="text-xs text-slate-500 dark:text-slate-400">当前回撤（距峰值）</p>
        <p class="text-2xl font-bold text-emerald-500">{format_pct(metrics['current_dd'])}</p>
        <p class="text-xs text-slate-400 mt-1">峰值 {format_money(metrics['peak_value'])}（{_esc(metrics['peak_date'])}）</p>
      </div>
      <div class="bg-slate-50 dark:bg-slate-700/50 rounded-xl p-4">
        <p class="text-xs text-slate-500 dark:text-slate-400">最长连续回撤</p>
        <p class="text-2xl font-bold">{metrics['longest_dd_days']} 天</p>
        <p class="text-xs text-slate-400 mt-1">基于已记录的净值序列</p>
      </div>
    </div>
    <p class="text-xs text-slate-400 mt-4 italic">* XIRR 为估算值：现金流按定投计划重建，未记录逐笔真实交易，数据积累越长越准确。</p>
  </div>
"""


def render_html(config, history, portfolio, today, analysis=None, cache_used=False, today_obj=None, benchmark=None):
    if today_obj is None:
        today_obj = datetime.strptime(today, "%Y-%m-%d")
    funds = config["funds"]
    details = portfolio["details"]
    entries = history.get("entries", [])

    # Category allocation
    category_values = {}
    for d in details:
        if "error" in d:
            continue
        category_values[d["category"]] = category_values.get(d["category"], 0.0) + d["current_value"]
    category_labels = list(category_values.keys())
    category_data = [round(v, 2) for v in category_values.values()]

    # Platform distribution
    platform_values = {}
    for d in details:
        if "error" in d:
            continue
        platform_values[d["platform"]] = platform_values.get(d["platform"], 0.0) + d["current_value"]
    platform_labels = list(platform_values.keys())
    platform_data = [round(v, 2) for v in platform_values.values()]

    # Total value trend
    trend_labels = [e["date"] for e in entries]
    trend_values = [e["total_value"] for e in entries]
    trend_invested = [e.get("total_invested") for e in entries]

    # Per-fund trend
    fund_codes = [f["code"] for f in funds]
    fund_line_datasets = build_fund_line_datasets(history, fund_codes)
    # Enrich dataset names
    name_map = {f["code"]: f["name"] for f in funds}
    for ds in fund_line_datasets:
        ds["name"] = name_map.get(ds["name"], ds["name"])

    # Risk metrics / XIRR / benchmark / per-fund profit
    metrics = compute_risk_metrics(trend_labels, trend_values)
    xirr_rate = compute_xirr(history, config, portfolio)
    benchmark_series = build_benchmark_series(history, benchmark)
    profit_data = [{"name": d["name"], "pnl": round(d["hold_pnl"], 2)} for d in details if "error" not in d]
    profit_data.sort(key=lambda x: x["pnl"], reverse=True)

    # Status note
    notes = []
    if not is_trading_day(today_obj):
        notes.append("今日为非交易日，显示最新可用数据。")
    if cache_used:
        notes.append("部分基金使用了本地缓存数据，可能存在延迟。")
    nav_dates = {d.get("nav_date") for d in details if "error" not in d and d.get("nav_date")}
    if nav_dates and all(nd != today for nd in nav_dates):
        notes.append(f"最新净值日期为 {', '.join(sorted(nav_dates))}，尚未更新今日数据。")
    status_note = " ".join(notes)

    parts = []
    parts.append(render_header(today, status_note))
    parts.append(render_kpi_cards(portfolio))
    parts.append(render_eval_section(metrics, xirr_rate))
    parts.append(render_analysis(analysis))
    parts.append(render_charts(category_labels, category_data, trend_labels, trend_values, trend_invested, platform_labels, platform_data, fund_line_datasets, benchmark=benchmark_series, profit_data=profit_data))
    parts.append(render_table(portfolio))
    parts.append(render_sip_reminder(funds, today_obj))
    parts.append(render_footer())

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write("".join(parts))


def apply_sip_bookings(config, fetched, today_obj, trading):
    """Auto-book scheduled investment (定投) purchases.

    Parses each fund's investment_status (e.g. "每月 12 日 700 元" or
    "每日定投 10 元") and, on the due trading day, adds the booked shares
    (amount ÷ that day's NAV) to the holding and to total_invested. Shares are
    estimated from the NAV available at run time; the platform's confirmed
    shares may differ slightly (confirmation at the T-day NAV, and A-class
    funds may carry a small purchase fee). The per-fund `last_booking` field
    (persisted in funds.json, untouched by the dashboard UI) prevents
    double-booking. Manual buys/sells still need to be entered by the user via
    修改持仓.
    """
    if not trading:
        return []
    today = today_obj.strftime("%Y-%m-%d")
    booked = []
    for fund in config["funds"]:
        status = fund.get("investment_status", "")
        raw = fetched.get(fund["code"], {})
        dwjz = parse_float(raw.get("dwjz"), 0.0)
        if "error" in raw or dwjz <= 0:
            continue  # no NAV available yet — retry on the next run
        last = fund.get("last_booking", "") or ""
        m = re.match(r"每月\s*(\d+)\s*日\s*([\d.]+)\s*元", status)
        if m:
            # Book on the first trading day on/after the due day of the month
            if today_obj.day < int(m.group(1)) or last[:7] == today[:7]:
                continue
            amt = float(m.group(2))
        else:
            m = re.match(r"每周([一二三四五六日])\s*定投\s*([\d.]+)\s*元", status)
            if m:
                # Book once per week on the specified weekday (0=Monday)
                if today_obj.weekday() != "一二三四五六日".index(m.group(1)):
                    continue
                # Skip if already booked this week (week starts Monday)
                monday = today_obj - timedelta(days=today_obj.weekday())
                if last and datetime.strptime(last, "%Y-%m-%d") >= monday:
                    continue
                amt = float(m.group(2))
            else:
                m = re.search(r"每日定投\s*([\d.]+)\s*元", status)
                if not m or last == today:
                    continue
                amt = float(m.group(1))
        shares = float(fund.get("shares", 0.0) or 0.0)
        fund["shares"] = round(shares + amt / dwjz, 4)
        fund["total_invested"] = round(float(fund.get("total_invested") or amt) + amt, 2)
        # Keep amount consistent with shares × NAV so the manual-edit guard in
        # calculate_portfolio never mistakes a booking for a user edit.
        fund["amount"] = round(fund["shares"] * dwjz, 2)
        fund["last_booking"] = today
        booked.append((fund["code"], amt, dwjz, fund["shares"]))
    return booked


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    today = today_shanghai().strftime("%Y-%m-%d")
    print(f"[INFO] Updating fund dashboard for {today} (Asia/Shanghai)...")

    config = load_funds()
    if not config or not config.get("funds"):
        print("[ERROR] funds.json not found or empty.", file=sys.stderr)
        sys.exit(1)

    history = load_history()
    cache = load_cache()

    # Snapshot each fund's NAV as of the previous run (from cache) BEFORE the
    # fetch loop below overwrites the cache — used to detect manual edits.
    prev_nav_map = {}
    for fund in config["funds"]:
        cached = cache.get(fund["code"]) or {}
        nav = parse_float(cached.get("dwjz", 0.0), 0.0)
        if nav > 0:
            prev_nav_map[fund["code"]] = nav

    benchmark = fetch_benchmark(cache, history)
    print(f"[INFO] Benchmark data: {len(benchmark.get('dates', {}))} dates")

    fetched = {}
    cache_used = False
    for fund in config["funds"]:
        code = fund["code"]
        print(f"[INFO] Fetching {code}...")
        data, success = fetch_fund_cached(code, cache)
        fetched[code] = data
        if not success:
            cache_used = True
            print(f"[WARN] {code}: fetch failed, using cached data")
        if "error" in data and "_from_cache" not in data:
            print(f"[WARN] {code}: {data['error']}")

    today_obj = datetime.strptime(today, "%Y-%m-%d")
    trading = is_trading_day(today_obj)

    booked = apply_sip_bookings(config, fetched, today_obj, trading)
    for code, amt, dwjz, new_shares in booked:
        print(f"[INFO] 定投入账 {code}: +{amt:g} 元（净值 {dwjz}）→ 约 {new_shares:.4f} 份")

    portfolio = calculate_portfolio(config, fetched, prev_nav_map)

    if trading:
        history = update_history(history, today, portfolio)
        analysis = generate_analysis(config, portfolio, history, today)
        save_history(history)
    else:
        # On non-trading days, don't add duplicate history entries or sync amounts.
        # Still generate the dashboard with the latest available analysis.
        analysis = generate_analysis(config, portfolio, history, today)
        print(f"[INFO] {today} is not a trading day; skipping history update and amount sync.")

    save_cache(cache)
    render_html(config, history, portfolio, today, analysis=analysis, cache_used=cache_used, today_obj=today_obj, benchmark=benchmark)

    # Sync shares back to funds.json (always — shares are stable)
    shares_map = {d["code"]: d["_shares"] for d in portfolio["details"] if "error" not in d and d.get("_shares", 0) > 0}
    nav_map = {d["code"]: d["nav"] for d in portfolio["details"] if "error" not in d}
    updated = False
    for fund in config["funds"]:
        code = fund["code"]
        if code in shares_map:
            new_shares = round(shares_map[code], 4)
            if abs(float(fund.get("shares", 0)) - new_shares) > 0.0001:
                fund["shares"] = new_shares
                updated = True
            # Always keep amount in sync too (shares * dwjz = value at previous
            # close) and persist it — a stale amount is what made the
            # manual-edit guard misfire on ordinary NAV moves.
            if code in nav_map and nav_map[code] > 0:
                new_amount = round(new_shares * nav_map[code], 2)
                if abs(float(fund.get("amount", 0)) - new_amount) > 0.005:
                    fund["amount"] = new_amount
                    updated = True
    if updated:
        save_funds(config)
        print("[INFO] Synced shares/amounts to funds.json")

    print(f"[INFO] Dashboard saved to: {HTML_PATH}")
    print(f"[INFO] Total value: ¥{format_money(portfolio['total_value'])}")
    print(f"[INFO] Daily P&L: {'+' if portfolio['total_pnl'] >= 0 else ''}{format_money(portfolio['total_pnl'])}")
    print(f"[INFO] Hold return: {format_pct(portfolio['total_hold_return_pct'])}")
    if cache_used:
        print("[WARN] Some funds used cached data; dashboard shows stale-data notice.")


if __name__ == "__main__":
    main()
