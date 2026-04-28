"""One-shot script that turns the gitignored full fixtures into committable
sanitized samples used by tests. Run it manually after refreshing fixtures:

    python tests/_make_sanitized_fixtures.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_RAW = ROOT / "tests" / "fixtures"
FIXTURES_CLEAN = ROOT / "tests" / "fixtures_clean"
FIXTURES_CLEAN.mkdir(exist_ok=True)


def sanitize_kwork(in_path: Path, out_path: Path, max_items: int = 3) -> None:
    data = json.loads(in_path.read_text())
    items = ((data.get("data") or {}).get("pagination") or {}).get("data") or []
    sanitized = []
    for i, item in enumerate(items[:max_items]):
        cleaned_user = {
            "USERID": 100000 + i,
            "username": f"user_anon_{i}",
            "profilepicture": "",
        }
        sanitized.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "description": (item.get("description") or "")[:300],
                "priceLimit": item.get("priceLimit"),
                "max_days": item.get("max_days"),
                "category_id": item.get("category_id"),
                "date_create": item.get("date_create"),
                "user": cleaned_user,
            }
        )
    out = {"success": True, "data": {"pagination": {"data": sanitized}}}
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"  {out_path.name}: {len(sanitized)} items")


def sanitize_flru(in_path: Path, out_path: Path) -> None:
    """For FL.ru we just strip cookies / csrf tokens / analytics from the
    head. The body (lenta cards) is kept as-is — it's public content.
    """
    html = in_path.read_text()
    # Strip the entire <head> ... </head> (no parser needs it for our tests).
    html = re.sub(r"<head\b[^>]*>.*?</head>", "<head></head>", html, flags=re.DOTALL)
    # Strip every <script>...</script>: contains analytics + may contain ids.
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    # Remove any leftover ad-style iframes.
    html = re.sub(r"<iframe\b[^>]*>.*?</iframe>", "", html, flags=re.DOTALL)
    out_path.write_text(html)
    print(f"  {out_path.name}: {len(html)} chars")


def main() -> None:
    print("Sanitizing Kwork JSON:")
    for cat in (41, 11, 45):
        src = FIXTURES_RAW / f"kwork_c{cat}.json"
        if src.exists():
            sanitize_kwork(src, FIXTURES_CLEAN / f"kwork_c{cat}.sanitized.json")

    print("\nSanitizing FL.ru HTML:")
    src = FIXTURES_RAW / "flru_programming_p1.html"
    if src.exists():
        sanitize_flru(
            src, FIXTURES_CLEAN / "flru_programming_p1.sanitized.html"
        )


if __name__ == "__main__":
    main()
