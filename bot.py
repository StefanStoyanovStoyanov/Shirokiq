import asyncio
import json
from pathlib import Path

import discord
from discord.ext import commands


class MusicBot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.load_extension("cogs.music_cog")
        self.command_count = sum(
            len(cog.get_commands()) for cog in self.cogs.values()
        )
        self.startup_reported = False

    async def on_ready(self) -> None:
        if self.startup_reported:
            return

        self.startup_reported = True
        print(
            f"Ready on {len(self.guilds)} servers as {self.user.name}. "
            f"Loaded {self.command_count} commands. "
        )


async def main():
    config_path = Path(__file__).with_name("config.json")
    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)

    token = config.get("discord_token")
    if not token:
        raise ValueError("config.json must contain a non-empty discord_token")

    intents = discord.Intents.default()
    intents.message_content = True
    async with MusicBot(command_prefix="debel ", intents=intents) as bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
