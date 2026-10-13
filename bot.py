import asyncio
import os
import re
import time
import zipfile
import tempfile
import shutil
import logging
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramAPIError,
)
from aiogram.types import (
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    CallbackQuery,
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
    "mongodb://localhost:27017/"
)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
ADMIN_USERNAMES = {
    "irtzaakash",
    "itzaakash",
}
DB_NAME = "sticker_bot"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("elite_creator")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing from your .env file.")
if OWNER_ID <= 0:
    raise RuntimeError("Set your numeric OWNER_ID in .env.")
# ============================================================
# BOT AND DATABASE
# ============================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client[DB_NAME]
users_col = db["users"]
# ============================================================
# STATES
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
# HELPERS
# ============================================================
def clean_name(value: str) -> str:
    """Keep only letters and numbers."""
    return re.sub(r"[^a-zA-Z0-9]", "", value or "")
def make_progress_bar(
    current: int,
    total: int,
    length: int = 15,
) -> str:
    if total <= 0:
        percent = 0
    else:
        percent = min(current / total, 1.0)
    filled = int(length * percent)
    return (
        f"[{'█' * filled}{'░' * (length - filled)}] "
        f"{percent:.0%}"
    )
async def init_user(user_id: int, username: str | None):
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        role = "owner" if user_id == OWNER_ID else "user"
        await users_col.insert_one({
            "user_id": user_id,
            "username": username,
            "role": role,
            "created_at": int(time.time()),
        })
    else:
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
async def is_admin(
    user_id: int,
    username: str | None = None,
) -> bool:
    return await get_role(user_id, username) in {
        "owner",
        "admin",
    }
async def ensure_user(message: Message):
    if message.from_user:
        await init_user(
            message.from_user.id,
            message.from_user.username,
        )
def make_button(
    text: str,
    callback_data: str,
    style: str | None = None,
    emoji_id: str | None = None,
) -> InlineKeyboardButton:
    kwargs = {
        "text": text,
        "callback_data": callback_data,
    }
    # Supported by sufficiently recent Aiogram/Bot API versions.
    if style:
        kwargs["style"] = style
    # Custom emoji icons are separate from the button label.
    if emoji_id:
        kwargs["icon_custom_emoji_id"] = emoji_id
    return InlineKeyboardButton(**kwargs)
# ============================================================
# CUSTOMIZABLE BUTTON SETTINGS
# ============================================================
# Set to None if your Bot API/library does not support styles.
BUTTON_STYLE_ENABLED = True
# Replace these IDs with valid custom emoji IDs if desired.
# Leave empty to use ordinary Unicode emoji in button text.
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
def styled_button(
    text: str,
    callback_data: str,
    style: str | None = None,
    emoji_id: str | None = None,
):
    if not BUTTON_STYLE_ENABLED:
        style = None
    return make_button(
        text=text,
        callback_data=callback_data,
        style=style,
        emoji_id=emoji_id,
    )
# ============================================================
# ADMIN PANEL KEYBOARD
# ============================================================
def admin_panel_keyboard(role: str):
    buttons = [
        [
            styled_button(
                "💎 Premium Stats",
                "admin_stats",
                STYLE_PRIMARY,
                EMOJI_STATS or None,
            )
        ],
        [
            styled_button(
                "📢 Broadcast Message",
                "admin_broadcast",
                STYLE_SUCCESS,
                EMOJI_BROADCAST or None,
            )
        ],
    ]
    if role == "owner":
        buttons.append([
            styled_button(
                "👑 Add VIP Admin",
                "admin_add",
                STYLE_SUCCESS,
                EMOJI_ADMIN or None,
            ),
            styled_button(
                "❌ Revoke VIP",
                "admin_remove",
                STYLE_DANGER,
                EMOJI_REMOVE or None,
            ),
        ])
    buttons.append([
        styled_button(
            "🖼 Create Sticker Pack",
            "menu_create",
            STYLE_PRIMARY,
            EMOJI_STICKER or None,
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
# ============================================================
# PACK TYPE KEYBOARD
# ============================================================
def pack_type_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                styled_button(
                    "🖼 Standard Sticker Pack",
                    "type_regular",
                    STYLE_SUCCESS,
                    EMOJI_STICKER or None,
                )
            ],
            [
                styled_button(
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
                styled_button(
                    "❌ Cancel",
                    "cancel_operation",
                    STYLE_DANGER,
                )
            ]
        ]
    )
# ============================================================
# START COMMAND
# ============================================================
@dp.message(Command("start"))
async def cmd_start(
    message: Message,
    state: FSMContext,
):
    await ensure_user(message)
    role = await get_role(
        message.from_user.id,
        message.from_user.username,
    )
    if role not in {"owner", "admin"}:
        await message.answer(
            "🚫 Access denied.\n"
            "This bot is available to authorized administrators only."
        )
        return
    await state.set_state(CloneStates.waiting_for_sticker)
    await message.answer(
        "✨ ELITE CREATOR\n\n"
        "Welcome to the sticker-pack creator.\n\n"
        "1. Send or forward a sticker from a pack.\n"
        "2. Send a message containing custom emoji.\n"
        "3. Upload a ZIP containing sticker files.\n\n"
        "Choose an action from the panel.",
        reply_markup=admin_panel_keyboard(role),
    )
# ============================================================
# ADMIN PANEL COMMAND
# ============================================================
@dp.message(Command("panel", "admin"))
async def cmd_panel(message: Message):
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
        reply_markup=admin_panel_keyboard(role),
    )
# ============================================================
# CANCEL COMMAND
# ============================================================
@dp.message(Command("cancel"))
async def cmd_cancel(
    message: Message,
    state: FSMContext,
):
    data = await state.get_data()
    temp_dir = data.get("temp_dir")
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
    await state.clear()
    await message.answer(
        "❌ Operation cancelled."
    )
@dp.callback_query(F.data == "cancel_operation")
async def cancel_callback(
    callback: CallbackQuery,
    state: FSMContext,
):
    data = await state.get_data()
    temp_dir = data.get("temp_dir")
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
    await state.clear()
    await callback.answer("Cancelled.")
    await callback.message.edit_text(
        "❌ Operation cancelled."
    )
# ============================================================
# ADMIN CALLBACKS
# ============================================================
@dp.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(
    callback: CallbackQuery,
    state: FSMContext,
):
    await init_user(
        callback.from_user.id,
        callback.from_user.username,
    )
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
        total_users = await users_col.count_documents({})
        admins = await users_col.count_documents({
            "role": "admin",
        })
        await callback.message.edit_text(
            "💎 PREMIUM STATISTICS\n\n"
            f"👥 Total registered users: {total_users}\n"
            f"👑 VIP admins: {admins}\n"
            f"🛡 Current role: {role.title()}",
            reply_markup=admin_panel_keyboard(role),
        )
    elif action == "broadcast":
        await state.set_state(
            AdminStates.waiting_for_broadcast
        )
        await callback.message.answer(
            "📢 Send the message to broadcast.\n\n"
            "Supported: text, photo, video and other "
            "messages Telegram allows the bot to copy.\n\n"
            "Use /cancel to stop.",
            reply_markup=cancel_keyboard(),
        )
    elif action == "add" and role == "owner":
        await state.set_state(
            AdminStates.waiting_for_new_admin
        )
        await callback.message.answer(
            "👑 Send the numeric Telegram user ID "
            "to promote to VIP admin.",
            reply_markup=cancel_keyboard(),
        )
    elif action == "remove" and role == "owner":
        await state.set_state(
            AdminStates.waiting_for_remove_admin
        )
        await callback.message.answer(
            "❌ Send the numeric user ID to revoke "
            "VIP admin access.",
            reply_markup=cancel_keyboard(),
        )
    else:
        await callback.answer(
            "You cannot perform this action.",
            show_alert=True,
        )
        return
    await callback.answer()
# ============================================================
# ADD ADMIN
# ============================================================
@dp.message(AdminStates.waiting_for_new_admin, F.text)
async def add_admin(
    message: Message,
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
                    "user_id": target_id,
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
            "❌ Invalid ID. Send a numeric Telegram user ID."
        )
    await state.clear()
# ============================================================
# REMOVE ADMIN
# ============================================================
@dp.message(AdminStates.waiting_for_remove_admin, F.text)
async def remove_admin(
    message: Message,
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
        await message.answer(
            "❌ Invalid ID. Send a numeric Telegram user ID."
        )
    await state.clear()
# ============================================================
# BROADCAST
# ============================================================
@dp.message(AdminStates.waiting_for_broadcast)
async def broadcast_msg(
    message: Message,
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
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("❌ Broadcast cancelled.")
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
            await asyncio.sleep(0.05)
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
        except Exception:
            failed += 1
    await status.edit_text(
        "📢 BROADCAST COMPLETE\n\n"
        f"✅ Sent: {success}\n"
        f"❌ Failed: {failed}"
    )
# ============================================================
# CREATE PACK BUTTON
# ============================================================
@dp.callback_query(F.data == "menu_create")
async def create_menu_callback(
    callback: CallbackQuery,
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
    await state.set_state(
        CloneStates.waiting_for_sticker
    )
    await callback.message.answer(
        "📦 Send a sticker, a message containing "
        "custom emoji, or a ZIP file."
    )
    await callback.answer()
# ============================================================
# COLLECT SOURCE STICKERS
# ============================================================
@dp.message(CloneStates.waiting_for_sticker)
async def process_media(
    message: Message,
    state: FSMContext,
):
    role = await get_role(
        message.from_user.id,
        message.from_user.username,
    )
    if role not in {"owner", "admin"}:
        return
    wait_msg = await message.answer(
        "🔍 Processing source..."
    )
    original_stickers = []
    sticker_format = "static"
    source_type = "pack"
    temp_dir = None
    # --------------------------------------------------------
    # SOURCE 1: FORWARDED OR SENT STICKER
    # --------------------------------------------------------
    if message.sticker:
        sticker = message.sticker
        if not sticker.set_name:
            await wait_msg.edit_text(
                "❌ This sticker does not belong to a sticker set."
            )
            return
        try:
            sticker_set = await bot.get_sticker_set(
                name=sticker.set_name
            )
            original_stickers = sticker_set.stickers
            sticker_format = (
                "video" if sticker.is_video
                else "animated" if sticker.is_animated
                else "static"
            )
        except TelegramAPIError as exc:
            await wait_msg.edit_text(
                f"❌ Could not fetch sticker pack: {exc}"
            )
            return
    # --------------------------------------------------------
    # SOURCE 2: ZIP FILE
    # --------------------------------------------------------
    elif (
        message.document
        and message.document.file_name
        and message.document.file_name.lower().endswith(".zip")
    ):
        temp_dir = tempfile.mkdtemp(prefix="elite_creator_")
        zip_path = os.path.join(
            temp_dir,
            "stickers.zip",
        )
        try:
            await bot.download(
                message.document,
                destination=zip_path,
            )
            valid_files = []
            with zipfile.ZipFile(zip_path, "r") as archive:
                # Prevent path traversal from malicious ZIP entries.
                for item in archive.infolist():
                    if item.is_dir():
                        continue
                    target = os.path.realpath(
                        os.path.join(temp_dir, item.filename)
                    )
                    if not (
                        target == os.path.realpath(temp_dir)
                        or target.startswith(
                            os.path.realpath(temp_dir) + os.sep
                        )
                    ):
                        raise ValueError(
                            "ZIP contains an unsafe file path."
                        )
                archive.extractall(temp_dir)
            for root, _, files in os.walk(temp_dir):
                for filename in files:
                    if filename == "stickers.zip":
                        continue
                    if filename.lower().endswith(
                        (".webp", ".tgs", ".webm")
                    ):
                        valid_files.append(
                            os.path.join(root, filename)
                        )
            valid_files.sort()
            if not valid_files:
                await wait_msg.edit_text(
                    "❌ ZIP contains no .webp, .tgs or .webm files."
                )
                shutil.rmtree(temp_dir, ignore_errors=True)
                return
            extensions = {
                Path(path).suffix.lower()
                for path in valid_files
            }
            # A single Telegram sticker set cannot mix formats.
            if len(extensions) != 1:
                await wait_msg.edit_text(
                    "❌ Your ZIP contains mixed sticker formats.\n\n"
                    "Create separate ZIPs for:\n"
                    "• .webp static stickers\n"
                    "• .tgs animated stickers\n"
                    "• .webm video stickers"
                )
                shutil.rmtree(temp_dir, ignore_errors=True)
                return
            extension = next(iter(extensions))
            sticker_format = {
                ".webp": "static",
                ".tgs": "animated",
                ".webm": "video",
            }[extension]
            original_stickers = valid_files
            source_type = "zip"
        except Exception as exc:
            await wait_msg.edit_text(
                f"❌ Could not read ZIP: {exc}"
            )
            shutil.rmtree(temp_dir, ignore_errors=True)
            return
    # --------------------------------------------------------
    # SOURCE 3: CUSTOM EMOJI IN MESSAGE
    # --------------------------------------------------------
    else:
        entities = (
            message.entities or []
        ) + (
            message.caption_entities or []
        )
        custom_emoji_ids = list(dict.fromkeys(
            entity.custom_emoji_id
            for entity in entities
            if entity.type == "custom_emoji"
            and entity.custom_emoji_id
        ))
        if not custom_emoji_ids:
            await wait_msg.edit_text(
                "❌ No source found.\n\n"
                "Send a sticker, a custom emoji message, "
                "or a ZIP file."
            )
            return
        try:
            stickers = await bot.get_custom_emoji_stickers(
                custom_emoji_ids=custom_emoji_ids
            )
            original_stickers = stickers
            source_type = "custom_emoji"
            first = stickers[0]
            sticker_format = (
                "video" if first.is_video
                else "animated" if first.is_animated
                else "static"
            )
        except TelegramAPIError as exc:
            await wait_msg.edit_text(
                f"❌ Could not retrieve custom emojis: {exc}"
            )
            return
    if not original_stickers:
        await wait_msg.edit_text(
            "❌ No stickers were found."
        )
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return
    await state.update_data(
        original_stickers=original_stickers,
        sticker_format=sticker_format,
        source_type=source_type,
        temp_dir=temp_dir,
    )
    await wait_msg.edit_text(
        "📦 SOURCE READY\n\n"
        f"📊 Items found: {len(original_stickers)}\n"
        f"🎨 Format: {sticker_format.title()}\n\n"
        "Choose the destination pack type:",
        reply_markup=pack_type_keyboard(),
    )
    await state.set_state(
        CloneStates.waiting_for_pack_type
    )
# ============================================================
# CHOOSE PACK TYPE
# ============================================================
@dp.callback_query(
    CloneStates.waiting_for_pack_type,
    F.data.startswith("type_"),
)
async def process_pack_type(
    callback: CallbackQuery,
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
    if sticker_type == "custom_emoji" and source_type == "zip":
        # ZIP files do not carry the source emoji entities needed
        # to reliably map custom emoji files into an emoji pack.
        await callback.answer(
            "For emoji packs, send custom emoji directly in a message.",
            show_alert=True,
        )
        return
    if sticker_type == "custom_emoji" and sticker_format == "video":
        await callback.answer(
            "Video stickers cannot be used as custom emoji.",
            show_alert=True,
        )
        return
    await state.update_data(
        sticker_type=sticker_type
    )
    await callback.message.edit_text(
        "✅ Pack type selected:\n"
        f"{'💎 Custom Emoji Pack' if sticker_type == 'custom_emoji' else '🖼 Standard Sticker Pack'}\n\n"
        "📝 Send a title for the new pack.",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(
        CloneStates.waiting_for_title
    )
    await callback.answer()
# ============================================================
# PACK TITLE
# ============================================================
@dp.message(CloneStates.waiting_for_title, F.text)
async def process_title(
    message: Message,
    state: FSMContext,
):
    title = message.text.strip()
    if not title:
        await message.answer(
            "❌ Title cannot be empty."
        )
        return
    if len(title) > 64:
        await message.answer(
            "❌ Pack title must be 64 characters or fewer."
        )
        return
    await state.update_data(
        new_title=title
    )
    username = message.from_user.username
    user_prefix = clean_name(
        username or str(message.from_user.id)
    ).lower()
    await message.answer(
        "🔗 Send a short link name.\n\n"
        "Letters and numbers only.\n\n"
        f"Example: magic\n"
        f"Your link will resemble:\n"
        f"{user_prefix}_magic_by_{(await bot.get_me()).username}",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(
        CloneStates.waiting_for_name
    )
# ============================================================
# CREATE THE STICKER OR EMOJI PACK
# ============================================================
@dp.message(CloneStates.waiting_for_name, F.text)
async def process_name(
    message: Message,
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
            "❌ Enter at least one letter or number."
        )
        return
    status_msg = await message.answer(
        "🚀 Preparing your pack..."
    )
    started_at = time.time()
    data = await state.get_data()
    original_stickers = data["original_stickers"]
    sticker_format = data["sticker_format"]
    sticker_type = data["sticker_type"]
    new_title = data["new_title"]
    source_type = data["source_type"]
    temp_dir = data.get("temp_dir")
    total = len(original_stickers)
    try:
        bot_info = await bot.get_me()
        user_prefix = clean_name(
            message.from_user.username
            or str(message.from_user.id)
        ).lower()
        if not user_prefix:
            user_prefix = str(message.from_user.id)
        # Telegram sticker-set names must be unique and end
        # in _by_<bot_username>.
        suffix = f"_by_{bot_info.username}"
        prefix = f"{user_prefix}_"
        max_name_length = 64 - len(suffix) - len(prefix)
        if max_name_length < 1:
            raise ValueError(
                "Bot username or user prefix is too long."
            )
        requested_name = requested_name[:max_name_length]
        full_short_name = (
            f"{prefix}{requested_name}{suffix}"
        )
        input_stickers = []
        # ----------------------------------------------------
        # Build InputSticker objects
        # ----------------------------------------------------
        if source_type == "pack":
            for sticker in original_stickers:
                emoji = getattr(sticker, "emoji", None) or "😀"
                input_stickers.append(
                    InputSticker(
                        sticker=sticker.file_id,
                        format=sticker_format,
                        emoji_list=[emoji],
                    )
                )
        elif source_type == "custom_emoji":
            for sticker in original_stickers:
                emoji = getattr(sticker, "emoji", None) or "😀"
                input_stickers.append(
                    InputSticker(
                        sticker=sticker.file_id,
                        format=sticker_format,
                        emoji_list=[emoji],
                    )
                )
        elif source_type == "zip":
            for path in original_stickers:
                input_stickers.append(
                    InputSticker(
                        sticker=FSInputFile(path),
                        format=sticker_format,
                        emoji_list=["😀"],
                    )
                )
        if not input_stickers:
            raise ValueError("No valid stickers to add.")
        # ----------------------------------------------------
        # Create pack with first sticker
        # ----------------------------------------------------
        await status_msg.edit_text(
            "📦 Creating sticker pack...\n"
            f"{make_progress_bar(0, total)}"
        )
        await bot.create_new_sticker_set(
            user_id=message.from_user.id,
            name=full_short_name,
            title=new_title,
            stickers=[input_stickers[0]],
            sticker_format=sticker_format,
            sticker_type=sticker_type,
        )
        added = 1
        failed = 0
        last_update = time.time()
        # ----------------------------------------------------
        # Add remaining stickers
        # ----------------------------------------------------
        for index, sticker_input in enumerate(
            input_stickers[1:],
            start=2,
        ):
            while True:
                try:
                    await bot.add_sticker_to_set(
                        user_id=message.from_user.id,
                        name=full_short_name,
                        sticker=sticker_input,
                    )
                    added += 1
                    break
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(
                        exc.retry_after + 1
                    )
                except (
                    TelegramBadRequest,
                    TelegramAPIError,
                ) as exc:
                    failed += 1
                    logger.warning(
                        "Failed to add sticker %s: %s",
                        index,
                        exc,
                    )
                    break
            if time.time() - last_update >= 2:
                try:
                    await status_msg.edit_text(
                        "⚡ BUILDING YOUR PACK\n\n"
                        f"{make_progress_bar(index, total)}\n\n"
                        f"📊 Processed: {index}/{total}\n"
                        f"✅ Added: {added}\n"
                        f"❌ Failed: {failed}"
                    )
                except TelegramBadRequest:
                    pass
                last_update = time.time()
            await asyncio.sleep(0.15)
        elapsed = int(time.time() - started_at)
        pack_link = (
            f"https://t.me/addstickers/{full_short_name}"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    styled_button(
                        "💎 Add Pack to Telegram",
                        "open_pack",
                        STYLE_SUCCESS,
                    )
                ],
                [
                    styled_button(
                        "🔗 Open Pack Link",
                        "open_pack",
                        STYLE_PRIMARY,
                    )
                ],
                [
                    styled_button(
                        "🏠 Admin Panel",
                        "back_panel",
                        STYLE_PRIMARY,
                    )
                ],
            ]
        )
        # The URL is included as ordinary text so the user
        # can copy it even when callback buttons are used.
        await status_msg.edit_text(
            "🎉 PACK CREATION COMPLETE!\n\n"
            f"📦 Title: {new_title}\n"
            f"📊 Source items: {total}\n"
            f"✅ Successfully added: {added}\n"
            f"❌ Failed: {failed}\n"
            f"⏱ Time: {elapsed} seconds\n\n"
            "👇 Your pack link:\n"
            f"{pack_link}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        styled_button(
                            "💎 Open Sticker Pack",
                            "open_pack",
                            STYLE_SUCCESS,
                        )
                    ],
                    [
                        styled_button(
                            "🏠 Admin Panel",
                            "back_panel",
                            STYLE_PRIMARY,
                        )
                    ],
                ]
            ),
            disable_web_page_preview=True,
        )
        # Save the pack URL for the callback button.
        await state.update_data(
            last_pack_url=pack_link
        )
    except Exception as exc:
        logger.exception("Pack creation failed")
        await status_msg.edit_text(
            "❌ PACK CREATION FAILED\n\n"
            f"Reason: {str(exc)[:1500]}"
        )
    finally:
        if source_type == "zip" and temp_dir:
            shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )
        await state.clear()
# ============================================================
# OPEN PACK CALLBACK
# ============================================================
@dp.callback_query(F.data == "open_pack")
async def open_pack_callback(
    callback: CallbackQuery,
    state: FSMContext,
):
    # Telegram inline keyboard callback buttons cannot directly
    # open a URL. Answer with the link in a message.
    data = await state.get_data()
    pack_url = data.get("last_pack_url")
    if pack_url:
        await callback.message.answer(
            f"🔗 Your sticker pack:\n{pack_url}"
        )
    else:
        await callback.answer(
            "Create a pack first.",
            show_alert=True,
        )
        return
    await callback.answer()
# ============================================================
# BACK TO ADMIN PANEL
# ============================================================
@dp.callback_query(F.data == "back_panel")
async def back_panel_callback(
    callback: CallbackQuery,
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
        reply_markup=admin_panel_keyboard(role),
    )
    await callback.answer()
# ============================================================
# FALLBACK HANDLER
# ============================================================
@dp.message()
async def fallback_handler(message: Message):
    role = await get_role(
        message.from_user.id,
        message.from_user.username,
    )
    if role not in {"owner", "admin"}:
        return
    await message.answer(
        "✨ Use /start to open the creator menu.\n"
        "Use /panel to open the admin panel.\n"
        "Use /cancel to stop an active operation."
    )
# ============================================================
# STARTUP / SHUTDOWN
# ============================================================
async def main():
    logger.info(
        "Elite Creator starting..."
    )
    try:
        await db_client.admin.command("ping")
        logger.info("MongoDB connected.")
    except Exception:
        logger.exception("MongoDB connection failed.")
        raise
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        db_client.close()
if __name__ == "__main__":
    asyncio.run(main())