from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from web.services.utils import format_date, format_datetime, parse_datetime, to_sortable_time

# laterhub 的 created_at 存 UTC（"2026-11-09T23:15:26+00:00" = 本地 2026-11-10 07:15）。
UTC_ISO = "2026-11-09T23:15:26+00:00"
EXPECTED_LOCAL = datetime.fromisoformat(UTC_ISO).astimezone().replace(tzinfo=None)
EXPECTED_LOCAL_TEXT = EXPECTED_LOCAL.strftime("%Y/%m/%d %H:%M")
EXPECTED_LOCAL_SECONDS_TEXT = EXPECTED_LOCAL.strftime("%Y/%m/%d %H:%M:%S")


class TimeLocalizationTest(unittest.TestCase):
    def test_utc_iso_is_converted_to_local(self):
        self.assertEqual(parse_datetime(UTC_ISO), EXPECTED_LOCAL)

    def test_offset_iso_matches_same_instant(self):
        # 同一时刻的带偏移写法（如 +08:00）解析结果必须与原 UTC 串一致
        self.assertEqual(parse_datetime(datetime.fromisoformat(UTC_ISO).astimezone().isoformat()), EXPECTED_LOCAL)

    def test_z_suffix_is_treated_as_utc(self):
        self.assertEqual(parse_datetime("2026-11-09T23:15:26Z"), EXPECTED_LOCAL)

    def test_naive_local_text_is_unchanged(self):
        self.assertEqual(parse_datetime("2026/11/10 07:15"), datetime(2026, 11, 10, 7, 15))

    def test_format_and_sort_agree_with_local_naive(self):
        self.assertEqual(format_date(UTC_ISO), EXPECTED_LOCAL.strftime("%Y/%m/%d"))
        self.assertEqual(format_datetime(UTC_ISO), EXPECTED_LOCAL_TEXT)
        self.assertEqual(to_sortable_time(UTC_ISO), to_sortable_time(EXPECTED_LOCAL_SECONDS_TEXT))

    def test_sort_order_matches_real_chronology(self):
        later = (EXPECTED_LOCAL + timedelta(minutes=5)).strftime("%Y/%m/%d %H:%M")
        self.assertGreater(to_sortable_time(later), to_sortable_time(UTC_ISO))


if __name__ == "__main__":
    unittest.main()
