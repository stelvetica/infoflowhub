"""游标续载的切片语义:游标锚在「排序键+身份」上,列表增删都不跳条、不重复。"""

from __future__ import annotations

from web.services.utils import decode_cursor, encode_cursor, sort_rows_with_tiebreak, take_after_cursor


def rows_of(pairs: list[tuple[int, str]]) -> list[dict]:
    return [{"id": item_id, "sort_time": key, "title": f"t{item_id}"} for item_id, key in pairs]


def take_all(rows: list[dict], sort: str = "sort_time", direction: str = "desc", size: int = 2) -> list[dict]:
    """按游标一页页取完,返回拼接结果(模拟客户端反复续载)。"""
    collected: list[dict] = []
    cursor = ""
    for _ in range(20):
        batch, cursor = take_after_cursor(rows, cursor, sort, direction, "id", size)
        collected.extend(batch)
        if not cursor:
            break
    return collected


def test_cursor_round_trip_keeps_separator_safe():
    # 排序键里出现 | 与 & 也不能串味:两段都百分号编码,分隔符不会歧义
    cursor = encode_cursor("2026/09/22 08:03|x&y", "id|7")

    assert decode_cursor(cursor) == ("2026/09/22 08:03|x&y", "id|7")


def test_walk_returns_every_row_once_in_order():
    rows = rows_of([(1, "3"), (2, "2"), (3, "1"), (4, "1"), (5, "0")])
    sort_rows_with_tiebreak(rows, "sort_time", "desc", "id")

    assert [row["id"] for row in take_all(rows)] == [1, 2, 4, 3, 5]


def test_tiebreak_keeps_order_deterministic_for_equal_keys():
    rows = rows_of([(5, "1"), (3, "1"), (9, "1")])
    sort_rows_with_tiebreak(rows, "sort_time", "desc", "id")

    assert [row["id"] for row in rows] == [9, 5, 3]


def test_ascending_direction_walks_the_same_set():
    rows = rows_of([(1, "3"), (2, "2"), (3, "1"), (4, "1"), (5, "0")])
    sort_rows_with_tiebreak(rows, "sort_time", "asc", "id")

    assert [row["id"] for row in take_all(rows, direction="asc")] == [5, 3, 4, 2, 1]


def test_removed_row_between_batches_does_not_skip_others():
    """稍后处理完成一条会让列表左移:游标锚在键上,续载既不跳条也不重复。"""
    rows = rows_of([(1, "5"), (2, "4"), (3, "3"), (4, "2"), (5, "1")])
    sort_rows_with_tiebreak(rows, "sort_time", "desc", "id")
    first, cursor = take_after_cursor(rows, "", "sort_time", "desc", "id", 2)

    # 取走首批后,用户完成了一条(它落在首批里),列表变短
    remaining = [row for row in rows if row["id"] != 1]
    second, cursor = take_after_cursor(remaining, cursor, "sort_time", "desc", "id", 2)
    third, cursor = take_after_cursor(remaining, cursor, "sort_time", "desc", "id", 2)

    walked = [row["id"] for row in first + second + third]
    assert walked == [1, 2, 3, 4, 5], "首批之后应无缝接上,不跳条"
    assert len(walked) == len(set(walked)), "不重复"


def test_no_next_cursor_when_rows_run_out():
    rows = rows_of([(1, "2"), (2, "1")])
    sort_rows_with_tiebreak(rows, "sort_time", "desc", "id")

    batch, cursor = take_after_cursor(rows, "", "sort_time", "desc", "id", 2)

    assert [row["id"] for row in batch] == [1, 2]
    assert cursor == ""


def test_cursor_beyond_last_row_returns_empty():
    rows = rows_of([(1, "2"), (2, "1")])
    sort_rows_with_tiebreak(rows, "sort_time", "desc", "id")

    batch, cursor = take_after_cursor(rows, encode_cursor("1", 2), "sort_time", "desc", "id", 2)

    assert batch == []
    assert cursor == ""