import unittest

from connectors._shared.bsk_browser import BskCommandError
from connectors.douyin.favorites import (
    DOUYIN_STABILIZE_MEDIA_JS,
    _install_stabilizer,
    _wheel_with_retry,
)


class FakePage:
    def __init__(self, failures: list[str]) -> None:
        self._failures = list(failures)
        self.calls = 0
        self.waits = 0

    def evaluate(self, js: str) -> None:
        assert js is DOUYIN_STABILIZE_MEDIA_JS
        self.calls += 1
        if self._failures:
            raise BskCommandError(self._failures.pop(0))

    def wait_for_timeout(self, ms: int) -> None:
        self.waits += 1


class InstallStabilizerTest(unittest.TestCase):
    def test_retries_permission_denied_until_installed(self):
        page = FakePage(["evaluate 失败 (exit=1, code=permission_denied)"] * 2)
        _install_stabilizer(page)
        self.assertEqual(page.calls, 3)
        self.assertEqual(page.waits, 2)

    def test_gives_up_after_attempts(self):
        page = FakePage(["evaluate 失败 (exit=1, code=permission_denied)"] * 5)
        with self.assertRaises(BskCommandError):
            _install_stabilizer(page)
        self.assertEqual(page.calls, 3)

    def test_non_permission_error_is_not_retried(self):
        page = FakePage(["evaluate 失败 (exit=1, code=timeout)"])
        with self.assertRaises(BskCommandError):
            _install_stabilizer(page)
        self.assertEqual(page.calls, 1)
        self.assertEqual(page.waits, 0)


if __name__ == "__main__":
    unittest.main()


class FakeMouse:
    def __init__(self, page: "FakeWheelPage") -> None:
        self._page = page

    def wheel(self, dx: int, dy: int) -> None:
        self._page.calls += 1
        if self._page.failures:
            raise BskCommandError(self._page.failures.pop(0))


class FakeWheelPage:
    def __init__(self, failures: list[str]) -> None:
        self.failures = list(failures)
        self.calls = 0
        self.waits = 0
        self.mouse = FakeMouse(self)

    def wait_for_timeout(self, ms: int) -> None:
        self.waits += 1


class WheelWithRetryTest(unittest.TestCase):
    def test_retries_permission_denied_and_reinstalls(self):
        page = FakeWheelPage(["wheel 失败 (exit=1, code=permission_denied)"] * 2)
        reinstalls: list[object] = []
        _wheel_with_retry(page, 3000, reinstall=reinstalls.append)
        self.assertEqual(page.calls, 3)
        self.assertEqual(len(reinstalls), 2)
        self.assertEqual(page.waits, 2)

    def test_gives_up_after_attempts(self):
        page = FakeWheelPage(["wheel 失败 (exit=1, code=permission_denied)"] * 5)
        with self.assertRaises(BskCommandError):
            _wheel_with_retry(page, 3000, reinstall=lambda p: None)
        self.assertEqual(page.calls, 3)

    def test_non_permission_error_is_not_retried(self):
        page = FakeWheelPage(["wheel 失败 (exit=1, code=timeout)"])
        with self.assertRaises(BskCommandError):
            _wheel_with_retry(page, 3000, reinstall=lambda p: None)
        self.assertEqual(page.calls, 1)
        self.assertEqual(page.waits, 0)
