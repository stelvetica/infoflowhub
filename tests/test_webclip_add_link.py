import sqlite3
import tempfile
import unittest
from pathlib import Path

import web.routes.main as routes_main
from web.routes.main import add_laterhub_link


BASE_DIR = Path(__file__).resolve().parents[1]


class WebclipAddLinkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "laterhub.sqlite3"
        self.addCleanup(setattr, routes_main, "DB_PATH", routes_main.DB_PATH)
        routes_main.DB_PATH = db_path
        self.db_path = db_path

    def _fetch_row(self, link_id: int):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT url, title, source, tags, tag_status FROM links WHERE id = ?",
                (link_id,),
            ).fetchone()
        finally:
            conn.close()

    def test_clipped_link_lands_as_untagged_manual_verify(self):
        first = add_laterhub_link("https://example.com/a/1", "示例文章")
        self.assertFalse(first["duplicated"])

        again = add_laterhub_link("https://example.com/a/1", "示例文章")
        self.assertTrue(again["duplicated"])
        self.assertEqual(again["id"], first["id"])

        # SSOT 契约：落库即未标签条目（tags 空 + tag_status pending），打标全权归每日自动流程
        self.assertEqual(
            self._fetch_row(first["id"]),
            ("https://example.com/a/1", "示例文章", "manual_verify", None, "pending"),
        )

    def test_title_falls_back_to_url(self):
        result = add_laterhub_link("https://example.com/no-title", "")
        self.assertEqual(self._fetch_row(result["id"])[1], "https://example.com/no-title")

    def test_rejects_non_http_or_bad_url(self):
        for bad in ("ftp://example.com/x", "javascript:alert(1)", "", "https://", "x" * 2049):
            with self.assertRaises(ValueError):
                add_laterhub_link(bad, "标题")


if __name__ == "__main__":
    unittest.main()
