import discord
from collections import defaultdict, deque
import yt_dlp

YDL_OPTS = {
    'outtmpl': '%(uploader)s - %(title)s[%(id)s].%(ext)s',
    'paths': {'home': '/home/ren/Music/DiscordBot/'},
    'format': 'bestaudio/best',
    'keepvideo': True,
    'default_search': 'ytsearch',
    'playlist_items': '1',
    'postprocessors': [{  # Extract audio using ffmpeg
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'opus',
    }]
}

MUSIC_FOLDER = "/home/ren/Music/"

class MyClient(discord.Client):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.voice_client = {}
        self.queue = defaultdict(deque)
        self.now_playing = {}

    async def on_ready(self):
        print(f'Logged on as {self.user}!')

    async def on_message(self, message):
        if message.author == self.user:
            return
        
        voice_channel = None
        voice_client = None
        text_channel = message.channel

        try:
            voice_channel = (await message.author.fetch_voice()).channel
        except discord.NotFound:
            pass

        q = message.content

        if q.startswith("%play "):
            if voice_channel is None:
                await text_channel.send("Please join a voice channel first!")
                return

            path = q.removeprefix("%play").strip()

            if path.startswith("local:"):
                path = MUSIC_FOLDER + path.removeprefix("local:").strip()
            else:
                with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
                    info = ydl.extract_info(path)
                    if 'entries' in info:
                        info = info['entries'][0]
                    filename = ydl.prepare_filename(info)
                    for i in range(len(filename) - 1, -1, -1):
                        if filename[i] == ".":
                            filename = filename[0:i] + ".opus"
                            break

                path = filename

            voice_client = (await self.get_voice_client(voice_channel))
            
            try:
                self.play(voice_client, path)
                await text_channel.send(f"Playing {path[len(MUSIC_FOLDER):]}")
            except discord.ClientException:
                self.queue[voice_client.guild.id].append(path)
                await text_channel.send(f"Added to queue {path[len(MUSIC_FOLDER):]}")

        if q.startswith("%skip"):
            voice_client = (await self.get_voice_client(voice_channel))
            voice_client.stop()
            self.play_next_song_in_queue()
            await text_channel.send(f"Skipped: {self.now_playing.get(text_channel.guild.id, "None")}")

        if q.startswith("%playing"):
            await text_channel.send(f"Now playing: {self.now_playing.get(text_channel.guild.id, "None")}")

        if q.startswith("%queue"):
            command = q.removeprefix("%queue").strip()
            if command == "clear":
                self.queue[text_channel.guild.id] = []
                await text_channel.send("Queue is cleared")
            else:
                page = 1
                if command.isdigit():
                    page = int(command)
                if page <= 0:
                    page = 1
                queue = self.queue[text_channel.guild.id]
                if queue:
                    msg = f"Page {page}/{(len(queue) - 1) // 10 + 1}\n"
                    for i in range((page - 1) * 10, min(page * 10, len(queue))):
                        msg += f"{i + 1}. {queue[i][len(MUSIC_FOLDER):]}\n\n"
                    await text_channel.send(msg)
                else:
                    await text_channel.send("Queue is empty")

    def play_next_song_in_queue(self, error=None):
        if error is not None:
            raise error
            return
        
        for v in self.voice_clients:
            if not v.is_playing():
                queue = self.queue.get(v.guild.id, None)
                if queue:
                    path = queue.popleft()
                    self.play(v, path)
    
    def play(self, voice_client, path):
        audio = discord.FFmpegPCMAudio(path)
        voice_client.play(audio, after=self.play_next_song_in_queue)
        self.now_playing[voice_client.guild.id] = path[len(MUSIC_FOLDER):]

    async def get_voice_client(self, voice_channel):
        voice_client = self.voice_client.get(voice_channel.guild.id, None)
        if voice_client is None or not voice_client.is_connected():
            voice_client = (await voice_channel.connect())
            self.voice_client[voice_channel.guild.id] = voice_client

        if voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)

        return voice_client

intents = discord.Intents.default()
intents.message_content = True

token = ""
with open("token.txt", "r") as token_file:
    token = token_file.read().strip()

client = MyClient(intents=intents)
client.run(token)

