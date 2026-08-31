#!/usr/bin/env python3
"""Lightweight, deterministic portfolio diagnostics for personal use."""

from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, Iterable, List, Optional

from fund_data import load_funds

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "portfolio_settings.json")
TRANSACTIONS_PATH = os.path.join(BASE_DIR, "transactions.json")


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _money(value: float) -> float:
    return round(float(value), 2)


def _pct(value: float) -> float:
    return round(float(value), 2)


def load_settings() -> Dict[str, Any]:
    return _load_json(SETTINGS_PATH, {"theme_targets": {}, "alerts": {}})


def load_transactions() -> List[Dict[str, Any]]:
    data = _load_json(TRANSACTIONS_PATH, {"transactions": []})
    rows = data.get("transactions", []) if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else []


def transaction_summary(transactions: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
    rows = list(load_transactions() if transactions is None else transactions)
    totals = {"buy": 0.0, "sell": 0.0, "dividend": 0.0, "fee": 0.0}
    valid, invalid = 0, 0
    by_fund: Dict[str, Dict[str, float]] = {}
    for row in rows:
        try:
            tx_type = str(row.get("type", "")).lower()
            code = str(row.get("fund_code", ""))
            amount = float(row.get("amount", 0) or 0)
            fee = float(row.get("fee", 0) or 0)
            if tx_type not in ("buy", "sell", "dividend") or len(code) != 6:
                raise ValueError
            if not all(math.isfinite(v) and v >= 0 for v in (amount, fee)):
                raise ValueError
        except (TypeError, ValueError):
            invalid += 1
            continue
        valid += 1
        totals[tx_type] += amount
        totals["fee"] += fee
        fund = by_fund.setdefault(code, {"buy": 0.0, "sell": 0.0, "dividend": 0.0, "fee": 0.0})
        fund[tx_type] += amount
        fund["fee"] += fee
    return {
        "count": valid,
        "invalid_count": invalid,
        "totals": {k: _money(v) for k, v in totals.items()},
        "net_cash_in": _money(totals["buy"] + totals["fee"] - totals["sell"] - totals["dividend"]),
        "by_fund": {code: {k: _money(v) for k, v in values.items()} for code, values in by_fund.items()},
    }


def analyze_portfolio(funds: Optional[List[Dict[str, Any]]] = None,
                      settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    funds = list((load_funds() or {}).get("funds", []) if funds is None else funds)
    settings = load_settings() if settings is None else settings
    alerts_cfg = settings.get("alerts", {})
    total = sum(float(f.get("amount", 0) or 0) for f in funds)
    if total <= 0:
        return {"total_value": 0, "fund_count": len(funds), "alerts": ["组合当前市值为 0，无法分析仓位。"]}

    theme_values: Dict[str, float] = {}
    high_risk_value = 0.0
    positions = []
    for fund in funds:
        value = float(fund.get("amount", 0) or 0)
        theme = str(fund.get("theme") or "未分类")
        theme_values[theme] = theme_values.get(theme, 0.0) + value
        risk = str(fund.get("risk") or "")
        if "高风险" in risk or "中高风险" in risk or "R4" in risk or "R5" in risk:
            high_risk_value += value
        positions.append({
            "code": fund.get("code"),
            "name": fund.get("name"),
            "theme": theme,
            "value": _money(value),
            "weight_pct": _pct(value / total * 100),
        })
    positions.sort(key=lambda x: x["weight_pct"], reverse=True)
    themes = [
        {"theme": theme, "value": _money(value), "weight_pct": _pct(value / total * 100)}
        for theme, value in sorted(theme_values.items(), key=lambda item: item[1], reverse=True)
    ]

    single_limit = float(alerts_cfg.get("single_fund_max_pct", 20))
    theme_limit = float(alerts_cfg.get("single_theme_max_pct", 55))
    high_limit = float(alerts_cfg.get("high_risk_max_pct", 50))
    alerts = []
    for pos in positions:
        if pos["weight_pct"] > single_limit:
            alerts.append(f"单只基金 {pos['name']} 占比 {pos['weight_pct']}%，超过 {single_limit:g}% 阈值。")
    for theme in themes:
        if theme["weight_pct"] > theme_limit:
            alerts.append(f"{theme['theme']} 方向占比 {theme['weight_pct']}%，超过 {theme_limit:g}% 阈值。")
    high_risk_pct = _pct(high_risk_value / total * 100)
    if high_risk_pct > high_limit:
        alerts.append(f"中高及高风险基金合计 {high_risk_pct}%，超过 {high_limit:g}% 阈值。")
    if not alerts:
        alerts.append("未触发当前配置中的集中度和风险阈值。")

    return {
        "total_value": _money(total),
        "fund_count": len(funds),
        "high_risk_weight_pct": high_risk_pct,
        "top_positions": positions[:5],
        "themes": themes,
        "alerts": alerts,
        "thresholds": {
            "single_fund_max_pct": single_limit,
            "single_theme_max_pct": theme_limit,
            "high_risk_max_pct": high_limit,
        },
        "note": "风险等级来自 funds.json 标签；结果用于仓位检查，不预测未来涨跌。",
    }


def simulate_rebalance(funds: Optional[List[Dict[str, Any]]] = None,
                       targets: Optional[Dict[str, float]] = None,
                       settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    funds = list((load_funds() or {}).get("funds", []) if funds is None else funds)
    settings = load_settings() if settings is None else settings
    targets = settings.get("theme_targets", {}) if targets is None else targets
    if not targets:
        return {"error": "请先在 portfolio_settings.json 中配置 theme_targets。"}
    try:
        clean_targets = {str(k): float(v) for k, v in targets.items()}
    except (TypeError, ValueError):
        return {"error": "目标比例必须是数字。"}
    if any(not math.isfinite(v) or v < 0 for v in clean_targets.values()):
        return {"error": "目标比例必须是非负有限数。"}
    target_sum = sum(clean_targets.values())
    if abs(target_sum - 100.0) > 0.01:
        return {"error": f"目标比例合计必须为 100%，当前为 {_pct(target_sum)}%。"}

    total = sum(float(f.get("amount", 0) or 0) for f in funds)
    current: Dict[str, float] = {}
    for fund in funds:
        theme = str(fund.get("theme") or "未分类")
        current[theme] = current.get(theme, 0.0) + float(fund.get("amount", 0) or 0)
    all_themes = sorted(set(current) | set(clean_targets))
    minimum = float(settings.get("alerts", {}).get("rebalance_min_amount", 100))
    actions = []
    for theme in all_themes:
        value = current.get(theme, 0.0)
        target_pct = clean_targets.get(theme, 0.0)
        target_value = total * target_pct / 100
        diff = target_value - value
        action = "hold" if abs(diff) < minimum else ("buy" if diff > 0 else "sell")
        actions.append({
            "theme": theme,
            "current_pct": _pct(value / total * 100) if total else 0.0,
            "target_pct": _pct(target_pct),
            "difference_amount": _money(diff),
            "action": action,
        })
    return {
        "total_value": _money(total),
        "minimum_action_amount": _money(minimum),
        "actions": actions,
        "note": "这是按主题等额调到目标比例的数学模拟，未考虑申赎费、锁定期和税费；不自动执行。",
    }
