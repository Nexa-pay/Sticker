# -*- coding: utf-8 -*-
import asyncio
import gzip
import json
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageOps
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import StickerFormat
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramServerError,
)
from aiogram.types import (
    FSInputFile,
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
)
from motor.motor_asyncio import AsyncIOMotorClient

# ============================================================
# CONFIGURATION
# ============================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_NAME = os.getenv("DB_NAME", "sticker_bot").strip()

ADMIN_USERNAMES = {"irtzaakash", "itzaakash"}

# ---- Public mode & branding -------------------------------------------------
# PUBLIC_MODE=True  -> anyone can create / clone packs (admin tools stay private)
PUBLIC_MODE = os.getenv("PUBLIC_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}
# Every pack title becomes "<name> : @stickerclubs"
BRAND_TAG = os.getenv("BRAND_TAG", "@stickerclubs").strip() or "@stickerclubs"
TITLE_SUFFIX = f" : {BRAND_TAG}"
MAX_USER_TITLE = 64 - len(TITLE_SUFFIX)
DEFAULT_EMOJI = "😀"
# Showcase pack: 1 sticker from every pack anyone creates is auto-added here
COLLECTION_ENABLED = os.getenv("COLLECTION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
COLLECTION_TITLE = os.getenv("COLLECTION_TITLE", "All Packs").strip() or "All Packs"
# Channel auto-post: 1 sticker from every new pack is posted here.
# Use @channelusername or the numeric -100... id. Bot must be admin. Set "off" to disable.
POST_CHANNEL = os.getenv("POST_CHANNEL", BRAND_TAG).strip()
VIDEO_EXTS = {".webm", ".mp4", ".mov", ".mkv", ".gif", ".avi", ".m4v", ".mpeg", ".mpg"}

# Optional Telegram custom emoji IDs for button icons. Leave blank until configured.
BUTTON_STYLES_ENABLED = True
EMOJI_PREMIUM = os.getenv("EMOJI_PREMIUM", "").strip()
EMOJI_STATS = os.getenv("EMOJI_STATS", "").strip()
EMOJI_BROADCAST = os.getenv("EMOJI_BROADCAST", "").strip()
EMOJI_ADMIN = os.getenv("EMOJI_ADMIN", "").strip()
EMOJI_REMOVE = os.getenv("EMOJI_REMOVE", "").strip()
EMOJI_STICKER = os.getenv("EMOJI_STICKER", "").strip()
EMOJI_CUSTOM = os.getenv("EMOJI_CUSTOM", "").strip()

STYLE_PRIMARY = "primary"
STYLE_SUCCESS = "success"
STYLE_DANGER = "danger"

# Upload and ZIP limits
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ZIP_FILES = 200
MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024

# Telegram sticker limits
STATIC_STICKER_SIZE = 512
CUSTOM_EMOJI_SIZE = 100
MAX_STATIC_STICKER_BYTES = 512 * 1024
MAX_VIDEO_STICKER_BYTES = 256 * 1024
MAX_TGS_BYTES = 64 * 1024
MAX_REGULAR_PACK_ITEMS = 120
MAX_CUSTOM_EMOJI_PACK_ITEMS = 200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("elite_creator")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing from .env")
if OWNER_ID <= 0:
    raise RuntimeError("Set your numeric OWNER_ID in .env")

FFMPEG = shutil.which("ffmpeg")

# ============================================================
# BOT AND DATABASE
# ============================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
mongo_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo_client[DB_NAME]
users_col = db["users"]
settings_col = db["settings"]

# ============================================================
# FSM STATES
# ============================================================
class CloneStates(StatesGroup):
    waiting_for_sticker = State()
    waiting_for_pack_type = State()
    waiting_for_title = State()


class AdminStates(StatesGroup):
    waiting_for_new_admin = State()
    waiting_for_remove_admin = State()
    waiting_for_broadcast = State()


class EmojiIDStates(StatesGroup):
    waiting_for_emoji_message = State()


# ============================================================
# GENERAL HELPERS
# ============================================================
def clean_name(value: str) -> str:
    """Remove unsupported characters from a pack-name fragment."""
    return re.sub(r"[^a-zA-Z0-9]", "", value or "")


def progress_bar(current: int, total: int, length: int = 15) -> str:
    percent = min(current / total, 1.0) if total else 0.0
    filled = int(length * percent)
    return f"[{'█' * filled}{'░' * (length - filled)}] {percent:.0%}"


async def init_user(user_id: int, username: str | None):
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        await users_col.insert_one({
            "user_id": user_id,
            "username": username,
            "role": "owner" if user_id == OWNER_ID else "user",
            "created_at": int(time.time()),
        })
        return
    updates = {"username": username}
    if user_id == OWNER_ID:
        updates["role"] = "owner"
    await users_col.update_one({"user_id": user_id}, {"$set": updates})


async def get_role(user_id: int, username: str | None = None) -> str:
    if user_id == OWNER_ID:
        return "owner"
    if username and username.lower() in ADMIN_USERNAMES:
        return "admin"
    user = await users_col.find_one({"user_id": user_id})
    return user.get("role", "user") if user else "user"


async def allowed(user_id: int, username: str | None = None) -> bool:
    return await get_role(user_id, username) in {"owner", "admin"}


async def can_create(user_id: int, username: str | None = None) -> bool:
    """Creating / cloning packs: everyone in public mode, admins otherwise."""
    return PUBLIC_MODE or await allowed(user_id, username)


async def ensure_user(message: types.Message):
    if message.from_user:
        await init_user(message.from_user.id, message.from_user.username)


def media_kind_from_sticker(sticker) -> str:
    if getattr(sticker, "is_video", False):
        return "video"
    if getattr(sticker, "is_animated", False):
        return "animated"
    return "static"


# ============================================================
# INLINE BUTTON HELPERS
# ============================================================
def make_button(
    text: str,
    callback_data: str | None = None,
    style: str | None = None,
    emoji_id: str | None = None,
    url: str | None = None,
) -> InlineKeyboardButton:
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if BUTTON_STYLES_ENABLED and style:
        kwargs["style"] = style
    if emoji_id and emoji_id.isdigit():
        kwargs["icon_custom_emoji_id"] = emoji_id
    try:
        return InlineKeyboardButton(**kwargs)
    except (TypeError, ValueError):
        kwargs.pop("style", None)
        kwargs.pop("icon_custom_emoji_id", None)
        return InlineKeyboardButton(**kwargs)


def admin_keyboard(role: str):
    rows = [
        [make_button(" Premium Stats", "admin_stats", STYLE_PRIMARY, EMOJI_STATS or None)],
        [make_button(" Broadcast Message", "admin_broadcast", STYLE_SUCCESS, EMOJI_BROADCAST or None)],
        [make_button(" Create Sticker/Emoji Pack", "menu_create", STYLE_PRIMARY, EMOJI_STICKER or None)],
        [make_button(" Get Custom Emoji ID", "menu_emoji_id", STYLE_PRIMARY, EMOJI_CUSTOM or EMOJI_PREMIUM or None)],
    ]
    if role == "owner":
        rows.append([
            make_button(" Add VIP Admin", "admin_add", STYLE_SUCCESS, EMOJI_ADMIN or None),
            make_button(" Revoke VIP", "admin_remove", STYLE_DANGER, EMOJI_REMOVE or None),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pack_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [make_button(" Standard Sticker Pack", "type_regular", STYLE_SUCCESS, EMOJI_STICKER or None)],
        [make_button(" Custom Emoji Pack", "type_custom_emoji", STYLE_PRIMARY, EMOJI_CUSTOM or EMOJI_PREMIUM or None)],
    ])


def cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [make_button("❌ Cancel", "cancel_operation", STYLE_DANGER)]
    ])


def user_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [make_button(" Create / Clone Sticker Pack", "menu_create", STYLE_SUCCESS, EMOJI_STICKER or None)],
        [make_button(" Get Custom Emoji ID", "menu_emoji_id", STYLE_PRIMARY, EMOJI_CUSTOM or EMOJI_PREMIUM or None)],
    ])


def home_keyboard(role: str):
    return admin_keyboard(role) if role in {"owner", "admin"} else user_keyboard()


# ============================================================
# DOWNLOAD / SOURCE HELPERS
# ============================================================
async def download_telegram_file(file_id: str, destination: str) -> str:
    info = await bot.get_file(file_id)
    if info.file_size and info.file_size > MAX_UPLOAD_BYTES:
        raise ValueError("Telegram source file is larger than 20 MB.")
    await bot.download_file(info.file_path, destination=destination)
    return destination


async def save_message_media(message: types.Message, temp_dir: str) -> tuple[str, str]:
    if message.photo:
        path = os.path.join(temp_dir, f"direct_{time.time_ns()}.jpg")
        await download_telegram_file(message.photo[-1].file_id, path)
        return path, "static"

    if message.video:
        name = message.video.file_name or "upload.mp4"
        ext = Path(name).suffix.lower() or ".mp4"
        if ext not in {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg"}:
            ext = ".mp4"
        path = os.path.join(temp_dir, f"direct_{time.time_ns()}{ext}")
        await download_telegram_file(message.video.file_id, path)
        return path, "video"

    if message.animation:
        name = message.animation.file_name or "upload.gif"
        ext = Path(name).suffix.lower() or ".gif"
        if ext not in {".gif", ".mp4", ".webm"}:
            ext = ".gif"
        path = os.path.join(temp_dir, f"direct_{time.time_ns()}{ext}")
        await download_telegram_file(message.animation.file_id, path)
        return path, "video"

    if message.document:
        name = message.document.file_name or "upload.bin"
        ext = Path(name).suffix.lower()
        mime = (message.document.mime_type or "").lower()
        if ext == ".zip" or mime in {"application/zip", "application/x-zip-compressed"}:
            path = os.path.join(temp_dir, f"upload_{time.time_ns()}.zip")
            await download_telegram_file(message.document.file_id, path)
            return path, "zip"
        if ext == ".tgs":
            path = os.path.join(temp_dir, f"direct_{time.time_ns()}.tgs")
            await download_telegram_file(message.document.file_id, path)
            return path, "animated"
        if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp"} or mime.startswith("image/"):
            safe_ext = ext if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp"} else ".png"
            path = os.path.join(temp_dir, f"direct_{time.time_ns()}{safe_ext}")
            await download_telegram_file(message.document.file_id, path)
            return path, "static"
        if ext in {".webm", ".mp4", ".mov", ".mkv", ".gif", ".avi", ".m4v", ".mpeg", ".mpg"} or mime.startswith("video/"):
            safe_ext = ext if ext in {".webm", ".mp4", ".mov", ".mkv", ".gif", ".avi", ".m4v", ".mpeg", ".mpg"} else ".mp4"
            path = os.path.join(temp_dir, f"direct_{time.time_ns()}{safe_ext}")
            await download_telegram_file(message.document.file_id, path)
            return path, "video"
    raise ValueError(
        "Send a sticker, custom emoji message, ZIP file, image, TGS animation, video, or GIF."
    )


# ============================================================
# IMAGE CONVERSION
# ============================================================
def convert_image(input_path: str, output_dir: str, custom_emoji: bool = False) -> str:
    os.makedirs(output_dir, exist_ok=True)
    target = CUSTOM_EMOJI_SIZE if custom_emoji else STATIC_STICKER_SIZE
    output_path = os.path.join(output_dir, f"converted_{time.time_ns()}.webp")

    try:
        with Image.open(input_path) as source:
            image = ImageOps.exif_transpose(source).convert("RGBA")
            image.thumbnail((target, target), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (target, target), (0, 0, 0, 0))
            canvas.alpha_composite(image, ((target - image.width) // 2, (target - image.height) // 2))

            out = tempfile.NamedTemporaryFile(suffix=".webp", dir=output_dir, delete=False)
            temp_output = out.name
            out.close()
            canvas.save(temp_output, "WEBP", quality=90, method=6)
            if os.path.getsize(temp_output) > MAX_STATIC_STICKER_BYTES:
                canvas.save(temp_output, "WEBP", quality=72, method=5)
            if os.path.getsize(temp_output) > MAX_STATIC_STICKER_BYTES:
                os.unlink(temp_output)
                raise ValueError("Converted WebP exceeds Telegram's 512 KB static sticker limit.")
            os.replace(temp_output, output_path)
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Could not read this image: {exc}") from exc
    return output_path


# ============================================================
# TGS VALIDATION
# ============================================================
def validate_tgs(path: str, custom_emoji: bool = False):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as file:
            data = json.load(file)
    except Exception as exc:
        raise ValueError(f"Invalid TGS animation: {exc}") from exc

    width, height = data.get("w"), data.get("h")
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        raise ValueError("TGS animation has no valid width/height.")
    expected = CUSTOM_EMOJI_SIZE if custom_emoji else STATIC_STICKER_SIZE
    if width != expected or height != expected:
        raise ValueError(
            f"TGS canvas is {width}x{height}; this pack type requires "
            f"{expected}x{expected}. TGS files are not automatically resized."
        )
    if os.path.getsize(path) > MAX_TGS_BYTES:
        raise ValueError("TGS file exceeds Telegram's 64 KB animation limit.")


# ============================================================
# VIDEO CONVERSION
# ============================================================
async def convert_video(input_path: str, output_dir: str, custom_emoji: bool = False) -> str:
    if not shutil.which("ffmpeg"):
        raise ValueError("FFmpeg is not installed. Run: sudo apt-get install -y ffmpeg")

    os.makedirs(output_dir, exist_ok=True)
    target = CUSTOM_EMOJI_SIZE if custom_emoji else STATIC_STICKER_SIZE
    output_path = os.path.join(output_dir, f"video_{time.time_ns()}.webm")
    video_filter = (
        f"scale={target}:{target}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={target}:{target}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
        "setsar=1,fps=30,format=yuva420p"
    )

    async def encode(crf: int, deadline: str, cpu_used: int, bitrate: str):
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", input_path,
            "-t", "3",
            "-an", "-sn", "-dn",
            "-vf", video_filter,
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-b:v", bitrate,
            "-crf", str(crf),
            "-deadline", deadline,
            "-cpu-used", str(cpu_used),
            "-auto-alt-ref", "0",
            "-metadata:s:v:0", "alpha_mode=1",
            "-f", "webm", output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise ValueError("Video conversion timed out. Try a shorter/smaller source.")
        if process.returncode != 0:
            raise ValueError("FFmpeg conversion failed: " + stderr.decode(errors="replace")[-900:])

    await encode(crf=38, deadline="good", cpu_used=4, bitrate="0")
    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        raise ValueError("FFmpeg did not produce a WebM file.")

    if os.path.getsize(output_path) > MAX_VIDEO_STICKER_BYTES:
        await encode(crf=48, deadline="realtime", cpu_used=8, bitrate="90K")

    if os.path.getsize(output_path) > MAX_VIDEO_STICKER_BYTES:
        raise ValueError(
            "Converted WebM is still larger than Telegram's 256 KB limit. "
            "Trim the video or use a simpler clip."
        )
    return output_path


# ============================================================
# ZIP HANDLING
# ============================================================
def safe_extract_zip(zip_path: str, destination: str):
    with zipfile.ZipFile(zip_path, "r") as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_FILES:
            raise ValueError(f"ZIP contains too many entries. Maximum: {MAX_ZIP_FILES}.")
        total_size = sum(item.file_size for item in entries)
        if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ValueError("ZIP is too large after extraction.")
        root = os.path.realpath(destination)
        for item in entries:
            target = os.path.realpath(os.path.join(destination, item.filename))
            if not target.startswith(root + os.sep) and target != root:
                raise ValueError("ZIP contains an unsafe file path.")
        archive.extractall(destination)


def find_media_files(directory: str):
    supported = {
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tgs",
        ".webm", ".mp4", ".mov", ".mkv", ".gif", ".avi", ".m4v", ".mpeg", ".mpg",
    }
    result = []
    for root, _, files in os.walk(directory):
        for filename in files:
            if filename.lower() == "stickers.zip":
                continue
            if Path(filename).suffix.lower() in supported:
                result.append(os.path.join(root, filename))
    return sorted(result)


# ============================================================
# SOURCE PREPARATION (MIXED FORMAT UNLOCKED)
# ============================================================
async def prepare_source(message: types.Message, state: FSMContext):
    temp_dir = tempfile.mkdtemp(prefix="elite_creator_")
    try:
        set_name = None
        if message.sticker:
            set_name = message.sticker.set_name
            if not set_name:
                raise ValueError("This sticker does not belong to a sticker set.")
        elif message.text or message.caption:
            link = re.search(r"t\.me/(?:addstickers|addemoji)/([A-Za-z0-9_]+)", message.text or message.caption)
            if link:
                set_name = link.group(1)

        if set_name:
            try:
                sticker_set = await tg_call(lambda: bot.get_sticker_set(name=set_name))
            except TelegramBadRequest as exc:
                raise ValueError(f"Pack not found: {set_name}") from exc
            stickers = sticker_set.stickers
            if not stickers:
                raise ValueError("The sticker set is empty.")

            await state.update_data(
                original_stickers=stickers,
                source_type="pack",
                sticker_format="mixed",
                temp_dir=temp_dir,
                source_set_type=getattr(sticker_set, "sticker_type", None),
                source_set_title=sticker_set.title,
                needs_repainting=any(getattr(s, "needs_repainting", False) for s in stickers),
            )
            return len(stickers), "mixed"

        if message.document and message.document.file_name and message.document.file_name.lower().endswith(".zip"):
            zip_path = os.path.join(temp_dir, "stickers.zip")
            await download_telegram_file(message.document.file_id, zip_path)
            safe_extract_zip(zip_path, temp_dir)
            paths = find_media_files(temp_dir)
            if not paths:
                raise ValueError("ZIP contains no supported sticker files.")
            
            await state.update_data(
                original_stickers=paths,
                source_type="zip",
                sticker_format="mixed",
                temp_dir=temp_dir,
            )
            return len(paths), "mixed"

        entities = list(message.entities or []) + list(message.caption_entities or [])
        custom_ids = list(dict.fromkeys(
            entity.custom_emoji_id
            for entity in entities
            if entity.type == "custom_emoji" and entity.custom_emoji_id
        ))
        if custom_ids:
            stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=custom_ids)
            if not stickers:
                raise ValueError("Telegram returned no custom emoji stickers.")
            
            await state.update_data(
                original_stickers=stickers,
                source_type="custom_emoji",
                sticker_format="mixed",
                temp_dir=temp_dir,
            )
            return len(stickers), "mixed"

        path, sticker_format = await save_message_media(message, temp_dir)
        if sticker_format == "zip":
            safe_extract_zip(path, temp_dir)
            paths = find_media_files(temp_dir)
            if not paths:
                raise ValueError("ZIP contains no supported sticker files.")
            
            await state.update_data(
                original_stickers=paths,
                source_type="zip",
                sticker_format="mixed",
                temp_dir=temp_dir,
            )
            return len(paths), "mixed"

        await state.update_data(
            original_stickers=[path],
            source_type="files",
            sticker_format=sticker_format,
            temp_dir=temp_dir,
        )
        return 1, sticker_format

    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


# ============================================================
# START
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await ensure_user(message)
    role = await get_role(message.from_user.id, message.from_user.username)
    if not await can_create(message.from_user.id, message.from_user.username):
        await message.answer("🚫 Access denied. This is a private bot.")
        return
    await state.clear()
    await message.answer(
        "✨ ELITE CREATOR\n\n"
        "Create or clone a sticker pack or a custom emoji pack.\n"
        f"Every pack is named like: happy{TITLE_SUFFIX}\n\n"
        "Sources supported:\n"
        "• Existing sticker packs\n"
        "• PNG/JPG/WebP images\n"
        "• TGS animations\n"
        "• WebM/MP4/video/GIF files\n"
        "• ZIP archives containing media files\n"
        "• Messages containing Telegram custom emojis\n\n"
        "Tip: send any sticker (or a t.me/addstickers/... link) to clone the whole pack.\n\n"
        "Choose an action below.",
        reply_markup=home_keyboard(role),
    )


# ============================================================
# ADMIN PANEL
# ============================================================
@dp.message(Command("panel", "admin"))
async def cmd_panel(message: types.Message):
    await ensure_user(message)
    role = await get_role(message.from_user.id, message.from_user.username)
    if role not in {"owner", "admin"}:
        await message.answer("🚫 Access denied.")
        return
    await message.answer("💎 ELITE ADMIN PANEL", reply_markup=admin_keyboard(role))


# ============================================================
# CANCEL
# ============================================================
@dp.message(Command("cancel"))
async def cancel_command(message: types.Message, state: FSMContext):
    data = await state.get_data()
    temp_dir = data.get("temp_dir")
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
    await state.clear()
    await message.answer("❌ Operation cancelled.")


@dp.callback_query(F.data == "cancel_operation")
async def cancel_callback(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    temp_dir = data.get("temp_dir")
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
    await state.clear()
    await callback.answer("Cancelled.")
    await callback.message.answer("❌ Operation cancelled.")


# ============================================================
# CUSTOM EMOJI ID BUTTON + COMMAND
# ============================================================
@dp.callback_query(F.data == "menu_emoji_id")
async def menu_emoji_id(callback: types.CallbackQuery, state: FSMContext):
    if not await can_create(callback.from_user.id, callback.from_user.username):
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.set_state(EmojiIDStates.waiting_for_emoji_message)
    await callback.message.answer(
        "💎 <b>Get Telegram Custom Emoji ID</b>\n\n"
        "Send a message containing the actual Telegram Premium custom emoji(s).\n"
        "Regular Unicode emoji like 😀 or 🔥 do not have custom emoji IDs.\n\n"
        "You can also reply to a message containing custom emojis with /emojiid.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


def extract_custom_emoji_ids(message: types.Message) -> list[str]:
    entities = list(message.entities or []) + list(message.caption_entities or [])
    ids = [
        entity.custom_emoji_id
        for entity in entities
        if entity.type == "custom_emoji" and getattr(entity, "custom_emoji_id", None)
    ]
    return list(dict.fromkeys(ids))


@dp.message(EmojiIDStates.waiting_for_emoji_message)
async def receive_emoji_for_id(message: types.Message, state: FSMContext):
    ids = extract_custom_emoji_ids(message)
    await state.clear()
    if not ids:
        await message.answer(
            "❌ No Telegram custom emoji ID found.\n\n"
            "Send the actual custom emoji from Telegram's custom emoji panel, "
            "not a regular Unicode emoji.",
            reply_markup=home_keyboard(await get_role(message.from_user.id, message.from_user.username)),
        )
        return
    result = "\n".join(f"{index}. <code>{emoji_id}</code>" for index, emoji_id in enumerate(ids, 1))
    await message.answer(
        "💎 <b>Custom Emoji ID(s)</b>\n\n"
        f"{result}\n\n"
        "Example .env setting:\n<code>EMOJI_CUSTOM=1234567890123456789</code>\n\n"
        "Restart the bot after editing .env.",
        parse_mode="HTML",
        reply_markup=home_keyboard(await get_role(message.from_user.id, message.from_user.username)),
    )


@dp.message(Command("emojiid"))
async def cmd_emojiid(message: types.Message):
    """Use /emojiid as a reply to the message containing custom emoji(s)."""
    if not await can_create(message.from_user.id, message.from_user.username):
        await message.answer("🚫 Access denied.")
        return
    target = message.reply_to_message
    if target is None:
        await message.answer(
            "Reply to a message containing Telegram custom emoji(s) with /emojiid."
        )
        return
    ids = extract_custom_emoji_ids(target)
    if not ids:
        await message.answer("❌ The replied-to message contains no custom emoji entities.")
        return
    result = "\n".join(f"{index}. <code>{emoji_id}</code>" for index, emoji_id in enumerate(ids, 1))
    await message.answer(f"💎 <b>Custom Emoji ID(s)</b>\n\n{result}", parse_mode="HTML")


# ============================================================
# ADMIN CALLBACKS
# ============================================================
@dp.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(callback: types.CallbackQuery, state: FSMContext):
    role = await get_role(callback.from_user.id, callback.from_user.username)
    if role not in {"owner", "admin"}:
        await callback.answer("Access denied.", show_alert=True)
        return

    action = callback.data.removeprefix("admin_")
    if action == "stats":
        total = await users_col.count_documents({})
        admins = await users_col.count_documents({"role": "admin"})
        await callback.message.answer(
            "💎 PREMIUM STATISTICS\n\n"
            f"👥 Registered users: {total}\n"
            f"👑 VIP admins: {admins}",
            reply_markup=admin_keyboard(role),
        )
    elif action == "broadcast":
        await state.set_state(AdminStates.waiting_for_broadcast)
        await callback.message.answer(
            "📢 Send the message you want to broadcast.\n"
            "Text, photos, videos and other copyable messages are supported.\n\n"
            "Use /cancel to stop.",
            reply_markup=cancel_keyboard(),
        )
    elif action == "add" and role == "owner":
        await state.set_state(AdminStates.waiting_for_new_admin)
        await callback.message.answer("👑 Send the numeric Telegram user ID to promote.", reply_markup=cancel_keyboard())
    elif action == "remove" and role == "owner":
        await state.set_state(AdminStates.waiting_for_remove_admin)
        await callback.message.answer("❌ Send the numeric Telegram user ID to demote.", reply_markup=cancel_keyboard())
    await callback.answer()


# ============================================================
# ADD VIP ADMIN
# ============================================================
@dp.message(AdminStates.waiting_for_new_admin, F.text)
async def add_admin(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        await state.clear()
        await message.answer("🚫 Owner access required.")
        return
    try:
        target_id = int(message.text.strip())
        if target_id <= 0:
            raise ValueError
        await users_col.update_one(
            {"user_id": target_id},
            {
                "$set": {"role": "admin"},
                "$setOnInsert": {"username": None, "created_at": int(time.time())},
            },
            upsert=True,
        )
        await message.answer(f"👑 User {target_id} is now a VIP admin.")
    except ValueError:
        await message.answer("❌ Enter a valid numeric Telegram user ID.")
    await state.clear()


# ============================================================
# REMOVE VIP ADMIN
# ============================================================
@dp.message(AdminStates.waiting_for_remove_admin, F.text)
async def remove_admin(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        await state.clear()
        await message.answer("🚫 Owner access required.")
        return
    try:
        target_id = int(message.text.strip())
        if target_id <= 0:
            raise ValueError
        if target_id == OWNER_ID:
            await message.answer("❌ The owner cannot be demoted.")
        else:
            await users_col.update_one({"user_id": target_id}, {"$set": {"role": "user"}})
            await message.answer(f"❌ User {target_id} has been demoted.")
    except ValueError:
        await message.answer("❌ Invalid user ID.")
    await state.clear()


# ============================================================
# BROADCAST
# ============================================================
@dp.message(AdminStates.waiting_for_broadcast)
async def broadcast_message(message: types.Message, state: FSMContext):
    role = await get_role(message.from_user.id, message.from_user.username)
    if role not in {"owner", "admin"}:
        await state.clear()
        await message.answer("🚫 Access denied.")
        return
    await state.clear()
    status = await message.answer("📢 Broadcasting in progress...")
    users = await users_col.find({}, {"user_id": 1}).to_list(length=None)
    success = failed = 0

    for user in users:
        user_id = user.get("user_id")
        if not user_id:
            continue
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            success += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                success += 1
            except Exception:
                failed += 1
        except (TelegramForbiddenError, TelegramAPIError):
            failed += 1
        await asyncio.sleep(0.05)

    await status.edit_text(f"📢 BROADCAST COMPLETE\n\n✅ Sent: {success}\n❌ Failed: {failed}")


# ============================================================
# START CREATION
# ============================================================
@dp.callback_query(F.data == "menu_create")
async def menu_create(callback: types.CallbackQuery, state: FSMContext):
    if not await can_create(callback.from_user.id, callback.from_user.username):
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.clear()
    await state.set_state(CloneStates.waiting_for_sticker)
    await callback.message.answer(
        "📦 Send any one of these:\n"
        "• A sticker from an existing pack (clones the WHOLE pack)\n"
        "• A pack link: t.me/addstickers/NAME or t.me/addemoji/NAME\n"
        "• A message containing custom emoji(s)\n"
        "• A ZIP with sticker media\n"
        "• A direct image, TGS, video, or GIF file\n\n"
        "Use /cancel to stop.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


# ============================================================
# PROCESS SOURCE
# ============================================================
@dp.message(CloneStates.waiting_for_sticker)
async def process_source(message: types.Message, state: FSMContext):
    if not await can_create(message.from_user.id, message.from_user.username):
        return

    status = await message.answer("🔍 Inspecting source...")
    try:
        count, sticker_format = await prepare_source(message, state)
        data = await state.get_data()
        src_type = data.get("source_set_type")
        src_line = ""
        if src_type:
            nice = "Custom Emoji" if src_type == "custom_emoji" else "Standard Sticker"
            src_line = f"📁 Source: {data.get('source_set_title', '')} ({nice})\n"
        await status.edit_text(
            "📦 SOURCE READY\n\n"
            f"{src_line}"
            f"📊 Items: {count}\n"
            f"🎨 Format: {sticker_format.title()}\n\n"
            "Choose the destination pack type.\n"
            "Tip: pick the SAME type as the source for an instant, lossless clone.",
            reply_markup=pack_type_keyboard(),
        )
        await state.set_state(CloneStates.waiting_for_pack_type)
    except Exception as exc:
        logger.exception("Source preparation failed")
        await status.edit_text(f"❌ Source error:\n{str(exc)[:1200]}")


# ============================================================
# SELECT PACK TYPE
# ============================================================
@dp.callback_query(CloneStates.waiting_for_pack_type, F.data.startswith("type_"))
async def select_pack_type(callback: types.CallbackQuery, state: FSMContext):
    if not await can_create(callback.from_user.id, callback.from_user.username):
        await callback.answer("Access denied.", show_alert=True)
        return

    sticker_type = "custom_emoji" if callback.data == "type_custom_emoji" else "regular"
        
    await state.update_data(sticker_type=sticker_type)
    await state.set_state(CloneStates.waiting_for_title)
    await callback.message.answer(
        "📝 Send a name for your pack, e.g. <b>happy</b>\n\n"
        f"It will be saved as: <b>happy{TITLE_SUFFIX}</b>\n"
        "The pack link is created automatically.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


# ============================================================
# PACK TITLE  ->  "<title> : @stickerclubs"  (link name is automatic)
# ============================================================
@dp.message(CloneStates.waiting_for_title, F.text)
async def process_title(message: types.Message, state: FSMContext):
    raw = strip_brand(message.text)
    if not raw:
        await message.answer("❌ Send a name for your pack, e.g. happy")
        return
    if len(raw) > MAX_USER_TITLE:
        await message.answer(
            f"❌ Keep the name under {MAX_USER_TITLE} characters "
            f"(\"{TITLE_SUFFIX.strip()}\" is added automatically)."
        )
        return
    await state.update_data(new_title=raw)
    await run_clone(message, state)


# ============================================================
# BRANDING + PACK NAME HELPERS
# ============================================================
def strip_brand(text: str) -> str:
    """Remove any @stickerclubs tag the user typed so it is never doubled."""
    value = (text or "").strip()
    value = re.sub(rf"\s*[:|\-–]*\s*{re.escape(BRAND_TAG)}\s*$", "", value, flags=re.IGNORECASE)
    return value.strip(" :|-–")


def brand_title(base: str, part: int = 1) -> str:
    base = strip_brand(base) or "Stickers"
    label = f"{base} ({part})" if part > 1 else base
    room = 64 - len(TITLE_SUFFIX)
    return f"{label[:room].rstrip()}{TITLE_SUFFIX}"


def make_short_name(title: str, bot_username: str) -> str:
    """Telegram link name: <slug>_<brand>_<random>_by_<bot>, max 64 chars."""
    slug = clean_name(title).lower() or "pack"
    if not slug[0].isalpha():
        slug = "p" + slug
    brand = clean_name(BRAND_TAG).lower()
    rand = format(time.time_ns() % (36 ** 5), "x")[-5:]
    suffix = f"_{brand}_{rand}_by_{bot_username}"
    if len(suffix) + 1 > 64:  # very long bot username: drop brand fragment
        suffix = f"_{rand}_by_{bot_username}"
    return f"{slug[:64 - len(suffix)]}{suffix}"


def pack_url_for(name: str, sticker_type: str) -> str:
    kind = "addemoji" if sticker_type == "custom_emoji" else "addstickers"
    return f"https://t.me/{kind}/{name}"


# ============================================================
# RESILIENT TELEGRAM CALLS
# ============================================================
FATAL_ERRORS = ("PEER_ID_INVALID", "USER_IS_BOT", "BOT_BLOCKED", "user not found")
PACK_FULL_ERRORS = ("STICKERS_TOO_MUCH", "STICKERSET_FULL")


async def tg_call(factory, attempts: int = 6):
    """Run a Telegram request, riding out flood waits and network/server hiccups."""
    delay = 2
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError):
            if attempt == attempts:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError("Telegram did not respond after retries.")


def sticker_emoji(item) -> str:
    emoji = getattr(item, "emoji", None)
    return emoji if emoji else DEFAULT_EMOJI


def sticker_format_enum(kind: str):
    return {
        "static": StickerFormat.STATIC,
        "video": StickerFormat.VIDEO,
        "animated": StickerFormat.ANIMATED,
    }[kind]


def scale_tgs(path: str, output_dir: str, target: int) -> str:
    """Resize a Lottie/TGS canvas by wrapping it in a scaled pre-composition."""
    with gzip.open(path, "rt", encoding="utf-8") as file:
        data = json.load(file)
    w, h = data.get("w"), data.get("h")
    if not w or not h:
        raise ValueError("TGS has no canvas size.")
    scale = target / max(w, h) * 100
    asset_id = f"scaled_{time.time_ns()}"
    layer = {
        "ddd": 0, "ind": 1, "ty": 0, "refId": asset_id, "sr": 1, "ao": 0, "bm": 0,
        "w": w, "h": h, "ip": data.get("ip", 0), "op": data.get("op", 60), "st": 0,
        "ks": {
            "o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0},
            "p": {"a": 0, "k": [target / 2, target / 2, 0]},
            "a": {"a": 0, "k": [w / 2, h / 2, 0]},
            "s": {"a": 0, "k": [scale, scale, 100]},
        },
    }
    assets = list(data.get("assets", []))
    assets.append({"id": asset_id, "layers": data.get("layers", [])})
    data.update({"w": target, "h": target, "layers": [layer], "assets": assets})
    out = os.path.join(output_dir, f"scaled_{time.time_ns()}.tgs")
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as file:
        json.dump(data, file, separators=(",", ":"))
    if os.path.getsize(out) > MAX_TGS_BYTES:
        raise ValueError("Scaled TGS exceeds 64 KB.")
    return out


async def video_to_static(path: str, output_dir: str, custom_emoji: bool) -> str:
    """Last-resort fallback: grab one frame of a video sticker as a static sticker."""
    if not FFMPEG:
        raise ValueError("FFmpeg missing for static fallback.")
    frame = os.path.join(output_dir, f"frame_{time.time_ns()}.png")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-c:v", "libvpx-vp9", "-i", path, "-frames:v", "1", frame,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=60)
    if not os.path.isfile(frame):  # retry without forcing the alpha-capable decoder
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", path, "-frames:v", "1", frame,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=60)
    if not os.path.isfile(frame):
        raise ValueError("Could not extract a frame.")
    return convert_image(frame, output_dir, custom_emoji=custom_emoji)


# ============================================================
# PER-STICKER ATTEMPT LADDER
# Each item is tried several ways; one bad sticker never stops the clone.
# ============================================================
def build_attempts(item, index: int, source_type: str, sticker_type: str, temp_dir: str):
    custom_emoji = sticker_type == "custom_emoji"
    attempts = []

    if source_type in {"pack", "custom_emoji"}:
        kind = media_kind_from_sticker(item)
        emoji = sticker_emoji(item)
        same_type = getattr(item, "type", "regular") == sticker_type

        # 1) Re-use Telegram's own file: instant, lossless, no re-encoding.
        if same_type:
            async def via_file_id():
                return InputSticker(sticker=item.file_id, format=sticker_format_enum(kind), emoji_list=[emoji])
            attempts.append(("reuse", via_file_id))

        cache = {}

        async def download():
            if "src" not in cache:
                ext = {"static": ".webp", "video": ".webm", "animated": ".tgs"}[kind]
                cache["src"] = await tg_call(lambda: download_telegram_file(
                    item.file_id, os.path.join(temp_dir, f"source_{index}{ext}")))
            return cache["src"]

        # 2) Download + convert to the destination's exact spec.
        async def converted(emoji_value=emoji):
            src = await download()
            if kind == "static":
                return InputSticker(sticker=FSInputFile(convert_image(src, temp_dir, custom_emoji)),
                                    format=StickerFormat.STATIC, emoji_list=[emoji_value])
            if kind == "video":
                return InputSticker(sticker=FSInputFile(await convert_video(src, temp_dir, custom_emoji)),
                                    format=StickerFormat.VIDEO, emoji_list=[emoji_value])
            target = CUSTOM_EMOJI_SIZE if custom_emoji else STATIC_STICKER_SIZE
            try:
                validate_tgs(src, custom_emoji=custom_emoji)
                path = src
            except ValueError:
                path = scale_tgs(src, temp_dir, target)
            return InputSticker(sticker=FSInputFile(path), format=StickerFormat.ANIMATED, emoji_list=[emoji_value])

        attempts.append(("convert", converted))
        # 3) Same, but with a safe default emoji (fixes STICKER_EMOJI_INVALID).
        if emoji != DEFAULT_EMOJI:
            attempts.append(("convert+emoji", lambda: converted(DEFAULT_EMOJI)))
        # 4) Video that won't fit the limits -> keep it as a static sticker.
        if kind == "video":
            async def as_static():
                return InputSticker(sticker=FSInputFile(await video_to_static(await download(), temp_dir, custom_emoji)),
                                    format=StickerFormat.STATIC, emoji_list=[DEFAULT_EMOJI])
            attempts.append(("static-fallback", as_static))
        return attempts

    # Files from ZIP / direct uploads
    path = item
    ext = Path(path).suffix.lower()

    async def from_file():
        if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            return InputSticker(sticker=FSInputFile(convert_image(path, temp_dir, custom_emoji)),
                                format=StickerFormat.STATIC, emoji_list=[DEFAULT_EMOJI])
        if ext == ".tgs":
            target = CUSTOM_EMOJI_SIZE if custom_emoji else STATIC_STICKER_SIZE
            try:
                validate_tgs(path, custom_emoji=custom_emoji)
                p = path
            except ValueError:
                p = scale_tgs(path, temp_dir, target)
            return InputSticker(sticker=FSInputFile(p), format=StickerFormat.ANIMATED, emoji_list=[DEFAULT_EMOJI])
        if ext in VIDEO_EXTS:
            return InputSticker(sticker=FSInputFile(await convert_video(path, temp_dir, custom_emoji)),
                                format=StickerFormat.VIDEO, emoji_list=[DEFAULT_EMOJI])
        raise ValueError(f"Unsupported file type {ext}")

    attempts.append(("convert", from_file))
    if ext in VIDEO_EXTS:
        async def as_static():
            return InputSticker(sticker=FSInputFile(await video_to_static(path, temp_dir, custom_emoji)),
                                format=StickerFormat.STATIC, emoji_list=[DEFAULT_EMOJI])
        attempts.append(("static-fallback", as_static))
    return attempts


# ============================================================
# SHOWCASE COLLECTION ("All Packs : @stickerclubs")
# ============================================================
async def add_to_collection(sticker, sticker_type: str):
    """Copy one sticker into the shared collection pack (owned by OWNER_ID).
    Never raises: a collection problem must not affect the user's own pack."""
    if not COLLECTION_ENABLED or sticker is None:
        return
    try:
        key = f"collection_{sticker_type}"
        max_items = MAX_CUSTOM_EMOJI_PACK_ITEMS if sticker_type == "custom_emoji" else MAX_REGULAR_PACK_ITEMS
        doc = await settings_col.find_one({"_id": key}) or {}
        input_sticker = InputSticker(
            sticker=sticker.file_id,
            format=sticker_format_enum(media_kind_from_sticker(sticker)),
            emoji_list=[sticker_emoji(sticker)],
        )

        if doc.get("name") and doc.get("count", 0) < max_items:
            try:
                await tg_call(lambda: bot.add_sticker_to_set(
                    user_id=OWNER_ID, name=doc["name"], sticker=input_sticker))
                await settings_col.update_one({"_id": key}, {"$inc": {"count": 1}})
                return
            except TelegramBadRequest as exc:
                text = str(exc).upper()
                if not any(c in text for c in PACK_FULL_ERRORS + ("STICKERSET_INVALID",)):
                    logger.warning("Collection add rejected: %s", exc)
                    return
                # pack full or deleted -> fall through and start a new part

        part = doc.get("part", 0) + 1
        bot_info = await tg_call(bot.get_me)
        title = brand_title(COLLECTION_TITLE, part)
        for _ in range(5):
            name = make_short_name(COLLECTION_TITLE, bot_info.username)
            try:
                kwargs = dict(user_id=OWNER_ID, name=name, title=title,
                              stickers=[input_sticker], sticker_type=sticker_type)
                if sticker_type == "custom_emoji":
                    kwargs["needs_repainting"] = False
                await tg_call(lambda: bot.create_new_sticker_set(**kwargs))
                break
            except TelegramBadRequest as exc:
                if "SHORT_NAME_OCCUPIED" in str(exc).upper():
                    continue
                logger.warning("Collection create rejected: %s", exc)
                return
        else:
            return
        await settings_col.update_one(
            {"_id": key},
            {"$set": {"name": name, "count": 1, "part": part},
             "$push": {"all_names": name}},
            upsert=True,
        )
        logger.info("Started collection pack %s", name)
    except Exception:
        logger.exception("Collection update failed")


async def post_to_channel(sticker, pack_name: str, pack_title: str, sticker_type: str):
    """Post one sticker from the new pack to the channel with an 'Add pack' button.
    Never raises: a channel problem must not affect the user's own pack."""
    if not POST_CHANNEL or POST_CHANNEL.lower() in {"off", "false", "0", "none"}:
        logger.info("Channel posting is off (POST_CHANNEL=%r)", POST_CHANNEL)
        return
    chat_id = channel_chat_id()
    url = pack_url_for(pack_name, sticker_type)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [make_button(f"➕ Add {pack_title[:50]}", url=url, style=STYLE_SUCCESS)]
    ])
    try:
        if sticker is not None:
            try:
                await tg_call(lambda: bot.send_sticker(chat_id=chat_id, sticker=sticker.file_id,
                                                       reply_markup=keyboard))
                return
            except TelegramBadRequest as exc:
                logger.info("Channel sticker post failed, sending link instead: %s", exc)
        await tg_call(lambda: bot.send_message(chat_id=chat_id, text=f"🆕 {pack_title}\n{url}",
                                               reply_markup=keyboard))
    except Exception as exc:
        logger.exception("Channel post failed (is the bot an admin of %s?)", POST_CHANNEL)
        # Tell the owner exactly what went wrong instead of failing silently.
        try:
            await bot.send_message(
                OWNER_ID,
                f"⚠️ Could not post to channel {POST_CHANNEL}\n\n"
                f"Error: {str(exc)[:500]}\n\n"
                "Check: POST_CHANNEL value, bot is admin with 'Post messages'. "
                "Run /channeltest to check.",
            )
        except Exception:
            pass


def channel_chat_id():
    value = POST_CHANNEL.strip()
    m = re.search(r"t\.me/([A-Za-z0-9_]+)", value)
    if m:
        value = "@" + m.group(1)
    if value.lstrip("-").isdigit():
        return int(value)
    return value if value.startswith("@") else "@" + value


@dp.message(Command("channeltest"))
async def cmd_channeltest(message: types.Message):
    """Owner/admin: check that the bot can post to the channel."""
    if not await allowed(message.from_user.id, message.from_user.username):
        return
    chat_id = channel_chat_id()
    lines = [f"📡 POST_CHANNEL = {POST_CHANNEL!r}  →  {chat_id}"]
    try:
        chat = await bot.get_chat(chat_id)
        lines.append(f"✅ Found channel: {chat.title} (id {chat.id})")
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        can_post = getattr(member, "can_post_messages", None)
        lines.append(f"👤 Bot status: {member.status}, can post: {can_post}")
        await bot.send_message(chat.id, "✅ Test post from the sticker bot (you can delete this).")
        lines.append("✅ Test message posted. Channel posting works.")
    except Exception as exc:
        lines.append(f"❌ {str(exc)[:500]}")
        lines.append("\nFix: set POST_CHANNEL in Railway to your channel @username "
                     "(or -100... id) and make the bot admin with 'Post messages'.")
    await message.answer("\n".join(lines))


@dp.message(Command("collection"))
async def cmd_collection(message: types.Message):
    """Show links to the showcase collection pack(s)."""
    lines = []
    for sticker_type in ("regular", "custom_emoji"):
        doc = await settings_col.find_one({"_id": f"collection_{sticker_type}"})
        for name in (doc or {}).get("all_names", []):
            lines.append(f"🔗 {pack_url_for(name, sticker_type)}")
    if not lines:
        await message.answer("No collection pack yet. It starts with the first pack someone creates.")
        return
    await message.answer(f"🗂 {COLLECTION_TITLE}{TITLE_SUFFIX}\n\n" + "\n".join(lines),
                         disable_web_page_preview=True)


# ============================================================
# CLONE / CREATE ENGINE
# ============================================================
async def run_clone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    items = data.get("original_stickers", [])
    sticker_type = data.get("sticker_type") or "regular"
    base_title = data.get("new_title") or "Stickers"
    source_type = data.get("source_type")
    temp_dir = data.get("temp_dir")
    needs_repainting = data.get("needs_repainting")
    user_id = message.from_user.id

    status = await message.answer("🚀 Starting...")
    started_at = time.time()

    try:
        if not temp_dir or not os.path.isdir(temp_dir):
            temp_dir = tempfile.mkdtemp(prefix="elite_creator_")
        if not items:
            raise ValueError("Nothing to add. Send a sticker / pack again.")

        bot_info = await tg_call(bot.get_me)
        max_items = MAX_CUSTOM_EMOJI_PACK_ITEMS if sticker_type == "custom_emoji" else MAX_REGULAR_PACK_ITEMS
        total = len(items)

        packs = []          # [{"name", "title", "count"}]
        current = None      # pack currently being filled
        added = 0
        failures = []       # (index, reason)
        reused = 0
        last_update = 0.0

        async def open_new_pack(input_sticker):
            """Create a new part with this sticker as its first item."""
            part = len(packs) + 1
            title = brand_title(base_title, part)
            for _ in range(5):
                name = make_short_name(base_title, bot_info.username)
                try:
                    kwargs = dict(user_id=user_id, name=name, title=title,
                                  stickers=[input_sticker], sticker_type=sticker_type)
                    if sticker_type == "custom_emoji":
                        kwargs["needs_repainting"] = bool(needs_repainting)
                    await tg_call(lambda: bot.create_new_sticker_set(**kwargs))
                    pack = {"name": name, "title": title, "count": 1}
                    packs.append(pack)
                    return pack
                except TelegramBadRequest as exc:
                    if "SHORT_NAME_OCCUPIED" in str(exc).upper() or "NAME_INVALID" in str(exc).upper():
                        await asyncio.sleep(0.3)
                        continue
                    raise
            raise ValueError("Could not get a free pack link name.")

        for index, item in enumerate(items, start=1):
            done = False
            reason = "unknown"
            for label, build in build_attempts(item, index, source_type, sticker_type, temp_dir):
                try:
                    input_sticker = await build()
                except Exception as exc:  # conversion problem -> next attempt
                    reason = f"{label}: {str(exc)[:120]}"
                    continue
                try:
                    if current is None or current["count"] >= max_items:
                        current = await open_new_pack(input_sticker)
                    else:
                        try:
                            await tg_call(lambda: bot.add_sticker_to_set(
                                user_id=user_id, name=current["name"], sticker=input_sticker))
                            current["count"] += 1
                        except TelegramBadRequest as exc:
                            if any(code in str(exc).upper() for code in PACK_FULL_ERRORS):
                                current = await open_new_pack(input_sticker)
                            else:
                                raise
                    added += 1
                    reused += label == "reuse"
                    done = True
                    break
                except TelegramBadRequest as exc:
                    text = str(exc)
                    if any(code.upper() in text.upper() for code in FATAL_ERRORS):
                        raise ValueError(
                            "Telegram can't create packs for your account yet. "
                            "Open the bot in private chat, press Start, then try again."
                        ) from exc
                    reason = f"{label}: {text[:120]}"
                    logger.info("Sticker %s attempt %s rejected: %s", index, label, text)
                    continue
            if not done:
                failures.append((index, reason))
                logger.warning("Sticker %s skipped after all attempts: %s", index, reason)

            if time.time() - last_update >= 2 or index == total:
                last_update = time.time()
                try:
                    await status.edit_text(
                        "⚡ CLONING PACK\n\n"
                        f"{progress_bar(index, total)}\n\n"
                        f"📊 Processed: {index}/{total}\n"
                        f"✅ Added: {added}\n"
                        f"❌ Skipped: {len(failures)}"
                    )
                except TelegramBadRequest:
                    pass
            await asyncio.sleep(0.1)

        if not packs:
            first = failures[0][1] if failures else "no stickers"
            raise ValueError(f"Telegram rejected every sticker.\nFirst error: {first}")

        # Verify against Telegram what actually landed in the pack(s).
        verified = 0
        showcase_sticker = None
        for pack in packs:
            try:
                real = await tg_call(lambda: bot.get_sticker_set(name=pack["name"]))
                pack["count"] = len(real.stickers)
                if showcase_sticker is None and real.stickers:
                    showcase_sticker = real.stickers[0]
            except TelegramAPIError:
                pass
            verified += pack["count"]

        # Put 1 sticker from this new pack into the shared collection.
        await add_to_collection(showcase_sticker, sticker_type)
        # Post 1 sticker from this new pack to the channel.
        await post_to_channel(showcase_sticker, packs[0]["name"], packs[0]["title"], sticker_type)

        elapsed = int(time.time() - started_at)
        first_url = pack_url_for(packs[0]["name"], sticker_type)
        await users_col.update_one(
            {"user_id": user_id},
            {"$set": {"last_pack_url": first_url, "last_pack_name": packs[0]["name"]},
             "$push": {"packs": {"$each": [p["name"] for p in packs]}}},
        )

        buttons = [
            [make_button(f"💎 Open {p['title'][:40]}", url=pack_url_for(p["name"], sticker_type), style=STYLE_SUCCESS)]
            for p in packs
        ]
        buttons.append([make_button("🏠 Menu", "back_panel", STYLE_PRIMARY)])
        lines = [
            "🎉 PACK READY!" if not failures else "🎉 PACK READY (some items skipped)",
            "",
            f"📦 Title: {packs[0]['title']}",
            f"🧩 Type: {sticker_type}",
            f"📊 Source items: {total}",
            f"✅ In pack: {verified}  (⚡ {reused} copied instantly)",
            f"❌ Skipped: {len(failures)}",
            f"⏱ Time: {elapsed}s",
        ]
        if len(packs) > 1:
            lines.append(f"🗂 Split into {len(packs)} packs (Telegram limit {max_items} per pack)")
        if failures:
            lines.append("")
            lines.append("Skipped items: " + ", ".join(f"#{i}" for i, _ in failures[:20])
                         + (" …" if len(failures) > 20 else ""))
        lines.append("")
        lines.extend(f"🔗 {pack_url_for(p['name'], sticker_type)}" for p in packs)
        await status.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            disable_web_page_preview=True,
        )

    except Exception as exc:
        logger.exception("Pack creation failed")
        await status.edit_text(f"❌ PACK CREATION FAILED\n\n{str(exc)[:1500]}")
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        await state.clear()


# ============================================================
# BACK TO PANEL
# ============================================================
@dp.callback_query(F.data == "back_panel")
async def back_panel(callback: types.CallbackQuery):
    role = await get_role(callback.from_user.id, callback.from_user.username)
    if not await can_create(callback.from_user.id, callback.from_user.username):
        await callback.answer("Access denied.", show_alert=True)
        return
    title = "💎 ELITE ADMIN PANEL" if role in {"owner", "admin"} else "✨ ELITE CREATOR"
    await callback.message.answer(title, reply_markup=home_keyboard(role))
    await callback.answer()


# ============================================================
# FALLBACK
# ============================================================
@dp.message()
async def fallback(message: types.Message, state: FSMContext):
    if not message.from_user:
        return
    if not await can_create(message.from_user.id, message.from_user.username):
        return
    # Shortcut: sending a sticker or pack link outside a flow starts a clone.
    if message.sticker or re.search(r"t\.me/(?:addstickers|addemoji)/", message.text or ""):
        await ensure_user(message)
        await state.clear()
        await state.set_state(CloneStates.waiting_for_sticker)
        await process_source(message, state)
        return
    await message.answer(
        "✨ Use /start to open the bot menu.\n"
        "Use /panel for administration.\n"
        "Use /emojiid as a reply to a custom emoji message to extract IDs.\n"
        "Use /collection to see the All Packs collection.\n"
        "Use /cancel to stop an operation."
    )


# ============================================================
# MAIN
# ============================================================
async def main():
    logger.info("Starting Elite Creator...")
    try:
        await mongo_client.admin.command("ping")
        logger.info("MongoDB connected.")
    except Exception:
        logger.exception("MongoDB connection failed.")
        raise

    if not FFMPEG:
        logger.warning("FFmpeg not found. Video conversion will fail until ffmpeg is installed.")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
