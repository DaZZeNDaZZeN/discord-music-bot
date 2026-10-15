import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import json
import os
import random
import subprocess
import time
from urllib.parse import urlparse
import discord
import yt_dlp

MUSIC_FOLDER = "/home/ren/Music/"
URL_MAP_FILENAME = "url_map.json"
VOLUME_MAP_FILENAME = "volumes.json"
LANG_MAP_FILENAME = "languages.json"
MAX_MUSIC_FOLDER_BYTES = 20 * 1024 * 1024 * 1024  # 20 GB
AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".opus", ".aac"}
PLAYLIST_EXTENSION = ".playlist"
SEARCHABLE_EXTENSIONS = AUDIO_EXTENSIONS | {PLAYLIST_EXTENSION}
PREFETCH_URL_COUNT = 5

YDL_OPTS = {
    "outtmpl": "%(uploader)s - %(title)s[%(id)s].%(ext)s",
    "paths": {"home": MUSIC_FOLDER},
    "format": "bestaudio/best",
    "keepvideo": False,
    "default_search": "ytsearch",
    "noplaylist": True,
    "playlist_items": "1",
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "opus",
        }
    ],
}

YDL_FLAT_PLAYLIST_OPTS = {
    "extract_flat": "in_playlist",
    "skip_download": True,
    "quiet": True,
    "no_warnings": True,
}

COMMAND_HELP = {
    "en": {
        "play": {
            "usage": "%play <name | url>",
            "short": "Plays a song or playlist by local match, YouTube search, or URL.",
            "long": (
                "Searches the local music folder for `<name>`. If exactly 1 match is found (an audio file or `.playlist`), "
                "it is added to the queue. If 0 or multiple local matches are found, the bot searches YouTube, downloads "
                "the audio in `.opus` format, and adds it to the queue. If a direct song URL is provided, it downloads and "
                "queues the song. If a playlist URL is provided, all songs are queued while keeping the 5 leftmost URLs "
                "downloaded in the background.\n\n"
                "**Examples:**\n"
                "```%play Cool guy```\n"
                "```%play Theta.wav```\n"
                "```%play https://www.youtube.com/watch?v=dQw4w9WgXcQ```\n"
                "```%play https://www.youtube.com/playlist?list=PL123456789```"
            ),
        },
        "playnext": {
            "usage": "%playnext <name | url>",
            "short": "Same as %play, but inserts the song or playlist at the beginning of the queue.",
            "long": (
                "Works identically to `%play` (supporting local files, `.playlist` files, YouTube searches, song URLs, "
                "and playlist URLs), but places the matched song(s) at the very front of the queue so they play next.\n\n"
                "**Examples:**\n"
                "```%playnext Cool guy```\n"
                "```%playnext https://www.youtube.com/watch?v=dQw4w9WgXcQ```"
            ),
        },
        "find": {
            "usage": "%find <pattern> [--page n]",
            "short": "Searches the music folder for matching audio and .playlist files.",
            "long": (
                "Searches the local music directory for audio files (`.opus`, `.mp3`, `.wav`, `.flac`, `.ogg`, `.m4a`, `.aac`) "
                "and `.playlist` files matching `<pattern>`. All words in `<pattern>` are matched case-insensitively against "
                "relative paths, or if `<pattern>` is an exact relative file path, only that file is matched. Supports pagination "
                "with `--page n`.\n\n"
                "**Examples:**\n"
                "```%find Cool guy```\n"
                "```%find Cool guy --page 2```\n"
                "```%find subdir/song.opus```"
            ),
        },
        "skip": {
            "usage": "%skip [amount | all]",
            "short": "Skips the specified number of songs, or clears the queue and stops if 'all'.",
            "long": (
                "Skips the currently playing song and advances through the queue. If `[amount]` is omitted, skips 1 song. "
                "If `[amount]` is greater than the number of songs in the queue or is `all`, clears the entire queue and stops playback.\n\n"
                "**Examples:**\n"
                "```%skip```\n"
                "```%skip 3```\n"
                "```%skip all```"
            ),
        },
        "queue": {
            "usage": "%queue [page_number | clear]",
            "short": "Displays paginated queue contents or clears the queue.",
            "long": (
                "Shows the current song queue (10 songs per page) along with a copyable command for the next page if more "
                "pages exist. Pass `clear` to empty the queue.\n\n"
                "**Examples:**\n"
                "```%queue```\n"
                "```%queue 2```\n"
                "```%queue clear```"
            ),
        },
        "shuffle": {
            "usage": "%shuffle",
            "short": "Shuffles the current queue in random order.",
            "long": (
                "Randomly reorders all songs currently waiting in the queue and updates background prefetching for the "
                "new 5 leftmost URLs.\n\n"
                "**Examples:**\n"
                "```%shuffle```"
            ),
        },
        "leave": {
            "usage": "%leave",
            "short": "Stops playback and leaves the voice channel.",
            "long": (
                "Stops any currently playing audio and disconnects the bot from the voice channel.\n\n"
                "**Examples:**\n"
                "```%leave```"
            ),
        },
        "playing": {
            "usage": "%playing",
            "short": "Shows the current song title, filename, playback time, and duration.",
            "long": (
                "Displays detailed information about the currently playing (or paused) track, including song title, "
                "relative filename, current playback position, and total duration.\n\n"
                "**Examples:**\n"
                "```%playing```"
            ),
        },
        "pause": {
            "usage": "%pause",
            "short": "Pauses playback and shows the command to resume.",
            "long": (
                "Pauses the currently playing track and sends a confirmation message with the copyable `%resume` command.\n\n"
                "**Examples:**\n"
                "```%pause```"
            ),
        },
        "resume": {
            "usage": "%resume",
            "short": "Resumes paused music playback.",
            "long": (
                "Resumes playback of a paused track from the exact position where it was paused.\n\n"
                "**Examples:**\n"
                "```%resume```"
            ),
        },
        "vol": {
            "usage": "%vol <percentage> (or %volume <percentage>)",
            "short": "Sets and saves the playback volume percentage for this server.",
            "long": (
                "Adjusts the playback volume to `<percentage>%` immediately and saves it on disk for this guild so "
                "the setting persists across bot restarts. Calling `%vol` without arguments displays the current volume.\n\n"
                "**Examples:**\n"
                "```%vol 50```\n"
                "```%volume 80%```\n"
                "```%vol```"
            ),
        },
        "volume": {
            "usage": "%volume <percentage> (alias for %vol)",
            "short": "Alias for %vol — sets and saves the playback volume percentage.",
            "long": (
                "Alias for `%vol`. Adjusts the playback volume to `<percentage>%` immediately and saves it on disk for this guild.\n\n"
                "**Examples:**\n"
                "```%volume 75```\n"
                "```%vol 100```"
            ),
        },
        "save": {
            "usage": "%save <name>",
            "short": "Saves the currently playing song and queue to a .playlist file.",
            "long": (
                "Saves the currently playing song followed by all queued items into `<name>.playlist` (JSON format) "
                "inside the music folder so it can be played later with `%play <name>`.\n\n"
                "**Examples:**\n"
                "```%save favorites```\n"
                "```%save chill.playlist```"
            ),
        },
        "download": {
            "usage": "%download",
            "short": "Downloads all queued URLs and replaces them in the queue with local paths.",
            "long": (
                "Downloads every remaining URL item in the queue to `.opus` files on disk and replaces all URLs in the "
                "queue with their local file paths. Reports if an error stops the download.\n\n"
                "**Examples:**\n"
                "```%download```"
            ),
        },
        "lang": {
            "usage": "%lang <en|ru>",
            "short": "Changes the bot language in this server to English (en) or Russian (ru).",
            "long": (
                "Switches all bot responses and help descriptions in this guild between English (`en`) and Russian (`ru`), "
                "and saves the preference on disk.\n\n"
                "**Examples:**\n"
                "```%lang en```\n"
                "```%lang ru```"
            ),
        },
        "help": {
            "usage": "%help [command]",
            "short": "Lists all commands or shows detailed help and examples for a specific command.",
            "long": (
                "Without arguments, lists all available bot commands with a short description. When given a command name, "
                "displays its full explanation and usage examples.\n\n"
                "**Examples:**\n"
                "```%help```\n"
                "```%help play```\n"
                "```%help %find```"
            ),
        },
    },
    "ru": {
        "play": {
            "usage": "%play <название | ссылка>",
            "short": "Воспроизводит песню или плейлист из локальной папки, поиска YouTube или по ссылке.",
            "long": (
                "Ищет `<название>` в локальной папке с музыкой. Если найдено ровно 1 совпадение (аудиофайл или `.playlist`), "
                "добавляет его в очередь. Если совпадений нет или их несколько — ищет песню на YouTube, скачивает в формате `.opus` "
                "и добавляет в очередь. Если передана ссылка на песню — скачивает её; если ссылка на плейлист — добавляет все песни "
                "в очередь и держит 5 первых ссылок в очереди предзагруженными в фоне.\n\n"
                "**Примеры:**\n"
                "```%play Cool guy```\n"
                "```%play Theta.wav```\n"
                "```%play https://www.youtube.com/watch?v=dQw4w9WgXcQ```\n"
                "```%play https://www.youtube.com/playlist?list=PL123456789```"
            ),
        },
        "playnext": {
            "usage": "%playnext <название | ссылка>",
            "short": "То же, что %play, но добавляет песню или плейлист в начало очереди.",
            "long": (
                "Работает аналогично `%play` (поддерживает локальные файлы, `.playlist`, поиск на YouTube, ссылки на треки и плейлисты), "
                "но помещает песни в самое начало очереди.\n\n"
                "**Примеры:**\n"
                "```%playnext Cool guy```\n"
                "```%playnext https://www.youtube.com/watch?v=dQw4w9WgXcQ```"
            ),
        },
        "find": {
            "usage": "%find <шаблон> [--page n]",
            "short": "Ищет аудиофайлы и файлы .playlist в музыкальной папке по шаблону.",
            "long": (
                "Ищет в папке с музыкой аудиофайлы (`.opus`, `.mp3`, `.wav`, `.flac`, `.ogg`, `.m4a`, `.aac`) и плейлисты `.playlist`, "
                "соответствующие `<шаблону>`. Все слова шаблона ищутся без учёта регистра в относительном пути файла; если указан "
                "точный относительный путь — возвращается только этот файл. Поддерживает пагинацию через `--page n`.\n\n"
                "**Примеры:**\n"
                "```%find Cool guy```\n"
                "```%find Cool guy --page 2```\n"
                "```%find subdir/song.opus```"
            ),
        },
        "skip": {
            "usage": "%skip [количество | all]",
            "short": "Пропускает указанное число песен или очищает очередь и останавливает музыку при 'all'.",
            "long": (
                "Пропускает текущую песню и переходит к следующей в очереди. Если аргумент не указан — пропускает 1 песню. "
                "Если число больше длины очереди или указано `all` — очищает очередь и останавливает воспроизведение.\n\n"
                "**Примеры:**\n"
                "```%skip```\n"
                "```%skip 3```\n"
                "```%skip all```"
            ),
        },
        "queue": {
            "usage": "%queue [номер_страницы | clear]",
            "short": "Показывает содержимое очереди по страницам или очищает очередь.",
            "long": (
                "Отображает текущую очередь воспроизведения (по 10 песен на странице) и команду для перехода на следующую страницу. "
                "Аргумент `clear` полностью очищает очередь.\n\n"
                "**Примеры:**\n"
                "```%queue```\n"
                "```%queue 2```\n"
                "```%queue clear```"
            ),
        },
        "shuffle": {
            "usage": "%shuffle",
            "short": "Перемешивает очередь в случайном порядке.",
            "long": (
                "Перемешивает все песни в очереди в случайном порядке и обновляет фоновую предзагрузку для первых 5 ссылок.\n\n"
                "**Примеры:**\n"
                "```%shuffle```"
            ),
        },
        "leave": {
            "usage": "%leave",
            "short": "Останавливает воспроизведение и выходит из голосового канала.",
            "long": (
                "Останавливает текущую музыку и отключает бота от голосового канала.\n\n"
                "**Примеры:**\n"
                "```%leave```"
            ),
        },
        "playing": {
            "usage": "%playing",
            "short": "Показывает информацию о текущей песне, файле, времени и длительности.",
            "long": (
                "Показывает подробную информацию о текущем треке: название, имя файла, текущее время воспроизведения и общую длительность.\n\n"
                "**Примеры:**\n"
                "```%playing```"
            ),
        },
        "pause": {
            "usage": "%pause",
            "short": "Ставит музыку на паузу и выводит команду для продолжения.",
            "long": (
                "Приостанавливает воспроизведение текущего трека и отправляет сообщение с командой `%resume`.\n\n"
                "**Примеры:**\n"
                "```%pause```"
            ),
        },
        "resume": {
            "usage": "%resume",
            "short": "Возобновляет воспроизведение музыки после паузы.",
            "long": (
                "Продолжает воспроизведение приостановленного трека с того же места.\n\n"
                "**Примеры:**\n"
                "```%resume```"
            ),
        },
        "vol": {
            "usage": "%vol <процент> (или %volume <процент>)",
            "short": "Изменяет и сохраняет громкость музыки на сервере в процентах.",
            "long": (
                "Устанавливает громкость воспроизведения на `<процент>%` и сохраняет её на диске для текущего сервера, "
                "чтобы настройка сохранялась после перезапуска бота.\n\n"
                "**Примеры:**\n"
                "```%vol 50```\n"
                "```%volume 80%```\n"
                "```%vol```"
            ),
        },
        "volume": {
            "usage": "%volume <процент> (синоним %vol)",
            "short": "Синоним %vol — изменяет и сохраняет громкость музыки в процентах.",
            "long": (
                "Синоним команды `%vol`. Изменяет громкость музыки на `<процент>%` и сохраняет настройку на диске.\n\n"
                "**Примеры:**\n"
                "```%volume 75```\n"
                "```%vol 100```"
            ),
        },
        "save": {
            "usage": "%save <название>",
            "short": "Сохраняет текущую песню и очередь в файл плейлиста .playlist.",
            "long": (
                "Сохраняет текущую воспроизводимую песню и всю очередь в JSON-файл `<название>.playlist` в музыкальной папке.\n\n"
                "**Примеры:**\n"
                "```%save favorites```\n"
                "```%save chill.playlist```"
            ),
        },
        "download": {
            "usage": "%download",
            "short": "Скачивает все песни из очереди и заменяет ссылки на локальные пути.",
            "long": (
                "Скачивает все оставшиеся ссылки в очереди в формат `.opus` и заменяет ссылки в очереди на локальные пути к файлам. "
                "В случае ошибки сообщает, сколько песен удалось скачать и на какой произошла ошибка.\n\n"
                "**Примеры:**\n"
                "```%download```"
            ),
        },
        "lang": {
            "usage": "%lang <en|ru>",
            "short": "Меняет язык бота на сервере на английский (en) или русский (ru).",
            "long": (
                "Переключает язык сообщений и справки бота на сервере между английским (`en`) и русским (`ru`) с сохранением на диске.\n\n"
                "**Примеры:**\n"
                "```%lang en```\n"
                "```%lang ru```"
            ),
        },
        "help": {
            "usage": "%help [команда]",
            "short": "Показывает список всех команд или подробную справку по выбранной команде.",
            "long": (
                "Без аргументов выводит список всех команд с кратким описанием. Если указано имя команды — выводит подробное описание и примеры.\n\n"
                "**Примеры:**\n"
                "```%help```\n"
                "```%help play```\n"
                "```%help %find```"
            ),
        },
    },
}

MESSAGES = {
    "en": {
        "join_voice": "Please join a voice channel first!",
        "searching": "Searching for song",
        "no_playable": "No playable songs found.",
        "playing_single": "Playing {name}",
        "playing_multi": "Playing {name} and added {count} song(s) to queue",
        "added_single": "Added to {pos} {name}",
        "added_multi": "Added {count} songs to {pos}",
        "pos_front": "beginning of queue",
        "pos_end": "queue",
        "now_playing_none": "Now playing: {name}",
        "status_paused": "Paused",
        "status_playing": "Playing",
        "now_playing_info": (
            "Now playing: {title} ({status})\n"
            "Filename: {filename}\n"
            "Current playback time: {elapsed}\n"
            "Duration: {duration}"
        ),
        "queue_empty": "Queue is empty",
        "started_download": "Started download",
        "download_error": (
            "Download stopped due to an error after downloading {count} song(s). "
            "Failed on song #{idx} (`{song}`): {err}"
        ),
        "download_done": "Downloaded all {count} song(s) in queue and replaced URLs with local paths.",
        "find_usage": "Usage: `%find <song or artist name>` --page n to specify page",
        "dir_missing": "Directory `{folder}` does not exist on host machine.",
        "find_page_err": "Please specify a number after '--page' for example %find something --page 3",
        "find_none": "No audio files found matching `{pattern}`.",
        "find_header": "🔍 **Found {total} matching file(s) (Page {page}/{pages}):**\n\n",
        "find_more": "\n\n... and {remaining} more result(s).\n\nPlease use ```%find {pattern} --page {next_page}``` to see the rest",
        "skip_pos_err": "Please specify a positive number of songs to skip or `all`.",
        "skip_invalid": "Invalid skip amount. Usage: `%skip [amount|all]`",
        "skip_cleared": "Cleared queue and stopped playing.",
        "skipped_n": "Skipped {amount} song(s) (was playing: {current})",
        "queue_cleared": "Queue is cleared",
        "queue_usage": "Usage: `%queue [page_number]`",
        "queue_next": "Use ```%queue {next_page}``` to see the next page",
        "shuffled": "Queue shuffled.",
        "not_in_voice": "Bot is not in a voice channel.",
        "left_voice": "Left voice channel.",
        "nothing_playing": "Nothing is currently playing.",
        "paused": "Music paused. Use ```%resume``` to resume it.",
        "not_paused": "Music is not paused.",
        "resumed": "Music resumed.",
        "vol_current": "Current volume: {vol}%. Usage: `{cmd} <percentage>`",
        "vol_neg": "Volume percentage cannot be negative.",
        "vol_invalid": "Invalid percentage. Usage: `{cmd} <percentage>`",
        "vol_set": "Volume set to {vol}%.",
        "save_usage": "Usage: `%save <name>`",
        "save_empty": "Nothing is playing and queue is empty.",
        "saved_playlist": "Saved {count} song(s) to playlist `{name}`.",
        "lang_usage": "Usage: `%lang <en|ru>`",
        "lang_set": "Language set to English (`en`).",
        "help_header": "**Available Commands:**\n",
        "help_footer": "\nUse `%help <command>` for detailed info and examples.",
        "help_unknown": "Unknown command `{cmd}`. Use `%help` to see all available commands.",
    },
    "ru": {
        "join_voice": "Пожалуйста, сначала зайдите в голосовой канал!",
        "searching": "Searching for song",
        "no_playable": "Не найдено доступных для воспроизведения песен.",
        "playing_single": "Playing {name}",
        "playing_multi": "Playing {name} and added {count} song(s) to queue",
        "added_single": "Added to {pos} {name}",
        "added_multi": "Added {count} songs to {pos}",
        "pos_front": "beginning of queue",
        "pos_end": "queue",
        "now_playing_none": "Сейчас играет: {name}",
        "status_paused": "Пауза",
        "status_playing": "Играет",
        "now_playing_info": (
            "Now playing: {title} ({status})\n"
            "Filename: {filename}\n"
            "Current playback time: {elapsed}\n"
            "Duration: {duration}"
        ),
        "queue_empty": "Очередь пуста",
        "started_download": "Started download",
        "download_error": (
            "Загрузка остановлена из-за ошибки после скачивания {count} песен(и). "
            "Ошибка на песне #{idx} (`{song}`): {err}"
        ),
        "download_done": "Скачано {count} песен(и) из очереди, все ссылки заменены на локальные пути.",
        "find_usage": "Использование: `%find <название песни или исполнителя>` --page n для выбора страницы",
        "dir_missing": "Директория `{folder}` не существует на хост-машине.",
        "find_page_err": "Пожалуйста, укажите число после '--page', например %find something --page 3",
        "find_none": "Аудиофайлы по запросу `{pattern}` не найдены.",
        "find_header": "🔍 **Найдено совпадающих файлов: {total} (Страница {page}/{pages}):**\n\n",
        "find_more": "\n\n... и ещё {remaining} результат(ов).\n\nИспользуйте ```%find {pattern} --page {next_page}``` для просмотра остальных",
        "skip_pos_err": "Пожалуйста, укажите положительное число песен для пропуска или `all`.",
        "skip_invalid": "Некорректное количество. Использование: `%skip [количество|all]`",
        "skip_cleared": "Очередь очищена и воспроизведение остановлено.",
        "skipped_n": "Пропущено песен: {amount} (играло: {current})",
        "queue_cleared": "Очередь очищена",
        "queue_usage": "Использование: `%queue [номер_страницы]`",
        "queue_next": "Используйте ```%queue {next_page}``` для следующей страницы",
        "shuffled": "Очередь перемешана.",
        "not_in_voice": "Бот не находится в голосовом канале.",
        "left_voice": "Бот покинул голосовой канал.",
        "nothing_playing": "Сейчас ничего не играет.",
        "paused": "Музыка на паузе. Используйте ```%resume``` для продолжения.",
        "not_paused": "Музыка не стоит на паузе.",
        "resumed": "Воспроизведение возобновлено.",
        "vol_current": "Текущая громкость: {vol}%. Использование: `{cmd} <процент>`",
        "vol_neg": "Громкость не может быть отрицательной.",
        "vol_invalid": "Некорректный процент. Использование: `{cmd} <процент>`",
        "vol_set": "Громкость установлена на {vol}%.",
        "save_usage": "Использование: `%save <название>`",
        "save_empty": "Сейчас ничего не играет и очередь пуста.",
        "saved_playlist": "Сохранено {count} песен(и) в плейлист `{name}`.",
        "lang_usage": "Использование: `%lang <en|ru>`",
        "lang_set": "Язык бота изменён на русский (`ru`).",
        "help_header": "**Список доступных команд:**\n",
        "help_footer": "\nИспользуйте `%help <команда>` для подробного описания и примеров.",
        "help_unknown": "Неизвестная команда `{cmd}`. Используйте `%help` для просмотра всех команд.",
    },
}


def is_url(text: str) -> bool:
    text = text.strip()
    if text.startswith(("http://", "https://", "www.")):
        return True
    parsed = urlparse(text)
    return bool(parsed.scheme in ("http", "https") and parsed.netloc)


def normalize_url(text: str) -> str:
    text = text.strip()
    if text.startswith("www."):
        return "https://" + text
    return text


def format_seconds(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "Unknown"
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def probe_audio_duration(file_path: str) -> float | None:
    if not file_path or not os.path.isfile(file_path):
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def find_matching_files(pattern: str, music_folder: str | None = None) -> list[str]:
    """Search music_folder for audio files and .playlist files matching pattern.

    - If pattern is an exact local path to a file relative to music_folder,
      returns only that file.
    - Otherwise, splits pattern into whitespace-separated tokens (case-insensitive)
      and matches all files whose relative path contains every token as a substring.
    """
    if music_folder is None:
        music_folder = MUSIC_FOLDER
    if not os.path.exists(music_folder):
        return []

    raw_pattern = pattern
    stripped_pattern = pattern.strip()
    if not stripped_pattern:
        return []

    norm_candidates = {
        raw_pattern.replace("\\", "/"),
        stripped_pattern.replace("\\", "/"),
        os.path.normpath(raw_pattern).replace("\\", "/"),
        os.path.normpath(stripped_pattern).replace("\\", "/"),
    }

    all_valid_files: list[tuple[str, str]] = []
    for root, _, files in os.walk(music_folder):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in SEARCHABLE_EXTENSIONS:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, music_folder).replace("\\", "/")
                all_valid_files.append((rel_path, full_path))

    all_valid_files.sort(key=lambda item: item[0].lower())

    # 1. Exact local path match relative to music_folder
    for rel_path, full_path in all_valid_files:
        if (
            rel_path in norm_candidates
            or os.path.normpath(rel_path).replace("\\", "/") in norm_candidates
        ):
            return [full_path]

    # Case-insensitive exact relative path fallback if exact path with extension was given
    lower_candidates = {c.lower() for c in norm_candidates}
    exact_ci = [
        full_path
        for rel_path, full_path in all_valid_files
        if rel_path.lower() in lower_candidates
    ]
    if len(exact_ci) == 1:
        return exact_ci

    # 2. Multi-token case-insensitive substring matching on relative path
    tokens = stripped_pattern.lower().split()
    matches = []
    for rel_path, full_path in all_valid_files:
        rel_lower = rel_path.lower()
        if all(token in rel_lower for token in tokens):
            matches.append(full_path)

    return matches


@dataclass
class QueueItem:
    title: str
    local_path: str | None = None
    url: str | None = None
    video_id: str | None = None
    duration: float | None = None
    download_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def __post_init__(self):
        if self.local_path and os.path.exists(self.local_path):
            self.download_event.set()

    def display_name(self, music_folder: str | None = None) -> str:
        if music_folder is None:
            music_folder = MUSIC_FOLDER
        if self.local_path:
            try:
                rel = os.path.relpath(self.local_path, music_folder).replace("\\", "/")
                if not rel.startswith(".."):
                    return rel
            except ValueError:
                pass
            return os.path.basename(self.local_path)
        if self.title and self.url and self.title != self.url:
            return f"{self.title} ({self.url})"
        return self.url or self.title

    def to_playlist_entry(self, music_folder: str | None = None) -> str:
        if music_folder is None:
            music_folder = MUSIC_FOLDER
        if self.local_path:
            try:
                rel = os.path.relpath(self.local_path, music_folder).replace("\\", "/")
                if not rel.startswith(".."):
                    return rel
            except ValueError:
                pass
            return self.local_path
        return self.url or self.title


class MyClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.voice_client: dict[int, discord.VoiceClient] = {}
        self.queue: dict[int, deque[QueueItem]] = defaultdict(deque)
        self.now_playing: dict[int, str] = {}
        self.now_playing_item: dict[int, QueueItem | None] = {}
        self.playback_state: dict[int, dict[str, float | None]] = {}
        self.volume: dict[int, float] = defaultdict(lambda: 1.0)
        self.language: dict[int, str] = defaultdict(lambda: "en")
        self.url_cache: dict[str, str] = {}
        self.video_id_cache: dict[str, str] = {}
        self.inflight_downloads: dict[
            str, asyncio.Future[tuple[str, float | None, str | None]]
        ] = {}
        self.prefetch_tasks: dict[int, asyncio.Task] = {}
        self.play_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

        self.load_url_map()
        self.load_volumes()
        self.load_languages()
        self.enforce_storage_limit()

    @property
    def url_map_path(self) -> str:
        return os.path.join(MUSIC_FOLDER, URL_MAP_FILENAME)

    @property
    def volume_map_path(self) -> str:
        return os.path.join(MUSIC_FOLDER, VOLUME_MAP_FILENAME)

    @property
    def lang_map_path(self) -> str:
        return os.path.join(MUSIC_FOLDER, LANG_MAP_FILENAME)

    def t(self, guild_id: int, key: str, **kwargs) -> str:
        lang = self.language.get(guild_id, "en")
        lang_dict = MESSAGES.get(lang, MESSAGES["en"])
        template = lang_dict.get(key, MESSAGES["en"].get(key, key))
        return template.format(**kwargs) if kwargs else template

    def load_url_map(self):
        """Load persistent URL -> local file path mapping from disk."""
        path = self.url_map_path
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for url, local_path in data.items():
                    if isinstance(url, str) and isinstance(local_path, str):
                        full_path = (
                            local_path
                            if os.path.isabs(local_path)
                            else os.path.join(MUSIC_FOLDER, local_path)
                        )
                        self.url_cache[normalize_url(url)] = full_path
            elif isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict) and "url" in entry and "path" in entry:
                        url = entry["url"]
                        local_path = entry["path"]
                        full_path = (
                            local_path
                            if os.path.isabs(local_path)
                            else os.path.join(MUSIC_FOLDER, local_path)
                        )
                        self.url_cache[normalize_url(url)] = full_path
        except Exception:
            pass

    def save_url_map(self):
        """Persist URL -> local file path mapping to disk."""
        try:
            os.makedirs(MUSIC_FOLDER, exist_ok=True)
            with open(self.url_map_path, "w", encoding="utf-8") as f:
                json.dump(self.url_cache, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def update_url_mapping(self, url: str | None, local_path: str):
        if not url or not local_path:
            return
        norm = normalize_url(url)
        self.url_cache[norm] = local_path
        self.save_url_map()

    def load_volumes(self):
        """Load persistent per-guild volume settings from disk."""
        path = self.volume_map_path
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for guild_id_str, vol in data.items():
                    try:
                        self.volume[int(guild_id_str)] = float(vol)
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    def save_volumes(self):
        """Persist per-guild volume settings to disk."""
        try:
            os.makedirs(MUSIC_FOLDER, exist_ok=True)
            serializable = {
                str(guild_id): float(vol) for guild_id, vol in self.volume.items()
            }
            with open(self.volume_map_path, "w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2)
        except Exception:
            pass

    def load_languages(self):
        """Load persistent per-guild language settings from disk."""
        path = self.lang_map_path
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for guild_id_str, lang in data.items():
                    if lang in ("en", "ru"):
                        try:
                            self.language[int(guild_id_str)] = lang
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass

    def save_languages(self):
        """Persist per-guild language settings to disk."""
        try:
            os.makedirs(MUSIC_FOLDER, exist_ok=True)
            serializable = {
                str(guild_id): lang for guild_id, lang in self.language.items()
            }
            with open(self.lang_map_path, "w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2)
        except Exception:
            pass

    def get_music_folder_size_bytes(self) -> int:
        if not os.path.exists(MUSIC_FOLDER):
            return 0
        total = 0
        for root, _, files in os.walk(MUSIC_FOLDER):
            for file in files:
                full_path = os.path.join(root, file)
                try:
                    if os.path.isfile(full_path):
                        total += os.path.getsize(full_path)
                except OSError:
                    pass
        return total

    def enforce_storage_limit(self, max_bytes: int | None = None) -> list[str]:
        """If MUSIC_FOLDER exceeds 20GB (or max_bytes), delete oldest bot-downloaded songs.

        Never deletes songs that were not downloaded by the bot.
        """
        if max_bytes is None:
            max_bytes = MAX_MUSIC_FOLDER_BYTES
        if not os.path.exists(MUSIC_FOLDER):
            return []

        total_size = self.get_music_folder_size_bytes()
        if total_size <= max_bytes:
            return []

        active_playing_paths = {
            os.path.abspath(item.local_path)
            for item in self.now_playing_item.values()
            if item is not None and item.local_path
        }

        bot_downloaded_paths = {
            os.path.abspath(path)
            for path in self.url_cache.values()
            if path and os.path.isfile(path)
        }

        candidates: list[tuple[float, int, str]] = []
        for path in bot_downloaded_paths:
            if path in active_playing_paths:
                continue
            try:
                stat = os.stat(path)
                candidates.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                pass

        # Sort oldest first by modification timestamp
        candidates.sort(key=lambda entry: entry[0])

        deleted_files: list[str] = []
        for _, file_size, path in candidates:
            if total_size <= max_bytes:
                break
            try:
                os.remove(path)
                total_size -= file_size
                deleted_files.append(path)
            except OSError:
                pass

        return deleted_files

    async def on_ready(self):
        print(f"Logged on as {self.user}!")

    async def get_user_voice_channel(self, message):
        voice_channel = None
        try:
            if hasattr(message.author, "fetch_voice"):
                voice_state = await message.author.fetch_voice()
                if voice_state is not None:
                    voice_channel = voice_state.channel
            elif getattr(message.author, "voice", None) is not None:
                voice_channel = message.author.voice.channel
        except (discord.NotFound, discord.HTTPException, AttributeError):
            if getattr(message.author, "voice", None) is not None:
                voice_channel = message.author.voice.channel
        return voice_channel

    def find_existing_opus_by_id(self, video_id: str | None) -> str | None:
        if not video_id:
            return None
        cached = self.video_id_cache.get(video_id)
        if cached and os.path.isfile(cached):
            return cached
        if not os.path.exists(MUSIC_FOLDER):
            return None
        suffix = f"[{video_id}].opus"
        for root, _, files in os.walk(MUSIC_FOLDER):
            for file in files:
                if file.endswith(suffix):
                    full_path = os.path.join(root, file)
                    self.video_id_cache[video_id] = full_path
                    return full_path
        return None

    def get_cached_download(
        self, url: str | None, video_id: str | None = None
    ) -> str | None:
        """Check URL mapping for existing file on disk.

        If a mapped path no longer exists on disk, return None so the caller
        re-downloads the song from the URL and updates the on-disk mapping.
        """
        if url:
            norm = normalize_url(url)
            cached = self.url_cache.get(norm)
            if cached:
                if os.path.isfile(cached):
                    return cached
                # Mapped local file does not exist on disk anymore; will re-download and update mapping
        if video_id:
            existing = self.find_existing_opus_by_id(video_id)
            if existing and os.path.isfile(existing):
                if url:
                    self.update_url_mapping(url, existing)
                return existing
        return None

    def _download_with_ytdlp_sync(
        self, query_or_url: str
    ) -> tuple[str, float | None, str | None, str | None]:
        os.makedirs(MUSIC_FOLDER, exist_ok=True)
        YDL_OPTS["paths"] = {"home": MUSIC_FOLDER}
        if is_url(query_or_url):
            norm_url = normalize_url(query_or_url)
            cached = self.get_cached_download(norm_url)
            if cached:
                return (
                    cached,
                    probe_audio_duration(cached),
                    os.path.basename(cached),
                    None,
                )

        # First extract info without downloading to check if video_id is already on disk
        probe_opts = dict(YDL_OPTS)
        probe_opts["skip_download"] = True
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(query_or_url, download=False)
            if "entries" in info:
                entries = [e for e in info["entries"] if e]
                if not entries:
                    raise ValueError(f"No results found for {query_or_url}")
                info = entries[0]

            video_id = info.get("id")
            duration = info.get("duration")
            title = info.get("title")
            existing = self.get_cached_download(
                query_or_url if is_url(query_or_url) else info.get("webpage_url"),
                video_id,
            )
            if existing:
                if is_url(query_or_url):
                    self.update_url_mapping(query_or_url, existing)
                if info.get("webpage_url"):
                    self.update_url_mapping(info["webpage_url"], existing)
                return existing, duration, title, video_id

            prepared = ydl.prepare_filename(info)
            base, _ = os.path.splitext(prepared)
            opus_path = base + ".opus"
            if os.path.isfile(opus_path):
                if video_id:
                    self.video_id_cache[video_id] = opus_path
                if is_url(query_or_url):
                    self.update_url_mapping(query_or_url, opus_path)
                if info.get("webpage_url"):
                    self.update_url_mapping(info["webpage_url"], opus_path)
                return opus_path, duration, title, video_id

        # Actually download and convert to .opus, then update the on-disk mapping
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query_or_url, download=True)
            if "entries" in info:
                entries = [e for e in info["entries"] if e]
                info = entries[0]
            filename = ydl.prepare_filename(info)
            base, _ = os.path.splitext(filename)
            opus_path = base + ".opus"
            video_id = info.get("id")
            duration = info.get("duration")
            title = info.get("title")
            if video_id:
                self.video_id_cache[video_id] = opus_path
            if is_url(query_or_url):
                self.update_url_mapping(query_or_url, opus_path)
            if info.get("webpage_url"):
                self.update_url_mapping(info["webpage_url"], opus_path)
            self.enforce_storage_limit()
            return opus_path, duration, title, video_id

    async def ensure_item_downloaded(self, item: QueueItem) -> str:
        if item.local_path and os.path.isfile(item.local_path):
            if item.url:
                self.update_url_mapping(item.url, item.local_path)
            item.download_event.set()
            return item.local_path

        # If local_path was set or mapped in url_cache, verify whether it still exists on disk
        cached = self.get_cached_download(item.url, item.video_id)
        if cached:
            item.local_path = cached
            if item.duration is None:
                item.duration = probe_audio_duration(cached)
            item.download_event.set()
            return cached

        # Reset missing local_path so it gets downloaded from URL and updates mapping
        item.local_path = None
        target = item.url or item.title
        dedup_key = normalize_url(item.url) if item.url else f"query:{target}"
        if item.video_id:
            dedup_key = f"id:{item.video_id}"

        if dedup_key in self.inflight_downloads:
            opus_path, duration, title = await self.inflight_downloads[dedup_key]
            item.local_path = opus_path
            if item.url:
                self.update_url_mapping(item.url, opus_path)
            if duration is not None:
                item.duration = duration
            if title and (not item.title or item.title == item.url):
                item.title = title
            item.download_event.set()
            return opus_path

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[str, float | None, str | None]] = loop.create_future()
        fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self.inflight_downloads[dedup_key] = fut
        try:
            opus_path, duration, title, video_id = await asyncio.to_thread(
                self._download_with_ytdlp_sync, target
            )
            if video_id and not item.video_id:
                item.video_id = video_id
            item.local_path = opus_path
            if item.url:
                self.update_url_mapping(item.url, opus_path)
            if duration is not None:
                item.duration = duration
            if title and (not item.title or item.title == item.url):
                item.title = title
            item.download_event.set()
            fut.set_result((opus_path, duration, title))
            return opus_path
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self.inflight_downloads.pop(dedup_key, None)

    def trigger_background_downloads(self, guild_id: int):
        existing_task = self.prefetch_tasks.get(guild_id)
        if existing_task and not existing_task.done():
            existing_task.cancel()
        try:
            loop = asyncio.get_running_loop()
            self.prefetch_tasks[guild_id] = loop.create_task(
                self._prefetch_leftmost_urls(guild_id)
            )
        except RuntimeError:
            pass

    async def _prefetch_leftmost_urls(self, guild_id: int):
        """Keep the 5 leftmost URL items in the guild's queue downloaded at all times."""
        try:
            while True:
                queue = self.queue.get(guild_id)
                if not queue:
                    return

                # Find the 5 leftmost URL items in the queue
                leftmost_url_items: list[QueueItem] = []
                for item in list(queue):
                    if item.url is not None:
                        leftmost_url_items.append(item)
                        if len(leftmost_url_items) >= PREFETCH_URL_COUNT:
                            break

                # Find the first among those 5 that isn't downloaded yet (or whose file is missing)
                next_to_download: QueueItem | None = None
                for item in leftmost_url_items:
                    cached = self.get_cached_download(item.url, item.video_id)
                    if cached:
                        item.local_path = cached
                        item.download_event.set()
                    elif not item.local_path or not os.path.isfile(item.local_path):
                        next_to_download = item
                        break

                if next_to_download is None:
                    return

                try:
                    await self.ensure_item_downloaded(next_to_download)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Avoid infinite retry loop if a URL fails to download
                    if next_to_download in queue:
                        queue.remove(next_to_download)
        except asyncio.CancelledError:
            return

    def _extract_url_entries_sync(self, url: str) -> list[dict]:
        norm_url = normalize_url(url)
        with yt_dlp.YoutubeDL(YDL_FLAT_PLAYLIST_OPTS) as ydl:
            info = ydl.extract_info(norm_url, download=False)
            if info is None:
                return []
            if "entries" in info or info.get("_type") in ("playlist", "multi_video"):
                raw_entries = [e for e in (info.get("entries") or []) if e]
                results = []
                for entry in raw_entries:
                    vid_id = entry.get("id")
                    entry_url = entry.get("url") or entry.get("webpage_url")
                    if entry_url and not is_url(entry_url) and vid_id:
                        entry_url = f"https://www.youtube.com/watch?v={vid_id}"
                    elif not entry_url and vid_id:
                        entry_url = f"https://www.youtube.com/watch?v={vid_id}"
                    results.append(
                        {
                            "url": normalize_url(entry_url) if entry_url else norm_url,
                            "id": vid_id,
                            "title": entry.get("title") or entry_url or norm_url,
                            "duration": entry.get("duration"),
                        }
                    )
                return results
            else:
                vid_id = info.get("id")
                webpage_url = info.get("webpage_url") or norm_url
                return [
                    {
                        "url": normalize_url(webpage_url),
                        "id": vid_id,
                        "title": info.get("title") or norm_url,
                        "duration": info.get("duration"),
                    }
                ]

    def load_playlist_file(self, playlist_path: str) -> list[QueueItem]:
        with open(playlist_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            entries = data.get("songs", data.get("items", []))
        elif isinstance(data, list):
            entries = data
        else:
            entries = []

        items: list[QueueItem] = []
        for entry in entries:
            if isinstance(entry, dict):
                entry_path = entry.get("local_path") or entry.get("path")
                entry_url = entry.get("url")
                title = entry.get("title") or entry_path or entry_url or "Unknown"
                video_id = entry.get("video_id")
                duration = entry.get("duration")
                if entry_path:
                    full_path = (
                        entry_path
                        if os.path.isabs(entry_path)
                        else os.path.join(MUSIC_FOLDER, entry_path)
                    )
                    items.append(
                        QueueItem(
                            title=title,
                            local_path=full_path,
                            url=entry_url,
                            video_id=video_id,
                            duration=duration,
                        )
                    )
                elif entry_url:
                    cached = self.get_cached_download(entry_url, video_id)
                    items.append(
                        QueueItem(
                            title=title,
                            local_path=cached,
                            url=normalize_url(entry_url),
                            video_id=video_id,
                            duration=duration,
                        )
                    )
            elif isinstance(entry, str):
                entry_str = entry.strip()
                if not entry_str:
                    continue
                if is_url(entry_str):
                    norm = normalize_url(entry_str)
                    cached = self.get_cached_download(norm)
                    items.append(QueueItem(title=norm, local_path=cached, url=norm))
                else:
                    full_path = (
                        entry_str
                        if os.path.isabs(entry_str)
                        else os.path.join(MUSIC_FOLDER, entry_str)
                    )
                    rel_title = os.path.relpath(full_path, MUSIC_FOLDER).replace(
                        "\\", "/"
                    )
                    items.append(QueueItem(title=rel_title, local_path=full_path))
        return items

    def enqueue_items(
        self, guild_id: int, items: list[QueueItem], play_next: bool = False
    ):
        queue = self.queue[guild_id]
        if play_next:
            for item in reversed(items):
                queue.appendleft(item)
        else:
            queue.extend(items)

    def is_guild_active(self, voice_client: discord.VoiceClient | None) -> bool:
        if voice_client is None:
            return False
        is_playing = getattr(voice_client, "is_playing", lambda: False)()
        is_paused = getattr(voice_client, "is_paused", lambda: False)()
        return bool(is_playing or is_paused)

    async def _send_or_edit(self, text_channel, status_msg, content: str):
        if status_msg is not None and hasattr(status_msg, "edit"):
            await status_msg.edit(content=content)
            return status_msg
        return await text_channel.send(content)

    async def handle_play_command(self, message, query: str, play_next: bool = False):
        text_channel = message.channel
        guild_id = text_channel.guild.id
        cmd_name = "%playnext" if play_next else "%play"

        if not query:
            await text_channel.send(
                f"""Usage:

play by name ```{cmd_name} name_of_the_song```

play by url(youtube) ```{cmd_name} https://youtube.com/```

if name of the song is ended with '.opus' '.mp3' or similar, it is treated as a path to a file on host pc
for example ```{cmd_name} Theta.wav``` will play file Theta.wav from host pc

```%find name``` finds all local files that match name"""
            )
            return

        voice_channel = await self.get_user_voice_channel(message)
        if voice_channel is None:
            await text_channel.send(self.t(guild_id, "join_voice"))
            return

        voice_client = await self.get_voice_client(voice_channel)
        items_to_add: list[QueueItem] = []
        status_msg = None

        if is_url(query):
            norm_url = normalize_url(query)
            cached = self.get_cached_download(norm_url)
            if cached:
                rel_name = os.path.relpath(cached, MUSIC_FOLDER).replace("\\", "/")
                items_to_add.append(
                    QueueItem(title=rel_name, local_path=cached, url=norm_url)
                )
            else:
                status_msg = await text_channel.send(self.t(guild_id, "searching"))
                entries = await asyncio.to_thread(
                    self._extract_url_entries_sync, norm_url
                )
                if len(entries) > 1:
                    # Playlist URL: add all individual song URLs to queue
                    for entry in entries:
                        entry_url = entry["url"]
                        vid_id = entry.get("id")
                        existing = self.get_cached_download(entry_url, vid_id)
                        items_to_add.append(
                            QueueItem(
                                title=entry.get("title") or entry_url,
                                local_path=existing,
                                url=entry_url,
                                video_id=vid_id,
                                duration=entry.get("duration"),
                            )
                        )
                else:
                    # Single song URL: download as .opus (or reuse if already downloaded)
                    entry = (
                        entries[0]
                        if entries
                        else {
                            "url": norm_url,
                            "id": None,
                            "title": norm_url,
                            "duration": None,
                        }
                    )
                    item = QueueItem(
                        title=entry.get("title") or norm_url,
                        url=entry.get("url") or norm_url,
                        video_id=entry.get("id"),
                        duration=entry.get("duration"),
                    )
                    await self.ensure_item_downloaded(item)
                    items_to_add.append(item)
        else:
            # Search local music directory using %find matching rules
            matches = find_matching_files(query, MUSIC_FOLDER)
            if len(matches) == 1:
                match_path = matches[0]
                if match_path.lower().endswith(PLAYLIST_EXTENSION):
                    items_to_add.extend(self.load_playlist_file(match_path))
                else:
                    rel_name = os.path.relpath(match_path, MUSIC_FOLDER).replace(
                        "\\", "/"
                    )
                    items_to_add.append(
                        QueueItem(title=rel_name, local_path=match_path)
                    )
            else:
                # 0 or multiple local matches: search on YouTube and download as .opus
                status_msg = await text_channel.send(self.t(guild_id, "searching"))
                item = QueueItem(title=query)
                await self.ensure_item_downloaded(item)
                items_to_add.append(item)

        if not items_to_add:
            await self._send_or_edit(
                text_channel, status_msg, self.t(guild_id, "no_playable")
            )
            return

        self.enqueue_items(guild_id, items_to_add, play_next=play_next)

        if not self.is_guild_active(voice_client):
            # Start first song as fast as possible, then download remaining 5 leftmost URLs in background
            first_item = self.queue[guild_id].popleft()
            self.trigger_background_downloads(guild_id)
            if not first_item.local_path or not os.path.isfile(first_item.local_path):
                if status_msg is None:
                    status_msg = await text_channel.send(self.t(guild_id, "searching"))
                await self.ensure_item_downloaded(first_item)
            self.play(voice_client, first_item)
            if len(items_to_add) == 1:
                final_text = self.t(
                    guild_id, "playing_single", name=first_item.display_name()
                )
            else:
                final_text = self.t(
                    guild_id,
                    "playing_multi",
                    name=first_item.display_name(),
                    count=len(items_to_add) - 1,
                )
            await self._send_or_edit(text_channel, status_msg, final_text)
        else:
            self.trigger_background_downloads(guild_id)
            position_label = self.t(guild_id, "pos_front" if play_next else "pos_end")
            if len(items_to_add) == 1:
                final_text = self.t(
                    guild_id,
                    "added_single",
                    pos=position_label,
                    name=items_to_add[0].display_name(),
                )
            else:
                final_text = self.t(
                    guild_id,
                    "added_multi",
                    count=len(items_to_add),
                    pos=position_label,
                )
            await self._send_or_edit(text_channel, status_msg, final_text)

    def get_current_playback_seconds(self, guild_id: int) -> float:
        state = self.playback_state.get(guild_id)
        if not state or state.get("start_time") is None:
            return 0.0
        start_time = state["start_time"]
        paused_total = state.get("paused_total") or 0.0
        pause_start = state.get("pause_start")
        now = pause_start if pause_start is not None else time.monotonic()
        elapsed = max(0.0, now - start_time - paused_total)
        duration = state.get("duration")
        if duration is not None and duration > 0:
            elapsed = min(elapsed, duration)
        return elapsed

    async def on_message(self, message):
        if message.author == self.user:
            return

        text_channel = message.channel
        guild_id = text_channel.guild.id
        q = message.content.strip()
        if not q.startswith("%"):
            return

        parts = q.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "%help":
            lang = self.language.get(guild_id, "en")
            help_table = COMMAND_HELP.get(lang, COMMAND_HELP["en"])
            target = arg.strip().lstrip("%").lower()
            if not target:
                lines = [self.t(guild_id, "help_header")]
                for cmd_key, info in help_table.items():
                    if cmd_key == "volume":
                        continue
                    lines.append(f"`{info['usage']}` — {info['short']}")
                lines.append(self.t(guild_id, "help_footer"))
                await text_channel.send("\n".join(lines))
                return

            if target not in help_table:
                await text_channel.send(self.t(guild_id, "help_unknown", cmd=target))
                return

            info = help_table[target]
            await text_channel.send(f"**`{info['usage']}`**\n\n{info['long']}")
            return

        if cmd == "%lang":
            target_lang = arg.strip().lower()
            if target_lang not in ("en", "ru"):
                await text_channel.send(self.t(guild_id, "lang_usage"))
                return
            self.language[guild_id] = target_lang
            self.save_languages()
            await text_channel.send(self.t(guild_id, "lang_set"))
            return

        if cmd == "%playing":
            current_item = self.now_playing_item.get(guild_id)
            voice_client = self.voice_client.get(guild_id)
            if current_item is None or not self.is_guild_active(voice_client):
                await text_channel.send(
                    self.t(
                        guild_id,
                        "now_playing_none",
                        name=self.now_playing.get(guild_id, "None"),
                    )
                )
                return

            filename = current_item.display_name()
            duration = current_item.duration
            if duration is None and current_item.local_path:
                duration = probe_audio_duration(current_item.local_path)
                current_item.duration = duration
                if guild_id in self.playback_state:
                    self.playback_state[guild_id]["duration"] = duration

            elapsed = self.get_current_playback_seconds(guild_id)
            song_title = os.path.splitext(os.path.basename(filename))[0]
            status_str = (
                self.t(guild_id, "status_paused")
                if getattr(voice_client, "is_paused", lambda: False)()
                else self.t(guild_id, "status_playing")
            )
            await text_channel.send(
                self.t(
                    guild_id,
                    "now_playing_info",
                    title=song_title,
                    status=status_str,
                    filename=filename,
                    elapsed=format_seconds(elapsed),
                    duration=format_seconds(duration),
                )
            )
            return

        if cmd == "%play":
            await self.handle_play_command(message, arg.strip(), play_next=False)
            return

        if cmd == "%playnext":
            await self.handle_play_command(message, arg.strip(), play_next=True)
            return

        if cmd == "%download":
            queue = self.queue[guild_id]
            if not queue:
                await text_channel.send(self.t(guild_id, "queue_empty"))
                return

            await text_channel.send(self.t(guild_id, "started_download"))

            existing_task = self.prefetch_tasks.get(guild_id)
            if existing_task and not existing_task.done():
                existing_task.cancel()

            downloaded_count = 0
            for idx, item in enumerate(list(queue), start=1):
                if isinstance(item, QueueItem):
                    song_label = item.display_name()
                    try:
                        local_path = await self.ensure_item_downloaded(item)
                        rel_title = os.path.relpath(local_path, MUSIC_FOLDER).replace(
                            "\\", "/"
                        )
                        item.local_path = local_path
                        item.title = rel_title
                        item.url = None
                        downloaded_count += 1
                    except Exception as exc:
                        await text_channel.send(
                            self.t(
                                guild_id,
                                "download_error",
                                count=downloaded_count,
                                idx=idx,
                                song=song_label,
                                err=exc,
                            )
                        )
                        return

            await text_channel.send(
                self.t(guild_id, "download_done", count=downloaded_count)
            )
            return

        if cmd == "%find":
            raw_arg = q[len("%find") :].strip()
            if not raw_arg:
                await text_channel.send(self.t(guild_id, "find_usage"))
                return

            if not os.path.exists(MUSIC_FOLDER):
                await text_channel.send(
                    self.t(guild_id, "dir_missing", folder=MUSIC_FOLDER)
                )
                return

            page = 1
            pattern = raw_arg
            if "--page" in raw_arg:
                split_parts = raw_arg.rsplit("--page", 1)
                pattern = split_parts[0].strip()
                page_str = split_parts[1].strip()
                try:
                    page = int(page_str)
                    if page < 1:
                        page = 1
                except ValueError:
                    await text_channel.send(self.t(guild_id, "find_page_err"))
                    return

            if not pattern:
                await text_channel.send(self.t(guild_id, "find_usage"))
                return

            matched_files = find_matching_files(pattern, MUSIC_FOLDER)
            if not matched_files:
                await text_channel.send(self.t(guild_id, "find_none", pattern=pattern))
                return

            MAX_DISPLAY = 15
            total_pages = (len(matched_files) - 1) // MAX_DISPLAY + 1
            start_idx = MAX_DISPLAY * (page - 1)
            end_idx = MAX_DISPLAY * page
            displayed_files = matched_files[start_idx:end_idx]

            lines = []
            for idx, full_path in enumerate(displayed_files, start=start_idx + 1):
                rel_path = os.path.relpath(full_path, MUSIC_FOLDER).replace("\\", "/")
                lines.append(f"{idx}. ```{rel_path}```")

            output = self.t(
                guild_id,
                "find_header",
                total=len(matched_files),
                page=page,
                pages=total_pages,
            )
            output += "\n\n".join(lines)

            if len(matched_files) > end_idx:
                remaining = len(matched_files) - end_idx
                output += self.t(
                    guild_id,
                    "find_more",
                    remaining=remaining,
                    pattern=pattern,
                    next_page=page + 1,
                )

            await text_channel.send(output)
            return

        if cmd == "%skip":
            voice_channel = await self.get_user_voice_channel(message)
            voice_client = self.voice_client.get(guild_id)
            if voice_client is None and voice_channel is not None:
                voice_client = await self.get_voice_client(voice_channel)

            queue = self.queue[guild_id]
            skip_arg = arg.strip().lower()

            if skip_arg == "":
                amount = 1
            elif skip_arg == "all":
                amount = None
            else:
                try:
                    amount = int(skip_arg)
                    if amount <= 0:
                        await text_channel.send(self.t(guild_id, "skip_pos_err"))
                        return
                except ValueError:
                    await text_channel.send(self.t(guild_id, "skip_invalid"))
                    return

            current_name = self.now_playing.get(guild_id, "None")

            if amount is None or amount > len(queue):
                queue.clear()
                self.trigger_background_downloads(guild_id)
                self.now_playing[guild_id] = "None"
                self.now_playing_item[guild_id] = None
                self.playback_state.pop(guild_id, None)
                if voice_client and self.is_guild_active(voice_client):
                    voice_client.stop()
                await text_channel.send(self.t(guild_id, "skip_cleared"))
                return

            # Skip (amount - 1) queued songs so the amount-th song in queue plays next
            for _ in range(amount - 1):
                if queue:
                    queue.popleft()

            self.trigger_background_downloads(guild_id)
            if voice_client and self.is_guild_active(voice_client):
                voice_client.stop()
            await self._advance_queue_async(guild_id)
            await text_channel.send(
                self.t(guild_id, "skipped_n", amount=amount, current=current_name)
            )
            return

        if cmd == "%queue":
            raw_arg = arg.strip()
            if raw_arg.lower() == "clear":
                self.queue[guild_id].clear()
                self.trigger_background_downloads(guild_id)
                await text_channel.send(self.t(guild_id, "queue_cleared"))
                return

            if raw_arg.startswith("--page"):
                raw_arg = raw_arg.removeprefix("--page").strip()

            page = 1
            if raw_arg:
                if raw_arg.isdigit():
                    page = int(raw_arg)
                else:
                    await text_channel.send(self.t(guild_id, "queue_usage"))
                    return
            if page <= 0:
                page = 1

            queue = self.queue[guild_id]
            if not queue:
                await text_channel.send(self.t(guild_id, "queue_empty"))
                return

            per_page = 10
            total_pages = (len(queue) - 1) // per_page + 1
            start_idx = (page - 1) * per_page
            end_idx = min(page * per_page, len(queue))
            queue_list = list(queue)

            msg = f"Page {page}/{total_pages}\n"
            for i in range(start_idx, end_idx):
                item = queue_list[i]
                name = item.display_name() if isinstance(item, QueueItem) else str(item)
                msg += f"{i + 1}. {name}\n\n"

            if page < total_pages:
                msg += self.t(guild_id, "queue_next", next_page=page + 1)

            await text_channel.send(msg)
            return

        if cmd == "%shuffle":
            queue = self.queue[guild_id]
            if not queue:
                await text_channel.send(self.t(guild_id, "queue_empty"))
                return
            items = list(queue)
            random.shuffle(items)
            queue.clear()
            queue.extend(items)
            self.trigger_background_downloads(guild_id)
            await text_channel.send(self.t(guild_id, "shuffled"))
            return

        if cmd == "%leave":
            voice_client = self.voice_client.get(guild_id)
            if voice_client is None or not voice_client.is_connected():
                await text_channel.send(self.t(guild_id, "not_in_voice"))
                return
            if self.is_guild_active(voice_client):
                voice_client.stop()
            await voice_client.disconnect()
            self.voice_client.pop(guild_id, None)
            self.now_playing[guild_id] = "None"
            self.now_playing_item[guild_id] = None
            self.playback_state.pop(guild_id, None)
            await text_channel.send(self.t(guild_id, "left_voice"))
            return

        if cmd == "%pause":
            voice_client = self.voice_client.get(guild_id)
            if (
                voice_client is None
                or not getattr(voice_client, "is_playing", lambda: False)()
            ):
                await text_channel.send(self.t(guild_id, "nothing_playing"))
                return
            voice_client.pause()
            state = self.playback_state.get(guild_id)
            if state is not None and state.get("pause_start") is None:
                state["pause_start"] = time.monotonic()
            await text_channel.send(self.t(guild_id, "paused"))
            return

        if cmd == "%resume":
            voice_client = self.voice_client.get(guild_id)
            if (
                voice_client is None
                or not getattr(voice_client, "is_paused", lambda: False)()
            ):
                await text_channel.send(self.t(guild_id, "not_paused"))
                return
            voice_client.resume()
            state = self.playback_state.get(guild_id)
            if state is not None and state.get("pause_start") is not None:
                state["paused_total"] = (state.get("paused_total") or 0.0) + (
                    time.monotonic() - state["pause_start"]
                )
                state["pause_start"] = None
            await text_channel.send(self.t(guild_id, "resumed"))
            return

        if cmd in ("%vol", "%volume"):
            vol_str = arg.strip().removesuffix("%").strip()
            if not vol_str:
                current_vol = int(round(self.volume[guild_id] * 100))
                await text_channel.send(
                    self.t(guild_id, "vol_current", vol=current_vol, cmd=cmd)
                )
                return
            try:
                percentage = float(vol_str)
                if percentage < 0:
                    await text_channel.send(self.t(guild_id, "vol_neg"))
                    return
            except ValueError:
                await text_channel.send(self.t(guild_id, "vol_invalid", cmd=cmd))
                return

            self.volume[guild_id] = percentage / 100.0
            self.save_volumes()
            voice_client = self.voice_client.get(guild_id)
            if (
                voice_client is not None
                and getattr(voice_client, "source", None) is not None
            ):
                if hasattr(voice_client.source, "volume"):
                    voice_client.source.volume = self.volume[guild_id]

            display_pct = int(percentage) if percentage.is_integer() else percentage
            await text_channel.send(self.t(guild_id, "vol_set", vol=display_pct))
            return

        if cmd == "%save":
            playlist_name = arg.strip()
            if not playlist_name:
                await text_channel.send(self.t(guild_id, "save_usage"))
                return

            if not playlist_name.lower().endswith(PLAYLIST_EXTENSION):
                playlist_name += PLAYLIST_EXTENSION

            entries: list[str] = []
            current_item = self.now_playing_item.get(guild_id)
            voice_client = self.voice_client.get(guild_id)
            if current_item is not None and self.is_guild_active(voice_client):
                entries.append(current_item.to_playlist_entry(MUSIC_FOLDER))

            for item in self.queue[guild_id]:
                if isinstance(item, QueueItem):
                    entries.append(item.to_playlist_entry(MUSIC_FOLDER))
                else:
                    entries.append(str(item))

            if not entries:
                await text_channel.send(self.t(guild_id, "save_empty"))
                return

            save_path = os.path.join(MUSIC_FOLDER, playlist_name)
            os.makedirs(os.path.dirname(save_path) or MUSIC_FOLDER, exist_ok=True)
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)

            rel_saved = os.path.relpath(save_path, MUSIC_FOLDER).replace("\\", "/")
            await text_channel.send(
                self.t(guild_id, "saved_playlist", count=len(entries), name=rel_saved)
            )
            return

    async def _advance_queue_async(self, guild_id: int):
        async with self.play_locks[guild_id]:
            voice_client = self.voice_client.get(guild_id)
            if voice_client is None or not voice_client.is_connected():
                return
            if self.is_guild_active(voice_client):
                return
            queue = self.queue.get(guild_id)
            if not queue:
                self.now_playing[guild_id] = "None"
                self.now_playing_item[guild_id] = None
                self.playback_state.pop(guild_id, None)
                return

            next_item = queue.popleft()
            self.trigger_background_downloads(guild_id)
            if isinstance(next_item, str):
                next_item = QueueItem(
                    title=os.path.relpath(next_item, MUSIC_FOLDER).replace("\\", "/"),
                    local_path=next_item,
                )
            await self.ensure_item_downloaded(next_item)
            self.play(voice_client, next_item)

    def play_next_song_in_queue(self, error=None):
        if error is not None:
            raise error

        for v in list(self.voice_clients):
            if not self.is_guild_active(v):
                guild_id = v.guild.id
                queue = self.queue.get(guild_id)
                if queue:
                    loop = getattr(self, "loop", None)
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._advance_queue_async(guild_id), loop
                        )
                    else:
                        item = queue.popleft()
                        self.play(v, item)
                else:
                    self.now_playing[guild_id] = "None"
                    self.now_playing_item[guild_id] = None
                    self.playback_state.pop(guild_id, None)

    def play(self, voice_client, item_or_path: QueueItem | str):
        guild_id = voice_client.guild.id
        if isinstance(item_or_path, str):
            path = item_or_path
            rel_name = (
                path[len(MUSIC_FOLDER) :]
                if path.startswith(MUSIC_FOLDER)
                else os.path.basename(path)
            )
            item = QueueItem(title=rel_name, local_path=path)
        else:
            item = item_or_path
            path = item.local_path

        if item.duration is None and path:
            item.duration = probe_audio_duration(path)

        raw_audio = discord.FFmpegPCMAudio(path)
        vol = self.volume[guild_id]
        audio = discord.PCMVolumeTransformer(raw_audio, volume=vol)
        voice_client.play(audio, after=self.play_next_song_in_queue)

        display_name = item.display_name(MUSIC_FOLDER)
        self.now_playing[guild_id] = display_name
        self.now_playing_item[guild_id] = item
        self.playback_state[guild_id] = {
            "start_time": time.monotonic(),
            "paused_total": 0.0,
            "pause_start": None,
            "duration": item.duration,
        }

    async def get_voice_client(self, voice_channel):
        voice_client = self.voice_client.get(voice_channel.guild.id, None)
        if voice_client is None or not voice_client.is_connected():
            voice_client = await voice_channel.connect()
            self.voice_client[voice_channel.guild.id] = voice_client

        if voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)

        return voice_client


if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True

    token = ""
    with open("token.txt", "r", encoding="utf-8") as token_file:
        token = token_file.read().strip()

    client = MyClient(intents=intents)
    client.run(token)
