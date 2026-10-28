"""Result grids show amounts with thousands separators, but not years, dates, periods or codes."""
import unittest

import pandas as pd

from agent.ui.results_page import number_columns


class NumberColumns(unittest.TestCase):
    def test_amounts_get_separators_and_labels_do_not(self):
        df = pd.DataFrame({"product_id": ["A"], "fiscal_year": [2025], "as_of_date": ["2026-03-26"],
                           "usage_amt": [154038.0], "limit_amt": [180000], "utilisation_pct": [85.58],
                           "partition_key": ["FY2025"], "row_id": [7], "breach": [False]})
        cols = number_columns(df)
        self.assertEqual(sorted(cols), ["limit_amt", "usage_amt", "utilisation_pct"])
        self.assertEqual(cols["usage_amt"]["type_config"]["format"], "localized")


if __name__ == "__main__":
    unittest.main()
