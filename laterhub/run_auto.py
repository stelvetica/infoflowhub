from __future__ import annotations

import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from laterhub.workflow.pipeline import main as core_main
else:
    from laterhub.workflow.pipeline import main as core_main


def build_main_argv(argv: list[str] | None = None) -> list[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in {"--fetch-bilibili", "--fetch-douyin", "--help", "-h"} for arg in args):
        return args
    return ["--fetch-bilibili", "--fetch-douyin", *args]


def main(argv: list[str] | None = None) -> int:
    return core_main(build_main_argv(argv))


def run(argv: list[str] | None = None) -> int:
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(run())
