from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
GENERATED_PATTERN = re.compile(r"Generated: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    should_run = True
    reason = "manual or unknown event"

    if event_name == "schedule":
        now = datetime.now(TAIPEI)
        target_hour = 14 if now.hour >= 14 else 6
        html_path = Path("docs/index.html")
        generated = None
        if html_path.exists():
            match = GENERATED_PATTERN.search(html_path.read_text(encoding="utf-8"))
            if match:
                generated = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TAIPEI)

        fresh = (
            generated is not None
            and generated.date() == now.date()
            and generated.hour >= target_hour
        )
        should_run = not fresh
        reason = (
            f"snapshot already fresh for {target_hour:02d}:00 window ({generated})"
            if fresh
            else f"snapshot missing or stale for {target_hour:02d}:00 window ({generated})"
        )

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"run={'true' if should_run else 'false'}\n")
            handle.write(f"reason={reason}\n")
    print(f"Run scan: {should_run} | {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
