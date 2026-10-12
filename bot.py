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
from aiogram.types import FSInputFile

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class CloneStates(StatesGroup):
    waiting_for_sticker = State()
    waiting_for_title = State()
    waiting_for_name = State()

def make_progress_bar(current: int, total: int, length: int = 15) -> str:
    if total == 0: return f"`[{'░'*length}] 0%`"
    percent = min(current / total, 1.0)
    filled = int(length * percent)
    bar = '█' * filled + '░' * (length - filled)
    return f"`[{bar}] {percent:.0%}`"

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await message.reply(
        "👋 **Welcome to the Elite Sticker Creator!**\n\n"
        "1️⃣ **Forward me a sticker** to clone an entire pack.\n"
        "2️⃣ **Send me a `.zip` file** containing `.webm`, `.webp`, or `.tgs` files to build a pack from scratch.",
        parse_mode="Markdown"
    )
    await state.set_state(CloneStates.waiting_for_sticker)

# --- HANDLER 1: FORWARDED STICKERS ---
@dp.message(CloneStates.waiting_for_sticker, F.sticker)
async def process_sticker(message: types.Message, state: FSMContext):
    sticker = message.sticker
    if not sticker.set_name:
        await message.reply("❌ This sticker doesn't belong to a pack. Please send a valid pack sticker or a ZIP file.")
        return

    wait_msg = await message.reply("🔍 Fetching pack details...")

    try:
        sticker_set = await bot.get_sticker_set(name=sticker.set_name)
    except Exception as e:
        await wait_msg.edit_text(f"❌ Could not fetch the pack: `{e}`", parse_mode="Markdown")
        return

    sticker_format = "animated" if sticker.is_animated else "video" if sticker.is_video else "static"
    
    await state.update_data(
        original_stickers=sticker_set.stickers,
        sticker_format=sticker_format,
        sticker_type=sticker_set.sticker_type,
        source_type="pack"
    )
    
    await wait_msg.edit_text(
        f"📦 **Pack Cloner Ready:** `{sticker_set.title}`\n"
        f"📊 **Total Stickers:** `{len(sticker_set.stickers)}`\n"
        f"⚙️ **Format:** `{sticker_format.title()}`\n\n"
        f"📝 **What do you want the Title of your new pack to be?**",
        parse_mode="Markdown"
    )
    await state.set_state(CloneStates.waiting_for_title)

# --- HANDLER 2: ZIP FILES ---
@dp.message(CloneStates.waiting_for_sticker, F.document)
async def process_zip(message: types.Message, state: FSMContext):
    if not message.document.file_name.lower().endswith('.zip'):
        await message.reply("❌ Please send a valid `.zip` file containing stickers.")
        return

    wait_msg = await message.reply("📥 Downloading and extracting ZIP... Please wait ⏳")
    
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
    except Exception as e:
        await wait_msg.edit_text("❌ Failed to read ZIP. Ensure it is not corrupted.")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    if not valid_files:
        await wait_msg.edit_text("❌ No `.webm`, `.webp`, or `.tgs` files found inside the ZIP.")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    valid_files.sort() # Keep alphabetical order
    first_ext = valid_files[0].lower().split('.')[-1]
    
    if first_ext == 'webm':
        sticker_format = "video"
    elif first_ext == 'tgs':
        sticker_format = "animated"
    else:
        sticker_format = "static"

    await state.update_data(
        original_stickers=valid_files,
        sticker_format=sticker_format,
        sticker_type="regular",
        source_type="zip",
        temp_dir=temp_dir
    )

    await wait_msg.edit_text(
        f"📦 **ZIP Extracted Successfully!**\n"
        f"📊 **Files Found:** `{len(valid_files)}`\n"
        f"⚙️ **Format Detected:** `{sticker_format.title()}`\n\n"
        f"📝 **What do you want the Title of your new pack to be?**",
        parse_mode="Markdown"
    )
    await state.set_state(CloneStates.waiting_for_title)

# --- SHARED TITLE & NAME HANDLERS ---
@dp.message(CloneStates.waiting_for_title, F.text)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(new_title=message.text)
    
    user_prefix = message.from_user.username or str(message.from_user.id)
    user_prefix = re.sub(r'[^a-zA-Z0-9]', '', user_prefix).lower()
    
    await message.reply(
        f"🔗 **Send me a Short Name for the link.**\n"
        f"Use English letters/numbers only.\n\n"
        f"💡 *Example:* If you type `magic`, your final link will look like:\n"
        f"`{user_prefix}_magic_by_Itzaakashbot`",
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
    
    # 1. Format the Short Name: {user_prefix}_{clean_name}_by_{bot_username}
    user_prefix = message.from_user.username or str(message.from_user.id)
    user_prefix = re.sub(r'[^a-zA-Z0-9]', '', user_prefix).lower()
    
    clean_short_name = re.sub(r'[^a-zA-Z0-9]', '', message.text).lower()
    if not clean_short_name: clean_short_name = "pack"
    
    # Telegram max short name length is 64. Truncate clean_short_name to ensure it fits.
    max_clean_len = 64 - len(f"{user_prefix}__by_{bot_info.username}")
    clean_short_name = clean_short_name[:max_clean_len]

    full_short_name = f"{user_prefix}_{clean_short_name}_by_{bot_info.username}"

    # 2. Prepare Stickers (Handle either Telegram file_ids OR local ZIP files)
    input_stickers = []
    if source_type == "pack":
        for st in original_stickers:
            input_stickers.append(types.InputSticker(
                sticker=st.file_id, 
                emoji_list=[st.emoji] if st.emoji else ["😀"],
                format=sticker_format
            ))
    elif source_type == "zip":
        for path in original_stickers:
            input_stickers.append(types.InputSticker(
                sticker=FSInputFile(path), 
                emoji_list=["😀"], # Default emoji for zip files
                format=sticker_format
            ))

    try:
        first_sticker = [input_stickers[0]]
        remaining_stickers = input_stickers[1:]

        # 3. Create the Pack
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

        # 4. Add remaining stickers
        total_added = 1
        failed = 0
        last_edit_time = time.time()

        for st_input in remaining_stickers:
            added = False
            while not added:
                try:
                    await bot.add_sticker_to_set(
                        user_id=message.from_user.id,
                        name=full_short_name,
                        sticker=st_input
                    )
                    added = True
                    total_added += 1
                    # Slower delay if uploading files from a ZIP to prevent heavy rate limits
                    await asyncio.sleep(0.5 if source_type == "zip" else 0.3) 
                    
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 0.5)
                except Exception as e:
                    print(f"Skipping sticker: {e}")
                    failed += 1
                    break

            # 5. Live UI Update
            current_processed = total_added + failed
            current_time = time.time()
            if current_time - last_edit_time > 2.5 or current_processed == total_stickers:
                p_bar = make_progress_bar(current_processed, total_stickers)
                elapsed = int(current_time - start_time)
                
                ui_text = (
                    f"⚡ **Processing in Progress...**\n\n"
                    f"{p_bar}\n\n"
                    f"📈 **Processed:** `{current_processed} / {total_stickers}`\n"
                    f"⏱ **Time Elapsed:** `{elapsed}s`\n"
                    f"🔄 *Handling Telegram API securely...*"
                )
                try:
                    await status_msg.edit_text(ui_text, parse_mode="Markdown")
                    last_edit_time = current_time
                except Exception:
                    pass 

        # 6. Success Output
        final_time = int(time.time() - start_time)
        failed_text = f"\n⚠️ Skipped {failed} invalid files." if failed > 0 else ""
        
        await status_msg.edit_text(
            f"🎉 **Pack Ready!**\n\n"
            f"✅ **Total Stickers:** `{total_added}`{failed_text}\n"
            f"⏱ **Total Time:** `{final_time} seconds`\n\n"
            f"👇 **Click below to add your new pack:**\n"
            f"👉 [t.me/addstickers/{full_short_name}](https://t.me/addstickers/{full_short_name})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ Critcal Error:\n{e}")

    finally:
        # VERY IMPORTANT: Delete the extracted ZIP files from the VPS hard drive
        if source_type == "zip" and temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
            
    await state.clear()

async def main():
    print("Elite Cloner Bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
