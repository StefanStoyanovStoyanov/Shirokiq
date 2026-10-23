import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import discord
import spotipy
from discord.ext import commands
from spotipy.oauth2 import SpotifyClientCredentials
from yt_dlp import YoutubeDL

logger = logging.getLogger(__name__)


class YTDLLogger:
    def debug(self, message: str) -> None:
        logger.debug(message)

    def info(self, message: str) -> None:
        logger.info(message)

    def warning(self, message: str) -> None:
        if "Unable to recognize playlist. Downloading just video" not in message:
            logger.warning("%s", message)

    def error(self, message: str) -> None:
        logger.error("%s", message)


YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "quiet": True,
    "logger": YTDLLogger(),
    "js_runtimes": {"node": {}},
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
    def __init__(self, client_id: str, client_secret: str, market: str = "US"):
        auth_manager = SpotifyClientCredentials(
            client_id=client_id, client_secret=client_secret
        )
        self.client = spotipy.Spotify(auth_manager=auth_manager)
        self.market = market.upper()

    def extract_tracks(self, url: str) -> list[str]:
        resource_type, resource_id = self._parse_resource(url)
        if resource_type is None or resource_id is None:
            return []

        if resource_type == "track":
            return self._track_query(self.client.track(resource_id))

        if resource_type == "artist":
            albums = self._get_all_items(
                self.client.artist_albums(
                    resource_id,
                    include_groups="album,single,compilation,appears_on",
                    country=self.market,
                    limit=50,
                )
            )
            pages = (
                self.client.album_tracks(album["id"], limit=50)
                for album in albums
                if album.get("id")
            )
        elif resource_type == "album":
            pages = (self.client.album_tracks(resource_id, limit=50),)
        else:
            pages = (self.client.playlist_items(resource_id, limit=100),)

        tracks = []
        seen_ids = set()
        for page in pages:
            for item in self._get_all_items(page):
                track = item.get("track", item)
                if not track or not track.get("artists"):
                    continue
                track_id = track.get("id")
                if track_id and track_id in seen_ids:
                    continue
                if track_id:
                    seen_ids.add(track_id)
                tracks.extend(self._track_query(track))
        return tracks

    def _get_all_items(self, page: dict[str, Any]) -> list[dict[str, Any]]:
        items = []
        while page:
            items.extend(page.get("items", []))
            page = self.client.next(page) if page.get("next") else None
        return items

    @staticmethod
    def _track_query(track: dict[str, Any]) -> list[str]:
        if not track or not track.get("artists") or not track.get("name"):
            return []
        return [SpotifyHelper._format_track(track)]

    @staticmethod
    def _parse_resource(url: str) -> tuple[Optional[str], Optional[str]]:
        if url.startswith("spotify:"):
            parts = url.split(":")
            if len(parts) >= 3 and parts[1] in {
                "track",
                "album",
                "playlist",
                "artist",
            }:
                return parts[1], parts[2]
            return None, None

        parsed = urlsplit(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
        if host != "spotify.com" and not host.endswith(".spotify.com"):
            return None, None
        parts = [part for part in parsed.path.split("/") if part]
        for index, part in enumerate(parts[:-1]):
            if part in {"track", "album", "playlist", "artist"}:
                return part, parts[index + 1]
        return None, None

    @staticmethod
    def is_spotify_link(url: str) -> bool:
        if url.startswith("spotify:"):
            return True
        parsed = urlsplit(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
        return host == "spotify.com" or host.endswith(".spotify.com")

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

    @classmethod
    async def expand_youtube_link(cls, url: str) -> list[str]:
        video_url = cls.youtube_video_url(url)
        if video_url:
            return [video_url]

        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                None,
                lambda: YoutubeDL(YTDL_FORMAT_OPTIONS).extract_info(
                    url, download=False
                ),
            )
            if not data:
                return []

            entries = data.get("entries")
            if entries is None:
                return [url]

            links = []
            for entry in entries:
                if not entry:
                    continue
                link = (
                    entry.get("webpage_url")
                    or entry.get("original_url")
                    or entry.get("url")
                )
                if link and not link.startswith("http"):
                    extractor = entry.get("extractor_key", "")
                    if "youtube" in extractor.lower():
                        link = f"https://www.youtube.com/watch?v={link}"
                if link:
                    links.append(link)
            return links
        except Exception:
            logger.exception("Could not read YouTube link %r", url)
            raise

    @classmethod
    async def resolve_title(cls, query: str) -> Optional[str]:
        loop = asyncio.get_running_loop()
        video_url = cls.youtube_video_url(query)
        target = video_url or (
            query if query.startswith("http") else f"ytsearch:{query}"
        )
        try:
            data = await loop.run_in_executor(
                None,
                lambda: YoutubeDL(
                    {**YTDL_FORMAT_OPTIONS, "noplaylist": True}
                ).extract_info(target, download=False),
            )
            if data and data.get("entries") is not None:
                data = next((entry for entry in data["entries"] if entry), None)
            return data.get("title") if data else None
        except Exception:
            logger.exception("Could not resolve title for %r", query)
            return None

    @staticmethod
    def youtube_video_url(url: str) -> Optional[str]:
        parsed = urlsplit(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
        video_id = None

        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
        elif host == "youtube.com" or host.endswith(".youtube.com"):
            video_id = parse_qs(parsed.query).get("v", [None])[0]
            if not video_id:
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
                    video_id = parts[1]

        if not video_id:
            return None
        return f"https://www.youtube.com/watch?v={video_id}"

    @staticmethod
    def is_youtube_link(url: str) -> bool:
        parsed = urlsplit(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
        return (
            host == "youtu.be"
            or host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtube-nocookie.com"
            or host.endswith(".youtube-nocookie.com")
        )


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

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if (
            self.bot.user is None
            or member.id != self.bot.user.id
            or before.channel is None
            or after.channel is not None
        ):
            return

        guild_id = member.guild.id
        async with self.get_lock(guild_id):
            self.queues.pop(guild_id, None)
            self.current.pop(guild_id, None)
            self.cancel_idle_disconnect(guild_id)

    @commands.command(name="play", help="Play music from a search or link.")
    @commands.guild_only()
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        await self.enqueue(ctx, query)

    @commands.command(name="playnext", help="Play a song or link next.")
    @commands.guild_only()
    async def playnext(self, ctx: commands.Context, *, query: str) -> None:
        await self.enqueue(ctx, query, play_next=True)

    async def enqueue(
        self, ctx: commands.Context, query: str, *, play_next: bool = False
    ) -> None:
        guild = ctx.guild
        if guild is None:
            return

        guild_id = guild.id
        self.text_channels[guild_id] = ctx.channel

        voice_state = ctx.author.voice
        voice_client = guild.voice_client
        if voice_client is None and (
            voice_state is None or voice_state.channel is None
        ):
            await ctx.send("You are not in a voice channel.")
            return
        if voice_client and (
            voice_state is None or voice_state.channel != voice_client.channel
        ):
            await ctx.send("Join the bot's voice channel to play music.")
            return

        is_link = SpotifyHelper.is_spotify_link(query) or YTDLSource.is_youtube_link(
            query
        )
        try:
            if SpotifyHelper.is_spotify_link(query):
                tracks = await asyncio.get_running_loop().run_in_executor(
                    None, self.spotify_helper.extract_tracks, query
                )
                if not tracks:
                    await ctx.send(
                        "Could not find songs in that Spotify link."
                    )
                    return
            elif YTDLSource.is_youtube_link(query):
                tracks = await YTDLSource.expand_youtube_link(query)
                if not tracks:
                    await ctx.send("Could not find videos in that YouTube link.")
                    return
            else:
                tracks = [query]
        except Exception:
            logger.exception("Could not expand music link %r", query)
            await ctx.send(
                "Could not read songs from that link."
            )
            return

        title = None
        if len(tracks) == 1 and is_link:
            title = await YTDLSource.resolve_title(tracks[0])

        async with self.get_lock(guild_id):
            voice_client = guild.voice_client
            voice_state = ctx.author.voice
            if voice_client is None:
                if voice_state is None or voice_state.channel is None:
                    await ctx.send("You are not in a voice channel.")
                    return
                voice_client = await voice_state.channel.connect(self_deaf=True)
            elif voice_state is None or voice_state.channel != voice_client.channel:
                await ctx.send("Join the bot's voice channel to play music.")
                return

            self.cancel_idle_disconnect(guild_id)
            queue = self.queues.setdefault(guild_id, [])
            if play_next:
                queue[0:0] = tracks
            else:
                queue.extend(tracks)

        if len(tracks) > 1:
            action = "play next" if play_next else "the queue"
            message = f"Added {len(tracks)} songs to {action}."
        elif title:
            action = "play next" if play_next else "the queue"
            message = f"Added **{title}** to {action}."
        else:
            action = "play next" if play_next else "the queue"
            message = f"Added `{query}` to {action}."
        await ctx.send(message)
        await self.play_next(guild_id)

    @commands.command(name="shuffle", help="Shuffle the songs waiting in the queue.")
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return

        async with self.get_lock(guild.id):
            queue = self.queues.get(guild.id, [])
            if len(queue) < 2:
                await ctx.send("Add at least two songs to the queue to shuffle it.")
                return

            random.shuffle(queue)
            track_count = len(queue)

        await ctx.send(f"Shuffled {track_count} songs in the queue.")

    @commands.command(name="stop", help="Stop playback and clear the queue.")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return

        guild_id = guild.id
        voice_client = guild.voice_client
        async with self.get_lock(guild_id):
            self.queues[guild_id] = []
            self.current.pop(guild_id, None)
            self.cancel_idle_disconnect(guild_id)
            if voice_client and voice_client.is_connected():
                if voice_client.is_playing():
                    voice_client.stop()
                await voice_client.disconnect()

        await ctx.send("Playback stopped and queue cleared.")

    @commands.command(name="skip", help="Skip the currently playing track.")
    @commands.guild_only()
    async def skip(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return

        voice_client = guild.voice_client
        if voice_client is None or not (
            voice_client.is_playing() or voice_client.is_paused()
        ):
            await ctx.send("Nothing is currently playing.")
            return

        voice_client.stop()
        await ctx.send("Skipped the current track.")


async def setup(bot: commands.Bot) -> None:
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    spotify_helper = SpotifyHelper(
        client_id=config["spotify_client_id"],
        client_secret=config["spotify_client_secret"],
        market=config.get("spotify_market", "US"),
    )
    await bot.add_cog(Music(bot, spotify_helper))
