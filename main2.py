import discord
import os
import asyncio
from collections import defaultdict, deque
import yt_dlp

MUSIC_FOLDER = "/home/ren/Music/"
AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".opus", ".aac"}

YDL_OPTS = {
    "outtmpl": os.path.join(MUSIC_FOLDER, "%(uploader)s - %(title)s[%(id)s].%(ext)s"),
    "format": "bestaudio/best",
    "keepvideo": False,
    "default_search": "ytsearch",
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "opus",
        }
    ],
}


class MyClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.voice_client = {}
        self.queue = defaultdict(deque)
        self.now_playing = {}
        self.volumes = defaultdict(lambda: 1.0)  # Default volume 100%

    async def on_ready(self):
        print(f"Logged on as {self.user}!")

    def get_display_name(self, path):
        """Helper to get a clean relative path for display"""
        try:
            rel = os.path.relpath(path, MUSIC_FOLDER)
            return os.path.basename(path) if rel.startswith("..") else rel
        except ValueError:
            return os.path.basename(path)

    def fetch_yt(self, query):
        """Synchronous yt-dlp downloader run in background thread"""
        opts = YDL_OPTS.copy()

        is_playlist = "http" in query and "list=" in query
        if not is_playlist and not query.startswith("http"):
            query = f"ytsearch:{query}"

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=True)
            paths = []
            if "entries" in info:
                for entry in info["entries"]:
                    if not entry:
                        continue
                    filename = ydl.prepare_filename(entry)
                    filename = os.path.splitext(filename)[0] + ".opus"
                    paths.append(filename)
            else:
                filename = ydl.prepare_filename(info)
                filename = os.path.splitext(filename)[0] + ".opus"
                paths.append(filename)
            return paths

    async def resolve_query(self, query):
        """Resolves whether a query is a local file or needs yt-dlp fetch"""
        query = query.strip()
        is_local = any(query.endswith(ext) for ext in AUDIO_EXTENSIONS)

        if is_local:
            return [os.path.join(MUSIC_FOLDER, query)]

        # If not a local file, download it without blocking Discord event loop
        return await asyncio.to_thread(self.fetch_yt, query)

    async def get_voice_client(self, voice_channel):
        guild_id = voice_channel.guild.id
        voice_client = self.voice_client.get(guild_id)
        if voice_client is None or not voice_client.is_connected():
            voice_client = await voice_channel.connect()
            self.voice_client[guild_id] = voice_client

        if voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)

        return voice_client

    def play_next_song_in_queue(self, guild_id, error=None):
        if error:
            print(f"Error in playback: {error}")

        vc = self.voice_client.get(guild_id)
        if vc and vc.is_connected():
            queue = self.queue.get(guild_id)
            if queue:
                path = queue.popleft()
                self.play(vc, path)
            else:
                self.now_playing.pop(guild_id, None)

    def play(self, voice_client, path):
        guild_id = voice_client.guild.id
        vol = self.volumes[guild_id]

        audio_source = discord.FFmpegPCMAudio(path)
        volume_source = discord.PCMVolumeTransformer(audio_source, volume=vol)

        def after_callback(error):
            self.play_next_song_in_queue(guild_id, error)

        voice_client.play(volume_source, after=after_callback)
        self.now_playing[guild_id] = path

    async def on_message(self, message):
        if message.author == self.user:
            return

        q = message.content
        if not q.startswith("%"):
            return

        text_channel = message.channel
        voice_channel = message.author.voice.channel if message.author.voice else None

        # Parse command and arguments
        parts = q.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        # --- %PLAY & %PLAYNEXT ---
        if cmd in ("%play", "%playnext"):
            if not arg:
                await text_channel.send(
                    "Usage:\n`%play <name of song>`\n`%play <URL>`\n`%play <filename.mp3>`"
                )
                return

            if not voice_channel:
                await text_channel.send("Please join a voice channel first!")
                return

            vc = await self.get_voice_client(voice_channel)
            processing_msg = await text_channel.send(f"🔍 Processing your request...")

            try:
                paths = await self.resolve_query(arg)
            except Exception as e:
                await text_channel.send(f"❌ Failed to process query: {e}")
                return

            if not paths:
                await text_channel.send("❌ No results found.")
                return

            guild_id = text_channel.guild.id
            if cmd == "%playnext":
                # Add sequentially to front, preserving order
                paths.reverse()
                for p in paths:
                    self.queue[guild_id].appendleft(p)
                await processing_msg.edit(
                    content=f"✅ Added {len(paths)} song(s) to the **beginning** of the queue."
                )
            else:
                for p in paths:
                    self.queue[guild_id].append(p)
                if len(paths) == 1:
                    await processing_msg.edit(
                        content=f"✅ Added to queue: `{self.get_display_name(paths[0])}`"
                    )
                else:
                    await processing_msg.edit(
                        content=f"✅ Added {len(paths)} song(s) to the queue."
                    )

            if not vc.is_playing() and not vc.is_paused():
                self.play_next_song_in_queue(guild_id)

        # --- %PLAYING ---
        elif cmd == "%playing":
            current = self.now_playing.get(text_channel.guild.id)
            if current:
                await text_channel.send(
                    f"🎵 **Now playing:** `{self.get_display_name(current)}`"
                )
            else:
                await text_channel.send("Nothing is currently playing.")

        # --- %SKIP ---
        elif cmd == "%skip":
            n = 1
            if arg.isdigit():
                n = int(arg)

            guild_id = text_channel.guild.id
            vc = self.voice_client.get(guild_id)

            if vc and vc.is_connected():
                # Pop (n-1) songs out of the queue first
                queue = self.queue[guild_id]
                for _ in range(n - 1):
                    if queue:
                        queue.popleft()

                # Stop currently playing song to trigger next (or empty queue)
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                elif queue:
                    self.play_next_song_in_queue(guild_id)

                await text_channel.send(f"⏭️ Skipped {n} song(s).")
            else:
                await text_channel.send("The bot isn't in a voice channel.")

        # --- %QUEUE ---
        elif cmd == "%queue":
            guild_id = text_channel.guild.id
            queue = self.queue[guild_id]

            if arg.lower() == "clear":
                self.queue[guild_id].clear()
                await text_channel.send("🗑️ Queue cleared.")
                return

            if not queue:
                await text_channel.send("The queue is currently empty.")
                return

            page = 1
            if "--page" in arg:
                parts = arg.split("--page")
                if parts[1].strip().isdigit():
                    page = int(parts[1].strip())
            elif arg.isdigit():
                page = int(arg)

            if page <= 0:
                page = 1
            max_pages = max(1, (len(queue) - 1) // 10 + 1)
            if page > max_pages:
                page = max_pages

            msg = f"📜 **Current Queue (Page {page}/{max_pages}):**\n\n"
            start = (page - 1) * 10
            for i in range(start, min(start + 10, len(queue))):
                msg += f"**{i + 1}.** `{self.get_display_name(queue[i])}`\n"

            await text_channel.send(msg)

        # --- %VOL / %VOLUME ---
        elif cmd in ("%vol", "%volume"):
            if not arg.isdigit():
                await text_channel.send(
                    "Please provide a valid percentage (e.g. 50, 100, 150)."
                )
                return

            pct = int(arg)
            guild_id = text_channel.guild.id
            self.volumes[guild_id] = pct / 100.0

            vc = self.voice_client.get(guild_id)
            if (
                vc
                and vc.is_playing()
                and isinstance(vc.source, discord.PCMVolumeTransformer)
            ):
                vc.source.volume = self.volumes[guild_id]

            await text_channel.send(f"🔊 Volume set to **{pct}%**")

        # --- %FIND ---
        elif cmd == "%find":
            query = arg
            page = 1
            if "--page" in query:
                parts = query.split("--page")
                query = parts[0].strip()
                if parts[1].strip().isdigit():
                    page = int(parts[1].strip())

            query = query.lower()
            if not query:
                await text_channel.send(
                    "Usage: `%find <song or artist name> [--page n]`"
                )
                return

            if not os.path.exists(MUSIC_FOLDER):
                await text_channel.send(
                    f"Directory `{MUSIC_FOLDER}` does not exist on host machine."
                )
                return

            matches = []
            for root, _, files in os.walk(MUSIC_FOLDER):
                for file in files:
                    if (
                        os.path.splitext(file)[1].lower() in AUDIO_EXTENSIONS
                        and query in file.lower()
                    ):
                        matches.append(os.path.join(root, file))

            if not matches:
                await text_channel.send(f"No audio files found matching `{query}`.")
                return

            if page < 1:
                page = 1
            max_pages = max(1, (len(matches) - 1) // 15 + 1)
            if page > max_pages:
                page = max_pages

            displayed = matches[(page - 1) * 15 : page * 15]

            output = f"🔍 **Found {len(matches)} matching file(s) (Page {page}/{max_pages}):**\n\n"
            for idx, m in enumerate(displayed):
                real_idx = idx + 1 + (page - 1) * 15
                output += f"**{real_idx}.** `{self.get_display_name(m)}`\n"

            if page < max_pages:
                output += f"\n*Use `%find {query} --page {page + 1}` to see more.*"

            await text_channel.send(output)

        # --- %LEAVE ---
        elif cmd == "%leave":
            guild_id = text_channel.guild.id
            vc = self.voice_client.get(guild_id)
            if vc and vc.is_connected():
                await vc.disconnect()
                self.queue[guild_id].clear()
                self.now_playing.pop(guild_id, None)
                await text_channel.send("👋 Disconnected from voice channel.")


intents = discord.Intents.default()
intents.message_content = True

token = ""
with open("token.txt", "r") as token_file:
    token = token_file.read().strip()

client = MyClient(intents=intents)
client.run(token)
