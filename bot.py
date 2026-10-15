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

# ============================================================
# FSM STATES
# ============================================================
class CloneStates(StatesGroup):
    waiting_for_sticker = State()
    waiting_for_pack_type = State()
    waiting_for_title = State()
    waiting_for_name = State()


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


async def ensure_user(message: types.Message):
    if message.from_user:
        await init_user(message.from_user.id, message.from_user.username)


def media_kind_from_sticker(sticker) -> str:
    if getattr(sticker, "is_video", False):
        return "video"
    if getattr(sticker, "is_animated", False):
        return "animated"
    return "static"


def infer_paths_format(paths: list[str]) -> str:
    """Reject mixed ZIPs because one sticker set can mix formats in newer clients,
    but each uploaded item still needs an accurate format. This bot keeps batches
    single-format to avoid accidental mismatches."""
    extensions = {Path(p).suffix.lower() for p in paths}
    has_video = bool(extensions & {".webm", ".mp4", ".mov", ".mkv", ".gif", ".avi", ".m4v", ".mpeg", ".mpg"})
    has_tgs = ".tgs" in extensions
    has_static = bool(extensions & {".png", ".jpg", ".jpeg", ".webp", ".bmp"})
    if sum((has_video, has_tgs, has_static)) != 1:
        raise ValueError(
            "ZIP must contain only one format family: static images, TGS animations, "
            "or video/GIF files. Please split mixed ZIPs."
        )
    return "video" if has_video else "animated" if has_tgs else "static"


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
    """
    Newer Bot API versions support button style/custom emoji icons.
    Fall back to standard inline buttons if the installed aiogram model
    or Telegram API version does not support those fields.
    """
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
        [make_button("💎 Premium Stats", "admin_stats", STYLE_PRIMARY, EMOJI_STATS or None)],
        [make_button("📢 Broadcast Message", "admin_broadcast", STYLE_SUCCESS, EMOJI_BROADCAST or None)],
        [make_button("🖼 Create Sticker/Emoji Pack", "menu_create", STYLE_PRIMARY, EMOJI_STICKER or None)],
        [make_button("💠 Get Custom Emoji ID", "menu_emoji_id", STYLE_PRIMARY, EMOJI_CUSTOM or EMOJI_PREMIUM or None)],
    ]
    if role == "owner":
        rows.append([
            make_button("👑 Add VIP Admin", "admin_add", STYLE_SUCCESS, EMOJI_ADMIN or None),
            make_button("❌ Revoke VIP", "admin_remove", STYLE_DANGER, EMOJI_REMOVE or None),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pack_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [make_button("🖼 Standard Sticker Pack", "type_regular", STYLE_SUCCESS, EMOJI_STICKER or None)],
        [make_button("💎 Custom Emoji Pack", "type_custom_emoji", STYLE_PRIMARY, EMOJI_CUSTOM or EMOJI_PREMIUM or None)],
    ])


def cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [make_button("❌ Cancel", "cancel_operation", STYLE_DANGER)]
    ])


def home_keyboard(role: str):
    return admin_keyboard(role)


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
    """Save a direct image/video/TGS file to a temporary path and return path + family."""
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
    """
    Normalize every static image before upload:
    regular sticker -> exact 512x512 transparent WebP canvas;
    custom emoji -> exact 100x100 transparent WebP canvas.
    """
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
    """Validate a pre-made TGS file; does not attempt unsafe animation resizing."""
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
    """
    Convert a video/GIF into a silent VP9 WebM file.
    Regular sticker: 512x512 canvas.
    Custom emoji: exact 100x100 canvas.
    Duration <=3 seconds, <=30 FPS, <=256 KB.
    """
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
    """Extract ZIP files with traversal, count, and uncompressed-size checks."""
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
# SOURCE PREPARATION
# ============================================================
async def prepare_source(message: types.Message, state: FSMContext):
    """
    Sources accepted:
    - Sticker from an existing set
    - ZIP of static images, TGS, or video/GIF files
    - Message containing custom emojis
    - Direct image/video/GIF/TGS upload
    """
    temp_dir = tempfile.mkdtemp(prefix="elite_creator_")
    try:
        if message.sticker:
            sticker = message.sticker
            if not sticker.set_name:
                raise ValueError("This sticker does not belong to a sticker set.")
            sticker_set = await bot.get_sticker_set(name=sticker.set_name)
            stickers = sticker_set.stickers
            if not stickers:
                raise ValueError("The sticker set is empty.")
            formats = {media_kind_from_sticker(item) for item in stickers}
            if len(formats) != 1:
                raise ValueError("The source pack contains mixed formats. Select a single-format pack.")
            sticker_format = formats.pop()
            await state.update_data(
                original_stickers=stickers,
                source_type="pack",
                sticker_format=sticker_format,
                temp_dir=temp_dir,
            )
            return len(stickers), sticker_format

        if message.document and message.document.file_name and message.document.file_name.lower().endswith(".zip"):
            zip_path = os.path.join(temp_dir, "stickers.zip")
            await download_telegram_file(message.document.file_id, zip_path)
            safe_extract_zip(zip_path, temp_dir)
            paths = find_media_files(temp_dir)
            if not paths:
                raise ValueError("ZIP contains no supported sticker files.")
            sticker_format = infer_paths_format(paths)
            await state.update_data(
                original_stickers=paths,
                source_type="zip",
                sticker_format=sticker_format,
                temp_dir=temp_dir,
            )
            return len(paths), sticker_format

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
            formats = {media_kind_from_sticker(item) for item in stickers}
            if len(formats) != 1:
                raise ValueError("Selected custom emojis use mixed formats.")
            sticker_format = formats.pop()
            await state.update_data(
                original_stickers=stickers,
                source_type="custom_emoji",
                sticker_format=sticker_format,
                temp_dir=temp_dir,
            )
            return len(stickers), sticker_format

        # Direct photo/video/GIF/TGS/document upload.
        path, sticker_format = await save_message_media(message, temp_dir)
        if sticker_format == "zip":
            safe_extract_zip(path, temp_dir)
            paths = find_media_files(temp_dir)
            if not paths:
                raise ValueError("ZIP contains no supported sticker files.")
            sticker_format = infer_paths_format(paths)
            await state.update_data(
                original_stickers=paths,
                source_type="zip",
                sticker_format=sticker_format,
                temp_dir=temp_dir,
            )
            return len(paths), sticker_format

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
    if role not in {"owner", "admin"}:
        await message.answer("🚫 Access denied. This is a private bot.")
        return
    await state.clear()
    await message.answer(
        "✨ ELITE CREATOR\n\n"
        "Create a regular sticker pack or a custom emoji pack.\n\n"
        "Sources supported:\n"
        "• Existing sticker packs\n"
        "• PNG/JPG/WebP images\n"
        "• TGS animations\n"
        "• WebM/MP4/video/GIF files\n"
        "• ZIP archives containing one media format family\n"
        "• Messages containing Telegram custom emojis\n\n"
        "Choose an action below.",
        reply_markup=admin_keyboard(role),
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
    role = await get_role(callback.from_user.id, callback.from_user.username)
    if role not in {"owner", "admin"}:
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
            reply_markup=admin_keyboard(await get_role(message.from_user.id, message.from_user.username)),
        )
        return
    result = "\n".join(f"{index}. <code>{emoji_id}</code>" for index, emoji_id in enumerate(ids, 1))
    await message.answer(
        "💎 <b>Custom Emoji ID(s)</b>\n\n"
        f"{result}\n\n"
        "Example .env setting:\n<code>EMOJI_CUSTOM=1234567890123456789</code>\n\n"
        "Restart the bot after editing .env.",
        parse_mode="HTML",
        reply_markup=admin_keyboard(await get_role(message.from_user.id, message.from_user.username)),
    )


@dp.message(Command("emojiid"))
async def cmd_emojiid(message: types.Message):
    """Use /emojiid as a reply to the message containing custom emoji(s)."""
    role = await get_role(message.from_user.id, message.from_user.username)
    if role not in {"owner", "admin"}:
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
    role = await get_role(callback.from_user.id, callback.from_user.username)
    if role not in {"owner", "admin"}:
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.clear()
    await state.set_state(CloneStates.waiting_for_sticker)
    await callback.message.answer(
        "📦 Send any one of these:\n"
        "• A sticker from an existing pack\n"
        "• A message containing custom emoji(s)\n"
        "• A ZIP with sticker media\n"
        "• A direct image, TGS, video, or GIF file\n\n"
        "ZIPs must contain one media family only. Use /cancel to stop.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


# ============================================================
# PROCESS SOURCE
# ============================================================
@dp.message(CloneStates.waiting_for_sticker)
async def process_source(message: types.Message, state: FSMContext):
    role = await get_role(message.from_user.id, message.from_user.username)
    if role not in {"owner", "admin"}:
        return

    status = await message.answer("🔍 Inspecting source...")
    try:
        count, sticker_format = await prepare_source(message, state)
        await status.edit_text(
            "📦 SOURCE READY\n\n"
            f"📊 Items: {count}\n"
            f"🎨 Format: {sticker_format.title()}\n\n"
            "Choose the destination pack type.",
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
    role = await get_role(callback.from_user.id, callback.from_user.username)
    if role not in {"owner", "admin"}:
        await callback.answer("Access denied.", show_alert=True)
        return

    sticker_type = "custom_emoji" if callback.data == "type_custom_emoji" else "regular"
    data = await state.get_data()
    source_type = data.get("source_type")
    sticker_format = data.get("sticker_format")

    # Video custom emoji ARE supported, but must be converted to 100x100 VP9/WebM.
    # TGS cannot safely be resized automatically, so validation happens during build.
    if sticker_type == "custom_emoji" and sticker_format == "animated":
        await callback.answer(
            "TGS animations must already have a 100x100 canvas for custom emoji.",
            show_alert=True,
        )
        # Don't block if it is already a 100x100 custom emoji source. build_input_stickers validates it.
    await state.update_data(sticker_type=sticker_type)
    await state.set_state(CloneStates.waiting_for_title)
    await callback.message.answer("📝 Send the title for your new pack (1-64 characters).", reply_markup=cancel_keyboard())
    await callback.answer()


# ============================================================
# PACK TITLE
# ============================================================
@dp.message(CloneStates.waiting_for_title, F.text)
async def process_title(message: types.Message, state: FSMContext):
    title = message.text.strip()
    if not title or len(title) > 64:
        await message.answer("❌ Enter a title between 1 and 64 characters.")
        return
    await state.update_data(new_title=title)
    await state.set_state(CloneStates.waiting_for_name)
    bot_info = await bot.get_me()
    await message.answer(
        "🔗 Send a short name for the pack link.\n\n"
        "Only letters and numbers are allowed.\n"
        f"Example: magic\n"
        f"Your pack name will end with _by_{bot_info.username}.",
        reply_markup=cancel_keyboard(),
    )


# ============================================================
# PREPARE INPUT STICKERS
# ============================================================
async def build_input_stickers(
    original_stickers,
    source_type: str,
    sticker_format: str,
    sticker_type: str,
    temp_dir: str,
):
    """
    Build InputSticker objects with destination-appropriate dimensions.
    Static images are normalized to 512x512 or 100x100 WebP.
    Videos are converted to silent VP9/WebM at 512x512 or 100x100.
    TGS files are validated and not resized.
    """
    result = []
    custom_emoji = sticker_type == "custom_emoji"
    target = CUSTOM_EMOJI_SIZE if custom_emoji else STATIC_STICKER_SIZE

    if source_type in {"pack", "custom_emoji"}:
        for index, sticker in enumerate(original_stickers):
            source_format = media_kind_from_sticker(sticker)
            if source_format == "static":
                # Always download/re-encode to guarantee destination dimensions.
                source_path = os.path.join(temp_dir, f"source_{index}.webp")
                await download_telegram_file(sticker.file_id, source_path)
                converted_path = convert_image(source_path, temp_dir, custom_emoji=custom_emoji)
                result.append(InputSticker(
                    sticker=FSInputFile(converted_path),
                    format=StickerFormat.STATIC,
                    emoji_list=[getattr(sticker, "emoji", None) or "😀"],
                ))
            elif source_format == "video":
                source_path = os.path.join(temp_dir, f"source_{index}.webm")
                await download_telegram_file(sticker.file_id, source_path)
                converted_path = await convert_video(source_path, temp_dir, custom_emoji=custom_emoji)
                result.append(InputSticker(
                    sticker=FSInputFile(converted_path),
                    format=StickerFormat.VIDEO,
                    emoji_list=[getattr(sticker, "emoji", None) or "😀"],
                ))
            else:
                source_path = os.path.join(temp_dir, f"source_{index}.tgs")
                await download_telegram_file(sticker.file_id, source_path)
                validate_tgs(source_path, custom_emoji=custom_emoji)
                result.append(InputSticker(
                    sticker=FSInputFile(source_path),
                    format=StickerFormat.ANIMATED,
                    emoji_list=[getattr(sticker, "emoji", None) or "😀"],
                ))
        return result

    for path in original_stickers:
        extension = Path(path).suffix.lower()
        if extension in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            converted_path = convert_image(path, temp_dir, custom_emoji=custom_emoji)
            result.append(InputSticker(
                sticker=FSInputFile(converted_path),
                format=StickerFormat.STATIC,
                emoji_list=["😀"],
            ))
        elif extension == ".tgs":
            validate_tgs(path, custom_emoji=custom_emoji)
            result.append(InputSticker(
                sticker=FSInputFile(path),
                format=StickerFormat.ANIMATED,
                emoji_list=["😀"],
            ))
        elif extension in {".webm", ".mp4", ".mov", ".mkv", ".gif", ".avi", ".m4v", ".mpeg", ".mpg"}:
            converted_path = await convert_video(path, temp_dir, custom_emoji=custom_emoji)
            result.append(InputSticker(
                sticker=FSInputFile(converted_path),
                format=StickerFormat.VIDEO,
                emoji_list=["😀"],
            ))
        else:
            raise ValueError(f"Unsupported file extension: {extension}")

    return result


# ============================================================
# CREATE PACK
# ============================================================
@dp.message(CloneStates.waiting_for_name, F.text)
async def create_pack(message: types.Message, state: FSMContext):
    role = await get_role(message.from_user.id, message.from_user.username)
    if role not in {"owner", "admin"}:
        await state.clear()
        await message.answer("🚫 Access denied.")
        return

    requested_name = clean_name(message.text.strip()).lower()
    if not requested_name:
        await message.answer("❌ Enter a valid name using letters and numbers.")
        return

    status = await message.answer("🚀 Preparing sticker files...")
    started_at = time.time()
    data = await state.get_data()
    original_stickers = data.get("original_stickers", [])
    sticker_format = data.get("sticker_format")
    sticker_type = data.get("sticker_type")
    new_title = data.get("new_title")
    source_type = data.get("source_type")
    temp_dir = data.get("temp_dir")

    try:
        if not temp_dir or not os.path.isdir(temp_dir):
            temp_dir = tempfile.mkdtemp(prefix="elite_creator_")

        bot_info = await bot.get_me()
        user_prefix = clean_name(message.from_user.username or str(message.from_user.id)).lower()
        if not user_prefix:
            user_prefix = str(message.from_user.id)
        suffix = f"_by_{bot_info.username}"
        prefix = f"{user_prefix}_"
        max_length = 64 - len(prefix) - len(suffix)
        if max_length < 1:
            raise ValueError("Unable to construct a valid pack name.")
        requested_name = requested_name[:max_length]
        full_name = f"{prefix}{requested_name}{suffix}"

        input_stickers = await build_input_stickers(
            original_stickers=original_stickers,
            source_type=source_type,
            sticker_format=sticker_format,
            sticker_type=sticker_type,
            temp_dir=temp_dir,
        )
        if not input_stickers:
            raise ValueError("No valid stickers to upload.")

        max_items = MAX_CUSTOM_EMOJI_PACK_ITEMS if sticker_type == "custom_emoji" else MAX_REGULAR_PACK_ITEMS
        if len(input_stickers) > max_items:
            raise ValueError(f"This pack contains {len(input_stickers)} items. Limit for this pack type is {max_items}.")

        total = len(input_stickers)
        added = 0
        failed = 0
        await status.edit_text("📦 Creating sticker pack...\n\n" + progress_bar(0, total))

        # createNewStickerSet accepts the first sticker, then remaining stickers are added individually.
        while True:
            try:
                await bot.create_new_sticker_set(
                    user_id=message.from_user.id,
                    name=full_name,
                    title=new_title,
                    stickers=[input_stickers[0]],
                    sticker_type=sticker_type,
                    needs_repainting=False if sticker_type == "custom_emoji" else None,
                )
                added = 1
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)

        last_update = time.time()
        for index, sticker in enumerate(input_stickers[1:], start=2):
            while True:
                try:
                    await bot.add_sticker_to_set(
                        user_id=message.from_user.id,
                        name=full_name,
                        sticker=sticker,
                    )
                    added += 1
                    break
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 1)
                except TelegramBadRequest as exc:
                    failed += 1
                    logger.warning("Sticker %s rejected: %s", index, exc)
                    break

            if time.time() - last_update >= 2:
                try:
                    await status.edit_text(
                        "⚡ CREATING PACK\n\n"
                        f"{progress_bar(index, total)}\n\n"
                        f"📊 Processed: {index}/{total}\n"
                        f"✅ Added: {added}\n"
                        f"❌ Failed: {failed}"
                    )
                except TelegramBadRequest:
                    pass
                last_update = time.time()
            await asyncio.sleep(0.15)

        elapsed = int(time.time() - started_at)
        pack_url = (
            f"https://t.me/addemoji/{full_name}"
            if sticker_type == "custom_emoji"
            else f"https://t.me/addstickers/{full_name}"
        )

        await users_col.update_one(
            {"user_id": message.from_user.id},
            {"$set": {"last_pack_url": pack_url, "last_pack_name": full_name}},
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [make_button("💎 Open Pack", url=pack_url, style=STYLE_SUCCESS)],
            [make_button("🏠 Admin Panel", "back_panel", STYLE_PRIMARY)],
        ])
        await status.edit_text(
            "🎉 PACK CREATION COMPLETE!\n\n"
            f"📦 Title: {new_title}\n"
            f"🧩 Type: {sticker_type}\n"
            f"📊 Source items: {total}\n"
            f"✅ Successfully added: {added}\n"
            f"❌ Failed: {failed}\n"
            f"⏱ Time: {elapsed}s\n\n"
            f"🔗 {pack_url}",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    except TelegramBadRequest as exc:
        logger.exception("Telegram rejected pack creation")
        error = str(exc)
        hints = []
        if "STICKER_PNG_DIMENSIONS" in error:
            hints.append("The source image dimensions were rejected. Images are normalized to exact 512x512 or 100x100 WebP before upload.")
        if "STICKER_VIDEO_NOWEBM" in error or "STICKER_VIDEO_NODOC" in error:
            hints.append("Telegram expects a valid VP9/WebM video with no audio.")
        if "STICKER_VIDEO_BIG" in error:
            hints.append("The video is above 256 KB. Use a shorter or simpler clip.")
        if "PACK_SHORT_NAME_OCCUPIED" in error:
            hints.append("That pack name already exists; choose another name.")
        if "STICKER_TGS_NOTGS" in error or "STICKER_TGS_NODOC" in error:
            hints.append("The animated file is not a valid Telegram TGS file.")
        extra = "\n\n" + "\n".join(hints) if hints else ""
        await status.edit_text(f"❌ PACK CREATION FAILED\n\n{error[:1200]}{extra}")
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
    if role not in {"owner", "admin"}:
        await callback.answer("Access denied.", show_alert=True)
        return
    await callback.message.answer("💎 ELITE ADMIN PANEL", reply_markup=admin_keyboard(role))
    await callback.answer()


# ============================================================
# FALLBACK
# ============================================================
@dp.message()
async def fallback(message: types.Message):
    if not message.from_user:
        return
    role = await get_role(message.from_user.id, message.from_user.username)
    if role not in {"owner", "admin"}:
        return
    await message.answer(
        "✨ Use /start to open the bot menu.\n"
        "Use /panel for administration.\n"
        "Use /emojiid as a reply to a custom emoji message to extract IDs.\n"
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
