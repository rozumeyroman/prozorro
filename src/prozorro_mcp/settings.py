"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KYIV_TZ = ZoneInfo("Europe/Kyiv")

DEFAULT_API_URL = "https://public-api.prozorro.gov.ua/api/2.5"
DEFAULT_FILTERS_DIR = PROJECT_ROOT / "config" / "filters"
DEFAULT_FILTER = "cybersecurity"
DEFAULT_FILTER_CONFIG = DEFAULT_FILTERS_DIR / f"{DEFAULT_FILTER}.yaml"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "prozorro.db"
# Excel exports and downloaded tender documents go here (visible to the user, unlike data/).
DEFAULT_OUTPUT_DIR = Path.home() / "Prozorro"
# Tender documents are downloaded only from these hosts (and their subdomains).
DEFAULT_DOC_HOSTS = ("prozorro.gov.ua",)
# Public site (unofficial search API used to check completeness and to find tenders by UA-… id).
DEFAULT_SITE_URL = "https://prozorro.gov.ua"
# Which documents of the winning offer to download in the "minimal" mode.
DEFAULT_WINNER_DOCS_CONFIG = PROJECT_ROOT / "config" / "winner-docs.yaml"


@dataclass(frozen=True)
class Settings:
    api_url: str = DEFAULT_API_URL
    db_path: Path = DEFAULT_DB_PATH
    filter_config: Path | None = None  # explicit single profile file (overrides the profile registry default)
    filters_dir: Path = DEFAULT_FILTERS_DIR
    default_filter: str = DEFAULT_FILTER
    # Concurrent GET /tenders/{id} requests; keep low to stay polite and avoid 429s.
    concurrency: int = 4
    request_timeout: float = 30.0
    max_retries: int = 6
    user_agent: str = "prozorro-mcp/0.1 (+https://github.com/rozumeyroman/prozorro)"
    extra_headers: dict[str, str] = field(default_factory=dict)
    output_dir: Path = DEFAULT_OUTPUT_DIR
    doc_hosts: tuple[str, ...] = DEFAULT_DOC_HOSTS
    site_url: str = DEFAULT_SITE_URL
    winner_docs_config: Path = DEFAULT_WINNER_DOCS_CONFIG
    # An empty feed page in the middle of the feed is requested again this many times before giving up.
    feed_empty_retries: int = 3
    feed_retry_delay: float = 2.0

    @property
    def user_filters_dir(self) -> Path:
        return self.output_dir / "Фільтри"

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            api_url=env.get("PROZORRO_API_URL", DEFAULT_API_URL).rstrip("/"),
            db_path=Path(env.get("PROZORRO_DB", DEFAULT_DB_PATH)).expanduser(),
            filter_config=Path(env["PROZORRO_FILTER_CONFIG"]).expanduser()
            if env.get("PROZORRO_FILTER_CONFIG")
            else None,
            filters_dir=Path(env.get("PROZORRO_FILTERS_DIR", DEFAULT_FILTERS_DIR)).expanduser(),
            default_filter=env.get("PROZORRO_FILTER", DEFAULT_FILTER),
            concurrency=int(env.get("PROZORRO_CONCURRENCY", 4)),
            request_timeout=float(env.get("PROZORRO_TIMEOUT", 30)),
            max_retries=int(env.get("PROZORRO_MAX_RETRIES", 6)),
            output_dir=Path(env.get("PROZORRO_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)).expanduser(),
            doc_hosts=tuple(
                h.strip() for h in env.get("PROZORRO_DOC_HOSTS", ",".join(DEFAULT_DOC_HOSTS)).split(",") if h.strip()
            ),
            site_url=env.get("PROZORRO_SITE_URL", DEFAULT_SITE_URL).rstrip("/"),
            winner_docs_config=Path(env.get("PROZORRO_WINNER_DOCS", DEFAULT_WINNER_DOCS_CONFIG)).expanduser(),
            feed_empty_retries=int(env.get("PROZORRO_FEED_EMPTY_RETRIES", 3)),
        )
