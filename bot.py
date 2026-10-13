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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
)
from motor.motor_asyncio import AsyncIOMotorClient
# ============================================================
# CONFIGURATION
# ============================================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb://localhost:27017/",
)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_NAME = "sticker_bot"
ADMIN_USERNAMES = {
    "irtzaakash",
    "itzaakash",
}
# Button styling is supported by compatible Bot API versions.
BUTTON_STYLES_ENABLED = True
# Insert actual Telegram custom emoji IDs here.
# Leave empty to use ordinary Unicode emoji in labels.
EMOJI_PREMIUM = ""
EMOJI_STATS = ""
EMOJI_BROADCAST = ""
EMOJI_ADMIN = ""
EMOJI_REMOVE = ""
EMOJI_STICKER = ""
EMOJI_CUSTOM = ""
STYLE_PRIMARY = "primary"
STYLE_SUCCESS = "success"
STYLE_DANGER = "danger"
# Maximum number of files accepted from one ZIP.
MAX_ZIP_FILES = 200
MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
# Telegram sticker limits.
STATIC_STICKER_SIZE = 512
CUSTOM_EMOJI_SIZE = 100
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("elite_creator")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing from .env")
if OWNER_ID <= 0:
    raise RuntimeError("Set your numeric OWNER_ID in .env")
# ============================================================
# BOT AND DATABASE
# ============================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
mongo_client = AsyncIOMotorClient(MONGO_URI)
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
# ============================================================
# GENERAL HELPERS
# ============================================================
def clean_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", value or "")
def progress_bar(current: int, total: int, length: int = 15) -> str:
    percent = min(current / total, 1.0) if total else 0.0
    filled = int(length * percent)
    return (
        f"[{'█' * filled}{'░' * (length - filled)}] "
        f"{percent:.0%}"
    )
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
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": updates},
    )
async def get_role(
    user_id: int,
    username: str | None = None,
) -> str:
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
        await init_user(
            message.from_user.id,
            message.from_user.username,
        )
# ============================================================
# INLINE BUTTON HELPERS
# ============================================================
def make_button(
    text: str,
    callback_data: str | None = None,
    style: str | None = None,
    emoji_id: str | None = None,
    url: str | None = None,
):
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if BUTTON_STYLES_ENABLED and style:
        kwargs["style"] = style
    if emoji_id:
        kwargs["icon_custom_emoji_id"] = emoji_id
    return InlineKeyboardButton(**kwargs)
def admin_keyboard(role: str):
    rows = [
        [
            make_button(
                "💎 Premium Stats",
                "admin_stats",
                STYLE_PRIMARY,
                EMOJI_STATS or None,
            )
        ],
        [
            make_button(
                "📢 Broadcast Message",
                "admin_broadcast",
                STYLE_SUCCESS,
                EMOJI_BROADCAST or None,
            )
        ],
        [
            make_button(
                "🖼 Create Sticker Pack",
                "menu_create",
                STYLE_PRIMARY,
                EMOJI_STICKER or None,
            )
        ],
    ]
    if role == "owner":
        rows.append([
            make_button(
                "👑 Add VIP Admin",
                "admin_add",
                STYLE_SUCCESS,
                EMOJI_ADMIN or None,
            ),
            make_button(
                "❌ Revoke VIP",
                "admin_remove",
                STYLE_DANGER,
                EMOJI_REMOVE or None,
            ),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def pack_type_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                make_button(
                    "🖼 Standard Sticker Pack",
                    "type_regular",
                    STYLE_SUCCESS,
                    EMOJI_STICKER or None,
                )
            ],
            [
                make_button(
                    "💎 Custom Emoji Pack",
                    "type_custom_emoji",
                    STYLE_PRIMARY,
                    EMOJI_CUSTOM or EMOJI_PREMIUM or None,
                )
            ],
        ]
    )
def cancel_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                make_button(
                    "❌ Cancel",
                    "cancel_operation",
                    STYLE_DANGER,
                )
            ]
        ]
    )
# ============================================================
# IMAGE CONVERSION
# ============================================================
def convert_image(
    input_path: str,
    output_dir: str,
    custom_emoji: bool = False,
) -> str:
    """
    Convert a static image into a transparent WebP.
    Standard sticker:
        512 x 512 maximum canvas.
    Custom emoji:
        100 x 100 canvas.
    """
    os.makedirs(output_dir, exist_ok=True)
    target = (
        CUSTOM_EMOJI_SIZE
        if custom_emoji
        else STATIC_STICKER_SIZE
    )
    output_path = os.path.join(
        output_dir,
        f"converted_{time.time_ns()}.webp",
    )
    with Image.open(input_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
        image.thumbnail(
            (target, target),
            Image.Resampling.LANCZOS,
        )
        canvas = Image.new(
            "RGBA",
            (target, target),
            (0, 0, 0, 0),
        )
        x = (target - image.width) // 2
        y = (target - image.height) // 2
        canvas.alpha_composite(image, (x, y))
        canvas.save(
            output_path,
            "WEBP",
            lossless=True,
            method=6,
        )
    if os.path.getsize(output_path) > 512 * 1024:
        # Re-encode with lossy compression if necessary.
        with Image.open(output_path) as image:
            image.save(
                output_path,
                "WEBP",
                quality=80,
                method=6,
            )
    if os.path.getsize(output_path) > 512 * 1024:
        raise ValueError(
            "The converted image exceeds Telegram's static sticker "
            "file-size limit."
        )
    return output_path
# ============================================================
# TGS VALIDATION
# ============================================================
def validate_tgs(
    path: str,
    custom_emoji: bool = False,
):
    """
    Check that a TGS file is valid and has the expected canvas.
    This does not resize or reanimate TGS files.
    """
    try:
        with gzip.open(path, "rt", encoding="utf-8") as file:
            data = json.load(file)
    except Exception as exc:
        raise ValueError(
            f"Invalid TGS animation: {exc}"
        ) from exc
    width = data.get("w")
    height = data.get("h")
    if not isinstance(width, (int, float)):
        raise ValueError("TGS animation has no valid width.")
    if not isinstance(height, (int, float)):
        raise ValueError("TGS animation has no valid height.")
    expected = CUSTOM_EMOJI_SIZE if custom_emoji else STATIC_STICKER_SIZE
    if width != expected or height != expected:
        raise ValueError(
            f"TGS canvas is {width}x{height}, but this destination "
            f"requires {expected}x{expected}. TGS files are not "
            "automatically resized because that can distort animation."
        )
    if os.path.getsize(path) > 64 * 1024:
        raise ValueError(
            "TGS file exceeds Telegram's 64 KB animation limit."
        )
# ============================================================
# VIDEO CONVERSION
# ============================================================
async def convert_video(
    input_path: str,
    output_dir: str,
) -> str:
    """
    Convert a video into a silent VP9 WebM video sticker.
    FFmpeg must be installed on the server.
    """
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(
        output_dir,
        f"video_{time.time_ns()}.webm",
    )
    # Scale the longer dimension to 512 and preserve aspect ratio.
    # Pad to 512x512 so one dimension is exactly 512.
    video_filter = (
        "scale=512:512:force_original_aspect_ratio=decrease,"
        "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0,"
        "setsar=1"
    )
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", input_path,
        "-t", "3",
        "-an",
        "-vf", video_filter,
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-b:v", "0",
        "-crf", "35",
        "-deadline", "good",
        "-cpu-used", "4",
        "-metadata:s:v:0", "alpha_mode=1",
        output_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise ValueError(
            "FFmpeg could not convert the video: "
            + stderr.decode(errors="replace")[:800]
        )
    if not os.path.exists(output_path):
        raise ValueError("FFmpeg did not produce a WebM file.")
    if os.path.getsize(output_path) > 256 * 1024:
        raise ValueError(
            "Converted video exceeds Telegram's video sticker size limit. "
            "Try a shorter or simpler video."
        )
    return output_path
# ============================================================
# ZIP HANDLING
# ============================================================
def safe_extract_zip(zip_path: str, destination: str):
    """
    Safely extract a ZIP with file-count and size limits.
    """
    with zipfile.ZipFile(zip_path, "r") as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_FILES:
            raise ValueError(
                f"ZIP contains too many files. Maximum: {MAX_ZIP_FILES}."
            )
        total_size = sum(item.file_size for item in entries)
        if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ValueError(
                "ZIP is too large after extraction."
            )
        root = os.path.realpath(destination)
        for item in entries:
            target = os.path.realpath(
                os.path.join(destination, item.filename)
            )
            if not target.startswith(root + os.sep) and target != root:
                raise ValueError(
                    "ZIP contains an unsafe file path."
                )
        archive.extractall(destination)
def find_media_files(directory: str):
    supported = {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".tgs",
        ".webm",
        ".mp4",
    }
    result = []
    for root, _, files in os.walk(directory):
        for filename in files:
            if filename.lower() == "stickers.zip":
                continue
            if Path(filename).suffix.lower() in supported:
                result.append(
                    os.path.join(root, filename)
                )
    return sorted(result)
# ============================================================
# SOURCE PREPARATION
# ============================================================
async def prepare_source(
    message: types.Message,
    state: FSMContext,
):
    """
    Accept:
    1. A sticker from an existing set.
    2. A ZIP of static, animated, or video files.
    3. A message containing custom emojis.
    """
    temp_dir = tempfile.mkdtemp(prefix="elite_creator_")
    try:
        if message.sticker:
            sticker = message.sticker
            if not sticker.set_name:
                raise ValueError(
                    "This sticker does not belong to a sticker set."
                )
            sticker_set = await bot.get_sticker_set(
                name=sticker.set_name
            )
            stickers = sticker_set.stickers
            if not stickers:
                raise ValueError("The sticker set is empty.")
            formats = {
                "video" if item.is_video
                else "animated" if item.is_animated
                else "static"
                for item in stickers
            }
            if len(formats) != 1:
                raise ValueError(
                    "The source pack contains mixed formats. "
                    "Select a single-format pack."
                )
            sticker_format = formats.pop()
            await state.update_data(
                original_stickers=stickers,
                source_type="pack",
                sticker_format=sticker_format,
                temp_dir=temp_dir,
            )
            return len(stickers), sticker_format
        if (
            message.document
            and message.document.file_name
            and message.document.file_name.lower().endswith(".zip")
        ):
            zip_path = os.path.join(
                temp_dir,
                "stickers.zip",
            )
            await bot.download(
                message.document,
                destination=zip_path,
            )
            safe_extract_zip(zip_path, temp_dir)
            paths = find_media_files(temp_dir)
            if not paths:
                raise ValueError(
                    "ZIP contains no supported sticker files."
                )
            # Infer the format of each item.
            extensions = {
                Path(path).suffix.lower()
                for path in paths
            }
            has_video = any(
                ext in {".mp4", ".webm"}
                for ext in extensions
            )
            has_animation = ".tgs" in extensions
            has_static = any(
                ext in {".png", ".jpg", ".jpeg", ".webp"}
                for ext in extensions
            )
            if sum([has_video, has_animation, has_static]) != 1:
                raise ValueError(
                    "Your ZIP mixes static, animated, or video files. "
                    "Please create one ZIP for each format."
                )
            sticker_format = (
                "video" if has_video
                else "animated" if has_animation
                else "static"
            )
            await state.update_data(
                original_stickers=paths,
                source_type="zip",
                sticker_format=sticker_format,
                temp_dir=temp_dir,
            )
            return len(paths), sticker_format
        # Extract custom emoji IDs from message or caption entities.
        entities = (
            message.entities or []
        ) + (
            message.caption_entities or []
        )
        custom_ids = list(dict.fromkeys(
            entity.custom_emoji_id
            for entity in entities
            if entity.type == "custom_emoji"
            and entity.custom_emoji_id
        ))
        if not custom_ids:
            raise ValueError(
                "No source found. Send a sticker, a custom emoji "
                "message, or a ZIP file."
            )
        stickers = await bot.get_custom_emoji_stickers(
            custom_emoji_ids=custom_ids
        )
        if not stickers:
            raise ValueError(
                "Telegram returned no custom emoji stickers."
            )
        formats = {
            "video" if item.is_video
            else "animated" if item.is_animated
            else "static"
            for item in stickers
        }
        if len(formats) != 1:
            raise ValueError(
                "The selected custom emojis use mixed formats."
            )
        sticker_format = formats.pop()
        await state.update_data(
            original_stickers=stickers,
            source_type="custom_emoji",
            sticker_format=sticker_format,
            temp_dir=temp_dir,
        )
        return len(stickers), sticker_format
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
# ============================================================
# START
# ============================================================
@dp.message(Command("start"))
async def cmd_start(
    message: types.Message,
    state: FSMContext,
):
    await ensure_user(message)
    role = await get_role(
        message.from_user.id,
        message.from_user.username,
    )
    if role not in {"owner", "admin"}:
        await message.answer(
            "🚫 Access denied. This is a private bot."
        )
        return
    await state.clear()
    await message.answer(
        "✨ ELITE CREATOR\n\n"
        "Create your sticker or custom emoji pack.\n\n"
        "Supported sources:\n"
        "• Existing sticker packs\n"
        "• PNG/JPG/WebP images\n"
        "• TGS animated stickers\n"
        "• WebM/MP4 video files\n"
        "• Custom emoji messages\n\n"
        "Choose an action below.",
        reply_markup=admin_keyboard(role),
    )
# ============================================================
# ADMIN PANEL
# ============================================================
@dp.message(Command("panel", "admin"))
async def cmd_panel(message: types.Message):
    await ensure_user(message)
    role = await get_role(
        message.from_user.id,
        message.from_user.username,
    )
    if role not in {"owner", "admin"}:
        await message.answer("🚫 Access denied.")
        return
    await message.answer(
        "💎 ELITE ADMIN PANEL",
        reply_markup=admin_keyboard(role),
    )
# ============================================================
# CANCEL
# ============================================================
@dp.message(Command("cancel"))
async def cancel_command(
    message: types.Message,
    state: FSMContext,
):
    data = await state.get_data()
    temp_dir = data.get("temp_dir")
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
    await state.clear()
    await message.answer("❌ Operation cancelled.")
@dp.callback_query(F.data == "cancel_operation")
async def cancel_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    data = await state.get_data()
    temp_dir = data.get("temp_dir")
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
    await state.clear()
    await callback.answer("Cancelled.")
    await callback.message.answer(
        "❌ Operation cancelled."
    )
# ============================================================
# ADMIN CALLBACKS
# ============================================================
@dp.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    role = await get_role(
        callback.from_user.id,
        callback.from_user.username,
    )
    if role not in {"owner", "admin"}:
        await callback.answer(
            "Access denied.",
            show_alert=True,
        )
        return
    action = callback.data.removeprefix("admin_")
    if action == "stats":
        total = await users_col.count_documents({})
        admins = await users_col.count_documents({
            "role": "admin",
        })
        await callback.message.answer(
            "💎 PREMIUM STATISTICS\n\n"
            f"👥 Registered users: {total}\n"
            f"👑 VIP admins: {admins}",
            reply_markup=admin_keyboard(role),
        )
    elif action == "broadcast":
        await state.set_state(
            AdminStates.waiting_for_broadcast
        )
        await callback.message.answer(
            "📢 Send the message you want to broadcast.\n"
            "Text, photos, videos and other copyable messages "
            "are supported.\n\n"
            "Use /cancel to stop.",
            reply_markup=cancel_keyboard(),
        )
    elif action == "add" and role == "owner":
        await state.set_state(
            AdminStates.waiting_for_new_admin
        )
        await callback.message.answer(
            "👑 Send the numeric user ID to promote.",
            reply_markup=cancel_keyboard(),
        )
    elif action == "remove" and role == "owner":
        await state.set_state(
            AdminStates.waiting_for_remove_admin
        )
        await callback.message.answer(
            "❌ Send the numeric user ID to demote.",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()
# ============================================================
# ADD VIP ADMIN
# ============================================================
@dp.message(AdminStates.waiting_for_new_admin, F.text)
async def add_admin(
    message: types.Message,
    state: FSMContext,
):
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
                "$setOnInsert": {
                    "username": None,
                    "created_at": int(time.time()),
                },
            },
            upsert=True,
        )
        await message.answer(
            f"👑 User {target_id} is now a VIP admin."
        )
    except ValueError:
        await message.answer(
            "❌ Enter a valid numeric Telegram user ID."
        )
    await state.clear()
# ============================================================
# REMOVE VIP ADMIN
# ============================================================
@dp.message(AdminStates.waiting_for_remove_admin, F.text)
async def remove_admin(
    message: types.Message,
    state: FSMContext,
):
    if message.from_user.id != OWNER_ID:
        await state.clear()
        await message.answer("🚫 Owner access required.")
        return
    try:
        target_id = int(message.text.strip())
        if target_id <= 0:
            raise ValueError
        if target_id == OWNER_ID:
            await message.answer(
                "❌ The owner cannot be demoted."
            )
        else:
            await users_col.update_one(
                {"user_id": target_id},
                {"$set": {"role": "user"}},
            )
            await message.answer(
                f"❌ User {target_id} has been demoted."
            )
    except ValueError:
        await message.answer("❌ Invalid user ID.")
    await state.clear()
# ============================================================
# BROADCAST
# ============================================================
@dp.message(AdminStates.waiting_for_broadcast)
async def broadcast_message(
    message: types.Message,
    state: FSMContext,
):
    role = await get_role(
        message.from_user.id,
        message.from_user.username,
    )
    if role not in {"owner", "admin"}:
        await state.clear()
        await message.answer("🚫 Access denied.")
        return
    await state.clear()
    status = await message.answer(
        "📢 Broadcasting in progress..."
    )
    users = await users_col.find(
        {},
        {"user_id": 1},
    ).to_list(length=None)
    success = 0
    failed = 0
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
        except TelegramForbiddenError:
            failed += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(
        "📢 BROADCAST COMPLETE\n\n"
        f"✅ Sent: {success}\n"
        f"❌ Failed: {failed}"
    )
# ============================================================
# START CREATION
# ============================================================
@dp.callback_query(F.data == "menu_create")
async def menu_create(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    role = await get_role(
        callback.from_user.id,
        callback.from_user.username,
    )
    if role not in {"owner", "admin"}:
        await callback.answer(
            "Access denied.",
            show_alert=True,
        )
        return
    await state.clear()
    await state.set_state(
        CloneStates.waiting_for_sticker
    )
    await callback.message.answer(
        "📦 Send a sticker, custom emoji message, "
        "or ZIP file containing sticker media."
    )
    await callback.answer()
# ============================================================
# PROCESS SOURCE
# ============================================================
@dp.message(CloneStates.waiting_for_sticker)
async def process_source(
    message: types.Message,
    state: FSMContext,
):
    role = await get_role(
        message.from_user.id,
        message.from_user.username,
    )
    if role not in {"owner", "admin"}:
        return
    status = await message.answer(
        "🔍 Inspecting source..."
    )
    try:
        count, sticker_format = await prepare_source(
            message,
            state,
        )
        await status.edit_text(
            "📦 SOURCE READY\n\n"
            f"📊 Items: {count}\n"
            f"🎨 Format: {sticker_format.title()}\n\n"
            "Choose the destination pack type.",
            reply_markup=pack_type_keyboard(),
        )
        await state.set_state(
            CloneStates.waiting_for_pack_type
        )
    except Exception as exc:
        await status.edit_text(
            f"❌ Source error:\n{str(exc)[:1200]}"
        )
# ============================================================
# SELECT PACK TYPE
# ============================================================
@dp.callback_query(
    CloneStates.waiting_for_pack_type,
    F.data.startswith("type_"),
)
async def select_pack_type(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    role = await get_role(
        callback.from_user.id,
        callback.from_user.username,
    )
    if role not in {"owner", "admin"}:
        await callback.answer(
            "Access denied.",
            show_alert=True,
        )
        return
    sticker_type = (
        "custom_emoji"
        if callback.data == "type_custom_emoji"
        else "regular"
    )
    data = await state.get_data()
    source_type = data.get("source_type")
    sticker_format = data.get("sticker_format")
    # Video stickers are not accepted as custom emoji.
    if sticker_type == "custom_emoji" and sticker_format == "video":
        await callback.answer(
            "Video stickers cannot be used as custom emojis.",
            show_alert=True,
        )
        return
    # A TGS file must already have the required canvas.
    if sticker_type == "custom_emoji" and source_type == "zip":
        if sticker_format == "animated":
            await callback.answer(
                "For animated emoji, use TGS files with a 100x100 canvas.",
                show_alert=True,
            )
            return
    await state.update_data(
        sticker_type=sticker_type
    )
    await state.set_state(
        CloneStates.waiting_for_title
    )
    await callback.message.answer(
        "📝 Send the title for your new pack.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()
# ============================================================
# PACK TITLE
# ============================================================
@dp.message(CloneStates.waiting_for_title, F.text)
async def process_title(
    message: types.Message,
    state: FSMContext,
):
    title = message.text.strip()
    if not title or len(title) > 64:
        await message.answer(
            "❌ Enter a title between 1 and 64 characters."
        )
        return
    await state.update_data(
        new_title=title
    )
    await state.set_state(
        CloneStates.waiting_for_name
    )
    username = message.from_user.username
    prefix = clean_name(
        username or str(message.from_user.id)
    ).lower()
    bot_info = await bot.get_me()
    await message.answer(
        "🔗 Send a short name for the pack link.\n\n"
        "Only letters and numbers are allowed.\n\n"
        f"Example: magic\n"
        f"Your link will resemble:\n"
        f"{prefix}_magic_by_{bot_info.username}",
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
    Prepare valid InputSticker objects.
    Static images are normalized to WebP.
    MP4 videos are converted to WebM.
    TGS files remain animated.
    """
    result = []
    custom_emoji = sticker_type == "custom_emoji"
    if source_type in {"pack", "custom_emoji"}:
        for sticker in original_stickers:
            if custom_emoji and sticker.is_video:
                raise ValueError(
                    "Video stickers cannot be used as custom emoji."
                )
            if sticker.is_animated and custom_emoji:
                raise ValueError(
                    "This animated sticker cannot be safely resized "
                    "to the required custom emoji canvas. Use a "
                    "compatible 100x100 TGS file."
                )
            if custom_emoji and not sticker.is_animated:
                downloaded_path = os.path.join(
                    temp_dir,
                    f"source_{len(result)}",
                )
                await bot.download(
                    sticker.file_id,
                    destination=downloaded_path,
                )
                converted_path = convert_image(
                    downloaded_path,
                    temp_dir,
                    custom_emoji=True,
                )
                result.append(
                    InputSticker(
                        sticker=FSInputFile(converted_path),
                        format="static",
                        emoji_list=[
                            getattr(sticker, "emoji", None) or "😀"
                        ],
                    )
                )
            else:
                result.append(
                    InputSticker(
                        sticker=sticker.file_id,
                        format=sticker_format,
                        emoji_list=[
                            getattr(sticker, "emoji", None) or "😀"
                        ],
                    )
                )
        return result
    for index, path in enumerate(original_stickers):
        extension = Path(path).suffix.lower()
        if extension in {".png", ".jpg", ".jpeg", ".webp"}:
            converted_path = convert_image(
                path,
                temp_dir,
                custom_emoji=custom_emoji,
            )
            result.append(
                InputSticker(
                    sticker=FSInputFile(converted_path),
                    format="static",
                    emoji_list=["😀"],
                )
            )
        elif extension == ".tgs":
            validate_tgs(
                path,
                custom_emoji=custom_emoji,
            )
            result.append(
                InputSticker(
                    sticker=FSInputFile(path),
                    format="animated",
                    emoji_list=["😀"],
                )
            )
        elif extension in {".mp4", ".webm"}:
            if custom_emoji:
                raise ValueError(
                    "Video files cannot be used as custom emoji."
                )
            converted_path = await convert_video(
                path,
                temp_dir,
            )
            result.append(
                InputSticker(
                    sticker=FSInputFile(converted_path),
                    format="video",
                    emoji_list=["😀"],
                )
            )
        else:
            raise ValueError(
                f"Unsupported file extension: {extension}"
            )
    return result
# ============================================================
# CREATE PACK
# ============================================================
@dp.message(CloneStates.waiting_for_name, F.text)
async def create_pack(
    message: types.Message,
    state: FSMContext,
):
    role = await get_role(
        message.from_user.id,
        message.from_user.username,
    )
    if role not in {"owner", "admin"}:
        await state.clear()
        await message.answer("🚫 Access denied.")
        return
    requested_name = clean_name(
        message.text.strip()
    ).lower()
    if not requested_name:
        await message.answer(
            "❌ Enter a valid name using letters and numbers."
        )
        return
    status = await message.answer(
        "🚀 Preparing sticker files..."
    )
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
            temp_dir = tempfile.mkdtemp(
                prefix="elite_creator_"
            )
        bot_info = await bot.get_me()
        user_prefix = clean_name(
            message.from_user.username
            or str(message.from_user.id)
        ).lower()
        if not user_prefix:
            user_prefix = str(message.from_user.id)
        suffix = f"_by_{bot_info.username}"
        prefix = f"{user_prefix}_"
        max_length = 64 - len(prefix) - len(suffix)
        if max_length < 1:
            raise ValueError(
                "Unable to construct a valid pack name."
            )
        requested_name = requested_name[:max_length]
        full_name = (
            f"{prefix}{requested_name}{suffix}"
        )
        input_stickers = await build_input_stickers(
            original_stickers=original_stickers,
            source_type=source_type,
            sticker_format=sticker_format,
            sticker_type=sticker_type,
            temp_dir=temp_dir,
        )
        if not input_stickers:
            raise ValueError("No valid stickers to upload.")
        total = len(input_stickers)
        added = 0
        failed = 0
        await status.edit_text(
            "📦 Creating sticker pack...\n\n"
            f"{progress_bar(0, total)}"
        )
        # Create the set with the first sticker.
        while True:
            try:
                await bot.create_new_sticker_set(
                    user_id=message.from_user.id,
                    name=full_name,
                    title=new_title,
                    stickers=[input_stickers[0]],
                    sticker_format=sticker_format,
                    sticker_type=sticker_type,
                )
                added = 1
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
        last_update = time.time()
        # Add remaining stickers.
        for index, sticker in enumerate(
            input_stickers[1:],
            start=2,
        ):
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
                    logger.warning(
                        "Sticker %s rejected: %s",
                        index,
                        exc,
                    )
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
            f"https://t.me/addstickers/{full_name}"
        )
        # Store link in MongoDB so the button continues to work
        # after the FSM state is cleared.
        await users_col.update_one(
            {"user_id": message.from_user.id},
            {
                "$set": {
                    "last_pack_url": pack_url,
                    "last_pack_name": full_name,
                }
            },
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    make_button(
                        "💎 Open Pack",
                        url=pack_url,
                        style=STYLE_SUCCESS,
                    )
                ],
                [
                    make_button(
                        "🏠 Admin Panel",
                        "back_panel",
                        STYLE_PRIMARY,
                    )
                ],
            ]
        )
        await status.edit_text(
            "🎉 PACK CREATION COMPLETE!\n\n"
            f"📦 Title: {new_title}\n"
            f"📊 Source items: {total}\n"
            f"✅ Successfully added: {added}\n"
            f"❌ Failed: {failed}\n"
            f"⏱ Time: {elapsed}s\n\n"
            f"🔗 {pack_url}",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception("Pack creation failed")
        await status.edit_text(
            "❌ PACK CREATION FAILED\n\n"
            f"{str(exc)[:1500]}"
        )
    finally:
        if temp_dir:
            shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )
        await state.clear()
# ============================================================
# BACK TO PANEL
# ============================================================
@dp.callback_query(F.data == "back_panel")
async def back_panel(
    callback: types.CallbackQuery,
):
    role = await get_role(
        callback.from_user.id,
        callback.from_user.username,
    )
    if role not in {"owner", "admin"}:
        await callback.answer(
            "Access denied.",
            show_alert=True,
        )
        return
    await callback.message.answer(
        "💎 ELITE ADMIN PANEL",
        reply_markup=admin_keyboard(role),
    )
    await callback.answer()
# ============================================================
# FALLBACK
# ============================================================
@dp.message()
async def fallback(message: types.Message):
    role = await get_role(
        message.from_user.id,
        message.from_user.username,
    )
    if role not in {"owner", "admin"}:
        return
    await message.answer(
        "✨ Use /start to open the bot menu.\n"
        "Use /panel for administration.\n"
        "Use /cancel to stop an operation."
    )
    
    @dp.message(Command("emojiid"))
async def get_emoji_ids(message: types.Message):
    entities = message.entities or []
    if message.reply_to_message:
        entities = (
            message.reply_to_message.entities or []
        )
    found = []
    for entity in entities:
        if entity.type == "custom_emoji":
            found.append(
                f"Emoji ID: {entity.custom_emoji_id}"
            )
    if found:
        await message.answer(
            "💎 CUSTOM EMOJI IDs\n\n" + "\n".join(found)
        )
    else:
        await message.answer(
            "❌ No custom emoji IDs found.\n\n"
            "Send or reply to a message containing "
            "actual Telegram custom emojis."
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
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        mongo_client.close()
if __name__ == "__main__":
    asyncio.run(main())