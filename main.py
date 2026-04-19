import asyncio
import logging
from db.db import Music, Analytics
from handlers import user_menu
from data.loader import *
from modules import tidal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main():
    bot, dp = await init_bot()
    dp.include_router(user_menu.router)

    # Initialize Tidal API instances (non-blocking — bot still works for YouTube if this fails)
    try:
        await tidal.init_instances()
    except Exception as e:
        logging.getLogger(__name__).warning("Tidal init failed: %s — YouTube-only mode", e)

    await dp.start_polling(bot)

if __name__ == '__main__':
    db = Music()
    db.createdb()
    db_analytics = Analytics()
    db_analytics.createdb()
    asyncio.run(main())