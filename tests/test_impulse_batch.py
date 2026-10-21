import unittest

import numpy as np
import pandas as pd

from analyze_impulse_batch import crossing_times, session_window
from utils.leadlag import time_to_fraction


class ImpulseBatchTests(unittest.TestCase):
    def test_matches_original_log_interpolated_crossings(self):
        ts = np.array([-1000, 0, 10, 100, 1000, 10000, 1000000000])
        curves = np.array([[.01, 0, .02, .2, .4, .7, 1.],
                           [0, 0, .1, .3, .5, .75, .8]])
        for fraction in (.5, .9):
            actual = crossing_times(curves, ts, fraction)
            for i, values in enumerate(curves):
                expected, _ = time_to_fraction(pd.DataFrame({'tau_ns': ts, 'beta': values}), fraction)
                self.assertAlmostEqual(actual[i], expected, delta=.51)

    def test_nonmonotone_curve_uses_first_crossing(self):
        ts = np.array([0, 10, 100, 1000])
        # First reaches 90% at 10, subsequently falls, then recovers.
        self.assertAlmostEqual(crossing_times([0, .9, .3, 1], ts, .9)[0], 10)

    def test_nonpositive_endpoint_is_undefined_and_same_time_can_be_zero(self):
        ts = np.array([0, 10, 100])
        result = crossing_times([[0, 0, 0], [.5, .3, -.1], [.6, .8, 1]], ts, .5)
        self.assertTrue(np.isnan(result[:2]).all())
        self.assertEqual(result[2], 0)

    def test_regular_session_timezone_and_length(self):
        start, end = session_window('20260701')
        self.assertEqual(start, pd.Timestamp('2026-07-01 13:30', tz='UTC').value)
        self.assertEqual(end-start, int(6.5*3600*1e9))


if __name__ == '__main__':
    unittest.main()
