"""Current deterministic risk-statistics regression, without DB/model dependencies.

The tested function is extracted verbatim from backend/assistance/risk.py by AST.
No algorithm is copied here. This deliberately excludes imports and surrounding
database orchestration; it does not verify HTTP, semantic clues or payment flows.
Run from the repository root: python -B -m unittest discover -s tests -v
"""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median, quantiles
import unittest
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).resolve().parents[1] / 'backend/assistance/risk.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'statistics')
namespace = dict(timezone=timezone, timedelta=timedelta, median=median,
                 quantiles=quantiles, TZ=ZoneInfo('Asia/Shanghai'))
exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SOURCE), 'exec'), namespace)
evaluate = namespace['statistics']
NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def rows(amounts, recipient='known', at=None, time_source='transfer_receipt'):
    return [dict(amount=a, recipient=recipient, at=at or NOW-timedelta(days=2),
                 time_source=time_source) for a in amounts]


def codes(result):
    return {s['code'] for s in result['signals']}


class RiskStatisticsTests(unittest.TestCase):
    def test_empty_history_does_not_infer_amount_or_recipient(self):
        result = evaluate([], 999999, 'new', NOW)
        self.assertEqual(result['signals'], [])
        self.assertEqual(result['sample_size'], 0)
        self.assertTrue(result['limitations'])

    def test_seven_samples_do_not_trigger_amount_warning(self):
        result = evaluate(rows([10000]*7), 999999, 'known', NOW)
        self.assertFalse({'amount_deviation', 'amount_quantile'} & codes(result))

    def test_eight_samples_enable_amount_evaluation(self):
        result = evaluate(rows([10000,20000,30000,40000,50000,60000,70000,80000]), 165001, 'known', NOW)
        signal = next(s for s in result['signals'] if s['code'] == 'amount_deviation')
        self.assertEqual(signal['threshold_cents'], 165000)
        self.assertEqual(signal['sample_size'], 8)

    def test_equal_to_nonzero_mad_threshold_is_not_above(self):
        result = evaluate(rows([10000,20000,30000,40000,50000,60000,70000,80000]), 165000, 'known', NOW)
        self.assertNotIn('amount_deviation', codes(result))

    def test_zero_mad_uses_fixed_100_yuan_margin_when_larger(self):
        history = rows([100]*8)
        self.assertNotIn('amount_quantile', codes(evaluate(history, 10100, 'known', NOW)))
        result = evaluate(history, 10101, 'known', NOW)
        self.assertIn('amount_quantile', codes(result))
        self.assertTrue(any('MAD' in s for s in result['limitations']))

    def test_zero_mad_uses_triple_quantile_when_larger(self):
        history = rows([10000]*8)
        self.assertNotIn('amount_quantile', codes(evaluate(history, 30000, 'known', NOW)))
        self.assertIn('amount_quantile', codes(evaluate(history, 30001, 'known', NOW)))

    def test_new_recipient_is_identified_against_existing_history(self):
        self.assertIn('new_recipient', codes(evaluate(rows([100]), 100, 'new', NOW)))

    def test_known_recipient_has_no_first_recipient_warning(self):
        self.assertNotIn('new_recipient', codes(evaluate(rows([100]), 100, 'known', NOW)))

    def test_future_and_nonpositive_history_do_not_affect_baseline(self):
        history = rows([10000]*8) + rows([999999], at=NOW+timedelta(seconds=1)) + rows([0,-100])
        result = evaluate(history, 10000, 'known', NOW)
        self.assertEqual(result['sample_size'], 8)
        self.assertNotIn('amount_deviation', codes(result))

    def test_eleven_time_samples_do_not_infer_unusual_hour(self):
        result = evaluate(rows([10000]*11, at=NOW-timedelta(hours=30)), 10000, 'known', NOW)
        self.assertNotIn('unusual_time', codes(result))

    def test_twelve_time_samples_allow_unusual_hour_warning(self):
        result = evaluate(rows([10000]*12, at=NOW-timedelta(hours=30)), 10000, 'known', NOW)
        self.assertIn('unusual_time', codes(result))

    def test_ten_percent_time_share_is_not_below_threshold(self):
        history = rows([10000]*18, at=NOW-timedelta(hours=30)) + rows([10000]*2)
        self.assertNotIn('unusual_time', codes(evaluate(history, 10000, 'known', NOW)))

    def test_synthetic_legacy_time_sources_do_not_make_time_baseline(self):
        result = evaluate(rows([10000]*20, at=NOW-timedelta(hours=30), time_source='demo_rule_v2'), 10000, 'known', NOW)
        self.assertNotIn('unusual_time', codes(result))

    def test_two_recent_transfers_do_not_trigger_frequency_warning(self):
        result = evaluate(rows([100]*2, at=NOW-timedelta(minutes=1)), 100, 'known', NOW)
        self.assertNotIn('recent_frequency', codes(result))

    def test_three_recent_transfers_trigger_frequency_warning(self):
        result = evaluate(rows([100]*3, at=NOW-timedelta(minutes=1)), 100, 'known', NOW)
        self.assertIn('recent_frequency', codes(result))

    def test_ten_minute_window_includes_exact_boundary(self):
        result = evaluate(rows([100]*3, at=NOW-timedelta(minutes=10)), 100, 'known', NOW)
        self.assertIn('recent_frequency', codes(result))

    def test_older_than_window_is_excluded(self):
        result = evaluate(rows([100]*3, at=NOW-timedelta(minutes=10, microseconds=1)), 100, 'known', NOW)
        self.assertNotIn('recent_frequency', codes(result))


if __name__ == '__main__':
    unittest.main()
