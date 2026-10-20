import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

import discord
import spotipy
from discord import app_commands
from discord.ext import commands
from spotipy.oauth2 import SpotifyClientCredentials
from yt_dlp import YoutubeDL

logger = logging.getLogger(__name__)

YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "source_address": "0.0.0.0",
    "prefer_ffmpeg": True,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.124 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.5",
    },
}
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
ytdl = YoutubeDL(YTDL_FORMAT_OPTIONS)


class SpotifyHelper:
    def __init__(self, client_id: str, client_secret: str):
        auth_manager = SpotifyClientCredentials(
            client_id=client_id, client_secret=client_secret
        )
        self.client = spotipy.Spotify(auth_manager=auth_manager)

    def extract_tracks(self, url: str) -> list[str]:
        if "/track/" in url:
            track = self.client.track(url)
            return [self._format_track(track)] if track.get("artists") else []

        if "/playlist/" in url:
            page = self.client.playlist_items(url)
        elif "/album/" in url:
            page = self.client.album_tracks(url)
        else:
            return []

        tracks = []
        while page:
            for item in page.get("items", []):
                track = item.get("track", item)
                if track and track.get("artists"):
                    tracks.append(self._format_track(track))
            page = self.client.next(page) if page.get("next") else None
        return tracks

    @staticmethod
    def _format_track(track: dict[str, Any]) -> str:
        return f"{track['artists'][0]['name']} - {track['name']}"


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(
        self, source: discord.AudioSource, *, data: dict[str, Any], volume: float = 0.5
    ):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title", "Unknown title")
        self.query = ""

    @classmethod
    async def from_query(cls, query: str, *, volume: float = 0.5):
        loop = asyncio.get_running_loop()
        search_query = query if query.startswith("http") else f"ytsearch:{query}"
        try:
            data = await loop.run_in_executor(
                None, lambda: ytdl.extract_info(search_query, download=False)
            )
            if not data:
                return None
            if "entries" in data:
                data = next((entry for entry in data["entries"] if entry), None)
                if not data:
                    return None

            source = discord.FFmpegPCMAudio(data["url"], **FFMPEG_OPTIONS)
            return cls(source, data=data, volume=volume)
        except Exception:
            logger.exception("Could not resolve or open audio for query %r", query)
            return None


class Music(commands.Cog):
    IDLE_DISCONNECT_DELAY = 180

    def __init__(self, bot: commands.Bot, spotify_helper: SpotifyHelper):
        self.bot = bot
        self.spotify_helper = spotify_helper
        self.queues: dict[int, list[str]] = {}
        self.current: dict[int, YTDLSource] = {}
        self.text_channels: dict[int, Any] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.idle_tasks: dict[int, asyncio.Task[None]] = {}

    def get_lock(self, guild_id: int) -> asyncio.Lock:
        return self.locks.setdefault(guild_id, asyncio.Lock())

    async def send_to_guild(self, guild_id: int, message: str) -> None:
        channel = self.text_channels.get(guild_id)
        if channel is not None:
            try:
                await channel.send(message)
            except discord.HTTPException:
                logger.exception("Could not send a music update in guild %s", guild_id)

    def cancel_idle_disconnect(self, guild_id: int) -> None:
        task = self.idle_tasks.pop(guild_id, None)
        if task is not None:
            task.cancel()

    def schedule_idle_disconnect(self, guild_id: int) -> None:
        self.cancel_idle_disconnect(guild_id)
        task = asyncio.create_task(self.disconnect_when_idle(guild_id))
        self.idle_tasks[guild_id] = task

    async def disconnect_when_idle(self, guild_id: int) -> None:
        task = asyncio.current_task()
        try:
            await asyncio.sleep(self.IDLE_DISCONNECT_DELAY)
            async with self.get_lock(guild_id):
                guild = self.bot.get_guild(guild_id)
                voice_client = guild.voice_client if guild else None
                if (
                    not self.queues.get(guild_id)
                    and voice_client
                    and voice_client.is_connected()
                    and not voice_client.is_playing()
                ):
                    await voice_client.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Could not disconnect idle voice client in guild %s", guild_id
            )
        finally:
            if self.idle_tasks.get(guild_id) is task:
                self.idle_tasks.pop(guild_id, None)

    async def play_next(self, guild_id: int) -> None:
        async with self.get_lock(guild_id):
            guild = self.bot.get_guild(guild_id)
            voice_client = guild.voice_client if guild else None
            if not voice_client or not voice_client.is_connected():
                return
            if voice_client.is_playing():
                return

            self.cancel_idle_disconnect(guild_id)
            self.current.pop(guild_id, None)

            while self.queues.get(guild_id):
                query = self.queues[guild_id].pop(0)
                player = await YTDLSource.from_query(query)
                if player is None:
                    await self.send_to_guild(
                        guild_id, f"Could not find or play `{query}`. Skipping."
                    )
                    continue

                self.current[guild_id] = player
                try:
                    voice_client.play(
                        player,
                        after=lambda error: self.after_track(guild_id, error),
                    )
                except Exception:
                    self.current.pop(guild_id, None)
                    player.cleanup()
                    logger.exception("Could not start playback for query %r", query)
                    await self.send_to_guild(
                        guild_id, f"An error occurred while trying to play `{query}`."
                    )
                    continue

                await self.send_to_guild(guild_id, f"Now playing: **{player.title}**")
                return

            self.current.pop(guild_id, None)
            self.schedule_idle_disconnect(guild_id)

    def after_track(self, guild_id: int, error: Optional[Exception]) -> None:
        if error:
            logger.error("Audio playback failed in guild %s: %s", guild_id, error)
        asyncio.run_coroutine_threadsafe(self.play_next(guild_id), self.bot.loop)

    @app_commands.command(name="play", description="Plays music from search or link")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "This command can only be used in a server."
            )
            return

        guild_id = guild.id
        self.text_channels[guild_id] = interaction.channel

        voice_state = interaction.user.voice
        voice_client = guild.voice_client
        if voice_client is None and (
            voice_state is None or voice_state.channel is None
        ):
            await interaction.followup.send(
                "You are not in a voice channel.", ephemeral=True
            )
            return
        if voice_client and (
            voice_state is None or voice_state.channel != voice_client.channel
        ):
            await interaction.followup.send(
                "Join the bot's voice channel to play music.", ephemeral=True
            )
            return

        try:
            if "open.spotify.com/" in query:
                tracks = await self.bot.loop.run_in_executor(
                    None, self.spotify_helper.extract_tracks, query
                )
                if not tracks:
                    await interaction.followup.send(
                        "Could not extract tracks from the provided link."
                    )
                    return
            else:
                tracks = [query]
        except Exception:
            logger.exception("Could not extract Spotify tracks from %r", query)
            await interaction.followup.send(
                "Could not read tracks from the provided Spotify link."
            )
            return

        async with self.get_lock(guild_id):
            voice_client = guild.voice_client
            voice_state = interaction.user.voice
            if voice_client is None:
                if voice_state is None or voice_state.channel is None:
                    await interaction.followup.send(
                        "You are not in a voice channel.", ephemeral=True
                    )
                    return
                voice_client = await voice_state.channel.connect(self_deaf=True)
            elif voice_state is None or voice_state.channel != voice_client.channel:
                await interaction.followup.send(
                    "Join the bot's voice channel to play music.", ephemeral=True
                )
                return

            self.cancel_idle_disconnect(guild_id)
            self.queues.setdefault(guild_id, []).extend(tracks)

        message = (
            f"Added {len(tracks)} tracks to the queue."
            if len(tracks) > 1
            else f"Added `{query}` to the queue."
        )
        await interaction.followup.send(message)
        await self.play_next(guild_id)

    @app_commands.command(name="stop", description="Stops music and clears the queue")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        guild_id = guild.id
        await interaction.response.defer(thinking=True)
        voice_client = guild.voice_client
        async with self.get_lock(guild_id):
            self.queues[guild_id] = []
            self.current.pop(guild_id, None)
            self.cancel_idle_disconnect(guild_id)
            if voice_client and voice_client.is_connected():
                if voice_client.is_playing():
                    voice_client.stop()
                await voice_client.disconnect()

        await interaction.followup.send("Playback stopped and queue cleared.")


async def setup(bot: commands.Bot) -> None:
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    spotify_helper = SpotifyHelper(
        client_id=config["spotify_client_id"],
        client_secret=config["spotify_client_secret"],
    )
    await bot.add_cog(Music(bot, spotify_helper))
