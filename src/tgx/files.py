from __future__ import annotations

import re
from datetime import datetime


def archive_name(title: str, created_at: str) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:100] or "Telegram"
    if title.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *[f"COM{i}" for i in range(1, 10)],
        *[f"LPT{i}" for i in range(1, 10)],
    }:
        title = "_" + title
    timestamp = datetime.fromisoformat(created_at).astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{title}_{timestamp}"
