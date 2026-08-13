"""权威搜索运行模式：默认离线 fail-closed；live 仅 allowlist 已核验源。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_AUTHORITY_SEARCH_MODE = "offline_fail_closed"
ALLOWED_AUTHORITY_SEARCH_MODES = frozenset({"offline_fail_closed", "live_allowlist"})
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ENV_PATH = PROJECT_ROOT / ".env"


class AuthorityModeError(ValueError):
    """AUTHORITY_SEARCH_MODE 非法。"""


def load_authority_search_mode() -> str:
    """读取权威搜索模式；缺省 offline_fail_closed。"""

    if PROJECT_ENV_PATH.is_file():
        values = dotenv_values(PROJECT_ENV_PATH)
        raw = values.get("AUTHORITY_SEARCH_MODE")
        text = DEFAULT_AUTHORITY_SEARCH_MODE if raw is None else str(raw)
    else:
        text = os.getenv("AUTHORITY_SEARCH_MODE", DEFAULT_AUTHORITY_SEARCH_MODE)
    mode = text.strip().lower()
    if mode not in ALLOWED_AUTHORITY_SEARCH_MODES:
        raise AuthorityModeError(
            "AUTHORITY_SEARCH_MODE 仅允许 offline_fail_closed 或 live_allowlist"
        )
    return mode
