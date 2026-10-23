#!/usr/bin/env python3
"""Fail if a reply can exceed the exchange character limit.

    python scripts/check_draft_limits.py

Exit code 0 means every checked text was cut to the platform ceiling.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from draft_limits import PLATFORM_DRAFT_LIMITS, draft_char_limit
    from llm import fit_reply
    expected = {"Kwork": 2000, "FL.ru": 5000}
    if PLATFORM_DRAFT_LIMITS != expected:
        print(f"FAIL platform limits {PLATFORM_DRAFT_LIMITS!r} != {expected!r}")
        return 1

    samples = [
        "я" * 20_000,
        "Шаг первый. " * 2_000,
        "word " * 8_000,
        "без пробелов_" * 3_000,
    ]
    for source, limit in expected.items():
        if draft_char_limit(source) != limit:
            print(f"FAIL {source} limit {draft_char_limit(source)} != {limit}")
            return 1
        if draft_char_limit(source, short=True) > limit:
            print(f"FAIL short mode for {source} exceeds the platform limit")
            return 1
        for sample in samples:
            fitted = fit_reply(sample, limit)
            count = len(fitted)
            if count > limit:
                print(f"FAIL {source}: {count} > {limit}")
                return 1
            print(f"OK {source} chars={count} limit={limit}")
    print("draft limits ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
