import aiohttp
import asyncio
import time
import requests
import re
import logging
import utils
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.methods import SendMessage
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from os import getenv
from dotenv import load_dotenv
import db

load_dotenv()

INTERVAL = 10  # seconds
LATEST = int(time.time() - 60 * 1)
START_DATE = time.strftime("%Y-%m-%d", time.gmtime(LATEST))
TASKS: dict[int, asyncio.Task] = {}

URL = "https://twitter154.p.rapidapi.com/search/search"

QUERY = {
    "query": "'pump.fun' filter:links",
    "section": "latest",
    "min_retweets": "0",
    "min_likes": "0",
    "limit": "20",
    "min_replies": "0",
    "start_date": START_DATE,
    "language": "en",
}

HEADERS = {
    "X-RapidAPI-Key": getenv("RAPIDAPI_KEY", ""),
    "X-RapidAPI-Host": "twitter154.p.rapidapi.com",
}


async def fetch_data(session: aiohttp.ClientSession):
    logging.info("Fetching data...")
    try:
        async with session.get(URL, headers=HEADERS, params=QUERY) as response:
            data = await response.json()
            return data
    except Exception as e:
        logging.error(f"Error fetching data: {e}")
        return None


async def fetch_data_continuation(session: aiohttp.ClientSession, continuation: str):
    logging.info("Fetching continuation data...")
    cont_query = QUERY.copy()
    cont_query["continuation_token"] = continuation
    try:
        async with session.get(URL, headers=HEADERS, params=cont_query) as response:
            data = await response.json()
            return data
    except Exception as e:
        logging.error(f"Error fetching continuation data: {e}")
        return None


async def send_tweet(tweet, chat_id: int, topic_id: int, bot: Bot):
    sanitazed_text = await utils.replace_short_urls(tweet["text"])
    pump_url = utils.extract_url_and_validate_mint_address(sanitazed_text)

    keyboard_buttons = [
        [
            InlineKeyboardButton(
                text="Tweet",
                url=f"https://twitter.com/{tweet['user']['username']}/status/{tweet['tweet_id']}",
            ),
            InlineKeyboardButton(
                text="Profile",
                url=f"https://x.com/{tweet['user']['username']}",
            ),
            InlineKeyboardButton(
                text="Block",
                callback_data=f"block:{tweet['user']['username']}:{tweet['user']['user_id']}",
            ),
        ],
    ]

    if pump_url:
        keyboard_buttons.append([InlineKeyboardButton(text="Pump", url=pump_url)])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    payload = (
        f"<b>NEW TWEET</b>\n\n"
        f"{await utils.replace_short_urls(tweet['text'])}\n\n"
        f"Followers: {tweet['user']['follower_count']}\n"
    )

    while True:
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                message_thread_id=topic_id,
                text=payload,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            await db.update_drop_messages(tweet["user"]["user_id"], msg.message_id)
            break
        except TelegramAPIError as e:
            logging.error(f"Failed to send message: {e}")
            await asyncio.sleep(1)


async def scheduled_function(session: aiohttp.ClientSession, chat_id: int, bot: Bot):
    global LATEST
    global INTERVAL
    global BOT
    global CHAT_ID

    while True:
        data = await fetch_data(session)

        if (
            not "results" in data
            or len(data["results"]) == 0
            or data["results"] is None
        ):
            logging.error("No results found.")
        else:
            new_latest = data["results"][0]["timestamp"]

            for tweet in data["results"]:
                if tweet["timestamp"] <= LATEST:
                    break

                if await db.check_banned(tweet["user"]["user_id"]):
                    logging.info(f"User {tweet['user']['user_id']} is banned")
                    continue

                if await db.check_drop(tweet["user"]["user_id"]):
                    await db.update_drop_posts(
                        tweet["user"]["user_id"], tweet["tweet_id"]
                    )
                else:
                    await db.insert_drop(
                        tweet["user"]["user_id"],
                        tweet["user"]["username"],
                        tweet["tweet_id"],
                        tweet["message_id"],
                    )

                topic_id = 0
                if tweet["user"]["follower_count"] > 500_000:
                    topic_id = 6
                elif tweet["user"]["follower_count"] > 100_000:
                    topic_id = 5
                elif tweet["user"]["follower_count"] > 10_000:
                    topic_id = 4
                else:
                    topic_id = 3

                logging.info(f"New tweet found: {tweet['tweet_id']}")
                await send_tweet(tweet, chat_id, topic_id, bot)
                await asyncio.sleep(1)

            LATEST = new_latest

        logging.info(f"Latest Timestamp: {LATEST}. Sleeping...")
        await asyncio.sleep(INTERVAL)


async def run(chat_id: int, bot: Bot):
    if chat_id in TASKS:
        await bot.send_message(chat_id, "Scrapping is already running")
        return
    async with aiohttp.ClientSession() as session:
        task = asyncio.create_task(scheduled_function(session, chat_id, bot))
        TASKS[chat_id] = task
        try:
            await bot.send_message(chat_id, "Starting Twitter scrapper...")
            await task
        except asyncio.CancelledError:
            logging.info(f"Task for chat_id {chat_id} was cancelled")


async def stop(chat_id: int, bot: Bot):
    task = TASKS.get(chat_id)
    if task:
        await bot.send_message(chat_id, "Stopping Twitter scrapper...")
        task.cancel()
        await task  # Ensure the task is properly awaited and any exceptions are handled
        del TASKS[chat_id]
    else:
        await bot.send_message(chat_id, "Twitter scrapper is not running")
