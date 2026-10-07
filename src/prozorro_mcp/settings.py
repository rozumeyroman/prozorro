"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KYIV_TZ = ZoneInfo("Europe/Kyiv")

DEFAULT_API_URL = "https://public-api.prozorro.gov.ua/api/2.5"
DEFAULT_FILTER_CONFIG = PROJECT_ROOT / "config" / "tender-filter.yaml"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "prozorro.db"
# Excel exports and downloaded tender documents go here (visible to the user, unlike data/).
DEFAULT_OUTPUT_DIR = Path.home() / "Prozorro"
# Tender documents are downloaded only from these hosts (and their subdomains).
DEFAULT_DOC_HOSTS = ("prozorro.gov.ua",)


@dataclass(frozen=True)
class Settings:
    api_url: str = DEFAULT_API_URL
    db_path: Path = DEFAULT_DB_PATH
    filter_config: Path = DEFAULT_FILTER_CONFIG
    # Concurrent GET /tenders/{id} requests; keep low to stay polite and avoid 429s.
    concurrency: int = 4
    request_timeout: float = 30.0
    max_retries: int = 6
    user_agent: str = "prozorro-mcp/0.1 (+https://github.com/rozumeyroman/prozorro)"
    extra_headers: dict[str, str] = field(default_factory=dict)
    output_dir: Path = DEFAULT_OUTPUT_DIR
    doc_hosts: tuple[str, ...] = DEFAULT_DOC_HOSTS

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            api_url=env.get("PROZORRO_API_URL", DEFAULT_API_URL).rstrip("/"),
            db_path=Path(env.get("PROZORRO_DB", DEFAULT_DB_PATH)).expanduser(),
            filter_config=Path(env.get("PROZORRO_FILTER_CONFIG", DEFAULT_FILTER_CONFIG)).expanduser(),
            concurrency=int(env.get("PROZORRO_CONCURRENCY", 4)),
            request_timeout=float(env.get("PROZORRO_TIMEOUT", 30)),
            max_retries=int(env.get("PROZORRO_MAX_RETRIES", 6)),
            output_dir=Path(env.get("PROZORRO_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)).expanduser(),
            doc_hosts=tuple(
                h.strip() for h in env.get("PROZORRO_DOC_HOSTS", ",".join(DEFAULT_DOC_HOSTS)).split(",") if h.strip()
            ),
        )
