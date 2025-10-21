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


if __name__ == "__main__":
    asyncio.run(main())
