<<<<<<< HEAD
import discord
import json
import asyncio
from discord.ext import commands


async def main():
    with open("config.json") as f:
        config = json.load(f)
        token = config.get("discord_token")

    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        print(f"Logged in as {bot.user}")
        print("------")
        try:
            # Load the single, restored cog
            await bot.load_extension("cogs.music_cog")
            print("Music cog loaded successfully.")
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} command(s)")
        except Exception as e:
            print(f"Failed to load cog or sync commands: {e}")

    await bot.start(token)
=======
import asyncio
import json
import logging
from pathlib import Path

import discord
from discord.ext import commands


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class MusicBot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.load_extension("cogs.music_cog")
        synced = await self.tree.sync()
        logger.info("Synced %s application command(s)", len(synced))


async def main():
    config_path = Path(__file__).with_name("config.json")
    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)

    token = config.get("discord_token")
    if not token:
        raise ValueError("config.json must contain a non-empty discord_token")

    intents = discord.Intents.default()
    async with MusicBot(command_prefix="!", intents=intents) as bot:
        await bot.start(token)
>>>>>>> 4138195 (Add Shirokiq music bot)


if __name__ == "__main__":
    asyncio.run(main())
