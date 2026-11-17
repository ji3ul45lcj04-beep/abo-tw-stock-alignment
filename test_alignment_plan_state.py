import datetime as dt
import json
import os
import tempfile
import time
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import tw_stock_alignment_scanner as scanner
from tw_stock_alignment_scanner import Bar
import alignment_plan_state as p

class Plans(unittest.TestCase):
    def setUp(self):
        self.store = {'version': 1, 'plans': {}}
        self.row = dict(alignment='BULL ALIGN', suggested_buy=100, warning_line=100,
                        stop_loss=90, tp1=110, tp2=120, tp3=130)
        self.bars = [self.bar(1, 80, 140)]
        self.run_plan()

    def bar(self, day, low, high):
        return Bar(dt.date(2026, 9, day), low, high, low, high, 100)

    def run_plan(self):
        return p.update(self.store, 'X', self.row, self.bars, dt.date(2026, 9, 1))

    def test_creation_not_hindsight(self):
        self.assertEqual(self.run_plan()['tp_status'], 'WAIT BUY')

    def test_entry_tp_and_persistence(self):
        self.bars += [self.bar(2, 99, 105), self.bar(3, 105, 112)]
        self.row['suggested_buy'] = 108
        result = self.run_plan()
        self.assertEqual(result['suggested_buy'], 100)
        self.assertEqual(result['tp_status'], 'TP1 GET')
        self.bars.append(self.bar(4, 103, 106))
        self.assertEqual(self.run_plan()['tp_status'], 'TP1 GET')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'
            p.save(path, self.store)
            self.assertEqual(p.load(path), self.store)

    def test_below_entry_is_not_touch(self):
        self.bars.append(self.bar(2, 95, 99))
        self.assertEqual(self.run_plan()['tp_status'], 'WAIT BUY')

    def test_ambiguous_entry(self):
        self.bars.append(self.bar(2, 99, 115))
        self.assertEqual(self.run_plan()['tp_status'], 'ORDER UNKNOWN')

    def test_stop_remains_stopped(self):
        self.bars += [self.bar(2, 99, 105), self.bar(3, 89, 105), self.bar(4, 105, 140)]
        self.assertEqual(self.run_plan()['tp_status'], 'STOP HIT / WAIT RESET')
        self.assertEqual(self.run_plan()['tp_status'], 'STOP HIT / WAIT RESET')

    def test_cancel_does_not_invent_entry(self):
        self.row['alignment'] = 'MIXED'
        self.bars.append(self.bar(2, 105, 109))
        self.assertEqual(self.run_plan()['tp_status'], 'CANCELLED / ALIGN LOST')
        self.bars.append(self.bar(3, 99, 105))
        self.assertEqual(self.run_plan()['tp_status'], 'CANCELLED / ALIGN LOST')
        self.assertIsNone(self.store['plans']['X']['entry_date'])

    def test_rearm_archives_old_plan(self):
        self.row['alignment'] = 'MIXED'
        self.bars.append(self.bar(2, 105, 109))
        self.run_plan()
        self.row['alignment'] = 'BULL ALIGN'
        self.bars.append(self.bar(3, 99, 105))
        self.assertEqual(self.run_plan()['tp_status'], 'WAIT BUY')
        self.assertEqual(len(self.store['archive']['X']), 1)

    def test_migrated_entry_continues_from_baseline(self):
        plan = self.store['plans']['X']
        plan.update(created_date='2026-09-01', baseline_status='ENTRY TOUCHED',
                    baseline_entry_date='2026-09-01', baseline_highest_tp=0)
        self.bars = [self.bar(1, 99, 105), self.bar(2, 105, 112)]
        self.assertEqual(self.run_plan()['tp_status'], 'TP1 GET')

    def test_entry_gap_only_after_entry(self):
        row = dict(current_price=35.60, suggested_buy=32.83, tp_status='ENTRY TOUCHED')
        self.assertEqual(scanner.entry_gap_value(row), 2.77)
        row['tp_status'] = 'WAIT BUY'
        self.assertEqual(scanner.entry_gap_value(row), '')

    def test_pending_plan_refreshes_only_after_old_order_is_checked(self):
        self.bars.append(self.bar(2, 101, 105))
        self.row.update(suggested_buy=102, warning_line=102, stop_loss=92,
                        tp1=112, tp2=122, tp3=132, tp_status='WAIT BUY')
        result = self.run_plan()
        self.assertEqual(result['suggested_buy'], 102)
        self.assertEqual(result['tp_status'], 'WAIT BUY')
        self.assertEqual(self.store['plans']['X']['created_date'], '2026-09-02')

    def test_risk_wide_plan_is_not_persisted(self):
        self.store = {'version': 1, 'plans': {}}
        self.row['tp_status'] = 'RISK TOO WIDE / SKIP'
        result = p.update(self.store, 'X', self.row, self.bars, dt.date(2026, 9, 1))
        self.assertEqual(result['tp_status'], 'RISK TOO WIDE / SKIP')
        self.assertNotIn('X', self.store['plans'])

    def test_tick_flooring_at_market_boundaries(self):
        cases = {
            9.999: 9.99,
            49.99: 49.95,
            99.99: 99.9,
            499.99: 499.5,
            999.99: 999.0,
            1003.0: 1000.0,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(scanner.floor_stock_tick(raw), expected)

    def test_entry_plan_has_legal_ticks_and_minimum_risk(self):
        bars = [Bar(dt.date(2026, 1, 1) + dt.timedelta(days=i), 20 + i * .05,
                    20.15 + i * .05, 19.95 + i * .05, 20.1 + i * .05, 1000)
                for i in range(70)]
        result = scanner.entry_plan(
            bars, 'BULL ALIGN', 2, 10, 2, 2, 0, 0, None, 0.2, 0.5,
            1, 2, 3, 30, 1, 0.8, 0.5, 3, 15)
        entry, _, stop, tp1, tp2, tp3, status = result
        self.assertEqual(status, 'WAIT BUY')
        self.assertGreaterEqual((entry - stop) / entry * 100, 3)
        for value in (entry, stop, tp1, tp2, tp3):
            tick = scanner.stock_tick_size(value)
            self.assertEqual(Decimal(str(value)) % tick, 0)

    def test_far_pending_order_is_no_chase(self):
        row = dict(tp_status='WAIT BUY', current_price=116, suggested_buy=100, stop_loss=95)
        self.assertEqual(scanner.presentation_status(row, 15, 15), 'TOO FAR / NO CHASE')

    def test_existing_active_plan_prices_are_legalized_without_repricing(self):
        plan = self.store['plans']['X']
        plan['levels'].update(suggested_buy=100.37, warning_line=100.37,
                              stop_loss=90.27, tp1=110.26, tp2=120.26, tp3=130.26)
        plan.update(baseline_status='ENTRY TOUCHED', baseline_entry_date='2026-09-01')
        result = self.run_plan()
        self.assertEqual(result['suggested_buy'], 100.0)
        self.assertEqual(result['stop_loss'], 90.2)
        self.assertEqual(result['tp_status'], 'ENTRY TOUCHED')

    def test_tight_active_stop_is_widened_and_rr_is_preserved(self):
        plan = self.store['plans']['X']
        plan['levels'].update(suggested_buy=100, warning_line=100,
                              stop_loss=99, tp1=101, tp2=102, tp3=103)
        plan.update(status='ENTRY TOUCHED', baseline_status='ENTRY TOUCHED',
                    baseline_entry_date='2026-09-01')
        result = self.run_plan()
        self.assertEqual(result['stop_loss'], 97)
        self.assertEqual((result['tp1'], result['tp2'], result['tp3']), (103, 106, 109))

    def test_active_wide_risk_is_visible_but_not_changed_to_skip(self):
        row = dict(tp_status='ENTRY TOUCHED', current_price=90, suggested_buy=100, stop_loss=80)
        self.assertEqual(scanner.presentation_status(row, 15, 15), 'ENTRY TOUCHED / RISK WIDE')
        row['tp_status'] = 'ENTRY TOUCHED / RISK WIDE'
        self.assertEqual(scanner.entry_gap_value(row), -10)

    def test_list_state_separates_qualified_tracking_and_watch(self):
        qualified = dict(alignment='BULL ALIGN', day_state='BULL', week_state='BULL', tp_status='WAIT BUY')
        tracking = dict(alignment='MIXED', day_state='WAIT', week_state='BULL', tp_status='ENTRY TOUCHED')
        watch_day = dict(alignment='MIXED', day_state='WAIT', week_state='BULL', tp_status='CANCELLED / ALIGN LOST')
        watch_both = dict(alignment='BEAR ALIGN', day_state='BEAR', week_state='BEAR', tp_status='CANCELLED / ALIGN LOST')
        completed = dict(alignment='BULL ALIGN', day_state='BULL', week_state='BULL', tp_status='TP3 GET')
        stopped = dict(alignment='MIXED', day_state='WAIT', week_state='BULL', tp_status='STOP HIT / WAIT RESET')
        too_far = dict(alignment='BULL ALIGN', day_state='BULL', week_state='BULL', tp_status='TOO FAR / NO CHASE')
        risk_wide = dict(alignment='BULL ALIGN', day_state='BULL', week_state='BULL', tp_status='RISK TOO WIDE / SKIP')

        self.assertEqual(scanner.list_state_value(qualified), 'QUALIFIED')
        self.assertEqual(scanner.list_state_value(tracking), 'TRACKING')
        self.assertEqual(scanner.list_state_value(watch_day), 'WATCH - DAY LOST')
        self.assertEqual(scanner.list_state_value(watch_both), 'ARCHIVED')
        self.assertEqual(scanner.list_state_value(completed), 'TRACKING')
        self.assertEqual(scanner.list_state_value(stopped), 'ARCHIVED')
        self.assertEqual(scanner.list_state_value(too_far), 'TRACKING')
        self.assertEqual(scanner.list_state_value(risk_wide), 'TRACKING')
        self.assertEqual(scanner.entry_eligibility_value(too_far), 'NO CHASE')
        self.assertEqual(scanner.entry_eligibility_value(risk_wide), 'SKIP')

    def test_qualified_audit_rejects_non_actionable_rows(self):
        good = dict(code='A', name='Good', data_date='2026-11-13', alignment='BULL ALIGN',
                    list_state='QUALIFIED', day_state='BULL', week_state='BULL', tp_status='WAIT BUY')
        bad = dict(good, code='B', name='Wide', tp_status='RISK TOO WIDE / SKIP')

        self.assertEqual(scanner.qualified_row_issues([good]), [])
        self.assertEqual(scanner.qualified_row_issues([good, bad]), ['B:Wide'])

    def test_entry_eligibility_separates_new_retest_and_management(self):
        new_entry = dict(code='A', list_state='QUALIFIED', alignment='BULL ALIGN', day_state='BULL', week_state='BULL',
                         tp_status='WAIT BUY', current_price=100, stop_loss=90, tp1=110)
        retest = dict(new_entry, list_state='TRACKING', tp_status='ENTRY TOUCHED', current_price=102)
        trend_lost = dict(retest, alignment='MIXED', day_state='WAIT')
        tp1_get = dict(retest, tp_status='TP1 GET')
        unknown = dict(retest, tp_status='ORDER UNKNOWN')

        self.assertEqual(scanner.entry_eligibility_value(new_entry), 'NEW ENTRY')
        self.assertEqual(scanner.entry_eligibility_value(retest), 'RETEST ENTRY')
        self.assertEqual(scanner.entry_eligibility_value(trend_lost), 'MANAGE ONLY')
        self.assertEqual(scanner.entry_eligibility_value(tp1_get), 'MANAGE ONLY')
        self.assertEqual(scanner.entry_eligibility_value(unknown), 'VERIFY FIRST')

        retest['entry_eligibility'] = 'RETEST ENTRY'
        self.assertEqual(scanner.entry_eligibility_issues([retest]), [])
        trend_lost['entry_eligibility'] = 'RETEST ENTRY'
        self.assertEqual(scanner.entry_eligibility_issues([trend_lost]), ['A:invalid-retest-entry'])

    def test_main_list_keeps_only_current_bull_alignment(self):
        bull = dict(alignment='BULL ALIGN', tp_status='WAIT BUY')
        prior_entry_lost = dict(alignment='MIXED', tp_status='ENTRY TOUCHED')
        prior_watch = dict(alignment='MIXED', tp_status='CANCELLED / ALIGN LOST')

        self.assertTrue(scanner.include_in_main_list(bull, only_align=True, only_bull=False))
        self.assertFalse(scanner.include_in_main_list(prior_entry_lost, only_align=True, only_bull=False))
        self.assertFalse(scanner.include_in_main_list(prior_watch, only_align=False, only_bull=True))
        self.assertTrue(scanner.include_in_main_list(prior_entry_lost, only_align=False, only_bull=False))

    def test_yahoo_failure_uses_stale_history_cache(self):
        payload = {
            'chart': {'result': [{
                'timestamp': [1798761600],
                'indicators': {'quote': [{
                    'open': [100], 'high': [102], 'low': [99],
                    'close': [101], 'volume': [1000]
                }]}
            }]}
        }
        stock = scanner.Stock('2330', 'TSMC', 'TWSE', '2330.TW')
        with tempfile.TemporaryDirectory() as d:
            cache_dir = Path(d)
            cache_file = cache_dir / '2330.TW.json'
            cache_file.write_text(json.dumps(payload), encoding='utf-8')
            stale_time = time.time() - 72 * 3600
            os.utime(cache_file, (stale_time, stale_time))
            scanner.YAHOO_REFRESH_AVAILABLE = True
            with patch.object(scanner, 'fetch_json', side_effect=urllib.error.URLError('rate limited')) as fetch:
                bars = scanner.fetch_history(stock, 6, cache_dir, 18, 0)
                second = scanner.fetch_history(stock, 6, cache_dir, 18, 0)
            scanner.YAHOO_REFRESH_AVAILABLE = True
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 101)
        self.assertEqual(second[0].close, 101)
        self.assertEqual(fetch.call_count, 1)

if __name__ == '__main__':
    unittest.main()
