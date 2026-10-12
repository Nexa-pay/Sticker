import asyncio
import os
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

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await message.reply("Send or forward me any sticker, and I will clone the entire pack for you!")
    await state.set_state(CloneStates.waiting_for_sticker)

@dp.message(CloneStates.waiting_for_sticker, F.sticker)
async def process_sticker(message: types.Message, state: FSMContext):
    sticker = message.sticker
    
    if not sticker.set_name:
        await message.reply("This sticker doesn't belong to a pack. Please send a sticker from a valid pack.")
        return

    try:
        sticker_set = await bot.get_sticker_set(name=sticker.set_name)
    except Exception as e:
        await message.reply(f"Could not fetch the pack: {e}")
        return

    sticker_format = "animated" if sticker.is_animated else "video" if sticker.is_video else "static"
    await state.update_data(
        original_stickers=sticker_set.stickers,
        sticker_format=sticker_format
    )
    
    await message.reply(
        f"Pack found: {sticker_set.title} ({len(sticker_set.stickers)} stickers).\n\n"
        f"What do you want the Title of your new pack to be?\n\n"
        f"Bot name this @Itzaakashbot\n"
        f"1st owner username @Irtzaakash"
    )
    await state.set_state(CloneStates.waiting_for_title)

@dp.message(CloneStates.waiting_for_title, F.text)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(new_title=message.text)
    bot_info = await bot.get_me()
    suffix = f"_by_{bot_info.username}"
    
    await message.reply(
        f"Send me a **Short Name** for the link (English letters/numbers).\n"
        f"*(I will automatically add `{suffix}` to it)*",
        parse_mode="Markdown"
    )
    await state.set_state(CloneStates.waiting_for_name)

@dp.message(CloneStates.waiting_for_name, F.text)
async def process_name(message: types.Message, state: FSMContext):
    status_msg = await message.reply("Cloning your pack... This might take a minute to avoid rate limits ⏳")
    
    data = await state.get_data()
    original_stickers = data['original_stickers']
    sticker_format = data['sticker_format']
    new_title = data['new_title']
    
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
        # STRATEGY: Create pack with ONLY 1 sticker first to avoid CreateNewStickerSet limits
        first_sticker = [input_stickers[0]]
        remaining_stickers = input_stickers[1:]

        # 1. Create the pack (with an infinite retry loop just in case)
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
                wait_time = e.retry_after + 2  # Add 2 seconds for safety
                await status_msg.edit_text(f"Telegram cooling down. Pausing for {wait_time} seconds... ⏳")
                await asyncio.sleep(wait_time)

        # 2. Add the rest of the stickers one by one with a delay
        total_added = 1
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
                    
                    # Intentional delay to NEVER hit the rate limit again
                    await asyncio.sleep(1.2) 
                    
                except TelegramRetryAfter as e:
                    wait_time = e.retry_after + 2
                    await status_msg.edit_text(f"Adding paused by Telegram. Waiting {wait_time} seconds... ⏳")
                    await asyncio.sleep(wait_time)

            # Update the user every 5 stickers so they know it's working
            if total_added % 5 == 0:
                await status_msg.edit_text(f"Cloning carefully to avoid limits... {total_added}/{len(input_stickers)} uploaded ⏳")

        await status_msg.edit_text(
            f"🎉 **Pack Cloned Successfully!**\n\n"
            f"Add it here: [t.me/addstickers/{full_short_name}](https://t.me/addstickers/{full_short_name})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to create pack.\nError: `{e}`", parse_mode="Markdown")

    await state.clear()

async def main():
    print("Cloner Bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
