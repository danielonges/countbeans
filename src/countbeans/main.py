import asyncio
import sys

from countbeans.bot.server import run
from countbeans.config import get_settings
from countbeans.logging import setup as setup_logging


def main() -> int:
    settings = get_settings()
    setup_logging(level=settings.log_level)
    asyncio.run(run(settings.bot_token.get_secret_value()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
