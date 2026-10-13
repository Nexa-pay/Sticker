import asyncio
import os
import time
import re
import zipfile
import tempfile
import shutil
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
OWNER_ID = int(os.getenv("OWNER_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client["sticker_bot"]
users_col = db["users"]

ADMIN_USERNAMES = ["irtzaakash", "itzaakash"]

class CloneStates(StatesGroup):
    waiting_for_sticker = State()
    waiting_for_pack_type = State()
    waiting_for_title = State()
    waiting_for_name = State()

class AdminStates(StatesGroup):
    waiting_for_new_admin = State()
    waiting_for_remove_admin = State()
    waiting_for_broadcast = State()

async def init_user(user_id: int, username: str):
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        role = "owner" if user_id == OWNER_ID else "user"
        await users_col.insert_one({"user_id": user_id, "username": username, "role": role})
    elif user_id == OWNER_ID and user.get("role") != "owner":
        await users_col.update_one({"user_id": user_id}, {"$set": {"role": "owner"}})

async def get_role(user_id: int, username: str = None) -> str:
    if user_id == OWNER_ID: return "owner"
    if username and username.lower() in ADMIN_USERNAMES: return "admin"
    user = await users_col.find_one({"user_id": user_id})
    return user.get("role", "user") if user else "user"

def make_progress_bar(current: int, total: int, length: int = 15) -> str:
    if total == 0: return f"`[{'░'*length}] 0%`"
    percent = min(current / total, 1.0)
    filled = int(length * percent)
    return f"`[{'█' * filled}{'░' * (length - filled)}] {percent:.0%}`"

# 💎 Updated Premium Admin Panel
def admin_panel_keyboard(role: str):
    buttons = [
        [InlineKeyboardButton(text="💎 Premium Stats", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Broadcast Message", callback_data="admin_broadcast")]
    ]
    if role == "owner":
        buttons.append([
            InlineKeyboardButton(text="👑 Add VIP Admin", callback_data="admin_add"),
            InlineKeyboardButton(text="❌ Revoke VIP", callback_data="admin_remove")
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# 💎 Updated Pack Selection Menu
def pack_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 Standard Sticker Pack", callback_data="type_regular")],
        [InlineKeyboardButton(text="💎 Premium Custom Emoji Pack", callback_data="type_custom_emoji")]
    ])

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await init_user(message.from_user.id, message.from_user.username)
    role = await get_role(message.from_user.id, message.from_user.username)
    
    if role not in ["admin", "owner"]:
        await message.reply("🚫 **Access Denied:** This is a private bot.")
        return

    await message.reply(
        "✨ **Welcome to the Elite Creator!**\n\n"
        "1️⃣ **Forward me a sticker** to clone a pack.\n"
        "2️⃣ **Send Premium Emojis** to convert them.\n"
        "3️⃣ **Send a `.zip` file** (`.webm`, `.webp`, `.tgs`) to build a pack.",
        parse_mode="Markdown"
    )
    await state.set_state(CloneStates.waiting_for_sticker)

@dp.message(Command("panel", "admin"))
async def cmd_panel(message: types.Message, state: FSMContext):
    role = await get_role(message.from_user.id, message.from_user.username)
    if role not in ["admin", "owner"]: return
    await message.reply("⚙️ **Settings & VIP Panel**", reply_markup=admin_panel_keyboard(role))

@dp.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(callback: types.CallbackQuery, state: FSMContext):
    role = await get_role(callback.from_user.id, callback.from_user.username)
    action = callback.data.split("_")[1]

    if action == "stats":
        total_users = await users_col.count_documents({})
        admins = await users_col.count_documents({"role": "admin"})
        await callback.message.edit_text(
            f"💎 **Premium Statistics**\n\n👥 Total Users: `{total_users}`\n👑 VIP Admins: `{admins}`",
            reply_markup=admin_panel_keyboard(role)
        )
    elif action == "broadcast":
        await callback.message.reply("📢 Send the message you want to broadcast (Text, Photo, or Video).")
        await state.set_state(AdminStates.waiting_for_broadcast)
    elif action == "add" and role == "owner":
        await callback.message.reply("✅ Send the numeric User ID to promote them to VIP Admin.")
        await state.set_state(AdminStates.waiting_for_new_admin)
    elif action == "remove" and role == "owner":
        await callback.message.reply("❌ Send the numeric User ID to revoke their VIP access.")
        await state.set_state(AdminStates.waiting_for_remove_admin)
    
    await callback.answer()

@dp.message(AdminStates.waiting_for_new_admin)
async def add_admin(message: types.Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        await users_col.update_one({"user_id": target_id}, {"$set": {"role": "admin"}}, upsert=True)
        await message.reply(f"👑 User `{target_id}` is now a VIP Admin.")
    except ValueError:
        await message.reply("❌ Invalid ID. Must be numeric.")
    await state.clear()

@dp.message(AdminStates.waiting_for_remove_admin)
async def remove_admin(message: types.Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        if target_id == OWNER_ID:
            await message.reply("❌ You cannot remove the Owner.")
        else:
            await users_col.update_one({"user_id": target_id}, {"$set": {"role": "user"}})
            await message.reply(f"❌ User `{target_id}` has been demoted.")
    except ValueError:
        await message.reply("❌ Invalid ID.")
    await state.clear()

@dp.message(AdminStates.waiting_for_broadcast)
async def broadcast_msg(message: types.Message, state: FSMContext):
    await state.clear()
    status = await message.reply("📢 Broadcasting message... ⏳")
    users = await users_col.find({}).to_list(length=None)
    
    success, failed = 0, 0
    for u in users:
        try:
            await bot.copy_message(chat_id=u["user_id"], from_chat_id=message.chat.id, message_id=message.message_id)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status.edit_text(f"📢 **Broadcast Complete!**\n✅ Sent: `{success}`\n❌ Failed: `{failed}`")

@dp.message(CloneStates.waiting_for_sticker)
async def process_media(message: types.Message, state: FSMContext):
    if await get_role(message.from_user.id, message.from_user.username) not in ["admin", "owner"]: return

    wait_msg = await message.reply("🔍 Processing source... ⏳")
    original_stickers = []
    sticker_format = "static"
    source_type = "pack"
    temp_dir = None

    if message.sticker:
        if not message.sticker.set_name:
            await wait_msg.edit_text("❌ This sticker doesn't belong to a pack.")
            return
        try:
            sticker_set = await bot.get_sticker_set(name=message.sticker.set_name)
            original_stickers = sticker_set.stickers
            sticker_format = "animated" if message.sticker.is_animated else "video" if message.sticker.is_video else "static"
        except Exception as e:
            await wait_msg.edit_text(f"❌ Error fetching pack: {e}")
            return

    elif message.document and message.document.file_name.lower().endswith('.zip'):
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, "stickers.zip")
        await bot.download(message.document, destination=zip_path)
        
        valid_files = []
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.lower().endswith(('.webm', '.webp', '.tgs')):
                        valid_files.append(os.path.join(root, file))
        except Exception:
            await wait_msg.edit_text("❌ Failed to read ZIP.")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return

        if not valid_files:
            await wait_msg.edit_text("❌ No valid files in ZIP.")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return

        valid_files.sort()
        first_ext = valid_files[0].lower().split('.')[-1]
        sticker_format = "video" if first_ext == 'webm' else "animated" if first_ext == 'tgs' else "static"
        original_stickers = valid_files
        source_type = "zip"

    else:
        entities = message.entities or message.caption_entities or []
        custom_emoji_ids = [e.custom_emoji_id for e in entities if e.type == "custom_emoji"]
        if not custom_emoji_ids:
            await wait_msg.edit_text("❌ No Stickers, ZIPs, or Premium Emojis found.")
            return
        try:
            stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=custom_emoji_ids)
            original_stickers = stickers
            first = stickers[0]
            sticker_format = "animated" if first.is_animated else "video" if first.is_video else "static"
        except Exception as e:
            await wait_msg.edit_text(f"❌ Error fetching emojis: {e}")
            return

    await state.update_data(
        original_stickers=original_stickers,
        sticker_format=sticker_format,
        source_type=source_type,
        temp_dir=temp_dir
    )

    await wait_msg.edit_text(
        f"📦 **Source Processed Successfully!**\n"
        f"📊 **Count:** `{len(original_stickers)}`\n"
        f"⚙️ **Format:** `{sticker_format.title()}`\n\n"
        f"🤔 **Choose your pack type:**",
        reply_markup=pack_type_keyboard()
    )
    await state.set_state(CloneStates.waiting_for_pack_type)

@dp.callback_query(F.data.startswith("type_"))
async def process_pack_type(callback: types.CallbackQuery, state: FSMContext):
    sticker_type = "custom_emoji" if callback.data == "type_custom_emoji" else "regular"
    await state.update_data(sticker_type=sticker_type)
    
    await callback.message.edit_text(
        f"✅ Selected: **{'💎 Premium Custom Emoji' if sticker_type == 'custom_emoji' else '🖼 Standard Sticker'} Pack**\n\n"
        f"📝 **What do you want the Title of your new pack to be?**",
        parse_mode="Markdown"
    )
    await state.set_state(CloneStates.waiting_for_title)
    await callback.answer()

@dp.message(CloneStates.waiting_for_title, F.text)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(new_title=message.text)
    user_prefix = message.from_user.username or str(message.from_user.id)
    user_prefix = re.sub(r'[^a-zA-Z0-9]', '', user_prefix).lower()
    
    await message.reply(
        f"🔗 **Send me a Short Name for the link.** (Letters/Numbers only)\n"
        f"💡 *Example:* `magic` -> `{user_prefix}_magic_by_{(await bot.get_me()).username}`",
        parse_mode="Markdown"
    )
    await state.set_state(CloneStates.waiting_for_name)

@dp.message(CloneStates.waiting_for_name, F.text)
async def process_name(message: types.Message, state: FSMContext):
    status_msg = await message.reply("🚀 Initializing creation sequence... ⏳")
    start_time = time.time()
    
    data = await state.get_data()
    original_stickers = data['original_stickers']
    sticker_format = data['sticker_format']
    sticker_type = data['sticker_type']
    new_title = data['new_title']
    source_type = data['source_type']
    temp_dir = data.get('temp_dir')
    
    total_stickers = len(original_stickers)
    bot_info = await bot.get_me()
    
    user_prefix = message.from_user.username or str(message.from_user.id)
    user_prefix = re.sub(r'[^a-zA-Z0-9]', '', user_prefix).lower()
    clean_short_name = re.sub(r'[^a-zA-Z0-9]', '', message.text).lower() or "pack"
    
    max_clean_len = 64 - len(f"{user_prefix}__by_{bot_info.username}")
    clean_short_name = clean_short_name[:max_clean_len]
    full_short_name = f"{user_prefix}_{clean_short_name}_by_{bot_info.username}"

    input_stickers = []
    if source_type == "pack":
        for st in original_stickers:
            input_stickers.append(types.InputSticker(
                sticker=st.file_id, 
                emoji_list=[st.emoji] if hasattr(st, 'emoji') and st.emoji else ["😀"],
                format=sticker_format
            ))
    elif source_type == "zip":
        for path in original_stickers:
            input_stickers.append(types.InputSticker(
                sticker=FSInputFile(path), 
                emoji_list=["😀"], 
                format=sticker_format
            ))

    try:
        first_sticker = [input_stickers[0]]
        remaining_stickers = input_stickers[1:]

        created = False
        while not created:
            try:
                await bot.create_new_sticker_set(
                    user_id=message.from_user.id,
                    name=full_short_name,
                    title=new_title,
                    stickers=first_sticker,
                    sticker_format=sticker_format,
                    sticker_type=sticker_type
                )
                created = True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)

        total_added = 1
        failed = 0
        consecutive_fails = 0
        last_edit_time = time.time()

        for st_input in remaining_stickers:
            if consecutive_fails >= 3: break 
            added = False
            while not added:
                try:
                    await bot.add_sticker_to_set(user_id=message.from_user.id, name=full_short_name, sticker=st_input)
                    added = True
                    total_added += 1
                    consecutive_fails = 0 
                    await asyncio.sleep(0.8) 
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    failed += 1
                    consecutive_fails += 1
                    break

            current_processed = total_added + failed
            if time.time() - last_edit_time > 2.5 or current_processed == total_stickers:
                try:
                    await status_msg.edit_text(
                        f"⚡ **Processing in Progress...**\n\n"
                        f"{make_progress_bar(current_processed, total_stickers)}\n\n"
                        f"📈 **Processed:** `{current_processed} / {total_stickers}`\n"
                        f"🔄 *Bypassing API restrictions safely...*",
                        parse_mode="Markdown"
                    )
                    last_edit_time = time.time()
                except Exception: pass 

        final_time = int(time.time() - start_time)
        failed_text = f"\n⚠️ Skipped {total_stickers - total_added} items." if total_added < total_stickers else ""
        
        await status_msg.edit_text(
            f"🎉 **Pack Ready!**\n\n"
            f"✅ **Total Added:** `{total_added}`{failed_text}\n"
            f"⏱ **Time:** `{final_time}s`\n\n"
            f"👇 **Click below to add your pack:**\n"
            f"👉 [t.me/addstickers/{full_short_name}](https://t.me/addstickers/{full_short_name})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ Critcal Error:\n{e}")
    finally:
        if source_type == "zip" and temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
    await state.clear()

async def main():
    print("Elite Owner Bot Database Connected. Running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
