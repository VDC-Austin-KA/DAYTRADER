"""Run the vertical-spread bot: ``python -m app.spreads``."""
from __future__ import annotations

import asyncio
import logging

from .bot import SpreadBot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    try:
        asyncio.run(SpreadBot().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
