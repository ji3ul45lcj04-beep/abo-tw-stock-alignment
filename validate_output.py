from __future__ import annotations

import csv
from collections import Counter
from datetime import date, datetime
from pathlib import Path


CSV_PATH = Path("docs/tw_day_week_alignment.csv")
HTML_PATH = Path("docs/index.html")
REQUIRED_FIELDS = {
    "code",
    "name",
    "data_date",
    "alignment",
    "day_state",
    "week_state",
    "entry_eligibility",
}


def main() -> int:
    if not CSV_PATH.exists() or not HTML_PATH.exists():
        raise RuntimeError("Generated CSV or HTML is missing")

    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_FIELDS.difference(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"Missing CSV fields: {sorted(missing)}")
        rows = list(reader)

    if not rows:
        raise RuntimeError("Scanner returned an empty list; keep the previous publication")

    invalid = [
        row.get("code", "unknown")
        for row in rows
        if row.get("alignment") != "BULL ALIGN"
        or row.get("day_state") != "BULL"
        or row.get("week_state") != "BULL"
    ]
    if invalid:
        raise RuntimeError(f"Non-bull rows leaked into output: {invalid[:20]}")

    dates = [datetime.strptime(row["data_date"], "%Y-%m-%d").date() for row in rows]
    newest = max(dates)
    if (date.today() - newest).days > 10:
        raise RuntimeError(f"Newest market date is stale: {newest}")

    html_text = HTML_PATH.read_text(encoding="utf-8")
    if "TW Day Week Alignment Scanner" not in html_text:
        raise RuntimeError("Generated HTML is not the scanner site")

    eligibility = Counter(row["entry_eligibility"] for row in rows)
    print(f"Validated {len(rows)} rows; newest data {newest}")
    print("Eligibility:", dict(sorted(eligibility.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
