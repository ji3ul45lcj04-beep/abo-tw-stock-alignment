from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
PUBLIC_SITE = "https://ji3ul45lcj04-beep.github.io/abo-tw-stock-alignment/"
GENERATED_PATTERN = re.compile(r"Generated: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def target_hour_for(now: datetime) -> int | None:
    """Return the latest due publication window in Taipei time."""
    if now.hour < 6:
        return None
    return 14 if now.hour >= 14 else 6


def parse_generated(html: str) -> datetime | None:
    match = GENERATED_PATTERN.search(html)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TAIPEI)


def is_fresh(generated: datetime | None, now: datetime, target_hour: int) -> bool:
    return generated is not None and generated.date() == now.date() and generated.hour >= target_hour


def read_local_generated() -> datetime | None:
    html_path = Path("docs/index.html")
    if not html_path.exists():
        return None
    return parse_generated(html_path.read_text(encoding="utf-8"))


def read_public_generated() -> datetime | None:
    request = Request(
        f"{PUBLIC_SITE}?freshness_check={int(datetime.now().timestamp())}",
        headers={"Cache-Control": "no-cache", "User-Agent": "abo-tw-stock-scheduler"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            return parse_generated(response.read().decode("utf-8"))
    except (OSError, URLError, UnicodeError):
        return None


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    deploy_only = os.environ.get("DEPLOY_ONLY", "").lower() == "true"
    should_scan = not deploy_only
    should_deploy = True
    reason = "manual full scan" if not deploy_only else "manual deploy-only recovery"

    if event_name == "schedule" and not deploy_only:
        now = datetime.now(TAIPEI)
        target_hour = target_hour_for(now)

        if target_hour is None:
            should_scan = False
            should_deploy = False
            reason = "before the first 06:00 Asia/Taipei update window"
        else:
            local_generated = read_local_generated()
            public_generated = read_public_generated()
            local_fresh = is_fresh(local_generated, now, target_hour)
            public_fresh = is_fresh(public_generated, now, target_hour)
            should_scan = not local_fresh
            should_deploy = not public_fresh
            reason = (
                f"window {target_hour:02d}:00; local={local_generated} ({'fresh' if local_fresh else 'stale'}); "
                f"public={public_generated} ({'fresh' if public_fresh else 'stale'})"
            )

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"scan={'true' if should_scan else 'false'}\n")
            handle.write(f"deploy={'true' if should_deploy else 'false'}\n")
            handle.write(f"reason={reason}\n")
    print(f"Scan: {should_scan} | Deploy: {should_deploy} | {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
