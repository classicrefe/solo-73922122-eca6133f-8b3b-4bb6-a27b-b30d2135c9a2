"""CSV 解析边界测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.csv_loader import CsvError, parse_events_csv  # noqa: E402
from app.reconcile import MOD  # noqa: E402


class TestCsvLoader(unittest.TestCase):
    def test_header_and_bom(self):
        text = "﻿事件号,计数\n1,10\n2,20\n3,30\n"
        events = parse_events_csv(text)
        self.assertEqual([e["event_no"] for e in events], [1, 2, 3])
        self.assertEqual([e["count"] for e in events], [10, 20, 30])

    def test_headerless_two_columns(self):
        events = parse_events_csv("1,0\n2,65535\n")
        self.assertEqual(len(events), 2)

    def test_english_header(self):
        events = parse_events_csv("event_no,count\n1,5\n2,8\n")
        self.assertEqual(events[1]["count"], 8)

    def test_non_integer_rejected(self):
        with self.assertRaises(CsvError):
            parse_events_csv("事件号,计数\n1,x\n2,3\n")

    def test_count_out_of_range(self):
        with self.assertRaises(CsvError):
            parse_events_csv("1,65536\n2,1\n")

    def test_non_sequential_event_no(self):
        with self.assertRaises(CsvError):
            parse_events_csv("1,10\n3,20\n")

    def test_max_residue_gap_accepted(self):
        # r 恒 <= 65535 < 90000，因此任何相邻计数对都至少有一个合法间隔；
        # 最大余数 65535 应被接受（固定间隔 65535ms）
        events = parse_events_csv(f"1,0\n2,{MOD - 1}\n")
        self.assertEqual(events[1]["count"], MOD - 1)

    def test_empty_file(self):
        with self.assertRaises(CsvError):
            parse_events_csv("")

    def test_single_row_rejected(self):
        with self.assertRaises(CsvError):
            parse_events_csv("事件号,计数\n1,10\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
