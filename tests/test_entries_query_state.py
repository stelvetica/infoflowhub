import unittest

from web.services.views import build_entries_state_params


class EntriesQueryStateTest(unittest.TestCase):
    def test_entries_unread_only_defaults_to_enabled_when_missing(self):
        params = build_entries_state_params({})
        self.assertEqual(params["entries_unread_only"], "1")

    def test_entries_unread_only_preserves_explicit_blank_for_show_all(self):
        params = build_entries_state_params({"entries_unread_only": ""})
        self.assertEqual(params["entries_unread_only"], "")

    def test_entries_unread_only_preserves_explicit_enabled_value(self):
        params = build_entries_state_params({"entries_unread_only": "1"})
        self.assertEqual(params["entries_unread_only"], "1")


if __name__ == "__main__":
    unittest.main()
