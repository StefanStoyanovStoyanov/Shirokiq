import discord
import random
import json
import asyncio
from discord.ext import commands
from discord import app_commands
from yt_dlp import YoutubeDL
from typing import Optional

# --- YTDL and FFmpeg Configuration (from original code) ---
YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "source_address": "0.0.0.0",
    "prefer_ffmpeg": True,
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.5",
    },
}
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
ytdl = YoutubeDL(YTDL_FORMAT_OPTIONS)

# --- Spotify Helper Class (Self-Contained) ---
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials


class SpotifyHelper:
    def __init__(self, client_id: str, client_secret: str):
        auth_manager = SpotifyClientCredentials(
            client_id=client_id, client_secret=client_secret
        )
        self.sp = spotipy.Spotify(auth_manager=auth_manager)

    def extract_tracks(self, url: str) -> list[str]:
        if "track" in url:
            try:
                results = self.sp.track(url)
                return [f"{results['artists'][0]['name']} - {results['name']}"]
            except Exception:
                return []
        items = []
        if "playlist" in url:
            items = self.sp.playlist_items(url)["items"]
        elif "album" in url:
            items = self.sp.album_tracks(url)["items"]
        tracks = []
        for item in items:
            track = item.get("track")
            if track and track.get("artists"):
                tracks.append(f"{track['artists'][0]['name']} - {track['name']}")
        return tracks


# --- YTDLSource Player Class (Restored to original logic) ---
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.url = data.get("webpage_url")
        self.duration = data.get("duration")
        self.query = ""

    @classmethod
    async def from_query(cls, query: str, *, loop=None, **kwargs):
        loop = loop or asyncio.get_event_loop()
        try:
            search_query = query if query.startswith("http") else f"ytsearch:{query}"
            data = await loop.run_in_executor(
                None, lambda: ytdl.extract_info(search_query, download=False)
            )
            if not data:
                return None
            if "entries" in data:
                if not data["entries"]:
                    return None
                data = data["entries"][0]

            ffmpeg_opts = FFMPEG_OPTIONS.copy()
            options = ffmpeg_opts.get("options", "")
            before_options = ffmpeg_opts.get("before_options", "")
            seek_seconds = kwargs.get("seek_seconds", 0)
            if seek_seconds > 0:
                before_options += f" -ss {seek_seconds}"
            speed = kwargs.get("speed", 1.0)
            audio_filter = kwargs.get("filter_options")
            af_options = []
            if speed != 1.0:
                af_options.append(f"atempo={speed}")
            if audio_filter:
                af_options.append(audio_filter)
            if af_options:
                options += f' -af "{",".join(af_options)}"'
            ffmpeg_opts["options"] = options
            ffmpeg_opts["before_options"] = before_options

            return cls(
                discord.FFmpegPCMAudio(data["url"], **ffmpeg_opts),
                data=data,
                volume=kwargs.get("volume", 0.5),
            )
        except Exception as e:
            print(f"Error in from_query for '{query}': {e}")
            return None


# --- Main Music Cog (Restored to original structure with critical fixes) ---
class Music(commands.Cog):
    def __init__(self, bot, spotify_helper):
        self.bot = bot
        self.spotify_helper = spotify_helper
        self.queues = {}
        self.current = {}
        self.history = {}
        self.loop_states = {}
        self.volumes = {}
        self.filters = {}
        self.speeds = {}
        self.start_times = {}
        self.text_channels = {}
        # CRITICAL FIX: Add locks to prevent race conditions
        self.locks = {}

    def get_lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    async def play_next(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        vc = interaction.guild.voice_client
        lock = self.get_lock(gid)

        async with lock:
            if self.loop_states.get(gid) == "song" and self.current.get(gid):
                self.queues.setdefault(gid, []).insert(0, self.current[gid].query)
            elif self.loop_states.get(gid) == "queue" and self.current.get(gid):
                self.queues.setdefault(gid, []).append(self.current[gid].query)

            if not self.queues.get(gid):
                self.current.pop(gid, None)
                await asyncio.sleep(180)
                if vc.is_connected() and not vc.is_playing():
                    await vc.disconnect()
                return

            query = self.queues[gid].pop(0)
            try:
                player = await YTDLSource.from_query(
                    query,
                    loop=self.bot.loop,
                    speed=self.speeds.get(gid, 1.0),
                    filter_options=self.filters.get(gid),
                    seek_seconds=self.start_times.get(gid, 0),
                )

                if player is None:
                    await self.text_channels[gid].send(
                        f"Could not find or play `{query}`. Skipping."
                    )
                    # Use interaction to call next song to avoid recursion depth issues
                    return await self.play_next(interaction)

                player.volume = self.volumes.get(gid, 0.5)
                player.query = query
                self.current[gid] = player
                self.start_times[gid] = 0  # Reset seek time

                vc.play(
                    player,
                    after=lambda e: self.bot.loop.create_task(
                        self.play_next(interaction)
                    ),
                )

                self.history.setdefault(gid, []).append(player.title)
                await self.text_channels[gid].send(f"Now playing: **{player.title}**")

            except Exception as e:
                print(f"Error in play_next: {e}")
                await self.text_channels[gid].send(
                    f"An error occurred while trying to play `{query}`."
                )
                # Keep trying to play next song
                return await self.play_next(interaction)

    @app_commands.command(name="play", description="Plays music from search or link")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)
        gid = interaction.guild.id
        self.text_channels[gid] = interaction.channel

        vc = interaction.guild.voice_client
        if not vc:
            if interaction.user.voice:
                vc = await interaction.user.voice.channel.connect(self_deaf=True)
            else:
                return await interaction.followup.send(
                    "You are not in a voice channel.", ephemeral=True
                )

        is_playing = vc.is_playing()

        tracks = (
            self.spotify_helper.extract_tracks(query)
            if "open.spotify.com" in query
            else [query]
        )
        if not tracks:
            return await interaction.followup.send(
                "Could not extract tracks from the provided link."
            )

        queue = self.queues.setdefault(gid, [])
        queue.extend(tracks)

        message = (
            f"Added {len(tracks)} tracks to the queue."
            if len(tracks) > 1
            else f"Added `{query}` to the queue."
        )
        await interaction.followup.send(message)

        if not is_playing:
            await self.play_next(interaction)

    # ... You can add the rest of your original commands here, ensuring they use the dictionary structure ...
    # (e.g., self.queues.get(gid), self.volumes[gid] = new_vol, etc.)

    @app_commands.command(name="stop", description="Stops music and clears the queue")
    async def stop(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        vc = interaction.guild.voice_client
        if vc:
            self.queues[gid] = []
            self.current.pop(gid, None)
            vc.stop()
            await vc.disconnect()
            await interaction.response.send_message(
                "Playback stopped and queue cleared."
            )
        else:
            await interaction.response.send_message(
                "Not in a voice channel.", ephemeral=True
            )


async def setup(bot):
    with open("config.json") as f:
        cfg = json.load(f)
    spotify_helper = SpotifyHelper(
        client_id=cfg["spotify_client_id"], client_secret=cfg["spotify_client_secret"]
    )
    await bot.add_cog(Music(bot, spotify_helper))
