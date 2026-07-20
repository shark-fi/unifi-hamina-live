"""``python -m unifi_hamina_live`` / ``unifi-hamina-live`` entry point."""

from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "unifi_hamina_live.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
