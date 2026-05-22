import unittest
from pathlib import Path

from fastapi.templating import Jinja2Templates

from web.services.views import build_laterhub_query_string, build_laterhub_state_params


BASE_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


class LaterhubQueryStateTest(unittest.TestCase):
    def test_build_laterhub_state_params_normalizes_single_source(self):
        params = build_laterhub_state_params(
            {
                "laterhub_q": "AI",
                "laterhub_filter_finished": "opened",
                "laterhub_filter_tag": "效率",
                "laterhub_filter_scope": "today",
                "laterhub_page": "3",
            }
        )
        self.assertEqual(
            params,
            {
                "laterhub_q": "AI",
                "laterhub_sort": "sort_time",
                "laterhub_dir": "desc",
                "laterhub_filter_finished": "opened",
                "laterhub_filter_tag": "效率",
                "laterhub_filter_scope": "today",
                "laterhub_page": "3",
            },
        )

    def test_query_string_contains_each_laterhub_key_once(self):
        query = build_laterhub_query_string(
            {
                "laterhub_q": "AI",
                "laterhub_sort": "sort_time",
                "laterhub_dir": "desc",
                "laterhub_filter_finished": "opened",
                "laterhub_filter_tag": "效率",
                "laterhub_filter_scope": "today",
                "laterhub_page": "2",
            }
        )
        for key in (
            "laterhub_q",
            "laterhub_sort",
            "laterhub_dir",
            "laterhub_filter_finished",
            "laterhub_filter_tag",
            "laterhub_filter_scope",
            "laterhub_page",
        ):
            self.assertEqual(query.count(f"{key}="), 1, key)

    def test_template_uses_canonical_state_query_for_actions_and_pagination(self):
        context = {
            "request": object(),
            "params": {"entries_q": "", "entries_sort": "", "entries_dir": ""},
            "laterhub_params": {
                "laterhub_q": "AI",
                "laterhub_sort": "sort_time",
                "laterhub_dir": "desc",
                "laterhub_filter_finished": "opened",
                "laterhub_filter_tag": "效率",
                "laterhub_filter_scope": "today",
                "laterhub_page": "2",
            },
            "laterhub": {
                "state_query": "laterhub_q=AI&laterhub_sort=sort_time&laterhub_dir=desc&laterhub_filter_finished=opened&laterhub_filter_tag=%E6%95%88%E7%8E%87&laterhub_filter_scope=today&laterhub_page=2",
                "prev_query": "laterhub_q=AI&laterhub_sort=sort_time&laterhub_dir=desc&laterhub_filter_finished=opened&laterhub_filter_tag=%E6%95%88%E7%8E%87&laterhub_filter_scope=today&laterhub_page=1",
                "next_query": "laterhub_q=AI&laterhub_sort=sort_time&laterhub_dir=desc&laterhub_filter_finished=opened&laterhub_filter_tag=%E6%95%88%E7%8E%87&laterhub_filter_scope=today&laterhub_page=3",
                "sort": "sort_time",
                "dir": "desc",
                "filter_finished": "opened",
                "selected_tags_text": "效率",
                "selected_tags": ["效率"],
                "filter_scope": "today",
                "q": "AI",
                "rows": [
                    {
                        "id": 7,
                        "display_time": "2026/05/22",
                        "title": "条目",
                        "url": "https://example.com",
                        "is_opened": True,
                        "tags_text": "效率",
                        "is_finished": False,
                    }
                ],
                "all_tags": ["效率"],
                "filtered_total": 3,
                "page": 2,
                "total_pages": 4,
            },
        }
        html = templates.get_template("partials/laterhub_panel.html").render(context)
        self.assertIn('/actions/laterhub/7/toggle-finished?laterhub_q=AI&amp;laterhub_sort=sort_time&amp;laterhub_dir=desc&amp;laterhub_filter_finished=opened&amp;laterhub_filter_tag=%E6%95%88%E7%8E%87&amp;laterhub_filter_scope=today&amp;laterhub_page=2', html)
        self.assertIn('/fragments/laterhub?laterhub_q=AI&amp;laterhub_sort=sort_time&amp;laterhub_dir=desc&amp;laterhub_filter_finished=opened&amp;laterhub_filter_tag=%E6%95%88%E7%8E%87&amp;laterhub_filter_scope=today&amp;laterhub_page=1', html)
        self.assertIn('/fragments/laterhub?laterhub_q=AI&amp;laterhub_sort=sort_time&amp;laterhub_dir=desc&amp;laterhub_filter_finished=opened&amp;laterhub_filter_tag=%E6%95%88%E7%8E%87&amp;laterhub_filter_scope=today&amp;laterhub_page=3', html)
        self.assertNotIn("params|urlencode", html)
        self.assertNotIn("params%7Curlencode", html)


if __name__ == "__main__":
    unittest.main()
