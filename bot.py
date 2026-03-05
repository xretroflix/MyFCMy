#!/usr/bin/env python3
"""
MyFC Forwarder v4.3 - Clean Media Edition
No forward header, no original caption, clean logs
"""

import asyncio
import random
import json
import os
import logging
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage
from telethon.sessions import StringSession

# Suppress Telethon internal logs
logging.getLogger('telethon').setLevel(logging.ERROR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('MyFC')

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_STRING = os.environ.get('SESSION_STRING', '')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))

if not API_ID or not API_HASH:
    raise ValueError("API_ID/API_HASH not set")
if not SESSION_STRING:
    raise ValueError("SESSION_STRING not set")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID not set")

DEFAULT_INTERVAL = 30
DEFAULT_VARIATION = 10
DEFAULT_LIMIT = 100
CHANNELS = {}

DATA_DIR = "/app/data"
if not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR)
    except:
        DATA_DIR = "."

DATA_FILE = os.path.join(DATA_DIR, "forwarder_data.json")
last_reset_date = None
channel_tasks = {}

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


def fix_channel_id(channel_id):
    id_str = str(channel_id).replace(" ", "")
    if id_str.startswith("-100"):
        return int(id_str)
    if id_str.startswith("-"):
        id_str = id_str[1:]
    if len(id_str) >= 10:
        return int(f"-100{id_str}")
    return int(f"-{id_str}") if not id_str.startswith("-") else int(id_str)


def save_data():
    try:
        data = {'channels': CHANNELS, 'last_reset_date': last_reset_date.isoformat() if last_reset_date else None}
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Save error: {e}")


def load_data():
    global CHANNELS, last_reset_date
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
            CHANNELS = data.get('channels', {})
            if data.get('last_reset_date'):
                last_reset_date = datetime.fromisoformat(data['last_reset_date'])
            logger.info(f"Loaded {len(CHANNELS)} channels")
    except Exception as e:
        logger.warning(f"Load error: {e}")


def reset_daily_counts():
    global last_reset_date
    today = datetime.now().date()
    if last_reset_date is None or last_reset_date.date() < today:
        for name in CHANNELS:
            CHANNELS[name]['daily_count'] = 0
        last_reset_date = datetime.now()
        save_data()


def get_random_interval(name):
    config = CHANNELS.get(name, {})
    base = config.get('interval', DEFAULT_INTERVAL)
    var = config.get('variation', DEFAULT_VARIATION)
    variation = random.randint(1, var) * random.choice([1, -1])
    return max(5, base + variation)


def get_content_type(message):
    if not message:
        return None
    if message.photo or isinstance(message.media, MessageMediaPhoto):
        return 'photos'
    if isinstance(message.media, MessageMediaDocument):
        if message.media.document:
            mime = message.media.document.mime_type or ''
            if mime.startswith('video/'):
                return 'videos'
            elif mime.startswith('audio/'):
                return 'audio'
            elif mime.startswith('image/'):
                return 'photos'
            else:
                return 'docs'
    if message.text or message.message:
        text = message.text or message.message
        if 'http://' in text or 'https://' in text:
            return 'links'
    if isinstance(message.media, MessageMediaWebPage):
        return 'links'
    return None


def should_forward(message, name):
    config = CHANNELS.get(name, {})
    allowed = config.get('content_types', ['photos', 'videos'])
    content_type = get_content_type(message)
    return content_type in allowed if content_type else False


async def send_as_new(dest, msg, custom_caption=None):
    """
    Send message as NEW:
    - No forward header
    - No original caption (removed)
    - Only custom caption if set
    """
    if msg.media:
        # Send media WITHOUT original caption
        # Only use custom caption if provided
        await client.send_file(dest, msg.media, caption=custom_caption or "")
    elif msg.text:
        # For text/links - send custom caption or original text
        await client.send_message(dest, custom_caption or msg.text)


@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    await event.respond(
        "**MyFC Forwarder v4.3**\n\n"
        "**SETUP:**\n"
        "`/quicksetup NAME SOURCE DEST INTERVAL VAR CONTENT`\n\n"
        "Example:\n"
        "`/quicksetup movies 3773414989 3255469862 15 5 photos,videos`\n\n"
        "**CONTROL:**\n"
        "`/test NAME` - Test now\n"
        "`/go NAME` - Start one\n"
        "`/go` - Start all\n"
        "`/stop NAME` - Stop one\n"
        "`/stop` - Stop all\n\n"
        "**INFO:**\n"
        "`/list` `/info NAME` `/stats`\n\n"
        "**SETTINGS:**\n"
        "`/interval` `/content` `/caption` `/limit` `/remove`"
    )


@client.on(events.NewMessage(pattern='/quicksetup'))
async def quicksetup_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 7:
        await event.respond(
            "**Usage:**\n"
            "`/quicksetup NAME SOURCE DEST INTERVAL VAR CONTENT`\n\n"
            "**Example:**\n"
            "`/quicksetup movies 3773414989 3255469862 15 5 photos,videos`"
        )
        return
    try:
        name = parts[1].lower()
        source = fix_channel_id(parts[2])
        dest = fix_channel_id(parts[3])
        interval = int(parts[4])
        variation = int(parts[5])
        content_str = parts[6].lower()
        if interval < 5:
            await event.respond("Minimum interval is 5 minutes")
            return
        valid_types = ['photos', 'videos', 'audio', 'docs', 'links']
        content_types = [t.strip() for t in content_str.split(',') if t.strip() in valid_types]
        if not content_types:
            await event.respond(f"Invalid content. Use: {', '.join(valid_types)}")
            return
        CHANNELS[name] = {
            'source_id': source,
            'dest_id': dest,
            'interval': interval,
            'variation': variation,
            'daily_limit': DEFAULT_LIMIT,
            'content_types': content_types,
            'caption': None,
            'enabled': False,
            'daily_count': 0,
            'forwarded_ids': [],
        }
        save_data()
        await event.respond(
            f"**Channel `{name}` created!**\n\n"
            f"Source: `{source}`\n"
            f"Dest: `{dest}`\n"
            f"Interval: {interval}+/-{variation} min\n"
            f"Content: {', '.join(content_types)}\n"
            f"Caption: None (clean media)\n\n"
            f"**Test:** `/test {name}`\n"
            f"**Start:** `/go {name}`"
        )
        logger.info(f"[+] Created: {name}")
    except Exception as e:
        await event.respond(f"Error: {e}")


@client.on(events.NewMessage(pattern='/test'))
async def test_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 2:
        await event.respond("**Usage:** `/test NAME`")
        return
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    config = CHANNELS[name]
    source = config.get('source_id')
    dest = config.get('dest_id')
    await event.respond(f"Testing `{name}`...")
    try:
        messages = await client.get_messages(source, limit=10)
        for msg in messages:
            content_type = get_content_type(msg)
            if content_type and content_type in config.get('content_types', []):
                custom_caption = config.get('caption')
                await send_as_new(dest, msg, custom_caption)
                await event.respond(f"**SUCCESS!** Sent 1 {content_type} (no original caption)")
                logger.info(f"[TEST] {name}: OK")
                return
        await event.respond(f"No matching content found")
    except Exception as e:
        logger.error(f"[TEST] {name}: {e}")
        await event.respond(f"**FAILED:** {e}")


@client.on(events.NewMessage(pattern='/go'))
async def go_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if not CHANNELS:
        await event.respond("No channels. Use `/quicksetup`")
        return
    if len(parts) > 1:
        name = parts[1].lower()
        if name not in CHANNELS:
            await event.respond(f"Channel `{name}` not found")
            return
        CHANNELS[name]['enabled'] = True
        save_data()
        if name not in channel_tasks or channel_tasks[name].done():
            channel_tasks[name] = asyncio.create_task(forward_loop(name))
        await event.respond(f"Started `{name}`")
        logger.info(f"[>] Started: {name}")
    else:
        started = []
        for name, config in CHANNELS.items():
            if config.get('source_id') and config.get('dest_id'):
                CHANNELS[name]['enabled'] = True
                if name not in channel_tasks or channel_tasks[name].done():
                    channel_tasks[name] = asyncio.create_task(forward_loop(name))
                started.append(name)
        save_data()
        await event.respond(f"**Started:** {', '.join(started)}")
        logger.info(f"[>] Started all: {', '.join(started)}")


@client.on(events.NewMessage(pattern='/stop'))
async def stop_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) > 1:
        name = parts[1].lower()
        if name not in CHANNELS:
            await event.respond(f"Channel `{name}` not found")
            return
        CHANNELS[name]['enabled'] = False
        if name in channel_tasks:
            channel_tasks[name].cancel()
            del channel_tasks[name]
        save_data()
        await event.respond(f"Stopped `{name}`")
        logger.info(f"[X] Stopped: {name}")
    else:
        for name in CHANNELS:
            CHANNELS[name]['enabled'] = False
            if name in channel_tasks:
                channel_tasks[name].cancel()
        channel_tasks.clear()
        save_data()
        await event.respond("**All stopped**")
        logger.info("[X] Stopped all")


@client.on(events.NewMessage(pattern='/list'))
async def list_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    if not CHANNELS:
        await event.respond("No channels. Use `/quicksetup`")
        return
    text = "**Channels:**\n\n"
    for name, config in CHANNELS.items():
        status = "ON" if config.get('enabled') else "OFF"
        text += f"**{name}** [{status}] - {config.get('daily_count', 0)}/{config.get('daily_limit')}\n"
    await event.respond(text)


@client.on(events.NewMessage(pattern='/info'))
async def info_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 2:
        await list_handler(event)
        return
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    config = CHANNELS[name]
    status = "ON" if config.get('enabled') else "OFF"
    text = f"**{name}** [{status}]\n\n"
    text += f"Source: `{config.get('source_id')}`\n"
    text += f"Dest: `{config.get('dest_id')}`\n"
    text += f"Interval: {config.get('interval')}+/-{config.get('variation')} min\n"
    text += f"Content: {', '.join(config.get('content_types', []))}\n"
    text += f"Caption: {config.get('caption') or 'None (clean)'}\n"
    text += f"Today: {config.get('daily_count', 0)}/{config.get('daily_limit')}"
    await event.respond(text)


@client.on(events.NewMessage(pattern='/stats'))
async def stats_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    reset_daily_counts()
    if not CHANNELS:
        await event.respond("No channels")
        return
    text = f"**Stats - {datetime.now().strftime('%Y-%m-%d')}**\n\n"
    total = 0
    for name, config in CHANNELS.items():
        count = config.get('daily_count', 0)
        status = "ON" if config.get('enabled') else "OFF"
        text += f"{name} [{status}]: {count}/{config.get('daily_limit')}\n"
        total += count
    text += f"\n**Total:** {total}"
    await event.respond(text)


@client.on(events.NewMessage(pattern='/interval'))
async def interval_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 4:
        await event.respond("**Usage:** `/interval NAME BASE VAR`")
        return
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    try:
        base = int(parts[2])
        var = int(parts[3])
        if base < 5:
            await event.respond("Minimum 5 minutes")
            return
        CHANNELS[name]['interval'] = base
        CHANNELS[name]['variation'] = var
        save_data()
        await event.respond(f"**Interval:** {base}+/-{var} min")
    except:
        await event.respond("Invalid numbers")


@client.on(events.NewMessage(pattern='/content'))
async def content_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 3:
        await event.respond("**Usage:** `/content NAME photos,videos`")
        return
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    valid = ['photos', 'videos', 'audio', 'docs', 'links']
    types = [t.strip() for t in parts[2].lower().split(',') if t.strip() in valid]
    if not types:
        await event.respond(f"Invalid. Use: {', '.join(valid)}")
        return
    CHANNELS[name]['content_types'] = types
    save_data()
    await event.respond(f"**Content:** {', '.join(types)}")


@client.on(events.NewMessage(pattern='/caption'))
async def caption_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split(maxsplit=2)
    if len(parts) < 3:
        await event.respond("**Usage:** `/caption NAME text`\n**Clear:** `/caption NAME clear`")
        return
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    caption = parts[2]
    if caption.lower() == 'clear':
        CHANNELS[name]['caption'] = None
        await event.respond("Caption cleared (clean media)")
    else:
        CHANNELS[name]['caption'] = caption
        await event.respond(f"**Caption:** {caption}")
    save_data()


@client.on(events.NewMessage(pattern='/limit'))
async def limit_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 3:
        await event.respond("**Usage:** `/limit NAME NUMBER`")
        return
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    try:
        limit = int(parts[2])
        CHANNELS[name]['daily_limit'] = limit
        save_data()
        await event.respond(f"**Limit:** {limit}/day")
    except:
        await event.respond("Invalid number")


@client.on(events.NewMessage(pattern='/remove'))
async def remove_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 2:
        await event.respond("**Usage:** `/remove NAME`")
        return
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    if name in channel_tasks:
        channel_tasks[name].cancel()
        del channel_tasks[name]
    del CHANNELS[name]
    save_data()
    await event.respond(f"Removed `{name}`")
    logger.info(f"[-] Removed: {name}")


async def forward_loop(name):
    logger.info(f"[{name}] Loop started")
    while CHANNELS.get(name, {}).get('enabled', False):
        try:
            reset_daily_counts()
            config = CHANNELS.get(name)
            if not config:
                break
            if config.get('daily_count', 0) >= config.get('daily_limit', DEFAULT_LIMIT):
                logger.info(f"[{name}] Limit reached")
                await asyncio.sleep(3600)
                continue
            source = config.get('source_id')
            dest = config.get('dest_id')
            if not source or not dest:
                break
            try:
                messages = await client.get_messages(source, limit=50)
                forwarded = set(config.get('forwarded_ids', []))
                for msg in messages:
                    if msg.id in forwarded:
                        continue
                    if not should_forward(msg, name):
                        continue
                    try:
                        custom_caption = config.get('caption')
                        await send_as_new(dest, msg, custom_caption)
                        CHANNELS[name]['forwarded_ids'].append(msg.id)
                        CHANNELS[name]['forwarded_ids'] = CHANNELS[name]['forwarded_ids'][-500:]
                        CHANNELS[name]['daily_count'] = config.get('daily_count', 0) + 1
                        save_data()
                        logger.info(f"[{name}] Sent #{CHANNELS[name]['daily_count']}")
                        break
                    except Exception as e:
                        logger.error(f"[{name}] Send error: {e}")
            except Exception as e:
                logger.error(f"[{name}] Read error: {e}")
            interval = get_random_interval(name)
            logger.info(f"[{name}] Next in {interval}m")
            await asyncio.sleep(interval * 60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[{name}] Error: {e}")
            await asyncio.sleep(60)
    logger.info(f"[{name}] Loop ended")


async def main():
    print("=" * 40)
    print("  MyFC Forwarder v4.3")
    print("=" * 40)
    load_data()
    logger.info(f"Admin: {ADMIN_ID}")
    logger.info(f"Channels: {len(CHANNELS)}")
    await client.start()
    me = await client.get_me()
    logger.info(f"Logged in: {me.first_name}")
    logger.info("Ready! Send /start")
    print("=" * 40)
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())
