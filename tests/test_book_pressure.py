"""Checks for look-ahead, invalid book states and regression aggregation."""
import unittest

import numpy as np
import pandas as pd

from analyze_book_pressure import asof_quotes, linear_fit, markout_bps, sufficient, targets_for_day, week_interval


class PressureTests(unittest.TestCase):
    def test_spread_hurdle_equals_buy_ask_sell_bid_and_reverse(self):
        entry = np.array([[99., 101.], [99., 101.]])
        future = np.array([[102., 106.], [94., 98.]])
        y, cost, signed, net = markout_bps(entry, future, np.array([.7, -.7]), fee_bps=2)
        np.testing.assert_allclose(y, [400, -400])
        np.testing.assert_allclose(cost, [300, 300])
        np.testing.assert_allclose(signed, [400, 400])
        expected = np.array([102.-101., 99.-98.]) / 100 * 1e4 - 2
        np.testing.assert_allclose(net, expected)

    def test_asof_excludes_equal_timestamp_and_retains_book_clear(self):
        quotes = pd.DataFrame({
            "ts_event": [10, 20, 20, 30, 40],
            "bid": [99., 100., 101., np.nan, 103.],
            "ask": [101., 102., 103., np.nan, 105.],
            "bid_sz": [100, 100, 300, 0, 100],
            "ask_sz": [100, 100, 100, 0, 100]})
        state, valid, _ = asof_quotes(quotes, np.array([10, 20, 21, 30, 31, 40, 41]))
        np.testing.assert_array_equal(valid, [False, True, True, True, False, False, True])
        self.assertEqual(state[1, 0], 99.)  # no observation at timestamp 20 yet
        self.assertEqual(state[2, 0], 101.)  # final state wins at duplicate timestamp
        self.assertTrue(np.isnan(state[4, 0]))  # no stale valid quote after clear

    def test_sufficient_statistics_preserve_pooled_regression_and_test_error(self):
        x = np.array([-.8, -.4, 0, .4, .8])
        y = .3 + 2.5*x
        a = sufficient(x[:2], y[:2])
        b = sufficient(x[2:], y[2:])
        stats = {k: a[k] + b[k] for k in a}
        intercept, beta = linear_fit(stats)
        self.assertAlmostEqual(intercept, .3)
        self.assertAlmostEqual(beta, 2.5)
        tx, ty = np.array([-.6, .2, .9]), np.array([-1., .7, 3.])
        s = sufficient(tx, ty)
        aggregated = (s['syy'] - 2*intercept*s['sy'] - 2*beta*s['sxy'] + intercept**2*s['n']
                      + 2*intercept*beta*s['sx'] + beta**2*s['sxx'])
        self.assertAlmostEqual(aggregated, np.sum((ty - intercept - beta*tx)**2))

    def test_grid_never_crosses_session_close(self):
        grid = targets_for_day('20260901', 100, [1, 1000, 10000])
        close = pd.Timestamp('2026-09-01 16:00', tz='America/New_York').tz_convert('UTC').value
        self.assertLess(grid[-1] + 10_000_000_000, close)
        self.assertTrue(np.all(np.diff(grid) == 100_000_000))

    def test_week_estimator_does_not_treat_days_as_independent_ticks(self):
        data = pd.DataFrame({'week': ['a', 'a', 'b'], 'value': [0., 2., 5.]})
        result = week_interval(data, 'value', np.random.default_rng(1), repetitions=100)
        self.assertEqual(result['weeks'], 2)
        self.assertAlmostEqual(result['mean'], 3.)  # (week-a mean 1 + week-b mean 5) / 2


if __name__ == '__main__':
    unittest.main()
