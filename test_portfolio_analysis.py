#!/usr/bin/env python3

import unittest

from portfolio_analysis import analyze_portfolio, simulate_rebalance, transaction_summary


FUNDS = [
    {"code": "000001", "name": "稳健基金", "amount": 600, "theme": "稳健固收", "risk": "中低风险"},
    {"code": "000002", "name": "科技基金", "amount": 400, "theme": "A股科技", "risk": "R5 高风险"},
]
SETTINGS = {
    "theme_targets": {"稳健固收": 50, "A股科技": 50},
    "alerts": {"single_fund_max_pct": 55, "single_theme_max_pct": 55, "high_risk_max_pct": 50, "rebalance_min_amount": 10},
}


class PortfolioAnalysisTests(unittest.TestCase):
    def test_analysis_detects_concentration(self):
        result = analyze_portfolio(FUNDS, SETTINGS)
        self.assertEqual(result["total_value"], 1000)
        self.assertEqual(result["high_risk_weight_pct"], 40)
        self.assertTrue(any("稳健基金" in message for message in result["alerts"]))

    def test_rebalance_is_cash_neutral(self):
        result = simulate_rebalance(FUNDS, settings=SETTINGS)
        differences = {row["theme"]: row["difference_amount"] for row in result["actions"]}
        self.assertEqual(differences, {"A股科技": 100, "稳健固收": -100})
        self.assertAlmostEqual(sum(differences.values()), 0)

    def test_rebalance_rejects_invalid_targets(self):
        result = simulate_rebalance(FUNDS, {"稳健固收": 80}, SETTINGS)
        self.assertIn("error", result)

    def test_transaction_summary(self):
        rows = [
            {"fund_code": "000001", "type": "buy", "amount": 100, "fee": 1},
            {"fund_code": "000001", "type": "dividend", "amount": 5, "fee": 0},
            {"fund_code": "bad", "type": "buy", "amount": 10},
        ]
        result = transaction_summary(rows)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["invalid_count"], 1)
        self.assertEqual(result["net_cash_in"], 96)


if __name__ == "__main__":
    unittest.main()
