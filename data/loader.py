import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from data.config import configfile


async def get_available_server() -> TelegramAPIServer:
    local_url = "http://localhost:8081"
    wide_url = "https://tg.aartzz.pp.ua"
    official_url = "https://api.telegram.org"

    async with aiohttp.ClientSession() as session:
        print("O trying localhost")
        try:
            async with session.get(f"{local_url}/getMe", timeout=10) as resp:
                print("! using localhost")
                return TelegramAPIServer.from_base(local_url)
        except Exception as e:
            print(f'X localhost returned {e}')
        print("X localhost unavailable")
        print("O trying tg.aartzz.pp.ua")
        try:
            async with session.get(f'{wide_url}/getMe', timeout=10) as resp:
                print('! using tg.aartzz.pp.ua')
                return TelegramAPIServer.from_base(wide_url)
        except Exception as e:
            print(f'X tg.aartzz.pp returned {e}')
    print("X tg.aartzz.pp.ua unavailable")
    print("! local api unavailable, falling back to default")
    return TelegramAPIServer.from_base(official_url)


async def init_bot() -> tuple[Bot, Dispatcher]:
    server = await get_available_server()
    session = AiohttpSession(api=server)
    bot = Bot(token=configfile["TOKEN"], session=session)
    dp = Dispatcher(bot=bot)
    return bot, dp