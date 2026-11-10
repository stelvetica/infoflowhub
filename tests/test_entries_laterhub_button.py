import unittest
from pathlib import Path

from fastapi.templating import Jinja2Templates


BASE_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


def _row(**overrides):
    row = {
        "display_time": "2026/11/10 08:00",
        "source_name": "alphapai",
        "title": "全球蓝宝书",
        "link": "/markdown/alphapai/a.md",
        "has_link": True,
        "default_read": False,
        "read_key": "k1",
    }
    row.update(overrides)
    return row


def _render(rows):
    entries = {
        "rows": rows,
        "sort": "sort_time",
        "dir": "desc",
        "q": "",
        "next_cursor": "",
        "filtered_total": len(rows),
        "total": len(rows),
        "unread_only": True,
        "read_keys_text": "",
    }
    return templates.get_template("partials/entries_table.html").render(entries=entries, params={})


class EntriesLaterhubButtonTest(unittest.TestCase):
    def test_each_linked_row_gets_add_button_with_its_url(self):
        html = _render([_row(), _row(title="外链条目", link="https://example.com/a")])
        self.assertEqual(html.count("data-laterhub-add"), 2)
        self.assertIn('data-add-url="/markdown/alphapai/a.md"', html)
        self.assertIn('data-add-url="https://example.com/a"', html)
        self.assertIn("<th class=\"entries-add-th\">稍后读</th>", html)

    def test_row_without_link_has_no_button(self):
        html = _render([_row(link="", has_link=False)])
        self.assertNotIn("data-laterhub-add", html)

    def test_empty_state_spans_all_four_columns(self):
        self.assertIn('colspan="4"', _render([]))


if __name__ == "__main__":
    unittest.main()
