import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import json
import os
import random
import re
import subprocess
import time
from urllib.parse import urlparse
import discord
import yt_dlp

MUSIC_FOLDER = "/home/ren/Music/"
URL_MAP_FILENAME = "url_map.json"
VOLUME_MAP_FILENAME = "volumes.json"
LANG_MAP_FILENAME = "languages.json"
LANG_DATA_FILENAME = "lang.json"
MAX_MUSIC_FOLDER_BYTES = 20 * 1024 * 1024 * 1024  # 20 GB
MIN_VOLUME_PERCENT = 0.0
MAX_VOLUME_PERCENT = 200.0
AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".opus", ".aac"}
PLAYLIST_EXTENSION = ".playlist"
SEARCHABLE_EXTENSIONS = AUDIO_EXTENSIONS | {PLAYLIST_EXTENSION}
PREFETCH_URL_COUNT = 5
PROGRESS_BAR_LENGTH = 16
HELP_PAGE_1_COMMANDS = (
    "play",
    "playnext",
    "cancel",
    "remove",
    "skip",
    "prev",
    "find",
    "vol",
)

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


def load_localization_data(
    lang_file_path: str | None = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Load COMMAND_HELP and MESSAGES dictionaries from lang.json in project folder."""
    candidate_paths: list[str] = []
    if lang_file_path:
        candidate_paths.append(lang_file_path)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidate_paths.append(os.path.join(base_dir, LANG_DATA_FILENAME))
    candidate_paths.append(os.path.abspath(LANG_DATA_FILENAME))

    for path in candidate_paths:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cmd_help = data.get("COMMAND_HELP", {})
                messages = data.get("MESSAGES", {})
                if cmd_help or messages:
                    return cmd_help, messages
    return {"en": {}, "ru": {}}, {"en": {}, "ru": {}}


COMMAND_HELP, MESSAGES = load_localization_data()


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


def format_seconds(seconds: float | None, unknown_label: str = "Unknown") -> str:
    if seconds is None or seconds < 0:
        return unknown_label
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def build_progress_bar(
    elapsed: float,
    duration: float | None,
    length: int = PROGRESS_BAR_LENGTH,
    filled_char: str = "█",
    empty_char: str = "░",
) -> str:
    """Build a partially filled playback progress bar."""
    if length < 2:
        length = 2
    if duration is None or duration <= 0:
        filled = length // 2 if elapsed > 0 else 0
        bar = filled_char * filled + empty_char * (length - filled)
        return f"[{bar}]"

    ratio = max(0.0, min(1.0, elapsed / duration))
    filled = int(round(ratio * length))
    if 0 < elapsed < duration:
        filled = max(1, min(length - 1, filled))
    pct = int(round(ratio * 100))
    bar = filled_char * filled + empty_char * (length - filled)
    return f"[{bar}] {pct}%"


def parse_filename_metadata(
    path_or_name: str,
) -> tuple[str | None, str | None, str | None]:
    """Extract (author, clean_title, video_id) from a filename like 'Uploader - Title[id].opus'."""
    if not path_or_name:
        return None, None, None
    base = os.path.basename(path_or_name.replace("\\", "/"))
    stem, ext = os.path.splitext(base)
    if ext.lower() not in SEARCHABLE_EXTENSIONS and not stem:
        stem = base

    video_id: str | None = None
    match = re.match(r"^(.*?)\[([A-Za-z0-9_-]{6,20})\]$", stem)
    if match:
        stem = match.group(1).strip()
        video_id = match.group(2).strip()

    author: str | None = None
    clean_title = stem.strip()
    if " - " in clean_title:
        parts = clean_title.split(" - ", 1)
        candidate_author = parts[0].strip()
        candidate_title = parts[1].strip()
        if candidate_author and candidate_title:
            author = candidate_author
            clean_title = candidate_title

    if author in ("NA", "None", "null"):
        author = None

    return author, clean_title or base, video_id


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
    author: str | None = None
    added_by: str | None = None
    thumbnail: str | None = None
    batch_id: int | None = None
    download_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def __post_init__(self):
        if self.local_path:
            parsed_author, _, parsed_vid = parse_filename_metadata(self.local_path)
            if not self.author and parsed_author:
                self.author = parsed_author
            if not self.video_id and parsed_vid:
                self.video_id = parsed_vid
            if os.path.exists(self.local_path):
                self.download_event.set()
        if not self.thumbnail and self.video_id:
            self.thumbnail = f"https://i.ytimg.com/vi/{self.video_id}/hqdefault.jpg"

    def clean_title(self) -> str:
        if self.title and not is_url(self.title):
            ext = os.path.splitext(self.title)[1].lower()
            if (
                ext not in SEARCHABLE_EXTENSIONS
                and "/" not in self.title
                and "\\" not in self.title
            ):
                _, parsed_title, _ = parse_filename_metadata(self.title)
                return parsed_title or self.title
            _, parsed_title, _ = parse_filename_metadata(self.title)
            if parsed_title:
                return parsed_title
        if self.local_path:
            _, parsed_title, _ = parse_filename_metadata(self.local_path)
            if parsed_title:
                return parsed_title
        return self.title or self.url or "Unknown"

    def get_author(self, default: str = "Unknown Artist") -> str:
        if self.author and self.author not in ("NA", "None"):
            return self.author
        for candidate in (self.local_path, self.title):
            if candidate:
                parsed_author, _, _ = parse_filename_metadata(candidate)
                if parsed_author:
                    return parsed_author
        return default

    def get_thumbnail(self) -> str | None:
        if self.thumbnail:
            return self.thumbnail
        vid = self.video_id
        if not vid and self.local_path:
            _, _, vid = parse_filename_metadata(self.local_path)
        if vid:
            return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        return None

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

    def clone_for_queue(self) -> "QueueItem":
        return QueueItem(
            title=self.title,
            local_path=self.local_path,
            url=self.url,
            video_id=self.video_id,
            duration=self.duration,
            author=self.author,
            added_by=self.added_by,
            thumbnail=self.thumbnail,
            batch_id=self.batch_id,
        )


# Safe UI base classes for both real discord.py and mocked test environments
_HAS_DISCORD_UI = (
    hasattr(discord, "ui")
    and hasattr(discord.ui, "View")
    and isinstance(getattr(discord.ui, "View", None), type)
    and hasattr(discord.ui, "Button")
    and isinstance(getattr(discord.ui, "Button", None), type)
)


class _FallbackButton:
    def __init__(
        self,
        *,
        label: str = "",
        style=None,
        custom_id: str | None = None,
        row: int | None = None,
        disabled: bool = False,
    ):
        self.label = label
        self.style = style
        self.custom_id = custom_id
        self.row = row
        self.disabled = disabled
        self.callback = None


class _FallbackView:
    def __init__(self, *, timeout: float | None = None):
        self.timeout = timeout
        self.children: list = []

    def add_item(self, item):
        self.children.append(item)
        return self


_ViewBase = discord.ui.View if _HAS_DISCORD_UI else _FallbackView
_ButtonClass = discord.ui.Button if _HAS_DISCORD_UI else _FallbackButton


def _button_style(name: str, default=None):
    styles = getattr(discord, "ButtonStyle", None)
    if styles is not None and hasattr(styles, name):
        return getattr(styles, name)
    return default


class PlayerView(_ViewBase):
    """Interactive music player view arranged in a clean 5x3 button grid (no modal)."""

    def __init__(self, client: "MyClient", guild_id: int):
        try:
            super().__init__(timeout=None)
        except Exception:
            _FallbackView.__init__(self, timeout=None)
        self.client = client
        self.guild_id = guild_id
        self.buttons_by_action: dict[str, object] = {}
        self._build_buttons()

    def _add_action_button(
        self,
        action: str,
        label_key: str,
        style_name: str,
        row: int,
        callback,
    ):
        label = self.client.t(self.guild_id, label_key)
        btn = _ButtonClass(
            label=label,
            style=_button_style(style_name),
            custom_id=f"player_{action}_{self.guild_id}",
            row=row,
        )
        btn.callback = callback
        self.buttons_by_action[action] = btn
        self.add_item(btn)

    def _build_buttons(self):
        vc = self.client.voice_client.get(self.guild_id)
        is_paused = bool(vc and getattr(vc, "is_paused", lambda: False)())
        pause_key = "btn_resume" if is_paused else "btn_pause"
        pause_style = "success" if is_paused else "primary"

        # Row 0 (5 buttons): Previous, Seek -10s, Pause/Resume, Seek +10s, Skip
        self._add_action_button("prev", "btn_prev", "secondary", 0, self.on_prev)
        self._add_action_button(
            "seek_back", "btn_seek_back", "secondary", 0, self.on_seek_back
        )
        self._add_action_button(
            "pause_resume", pause_key, pause_style, 0, self.on_pause_resume
        )
        self._add_action_button(
            "seek_fwd", "btn_seek_fwd", "secondary", 0, self.on_seek_fwd
        )
        self._add_action_button("skip", "btn_skip", "primary", 0, self.on_skip)

        # Row 1 (5 buttons): Shuffle, Queue, Cancel latest %play, Stop, Leave
        self._add_action_button(
            "shuffle", "btn_shuffle", "secondary", 1, self.on_shuffle
        )
        self._add_action_button("queue", "btn_queue", "secondary", 1, self.on_queue)
        self._add_action_button("cancel", "btn_cancel", "secondary", 1, self.on_cancel)
        self._add_action_button("stop", "btn_stop", "danger", 1, self.on_stop)
        self._add_action_button("leave", "btn_leave", "danger", 1, self.on_leave)

        # Row 2 (5 buttons): Volume -25%, Volume -10%, Volume 100%, Volume +10%, Volume +25%
        self._add_action_button(
            "vol_down_25", "btn_vol_down_25", "secondary", 2, self.on_vol_down_25
        )
        self._add_action_button(
            "vol_down", "btn_vol_down", "secondary", 2, self.on_vol_down
        )
        self._add_action_button(
            "vol_reset", "btn_vol_reset", "secondary", 2, self.on_vol_reset
        )
        self._add_action_button("vol_up", "btn_vol_up", "secondary", 2, self.on_vol_up)
        self._add_action_button(
            "vol_up_25", "btn_vol_up_25", "secondary", 2, self.on_vol_up_25
        )

    def sync_labels(self):
        vc = self.client.voice_client.get(self.guild_id)
        is_paused = bool(vc and getattr(vc, "is_paused", lambda: False)())
        pause_btn = self.buttons_by_action.get("pause_resume")
        if pause_btn is not None:
            pause_btn.label = self.client.t(
                self.guild_id, "btn_resume" if is_paused else "btn_pause"
            )
            pause_btn.style = _button_style("success" if is_paused else "primary")

    async def refresh_player_message(self, interaction=None):
        self.sync_labels()
        msg = getattr(interaction, "message", None) or self.client.player_messages.get(
            self.guild_id
        )
        if msg is not None and hasattr(msg, "edit"):
            current_item = self.client.now_playing_item.get(self.guild_id)
            if current_item is not None:
                fallback_text, embed = self.client.build_player_display(
                    self.guild_id, current_item
                )
                await self.client._safe_edit(
                    msg, None, embed=embed, view=self, fallback_text=fallback_text
                )

    async def on_pause_resume(self, interaction):
        await self.client._ack_interaction(interaction)
        vc = self.client.voice_client.get(self.guild_id)
        if vc is not None and getattr(vc, "is_playing", lambda: False)():
            vc.pause()
            state = self.client.playback_state.get(self.guild_id)
            if state is not None and state.get("pause_start") is None:
                state["pause_start"] = time.monotonic()
            await self.refresh_player_message(interaction)
        elif vc is not None and getattr(vc, "is_paused", lambda: False)():
            vc.resume()
            state = self.client.playback_state.get(self.guild_id)
            if state is not None and state.get("pause_start") is not None:
                state["paused_total"] = (state.get("paused_total") or 0.0) + (
                    time.monotonic() - state["pause_start"]
                )
                state["pause_start"] = None
            await self.refresh_player_message(interaction)

    async def on_skip(self, interaction):
        await self.client._ack_interaction(interaction)
        await self.client.execute_skip(self.guild_id, amount=1, recreate_player=True)

    async def on_prev(self, interaction):
        await self.client._ack_interaction(interaction)
        await self.client.execute_prev(self.guild_id, amount=1, recreate_player=True)

    async def on_seek_back(self, interaction):
        await self.client._ack_interaction(interaction)
        await self.client.execute_seek(self.guild_id, -10.0)

    async def on_seek_fwd(self, interaction):
        await self.client._ack_interaction(interaction)
        await self.client.execute_seek(self.guild_id, 10.0)

    async def on_shuffle(self, interaction):
        await self.client._ack_interaction(interaction)
        queue = self.client.queue[self.guild_id]
        if not queue:
            return
        items = list(queue)
        random.shuffle(items)
        queue.clear()
        queue.extend(items)
        self.client.trigger_background_downloads(self.guild_id)

    async def on_queue(self, interaction):
        await self.client._ack_interaction(interaction)

    async def on_cancel(self, interaction):
        await self.client._ack_interaction(interaction)
        await self.client.execute_cancel(self.guild_id, recreate_player=True)

    async def on_stop(self, interaction):
        await self.client._ack_interaction(interaction)
        await self.client.execute_skip(
            self.guild_id, amount=None, recreate_player=False
        )

    async def _adjust_volume(
        self,
        interaction,
        delta: float | None = None,
        exact: float | None = None,
    ):
        await self.client._ack_interaction(interaction)
        if exact is not None:
            new_pct = max(MIN_VOLUME_PERCENT, min(MAX_VOLUME_PERCENT, exact))
        else:
            current_pct = round(self.client.volume[self.guild_id] * 100)
            new_pct = max(
                MIN_VOLUME_PERCENT,
                min(MAX_VOLUME_PERCENT, current_pct + (delta or 0.0)),
            )
        self.client.set_guild_volume(self.guild_id, new_pct)
        await self.refresh_player_message(interaction)

    async def on_vol_down_25(self, interaction):
        await self._adjust_volume(interaction, delta=-25.0)

    async def on_vol_down(self, interaction):
        await self._adjust_volume(interaction, delta=-10.0)

    async def on_vol_reset(self, interaction):
        await self._adjust_volume(interaction, exact=100.0)

    async def on_vol_up(self, interaction):
        await self._adjust_volume(interaction, delta=10.0)

    async def on_vol_up_25(self, interaction):
        await self._adjust_volume(interaction, delta=25.0)

    async def on_leave(self, interaction):
        await self.client._ack_interaction(interaction)
        await self.client.execute_leave(self.guild_id)


class MyClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.voice_client: dict[int, discord.VoiceClient] = {}
        self.queue: dict[int, deque[QueueItem]] = defaultdict(deque)
        self.history: dict[int, deque[QueueItem]] = defaultdict(deque)
        self.play_batches: dict[int, list[int]] = defaultdict(list)
        self._next_batch_id: int = 1
        self.now_playing: dict[int, str] = {}
        self.now_playing_item: dict[int, QueueItem | None] = {}
        self.playback_state: dict[int, dict[str, float | None]] = {}
        self.volume: dict[int, float] = defaultdict(lambda: 1.0)
        self.language: dict[int, str] = defaultdict(lambda: "en")
        self.url_cache: dict[str, str] = {}
        self.video_id_cache: dict[str, str] = {}
        self.metadata_cache: dict[str, dict[str, object]] = {}
        self.inflight_downloads: dict[
            str, asyncio.Future[tuple[str, float | None, str | None]]
        ] = {}
        self.prefetch_tasks: dict[int, asyncio.Task] = {}
        self.play_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.player_messages: dict[int, object] = {}
        self.last_text_channel: dict[int, object] = {}
        self._suppress_after: set[int] = set()

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
        lang_dict = MESSAGES.get(lang, MESSAGES.get("en", {}))
        template = lang_dict.get(key, MESSAGES.get("en", {}).get(key, key))
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
        """Load persistent per-guild volume settings from disk (capped between 0 and 200%)."""
        path = self.volume_map_path
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for guild_id_str, vol in data.items():
                    try:
                        clamped_vol = max(
                            MIN_VOLUME_PERCENT / 100.0,
                            min(MAX_VOLUME_PERCENT / 100.0, float(vol)),
                        )
                        self.volume[int(guild_id_str)] = clamped_vol
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

    def set_guild_volume(self, guild_id: int, percentage: float) -> float:
        """Set and save guild volume percentage, capped between 0% and 200%."""
        clamped_pct = max(
            MIN_VOLUME_PERCENT, min(MAX_VOLUME_PERCENT, float(percentage))
        )
        self.volume[guild_id] = clamped_pct / 100.0
        self.save_volumes()
        voice_client = self.voice_client.get(guild_id)
        if (
            voice_client is not None
            and getattr(voice_client, "source", None) is not None
        ):
            if hasattr(voice_client.source, "volume"):
                voice_client.source.volume = self.volume[guild_id]
        return clamped_pct

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
        if video_id:
            existing = self.find_existing_opus_by_id(video_id)
            if existing and os.path.isfile(existing):
                if url:
                    self.update_url_mapping(url, existing)
                return existing
        return None

    def _extract_info_metadata(self, info: dict, opus_path: str | None = None):
        video_id = info.get("id")
        duration = info.get("duration")
        title = info.get("title")
        author = (
            info.get("uploader")
            or info.get("artist")
            or info.get("channel")
            or info.get("creator")
        )
        thumbnail = info.get("thumbnail")
        if not thumbnail and info.get("thumbnails"):
            thumbs = [
                t.get("url")
                for t in info["thumbnails"]
                if isinstance(t, dict) and t.get("url")
            ]
            if thumbs:
                thumbnail = thumbs[-1]
        if not thumbnail and video_id:
            thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

        meta = {
            "title": title,
            "author": author,
            "thumbnail": thumbnail,
            "duration": duration,
            "video_id": video_id,
        }
        if opus_path:
            self.metadata_cache[opus_path] = meta
        if video_id:
            self.metadata_cache[f"id:{video_id}"] = meta
        return meta

    def _apply_cached_metadata(self, item: QueueItem):
        meta = None
        if item.local_path and item.local_path in self.metadata_cache:
            meta = self.metadata_cache[item.local_path]
        elif item.video_id and f"id:{item.video_id}" in self.metadata_cache:
            meta = self.metadata_cache[f"id:{item.video_id}"]

        if meta:
            if not item.author and meta.get("author"):
                item.author = str(meta["author"])
            if not item.thumbnail and meta.get("thumbnail"):
                item.thumbnail = str(meta["thumbnail"])
            if item.duration is None and meta.get("duration") is not None:
                item.duration = float(meta["duration"])
            if not item.video_id and meta.get("video_id"):
                item.video_id = str(meta["video_id"])

        if item.local_path:
            parsed_author, _, parsed_vid = parse_filename_metadata(item.local_path)
            if not item.author and parsed_author:
                item.author = parsed_author
            if not item.video_id and parsed_vid:
                item.video_id = parsed_vid
            if item.duration is None and os.path.isfile(item.local_path):
                item.duration = probe_audio_duration(item.local_path)
        if not item.thumbnail and item.video_id:
            item.thumbnail = f"https://i.ytimg.com/vi/{item.video_id}/hqdefault.jpg"

    def _download_with_ytdlp_sync(
        self, query_or_url: str
    ) -> tuple[str, float | None, str | None, str | None]:
        os.makedirs(MUSIC_FOLDER, exist_ok=True)
        YDL_OPTS["paths"] = {"home": MUSIC_FOLDER}
        if is_url(query_or_url):
            norm_url = normalize_url(query_or_url)
            cached = self.get_cached_download(norm_url)
            if cached:
                _, parsed_title, parsed_vid = parse_filename_metadata(cached)
                return (
                    cached,
                    probe_audio_duration(cached),
                    parsed_title or os.path.basename(cached),
                    parsed_vid,
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
                self._extract_info_metadata(info, existing)
                if is_url(query_or_url):
                    self.update_url_mapping(query_or_url, existing)
                if info.get("webpage_url"):
                    self.update_url_mapping(info["webpage_url"], existing)
                return existing, duration, title, video_id

            prepared = ydl.prepare_filename(info)
            base, _ = os.path.splitext(prepared)
            opus_path = base + ".opus"
            if os.path.isfile(opus_path):
                self._extract_info_metadata(info, opus_path)
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
            self._extract_info_metadata(info, opus_path)
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
            self._apply_cached_metadata(item)
            item.download_event.set()
            return item.local_path

        # If local_path was set or mapped in url_cache, verify whether it still exists on disk
        cached = self.get_cached_download(item.url, item.video_id)
        if cached:
            item.local_path = cached
            self._apply_cached_metadata(item)
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
            if title and (
                not item.title or item.title == item.url or item.title == target
            ):
                item.title = title
            self._apply_cached_metadata(item)
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
            if title and (
                not item.title or item.title == item.url or item.title == target
            ):
                item.title = title
            self._apply_cached_metadata(item)
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
                        self._apply_cached_metadata(item)
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
                playlist_uploader = (
                    info.get("uploader") or info.get("channel") or info.get("artist")
                )
                for entry in raw_entries:
                    vid_id = entry.get("id")
                    entry_url = entry.get("url") or entry.get("webpage_url")
                    if entry_url and not is_url(entry_url) and vid_id:
                        entry_url = f"https://www.youtube.com/watch?v={vid_id}"
                    elif not entry_url and vid_id:
                        entry_url = f"https://www.youtube.com/watch?v={vid_id}"
                    author = (
                        entry.get("uploader")
                        or entry.get("artist")
                        or entry.get("channel")
                        or playlist_uploader
                    )
                    thumbnail = entry.get("thumbnail")
                    if not thumbnail and entry.get("thumbnails"):
                        thumbs = [
                            t.get("url")
                            for t in entry["thumbnails"]
                            if isinstance(t, dict) and t.get("url")
                        ]
                        if thumbs:
                            thumbnail = thumbs[-1]
                    if not thumbnail and vid_id:
                        thumbnail = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
                    results.append(
                        {
                            "url": normalize_url(entry_url) if entry_url else norm_url,
                            "id": vid_id,
                            "title": entry.get("title") or entry_url or norm_url,
                            "duration": entry.get("duration"),
                            "author": author,
                            "thumbnail": thumbnail,
                        }
                    )
                return results
            else:
                vid_id = info.get("id")
                webpage_url = info.get("webpage_url") or norm_url
                meta = self._extract_info_metadata(info)
                return [
                    {
                        "url": normalize_url(webpage_url),
                        "id": vid_id,
                        "title": info.get("title") or norm_url,
                        "duration": info.get("duration"),
                        "author": meta.get("author"),
                        "thumbnail": meta.get("thumbnail"),
                    }
                ]

    def load_playlist_file(
        self, playlist_path: str, added_by: str | None = None
    ) -> list[QueueItem]:
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
                author = entry.get("author")
                item_added_by = entry.get("added_by") or added_by
                thumbnail = entry.get("thumbnail")
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
                            author=author,
                            added_by=item_added_by,
                            thumbnail=thumbnail,
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
                            author=author,
                            added_by=item_added_by,
                            thumbnail=thumbnail,
                        )
                    )
            elif isinstance(entry, str):
                entry_str = entry.strip()
                if not entry_str:
                    continue
                if is_url(entry_str):
                    norm = normalize_url(entry_str)
                    cached = self.get_cached_download(norm)
                    items.append(
                        QueueItem(
                            title=norm,
                            local_path=cached,
                            url=norm,
                            added_by=added_by,
                        )
                    )
                else:
                    full_path = (
                        entry_str
                        if os.path.isabs(entry_str)
                        else os.path.join(MUSIC_FOLDER, entry_str)
                    )
                    rel_title = os.path.relpath(full_path, MUSIC_FOLDER).replace(
                        "\\", "/"
                    )
                    items.append(
                        QueueItem(
                            title=rel_title,
                            local_path=full_path,
                            added_by=added_by,
                        )
                    )
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

    def _create_embed(
        self,
        title: str,
        description: str = "",
        color: int = 0x5865F2,
        thumbnail: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None,
    ):
        embed_cls = getattr(discord, "Embed", None)
        if embed_cls is None:
            return None
        try:
            embed = embed_cls(title=title, description=description, color=color)
            if thumbnail and hasattr(embed, "set_thumbnail"):
                embed.set_thumbnail(url=thumbnail)
            if fields and hasattr(embed, "add_field"):
                for fname, fval, inline in fields:
                    embed.add_field(name=fname, value=fval, inline=inline)
            return embed
        except Exception:
            return None

    def build_searching_embed(self, guild_id: int, query: str):
        desc = self.t(guild_id, "searching", query=query)
        return self._create_embed(
            title="🔎",
            description=desc,
            color=0xFEE75C,
        )

    def build_error_embed(self, guild_id: int, query: str, err: object):
        desc = self.t(guild_id, "play_error", query=query, err=err)
        return self._create_embed(
            title="❌",
            description=desc,
            color=0xED4245,
        )

    def build_player_display(
        self,
        guild_id: int,
        item: QueueItem,
        extra_added_count: int = 0,
    ) -> tuple[str, object]:
        """Build interactive player Embed (without playback time bar or raw thumbnail URL)."""
        self._apply_cached_metadata(item)
        song_name = item.clean_title()
        author = item.get_author(self.t(guild_id, "unknown_author"))
        added_by = item.added_by or self.t(guild_id, "unknown_user")
        duration_str = format_seconds(
            item.duration, self.t(guild_id, "unknown_duration")
        )
        thumbnail = item.get_thumbnail()
        vol_pct = int(round(self.volume[guild_id] * 100))

        if extra_added_count == 0:
            fallback_text = self.t(
                guild_id,
                "playing_single",
                name=song_name,
                author=author,
                added_by=added_by,
                duration=duration_str,
            )
        else:
            fallback_text = self.t(
                guild_id,
                "playing_multi",
                name=song_name,
                author=author,
                added_by=added_by,
                duration=duration_str,
                count=extra_added_count,
            )

        fields = [
            (self.t(guild_id, "field_author"), author, True),
            (self.t(guild_id, "field_added_by"), added_by, True),
            (self.t(guild_id, "field_duration"), duration_str, True),
            (self.t(guild_id, "field_volume"), f"{vol_pct}%", True),
        ]
        if extra_added_count > 0:
            fields.append(
                (
                    self.t(guild_id, "field_extra_tracks"),
                    self.t(guild_id, "more_tracks_added", count=extra_added_count),
                    False,
                )
            )

        embed = self._create_embed(
            title=self.t(guild_id, "player_embed_title"),
            description=f"**{song_name}**",
            color=0x57F287,
            thumbnail=thumbnail,
            fields=fields,
        )
        return fallback_text, embed

    def build_added_to_queue_display(
        self,
        guild_id: int,
        item: QueueItem,
        total_added: int,
        play_next: bool = False,
    ) -> tuple[str, object]:
        """Build Added-to-Queue Embed (no duplicate fields, image-only thumbnail, no player view)."""
        self._apply_cached_metadata(item)
        song_name = item.clean_title()
        author = item.get_author(self.t(guild_id, "unknown_author"))
        added_by = item.added_by or self.t(guild_id, "unknown_user")
        duration_str = format_seconds(
            item.duration, self.t(guild_id, "unknown_duration")
        )
        thumbnail = item.get_thumbnail()
        extra_count = max(0, total_added - 1)
        pos_label = self.t(guild_id, "pos_front" if play_next else "pos_end")

        if extra_count == 0:
            fallback_text = self.t(
                guild_id,
                "added_single",
                pos=pos_label,
                name=song_name,
                author=author,
                added_by=added_by,
                duration=duration_str,
            )
        else:
            fallback_text = self.t(
                guild_id,
                "added_multi",
                pos=pos_label,
                name=song_name,
                author=author,
                added_by=added_by,
                duration=duration_str,
                count=extra_count,
            )

        embed_title = self.t(
            guild_id,
            "added_next_embed_title" if play_next else "added_embed_title",
        )
        fields = [
            (self.t(guild_id, "field_author"), author, True),
            (self.t(guild_id, "field_added_by"), added_by, True),
            (self.t(guild_id, "field_duration"), duration_str, True),
        ]
        if extra_count > 0:
            fields.append(
                (
                    self.t(guild_id, "field_extra_tracks"),
                    self.t(guild_id, "more_tracks_added", count=extra_count),
                    False,
                )
            )

        embed = self._create_embed(
            title=embed_title,
            description=f"**{song_name}**",
            color=0x5865F2,
            thumbnail=thumbnail,
            fields=fields,
        )
        return fallback_text, embed

    def build_playing_display(
        self, guild_id: int, current_item: QueueItem
    ) -> tuple[str, object]:
        """Build %playing Embed with partially filled progress bar and image-only thumbnail."""
        self._apply_cached_metadata(current_item)
        duration = current_item.duration
        if duration is None and current_item.local_path:
            duration = probe_audio_duration(current_item.local_path)
            current_item.duration = duration
            if guild_id in self.playback_state:
                self.playback_state[guild_id]["duration"] = duration

        elapsed = self.get_current_playback_seconds(guild_id)
        song_title = current_item.clean_title()
        author = current_item.get_author(self.t(guild_id, "unknown_author"))
        added_by = current_item.added_by or self.t(guild_id, "unknown_user")
        thumbnail = current_item.get_thumbnail()
        voice_client = self.voice_client.get(guild_id)
        status_str = (
            self.t(guild_id, "status_paused")
            if getattr(voice_client, "is_paused", lambda: False)()
            else self.t(guild_id, "status_playing")
        )
        elapsed_str = format_seconds(elapsed, self.t(guild_id, "unknown_duration"))
        duration_str = format_seconds(duration, self.t(guild_id, "unknown_duration"))
        bar = build_progress_bar(elapsed, duration)

        fallback_text = self.t(
            guild_id,
            "now_playing_info",
            title=song_title,
            status=status_str,
            author=author,
            added_by=added_by,
            bar=bar,
            elapsed=elapsed_str,
            duration=duration_str,
        )

        fields = [
            (self.t(guild_id, "field_author"), author, True),
            (self.t(guild_id, "field_added_by"), added_by, True),
            (self.t(guild_id, "field_status"), status_str, True),
            (
                self.t(guild_id, "field_progress"),
                f"`{bar}` `{elapsed_str} / {duration_str}`",
                False,
            ),
        ]
        embed = self._create_embed(
            title=self.t(guild_id, "playing_embed_title"),
            description=f"**{song_title}**",
            color=0x5865F2,
            thumbnail=thumbnail,
            fields=fields,
        )
        return fallback_text, embed

    def create_player_view(self, guild_id: int) -> PlayerView:
        return PlayerView(self, guild_id)

    async def _delete_user_message(self, message):
        if message is not None and hasattr(message, "delete"):
            try:
                await message.delete()
            except Exception:
                pass

    async def _delete_player_message(self, guild_id: int):
        old_msg = self.player_messages.pop(guild_id, None)
        if old_msg is not None and hasattr(old_msg, "delete"):
            try:
                await old_msg.delete()
            except Exception:
                pass

    async def recreate_player_message(self, guild_id: int, extra_added_count: int = 0):
        """Delete previous interactive player message and create a new one at the bottom of the channel."""
        await self._delete_player_message(guild_id)
        current_item = self.now_playing_item.get(guild_id)
        voice_client = self.voice_client.get(guild_id)
        text_channel = self.last_text_channel.get(guild_id)
        if (
            current_item is None
            or text_channel is None
            or not self.is_guild_active(voice_client)
        ):
            return None

        fallback_text, embed = self.build_player_display(
            guild_id, current_item, extra_added_count=extra_added_count
        )
        player_view = self.create_player_view(guild_id)
        sent = await self._safe_send(
            text_channel,
            None,
            embed=embed,
            view=player_view,
            fallback_text=fallback_text,
        )
        self.player_messages[guild_id] = sent
        return sent

    async def _safe_send(
        self,
        text_channel,
        content: str | None,
        *,
        embed=None,
        view=None,
        fallback_text: str | None = None,
    ):
        kwargs = {}
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        if kwargs:
            try:
                if content is not None:
                    return await text_channel.send(content, **kwargs)
                return await text_channel.send(**kwargs)
            except TypeError:
                pass
        text_to_send = content if content is not None else (fallback_text or "")
        return await text_channel.send(text_to_send)

    async def _safe_edit(
        self,
        status_msg,
        content: str | None,
        *,
        embed=None,
        view=None,
        fallback_text: str | None = None,
    ):
        kwargs = {"content": content}
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        try:
            await status_msg.edit(**kwargs)
            return status_msg
        except TypeError:
            text_to_send = content if content is not None else (fallback_text or "")
            await status_msg.edit(content=text_to_send)
            return status_msg

    async def _send_or_edit(
        self,
        text_channel,
        status_msg,
        content: str | None,
        *,
        embed=None,
        view=None,
        fallback_text: str | None = None,
    ):
        if status_msg is not None and hasattr(status_msg, "edit"):
            return await self._safe_edit(
                status_msg,
                content,
                embed=embed,
                view=view,
                fallback_text=fallback_text,
            )
        return await self._safe_send(
            text_channel,
            content,
            embed=embed,
            view=view,
            fallback_text=fallback_text,
        )

    async def _ack_interaction(self, interaction):
        """Silently acknowledge button interaction without sending a message to the user."""
        resp = getattr(interaction, "response", None)
        if resp is not None and hasattr(resp, "defer"):
            try:
                if hasattr(resp, "is_done") and resp.is_done():
                    return
                await resp.defer()
            except Exception:
                pass

    async def handle_play_command(self, message, query: str, play_next: bool = False):
        text_channel = message.channel
        guild_id = text_channel.guild.id
        self.last_text_channel[guild_id] = text_channel
        cmd_name = "%playnext" if play_next else "%play"

        if not query:
            await text_channel.send(self.t(guild_id, "play_usage", cmd_name=cmd_name))
            return

        voice_channel = await self.get_user_voice_channel(message)
        if voice_channel is None:
            await text_channel.send(self.t(guild_id, "join_voice"))
            return

        await self._delete_user_message(message)

        requester_name = (
            getattr(message.author, "display_name", None)
            or getattr(message.author, "name", None)
            or str(message.author)
        )

        voice_client = await self.get_voice_client(voice_channel)
        items_to_add: list[QueueItem] = []
        status_msg = None

        try:
            if is_url(query):
                norm_url = normalize_url(query)
                cached = self.get_cached_download(norm_url)
                if cached:
                    rel_name = os.path.relpath(cached, MUSIC_FOLDER).replace("\\", "/")
                    item = QueueItem(
                        title=rel_name,
                        local_path=cached,
                        url=norm_url,
                        added_by=requester_name,
                    )
                    self._apply_cached_metadata(item)
                    items_to_add.append(item)
                else:
                    search_text = self.t(guild_id, "searching", query=query)
                    status_msg = await self._safe_send(
                        text_channel,
                        None,
                        embed=self.build_searching_embed(guild_id, query),
                        fallback_text=search_text,
                    )
                    entries = await asyncio.to_thread(
                        self._extract_url_entries_sync, norm_url
                    )
                    if len(entries) > 1:
                        # Playlist URL: add all individual song URLs to queue
                        for entry in entries:
                            entry_url = entry["url"]
                            vid_id = entry.get("id")
                            existing = self.get_cached_download(entry_url, vid_id)
                            q_item = QueueItem(
                                title=entry.get("title") or entry_url,
                                local_path=existing,
                                url=entry_url,
                                video_id=vid_id,
                                duration=entry.get("duration"),
                                author=entry.get("author"),
                                added_by=requester_name,
                                thumbnail=entry.get("thumbnail"),
                            )
                            self._apply_cached_metadata(q_item)
                            items_to_add.append(q_item)
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
                                "author": None,
                                "thumbnail": None,
                            }
                        )
                        item = QueueItem(
                            title=entry.get("title") or norm_url,
                            url=entry.get("url") or norm_url,
                            video_id=entry.get("id"),
                            duration=entry.get("duration"),
                            author=entry.get("author"),
                            added_by=requester_name,
                            thumbnail=entry.get("thumbnail"),
                        )
                        await self.ensure_item_downloaded(item)
                        items_to_add.append(item)
            else:
                # Search local music directory using %find matching rules
                matches = find_matching_files(query, MUSIC_FOLDER)
                if len(matches) == 1:
                    match_path = matches[0]
                    if match_path.lower().endswith(PLAYLIST_EXTENSION):
                        items_to_add.extend(
                            self.load_playlist_file(match_path, added_by=requester_name)
                        )
                    else:
                        rel_name = os.path.relpath(match_path, MUSIC_FOLDER).replace(
                            "\\", "/"
                        )
                        item = QueueItem(
                            title=rel_name,
                            local_path=match_path,
                            added_by=requester_name,
                        )
                        self._apply_cached_metadata(item)
                        items_to_add.append(item)
                else:
                    # 0 or multiple local matches: search on YouTube and download as .opus
                    search_text = self.t(guild_id, "searching", query=query)
                    status_msg = await self._safe_send(
                        text_channel,
                        None,
                        embed=self.build_searching_embed(guild_id, query),
                        fallback_text=search_text,
                    )
                    item = QueueItem(title=query, added_by=requester_name)
                    await self.ensure_item_downloaded(item)
                    items_to_add.append(item)
        except Exception as exc:
            err_text = self.t(guild_id, "play_error", query=query, err=exc)
            err_embed = self.build_error_embed(guild_id, query, exc)
            await self._send_or_edit(
                text_channel,
                status_msg,
                None,
                embed=err_embed,
                fallback_text=err_text,
            )
            return

        if not items_to_add:
            await self._send_or_edit(
                text_channel, status_msg, self.t(guild_id, "no_playable")
            )
            return

        batch_id = self._next_batch_id
        self._next_batch_id += 1
        for item in items_to_add:
            if not item.added_by:
                item.added_by = requester_name
            item.batch_id = batch_id
        self.play_batches[guild_id].append(batch_id)

        was_queue_empty_and_inactive = (
            not self.is_guild_active(voice_client) and len(self.queue[guild_id]) == 0
        )
        self.enqueue_items(guild_id, items_to_add, play_next=play_next)

        if not self.is_guild_active(voice_client):
            # Start first song as fast as possible, then download remaining 5 leftmost URLs in background
            first_item = self.queue[guild_id].popleft()
            self.trigger_background_downloads(guild_id)
            if not first_item.local_path or not os.path.isfile(first_item.local_path):
                if status_msg is None:
                    search_text = self.t(guild_id, "searching", query=first_item.title)
                    status_msg = await self._safe_send(
                        text_channel,
                        None,
                        embed=self.build_searching_embed(guild_id, first_item.title),
                        fallback_text=search_text,
                    )
                try:
                    await self.ensure_item_downloaded(first_item)
                except Exception as exc:
                    err_text = self.t(
                        guild_id, "play_error", query=first_item.title, err=exc
                    )
                    err_embed = self.build_error_embed(guild_id, first_item.title, exc)
                    await self._send_or_edit(
                        text_channel,
                        status_msg,
                        None,
                        embed=err_embed,
                        fallback_text=err_text,
                    )
                    return

            # Always send/edit the song info card first
            card_text, card_embed = self.build_added_to_queue_display(
                guild_id,
                first_item,
                total_added=len(items_to_add),
                play_next=play_next,
            )
            await self._send_or_edit(
                text_channel,
                status_msg,
                None,
                embed=card_embed,
                view=None,
                fallback_text=card_text,
            )

            self.play(voice_client, first_item)
            if was_queue_empty_and_inactive:
                await self.recreate_player_message(guild_id)
        else:
            # Song was added to queue while already playing: send song info card without creating player
            self.trigger_background_downloads(guild_id)
            primary_item = items_to_add[0]
            fallback_text, embed = self.build_added_to_queue_display(
                guild_id,
                primary_item,
                total_added=len(items_to_add),
                play_next=play_next,
            )
            await self._send_or_edit(
                text_channel,
                status_msg,
                None,
                embed=embed,
                view=None,
                fallback_text=fallback_text,
            )

    def _record_item_in_history(self, guild_id: int, item: QueueItem | None):
        if item is None:
            return
        self.history[guild_id].append(item.clone_for_queue())

    async def execute_cancel(self, guild_id: int, recreate_player: bool = True) -> str:
        """Remove all tracks from queue, history, and current playing song added in the latest %play."""
        queue = self.queue[guild_id]
        history = self.history[guild_id]
        batches = self.play_batches[guild_id]
        if not batches:
            return self.t(guild_id, "cancel_empty")

        while batches:
            target_batch_id = batches.pop()
            queue_removed = 0
            history_removed = 0
            current_removed = 0

            remaining_queue: list[QueueItem] = []
            for item in queue:
                if getattr(item, "batch_id", None) == target_batch_id:
                    queue_removed += 1
                else:
                    remaining_queue.append(item)
            if queue_removed > 0:
                queue.clear()
                queue.extend(remaining_queue)

            remaining_history: list[QueueItem] = []
            for item in history:
                if getattr(item, "batch_id", None) == target_batch_id:
                    history_removed += 1
                else:
                    remaining_history.append(item)
            if history_removed > 0:
                history.clear()
                history.extend(remaining_history)

            current_item = self.now_playing_item.get(guild_id)
            if (
                current_item is not None
                and getattr(current_item, "batch_id", None) == target_batch_id
            ):
                current_removed = 1
                self.now_playing_item[guild_id] = None
                self.now_playing[guild_id] = "None"
                self.playback_state.pop(guild_id, None)
                voice_client = self.voice_client.get(guild_id)
                if voice_client and self.is_guild_active(voice_client):
                    self._suppress_after.add(guild_id)
                    try:
                        voice_client.stop()
                    finally:
                        self._suppress_after.discard(guild_id)
                await self._advance_queue_async(
                    guild_id,
                    record_previous=False,
                    recreate_player=recreate_player,
                )

            total_removed = queue_removed + history_removed + current_removed
            if total_removed > 0:
                self.trigger_background_downloads(guild_id)
                return self.t(
                    guild_id,
                    "cancel_done",
                    count=total_removed,
                    queue_count=queue_removed,
                    history_count=history_removed,
                    current_count=current_removed,
                )

        return self.t(guild_id, "cancel_empty")

    async def execute_remove(
        self,
        guild_id: int,
        amount: int | None = None,
        recreate_player: bool = True,
    ) -> str:
        """Remove current song (amount is None), upcoming songs from queue (amount > 0), or songs from history (amount < 0)."""
        if amount is None:
            current_item = self.now_playing_item.get(guild_id)
            voice_client = self.voice_client.get(guild_id)
            if current_item is None or not self.is_guild_active(voice_client):
                return self.t(guild_id, "nothing_playing")

            removed_name = current_item.clean_title()
            self.now_playing_item[guild_id] = None
            self.now_playing[guild_id] = "None"
            self.playback_state.pop(guild_id, None)
            self._suppress_after.add(guild_id)
            try:
                voice_client.stop()
            finally:
                self._suppress_after.discard(guild_id)

            await self._advance_queue_async(
                guild_id,
                record_previous=False,
                recreate_player=recreate_player,
            )
            return self.t(guild_id, "removed_current", name=removed_name)

        if amount > 0:
            queue = self.queue[guild_id]
            if not queue:
                return self.t(guild_id, "queue_empty")

            actual_removed = min(amount, len(queue))
            for _ in range(actual_removed):
                queue.popleft()
            self.trigger_background_downloads(guild_id)
            return self.t(guild_id, "removed_n", count=actual_removed)

        if amount < 0:
            history = self.history[guild_id]
            if not history:
                return self.t(guild_id, "prev_empty")

            actual_removed = min(abs(amount), len(history))
            for _ in range(actual_removed):
                history.pop()
            return self.t(guild_id, "removed_history_n", count=actual_removed)

        return self.t(guild_id, "remove_zero_err")

    async def execute_skip(
        self,
        guild_id: int,
        amount: int | None = 1,
        recreate_player: bool = True,
    ) -> str:
        voice_client = self.voice_client.get(guild_id)
        queue = self.queue[guild_id]
        current_name = self.now_playing.get(guild_id, self.t(guild_id, "none_playing"))
        current_item = self.now_playing_item.get(guild_id)

        if amount is None or amount > len(queue):
            if current_item is not None:
                self._record_item_in_history(guild_id, current_item)
            while queue:
                skipped_item = queue.popleft()
                self._record_item_in_history(guild_id, skipped_item)
            self.trigger_background_downloads(guild_id)
            self.now_playing[guild_id] = "None"
            self.now_playing_item[guild_id] = None
            self.playback_state.pop(guild_id, None)
            if voice_client and self.is_guild_active(voice_client):
                self._suppress_after.add(guild_id)
                try:
                    voice_client.stop()
                finally:
                    self._suppress_after.discard(guild_id)
            await self._delete_player_message(guild_id)
            return self.t(guild_id, "skip_cleared")

        if current_item is not None:
            self._record_item_in_history(guild_id, current_item)

        # Skip (amount - 1) queued songs so the amount-th song in queue plays next
        for _ in range(amount - 1):
            if queue:
                skipped_item = queue.popleft()
                self._record_item_in_history(guild_id, skipped_item)

        self.trigger_background_downloads(guild_id)
        self.now_playing_item[guild_id] = None
        if voice_client and self.is_guild_active(voice_client):
            self._suppress_after.add(guild_id)
            try:
                voice_client.stop()
            finally:
                self._suppress_after.discard(guild_id)
        await self._advance_queue_async(
            guild_id, record_previous=False, recreate_player=recreate_player
        )
        return self.t(guild_id, "skipped_n", amount=amount, current=current_name)

    async def execute_prev(
        self,
        guild_id: int,
        amount: int = 1,
        recreate_player: bool = True,
    ) -> str:
        history = self.history[guild_id]
        if not history:
            return self.t(guild_id, "prev_empty")

        actual_amount = min(amount, len(history))
        queue = self.queue[guild_id]

        # Preserve currently playing song by placing it at the front of the queue first
        current_item = self.now_playing_item.get(guild_id)
        if current_item is not None:
            queue.appendleft(current_item.clone_for_queue())

        # Add specified amount of songs that already played back to the beginning of queue
        for _ in range(actual_amount):
            prev_item = history.pop()
            queue.appendleft(prev_item)

        self.trigger_background_downloads(guild_id)

        voice_client = self.voice_client.get(guild_id)
        self.now_playing_item[guild_id] = None
        if voice_client and self.is_guild_active(voice_client):
            self._suppress_after.add(guild_id)
            try:
                voice_client.stop()
            finally:
                self._suppress_after.discard(guild_id)

        await self._advance_queue_async(
            guild_id, record_previous=False, recreate_player=recreate_player
        )
        now_item = self.now_playing_item.get(guild_id)
        now_name = (
            now_item.display_name(MUSIC_FOLDER)
            if now_item is not None
            else self.now_playing.get(guild_id, self.t(guild_id, "none_playing"))
        )
        return self.t(guild_id, "prev_played", amount=actual_amount, name=now_name)

    async def execute_seek(self, guild_id: int, delta_seconds: float) -> str:
        current_item = self.now_playing_item.get(guild_id)
        voice_client = self.voice_client.get(guild_id)
        if current_item is None or not self.is_guild_active(voice_client):
            return self.t(guild_id, "nothing_playing")

        if current_item.duration is None and current_item.local_path:
            current_item.duration = probe_audio_duration(current_item.local_path)

        elapsed = self.get_current_playback_seconds(guild_id)
        new_pos = max(0.0, elapsed + delta_seconds)
        if current_item.duration is not None and current_item.duration > 0:
            new_pos = min(new_pos, float(current_item.duration))

        was_paused = getattr(voice_client, "is_paused", lambda: False)()
        self._suppress_after.add(guild_id)
        try:
            voice_client.stop()
            self.play(
                voice_client,
                current_item,
                start_at=new_pos,
                record_history=False,
            )
            if was_paused and hasattr(voice_client, "pause"):
                voice_client.pause()
                if guild_id in self.playback_state:
                    self.playback_state[guild_id]["pause_start"] = time.monotonic()
        finally:
            self._suppress_after.discard(guild_id)

        if float(delta_seconds).is_integer():
            delta_num = int(delta_seconds)
            delta_str = f"+{delta_num}" if delta_num >= 0 else str(delta_num)
        else:
            delta_str = (
                f"+{delta_seconds}" if delta_seconds >= 0 else str(delta_seconds)
            )

        unknown_dur = self.t(guild_id, "unknown_duration")
        return self.t(
            guild_id,
            "seeked_to",
            delta=delta_str,
            elapsed=format_seconds(new_pos, unknown_dur),
            duration=format_seconds(current_item.duration, unknown_dur),
        )

    async def execute_leave(self, guild_id: int) -> str:
        voice_client = self.voice_client.get(guild_id)
        if voice_client is None or not voice_client.is_connected():
            return self.t(guild_id, "not_in_voice")
        if self.is_guild_active(voice_client):
            self._suppress_after.add(guild_id)
            try:
                voice_client.stop()
            finally:
                self._suppress_after.discard(guild_id)
        await voice_client.disconnect()
        self.voice_client.pop(guild_id, None)
        self.now_playing[guild_id] = "None"
        self.now_playing_item[guild_id] = None
        self.playback_state.pop(guild_id, None)
        await self._delete_player_message(guild_id)
        return self.t(guild_id, "left_voice")

    def format_queue_page(self, guild_id: int, page: int = 1) -> str:
        queue = self.queue[guild_id]
        if not queue:
            return self.t(guild_id, "queue_empty")

        if page <= 0:
            page = 1
        per_page = 10
        total_pages = (len(queue) - 1) // per_page + 1
        start_idx = (page - 1) * per_page
        end_idx = min(page * per_page, len(queue))
        queue_list = list(queue)

        msg = self.t(
            guild_id,
            "queue_header",
            page=page,
            total_pages=total_pages,
            total=len(queue),
        )
        for i in range(start_idx, end_idx):
            item = queue_list[i]
            name = item.display_name() if isinstance(item, QueueItem) else str(item)
            msg += f"{i + 1}. {name}\n\n"

        if page < total_pages:
            msg += self.t(guild_id, "queue_next", next_page=page + 1)

        return msg

    def format_history_page(self, guild_id: int, page: int = 1) -> str:
        history = self.history[guild_id]
        if not history:
            return self.t(guild_id, "history_empty")

        if page <= 0:
            page = 1
        per_page = 10
        total_pages = (len(history) - 1) // per_page + 1
        start_idx = (page - 1) * per_page
        end_idx = min(page * per_page, len(history))
        history_list = list(history)

        msg = self.t(
            guild_id,
            "history_header",
            page=page,
            total_pages=total_pages,
            total=len(history),
        )
        for i in range(start_idx, end_idx):
            item = history_list[i]
            name = item.display_name() if isinstance(item, QueueItem) else str(item)
            msg += f"{i + 1}. {name}\n\n"

        if page < total_pages:
            msg += self.t(guild_id, "history_next", next_page=page + 1)

        return msg

    def format_help_page(self, guild_id: int, page: int = 1) -> str:
        lang = self.language.get(guild_id, "en")
        help_table = COMMAND_HELP.get(lang, COMMAND_HELP["en"])
        total_pages = 2
        if page < 1:
            page = 1
        elif page > total_pages:
            page = total_pages

        if page == 1:
            keys = [k for k in HELP_PAGE_1_COMMANDS if k in help_table]
        else:
            page1_set = set(HELP_PAGE_1_COMMANDS)
            keys = [
                k for k in help_table.keys() if k != "volume" and k not in page1_set
            ]

        lines = [self.t(guild_id, "help_header", page=page, total_pages=total_pages)]
        for cmd_key in keys:
            info = help_table[cmd_key]
            lines.append(f"`{info['usage']}` — {info['short']}")
        lines.append(self.t(guild_id, "help_footer"))
        if page < total_pages:
            lines.append(self.t(guild_id, "help_next", next_page=page + 1))
        return "\n".join(lines)

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

        self.last_text_channel[guild_id] = text_channel
        parts = q.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "%help":
            lang = self.language.get(guild_id, "en")
            help_table = COMMAND_HELP.get(lang, COMMAND_HELP["en"])
            raw_target = arg.strip()
            if raw_target.startswith("--page"):
                raw_target = raw_target.removeprefix("--page").strip()

            if not raw_target:
                await self._delete_user_message(message)
                await text_channel.send(self.format_help_page(guild_id, page=1))
                return

            if raw_target.isdigit():
                await self._delete_user_message(message)
                await text_channel.send(
                    self.format_help_page(guild_id, page=int(raw_target))
                )
                return

            target = raw_target.lstrip("%").lower()
            if target not in help_table:
                await text_channel.send(self.t(guild_id, "help_unknown", cmd=target))
                return

            await self._delete_user_message(message)
            info = help_table[target]
            await text_channel.send(f"**`{info['usage']}`**\n\n{info['long']}")
            return

        if cmd == "%lang":
            target_lang = arg.strip().lower()
            if target_lang not in ("en", "ru"):
                await text_channel.send(self.t(guild_id, "lang_usage"))
                return
            await self._delete_user_message(message)
            self.language[guild_id] = target_lang
            self.save_languages()
            await text_channel.send(self.t(guild_id, "lang_set"))
            return

        if cmd == "%playing":
            await self._delete_user_message(message)
            current_item = self.now_playing_item.get(guild_id)
            voice_client = self.voice_client.get(guild_id)
            if current_item is None or not self.is_guild_active(voice_client):
                last_name = self.now_playing.get(guild_id)
                if not last_name or last_name == "None":
                    last_name = self.t(guild_id, "none_playing")
                await text_channel.send(
                    self.t(
                        guild_id,
                        "now_playing_none",
                        name=last_name,
                    )
                )
                return

            fallback_text, embed = self.build_playing_display(guild_id, current_item)
            await self._safe_send(
                text_channel,
                None,
                embed=embed,
                view=None,
                fallback_text=fallback_text,
            )
            return

        if cmd == "%play":
            await self.handle_play_command(message, arg.strip(), play_next=False)
            return

        if cmd == "%playnext":
            await self.handle_play_command(message, arg.strip(), play_next=True)
            return

        if cmd == "%cancel":
            await self._delete_user_message(message)
            prev_current = self.now_playing_item.get(guild_id)
            reply = await self.execute_cancel(guild_id, recreate_player=False)
            await text_channel.send(reply)
            if self.now_playing_item.get(guild_id) is not prev_current:
                if self.now_playing_item.get(guild_id) is not None:
                    await self.recreate_player_message(guild_id)
                else:
                    await self._delete_player_message(guild_id)
            return

        if cmd == "%remove":
            rem_arg = arg.strip()
            if not rem_arg:
                await self._delete_user_message(message)
                reply = await self.execute_remove(
                    guild_id, amount=None, recreate_player=False
                )
                await text_channel.send(reply)
                if self.now_playing_item.get(guild_id) is not None:
                    await self.recreate_player_message(guild_id)
                else:
                    await self._delete_player_message(guild_id)
                return
            try:
                amount = int(rem_arg)
                if amount == 0:
                    await text_channel.send(self.t(guild_id, "remove_zero_err"))
                    return
            except ValueError:
                await text_channel.send(self.t(guild_id, "remove_invalid"))
                return

            await self._delete_user_message(message)
            reply = await self.execute_remove(
                guild_id, amount=amount, recreate_player=False
            )
            await text_channel.send(reply)
            return

        if cmd == "%prev":
            voice_channel = await self.get_user_voice_channel(message)
            voice_client = self.voice_client.get(guild_id)
            if voice_client is None and voice_channel is not None:
                voice_client = await self.get_voice_client(voice_channel)

            prev_arg = arg.strip()
            if not prev_arg:
                amount = 1
            else:
                try:
                    amount = int(prev_arg)
                    if amount <= 0:
                        await text_channel.send(self.t(guild_id, "prev_pos_err"))
                        return
                except ValueError:
                    await text_channel.send(self.t(guild_id, "prev_invalid"))
                    return

            await self._delete_user_message(message)
            reply = await self.execute_prev(
                guild_id, amount=amount, recreate_player=False
            )
            await text_channel.send(reply)
            await self.recreate_player_message(guild_id)
            return

        if cmd == "%seek":
            seek_arg = arg.strip().removesuffix("s").strip()
            if not seek_arg:
                await text_channel.send(self.t(guild_id, "seek_usage"))
                return
            try:
                seconds = float(seek_arg)
            except ValueError:
                await text_channel.send(self.t(guild_id, "seek_invalid"))
                return

            await self._delete_user_message(message)
            reply = await self.execute_seek(guild_id, seconds)
            await text_channel.send(reply)
            return

        if cmd == "%download":
            await self._delete_user_message(message)
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

            await self._delete_user_message(message)
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

            await self._delete_user_message(message)
            reply = await self.execute_skip(
                guild_id, amount=amount, recreate_player=False
            )
            await text_channel.send(reply)
            if self.now_playing_item.get(guild_id) is not None:
                await self.recreate_player_message(guild_id)
            return

        if cmd == "%queue":
            raw_arg = arg.strip()
            if raw_arg.lower() == "clear":
                await self._delete_user_message(message)
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

            await self._delete_user_message(message)
            msg = self.format_queue_page(guild_id, page=page)
            await text_channel.send(msg)
            return

        if cmd == "%history":
            raw_arg = arg.strip()
            if raw_arg.lower() == "clear":
                await self._delete_user_message(message)
                self.history[guild_id].clear()
                await text_channel.send(self.t(guild_id, "history_cleared"))
                return

            if raw_arg.startswith("--page"):
                raw_arg = raw_arg.removeprefix("--page").strip()

            page = 1
            if raw_arg:
                if raw_arg.isdigit():
                    page = int(raw_arg)
                else:
                    await text_channel.send(self.t(guild_id, "history_usage"))
                    return
            if page <= 0:
                page = 1

            await self._delete_user_message(message)
            msg = self.format_history_page(guild_id, page=page)
            await text_channel.send(msg)
            return

        if cmd == "%shuffle":
            await self._delete_user_message(message)
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
            await self._delete_user_message(message)
            reply = await self.execute_leave(guild_id)
            await text_channel.send(reply)
            return

        if cmd == "%pause":
            await self._delete_user_message(message)
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
            await self._delete_user_message(message)
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
                await self._delete_user_message(message)
                current_vol = int(round(self.volume[guild_id] * 100))
                await text_channel.send(
                    self.t(guild_id, "vol_current", vol=current_vol, cmd=cmd)
                )
                return
            try:
                percentage = float(vol_str)
                if percentage < MIN_VOLUME_PERCENT:
                    await text_channel.send(self.t(guild_id, "vol_neg"))
                    return
                if percentage > MAX_VOLUME_PERCENT:
                    await text_channel.send(self.t(guild_id, "vol_max"))
                    return
            except ValueError:
                await text_channel.send(self.t(guild_id, "vol_invalid", cmd=cmd))
                return

            await self._delete_user_message(message)
            applied = self.set_guild_volume(guild_id, percentage)
            display_pct = int(applied) if applied.is_integer() else applied
            await text_channel.send(self.t(guild_id, "vol_set", vol=display_pct))
            return

        if cmd == "%save":
            playlist_name = arg.strip()
            if not playlist_name:
                await text_channel.send(self.t(guild_id, "save_usage"))
                return

            await self._delete_user_message(message)
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

    async def _advance_queue_async(
        self,
        guild_id: int,
        record_previous: bool = True,
        recreate_player: bool = True,
    ):
        async with self.play_locks[guild_id]:
            voice_client = self.voice_client.get(guild_id)
            if voice_client is None or not voice_client.is_connected():
                return
            if self.is_guild_active(voice_client):
                return

            if record_previous and self.now_playing_item.get(guild_id) is not None:
                self._record_item_in_history(guild_id, self.now_playing_item[guild_id])
                self.now_playing_item[guild_id] = None

            queue = self.queue.get(guild_id)
            if not queue:
                self.now_playing[guild_id] = "None"
                self.now_playing_item[guild_id] = None
                self.playback_state.pop(guild_id, None)
                await self._delete_player_message(guild_id)
                return

            next_item = queue.popleft()
            self.trigger_background_downloads(guild_id)
            if isinstance(next_item, str):
                next_item = QueueItem(
                    title=os.path.relpath(next_item, MUSIC_FOLDER).replace("\\", "/"),
                    local_path=next_item,
                )
            await self.ensure_item_downloaded(next_item)
            self.play(voice_client, next_item, record_history=False)
            if recreate_player:
                await self.recreate_player_message(guild_id)

    def play_next_song_in_queue(self, error=None):
        if error is not None:
            raise error

        for v in list(self.voice_clients):
            guild_id = v.guild.id
            if guild_id in self._suppress_after:
                continue
            if not self.is_guild_active(v):
                queue = self.queue.get(guild_id)
                loop = getattr(self, "loop", None)
                if queue:
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._advance_queue_async(
                                guild_id,
                                record_previous=True,
                                recreate_player=True,
                            ),
                            loop,
                        )
                    else:
                        if self.now_playing_item.get(guild_id) is not None:
                            self._record_item_in_history(
                                guild_id, self.now_playing_item[guild_id]
                            )
                            self.now_playing_item[guild_id] = None
                        item = queue.popleft()
                        self.play(v, item, record_history=False)
                else:
                    if self.now_playing_item.get(guild_id) is not None:
                        self._record_item_in_history(
                            guild_id, self.now_playing_item[guild_id]
                        )
                    self.now_playing[guild_id] = "None"
                    self.now_playing_item[guild_id] = None
                    self.playback_state.pop(guild_id, None)
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._delete_player_message(guild_id), loop
                        )

    def play(
        self,
        voice_client,
        item_or_path: QueueItem | str,
        start_at: float = 0.0,
        record_history: bool = False,
    ):
        guild_id = voice_client.guild.id
        if record_history and self.now_playing_item.get(guild_id) is not None:
            self._record_item_in_history(guild_id, self.now_playing_item[guild_id])

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

        self._apply_cached_metadata(item)
        if item.duration is None and path:
            item.duration = probe_audio_duration(path)

        start_offset = max(0.0, float(start_at))
        if start_offset > 0:
            try:
                raw_audio = discord.FFmpegPCMAudio(
                    path, before_options=f"-ss {start_offset}"
                )
            except TypeError:
                raw_audio = discord.FFmpegPCMAudio(path)
        else:
            raw_audio = discord.FFmpegPCMAudio(path)

        vol = max(
            MIN_VOLUME_PERCENT / 100.0,
            min(MAX_VOLUME_PERCENT / 100.0, self.volume[guild_id]),
        )
        self.volume[guild_id] = vol
        audio = discord.PCMVolumeTransformer(raw_audio, volume=vol)
        voice_client.play(audio, after=self.play_next_song_in_queue)

        display_name = item.display_name(MUSIC_FOLDER)
        self.now_playing[guild_id] = display_name
        self.now_playing_item[guild_id] = item
        self.playback_state[guild_id] = {
            "start_time": time.monotonic() - start_offset,
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
