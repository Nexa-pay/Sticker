import asyncio
import os
import time
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class CloneStates(StatesGroup):
    waiting_for_sticker = State()
    waiting_for_title = State()
    waiting_for_name = State()

def make_progress_bar(current: int, total: int, length: int = 15) -> str:
    """Generates a text-based progress bar."""
    percent = current / total
    filled = int(length * percent)
    bar = '█' * filled + '░' * (length - filled)
    return f"`[{bar}] {percent:.0%}`"

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await message.reply(
        "👋 **Welcome to the Advanced Sticker Cloner!**\n\n"
        "Send or forward me any sticker, and I will clone the entire pack for you instantly.",
        parse_mode="Markdown"
    )
    await state.set_state(CloneStates.waiting_for_sticker)

@dp.message(CloneStates.waiting_for_sticker, F.sticker)
async def process_sticker(message: types.Message, state: FSMContext):
    sticker = message.sticker
    
    if not sticker.set_name:
        await message.reply("❌ This sticker doesn't belong to a pack. Please send a valid pack sticker.")
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
        sticker_format=sticker_format
    )
    
    await wait_msg.edit_text(
        f"📦 **Pack Found:** `{sticker_set.title}`\n"
        f"📊 **Total Stickers:** `{len(sticker_set.stickers)}`\n"
        f"⚙️ **Format:** `{sticker_format.title()}`\n\n"
        f"📝 **What do you want the Title of your new pack to be?**\n\n"
        f"*(Bot name: @Itzaakashbot | 1st owner: @Irtzaakash)*",
        parse_mode="Markdown"
    )
    await state.set_state(CloneStates.waiting_for_title)

@dp.message(CloneStates.waiting_for_title, F.text)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(new_title=message.text)
    bot_info = await bot.get_me()
    suffix = f"_by_{bot_info.username}"
    
    await message.reply(
        f"🔗 **Send me a Short Name for the link.**\n"
        f"Use English letters/numbers only.\n\n"
        f"💡 *Example:* `my_cool_stickers`\n"
        f"*(Telegram requires it to end with `{suffix}`. I will add this automatically!)*",
        parse_mode="Markdown"
    )
    await state.set_state(CloneStates.waiting_for_name)

@dp.message(CloneStates.waiting_for_name, F.text)
async def process_name(message: types.Message, state: FSMContext):
    status_msg = await message.reply("🚀 Initializing cloning sequence...")
    start_time = time.time()
    
    data = await state.get_data()
    original_stickers = data['original_stickers']
    sticker_format = data['sticker_format']
    new_title = data['new_title']
    total_stickers = len(original_stickers)
    
    bot_info = await bot.get_me()
    short_name = message.text.replace(" ", "_").lower()
    full_short_name = f"{short_name}_by_{bot_info.username}"

    input_stickers = [
        types.InputSticker(
            sticker=st.file_id, 
            emoji_list=[st.emoji] if st.emoji else ["😀"],
            format=sticker_format
        ) for st in original_stickers
    ]

    try:
        # Create pack with 1 sticker to safely initialize
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
                    sticker_format=sticker_format
                )
                created = True
            except TelegramRetryAfter as e:
                await status_msg.edit_text(f"⚠️ Telegram server cooldown. Starting in {e.retry_after}s...")
                await asyncio.sleep(e.retry_after + 1)

        # Loop and add remaining stickers at optimized speed
        total_added = 1
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
                    
                    # Push fast, but leave a tiny 0.2s breather to prevent standard bans
                    await asyncio.sleep(0.2) 
                    
                except TelegramRetryAfter as e:
                    # If we hit the absolute ceiling, respect the exact timeout silently
                    await asyncio.sleep(e.retry_after + 0.5)

            # Live Update UI - strictly update every 2.5 seconds to avoid edit limits
            current_time = time.time()
            if current_time - last_edit_time > 2.5 or total_added == total_stickers:
                p_bar = make_progress_bar(total_added, total_stickers)
                elapsed = int(current_time - start_time)
                
                ui_text = (
                    f"⚡ **Cloning in Progress...**\n\n"
                    f"{p_bar}\n\n"
                    f"📈 **Processed:** `{total_added} / {total_stickers}`\n"
                    f"⏱ **Time Elapsed:** `{elapsed}s`\n"
                    f"🔄 *Optimizing speed to prevent limits...*"
                )
                try:
                    await status_msg.edit_text(ui_text, parse_mode="Markdown")
                    last_edit_time = current_time
                except Exception:
                    pass # Ignore if edit fails due to exact match or slight rate limit

        # Final Success Message
        final_time = int(time.time() - start_time)
        await status_msg.edit_text(
            f"🎉 **Pack Successfully Cloned!**\n\n"
            f"✅ **Total Stickers:** `{total_added}`\n"
            f"⏱ **Total Time:** `{final_time} seconds`\n\n"
            f"👇 **Click below to add your new pack:**\n"
            f"👉 [t.me/addstickers/{full_short_name}](https://t.me/addstickers/{full_short_name})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ **Critcal Error:**\n`{e}`", parse_mode="Markdown")

    await state.clear()

async def main():
    print("Advanced Cloner Bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
