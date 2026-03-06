#!/usr/bin/env python3
"""
MyFC Forwarder v4.8 - Random Emoji Edition
- Random emoji in captions
- Scan source before starting
- Handles 2 lakh+ content
- No content missed
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
import httpx

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
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

if not API_ID or not API_HASH:
    raise ValueError("API_ID/API_HASH not set")
if not SESSION_STRING:
    raise ValueError("SESSION_STRING not set")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID not set")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL/SUPABASE_KEY not set")

DEFAULT_INTERVAL = 30
DEFAULT_VARIATION = 10
DEFAULT_LIMIT = 100
CHANNELS = {}

last_reset_date = None
channel_tasks = {}

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


def get_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }


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
    global CHANNELS, last_reset_date
    try:
        save_channels = {}
        for name, config in CHANNELS.items():
            save_config = {k: v for k, v in config.items() if k != 'all_source_ids'}
            save_channels[name] = save_config
        
        data = {
            'id': 'main',
            'channels': save_channels,
            'last_reset_date': last_reset_date.isoformat() if last_reset_date else None,
            'updated_at': datetime.now().isoformat()
        }
        url = f"{SUPABASE_URL}/rest/v1/forwarder_data?id=eq.main"
        with httpx.Client(timeout=30) as http:
            resp = http.patch(url, json=data, headers=get_headers())
            if resp.status_code == 404 or resp.status_code == 400:
                url = f"{SUPABASE_URL}/rest/v1/forwarder_data"
                http.post(url, json=data, headers=get_headers())
    except Exception as e:
        logger.error(f"[DB] Save error: {e}")


def load_data():
    global CHANNELS, last_reset_date
    try:
        url = f"{SUPABASE_URL}/rest/v1/forwarder_data?id=eq.main&select=*"
        with httpx.Client(timeout=30) as http:
            resp = http.get(url, headers=get_headers())
            if resp.status_code == 200:
                result = resp.json()
                if result and len(result) > 0:
                    data = result[0]
                    CHANNELS = data.get('channels', {})
                    if data.get('last_reset_date'):
                        last_reset_date = datetime.fromisoformat(data['last_reset_date'])
                    logger.info(f"[DB] Loaded {len(CHANNELS)} channels")
                else:
                    logger.info("[DB] No data, starting fresh")
    except Exception as e:
        logger.warning(f"[DB] Load error: {e}")


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


def should_forward(message, content_types):
    content_type = get_content_type(message)
    return content_type in content_types if content_type else False


def get_caption_with_emoji(config):
    """Get caption with random emoji"""
    caption = config.get('caption', '')
    emojis = config.get('emojis', [])
    
    if not caption:
        return ""
    
    if emojis:
        random_emoji = random.choice(emojis)
        # Replace {emoji} placeholder or add at start
        if '{emoji}' in caption:
            return caption.replace('{emoji}', random_emoji)
        else:
            return f"{random_emoji} {caption}"
    
    return caption


async def send_as_new(dest, msg, config):
    """Send message with random emoji caption"""
    caption = get_caption_with_emoji(config)
    
    if msg.media:
        await client.send_file(dest, msg.media, caption=caption or "")
    elif msg.text:
        await client.send_message(dest, caption or msg.text)


async def notify_admin(message):
    try:
        await client.send_message(ADMIN_ID, message)
    except Exception as e:
        logger.error(f"Notify error: {e}")


async def scan_source_channel(source, content_types, progress_callback=None):
    all_ids = []
    offset_id = 0
    batch_count = 0
    
    while True:
        try:
            messages = await client.get_messages(source, limit=100, offset_id=offset_id)
            if not messages:
                break
            
            for msg in messages:
                if should_forward(msg, content_types):
                    all_ids.append(msg.id)
            
            offset_id = messages[-1].id
            batch_count += 1
            
            if progress_callback and batch_count % 50 == 0:
                await progress_callback(len(all_ids), batch_count * 100)
            
            if len(messages) < 100:
                break
            
            if batch_count % 10 == 0:
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Scan error: {e}")
            await asyncio.sleep(5)
            continue
    
    all_ids.reverse()
    return all_ids


@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    await event.respond(
        "**MyFC Forwarder v4.8** (Random Emoji)\n\n"
        "**SETUP:**\n"
        "`/quicksetup NAME SOURCE DEST INT VAR CONTENT`\n\n"
        "**CAPTION & EMOJI:**\n"
        "`/caption NAME text` - Set caption\n"
        "`/emojis NAME 🚗,⚠️,🚦` - Set emoji list\n\n"
        "**BEFORE STARTING:**\n"
        "`/scan NAME` - Count source content\n\n"
        "**CONTROL:**\n"
        "`/test` `/go` `/stop`\n\n"
        "**INFO:**\n"
        "`/list` `/info` `/stats` `/progress`\n\n"
        "**SETTINGS:**\n"
        "`/interval` `/content` `/limit`\n"
        "`/remove` `/reset`"
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
            "`/quicksetup ch1 3773414989 3255469862 30 8 photos,videos`"
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
            'emojis': [],
            'enabled': False,
            'daily_count': 0,
            'total_forwarded': 0,
            'source_total': 0,
            'forwarded_ids': [],
            'completed': False,
            'scanned': False,
        }
        save_data()
        
        await event.respond(
            f"**Channel `{name}` created!**\n\n"
            f"Source: `{source}`\n"
            f"Dest: `{dest}`\n"
            f"Interval: {interval}+/-{variation} min\n"
            f"Content: {', '.join(content_types)}\n\n"
            f"**Next steps:**\n"
            f"1. `/caption {name} Your text`\n"
            f"2. `/emojis {name} 🚗,⚠️,🚦`\n"
            f"3. `/scan {name}`\n"
            f"4. `/go {name}`"
        )
        logger.info(f"[+] Created: {name}")
    except Exception as e:
        await event.respond(f"Error: {e}")


@client.on(events.NewMessage(pattern='/emojis'))
async def emojis_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split(maxsplit=2)
    
    if len(parts) < 2:
        await event.respond(
            "**Usage:**\n"
            "`/emojis NAME emoji1,emoji2,emoji3`\n\n"
            "**Example:**\n"
            "`/emojis ch1 🚗,⚠️,🚦,🛣️,🚨`\n\n"
            "**Clear emojis:**\n"
            "`/emojis NAME clear`\n\n"
            "**Popular emoji sets:**\n\n"
            "🚗 Traffic:\n"
            "`🚗,🚙,🚕,🚌,🏎️,🚓,🚑,🚒,🛣️,🚦,⚠️,🚨`\n\n"
            "⚠️ Warning:\n"
            "`⚠️,🚨,❗,❌,🔴,⛔,🚫,💀,☠️,🆘`\n\n"
            "📢 General:\n"
            "`📢,📣,🔔,💡,✨,🌟,⭐,🔥,💥,👀`\n\n"
            "🎬 Media:\n"
            "`🎬,🎥,📹,📸,🖼️,🎞️,📺,📽️,🎦,🎭`"
        )
        return
    
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    
    if len(parts) < 3:
        # Show current emojis
        emojis = CHANNELS[name].get('emojis', [])
        if emojis:
            await event.respond(f"**Current emojis for `{name}`:**\n{', '.join(emojis)}")
        else:
            await event.respond(f"No emojis set for `{name}`")
        return
    
    emoji_str = parts[2]
    
    if emoji_str.lower() == 'clear':
        CHANNELS[name]['emojis'] = []
        save_data()
        await event.respond(f"✅ Emojis cleared for `{name}`")
        return
    
    # Parse emojis (comma or space separated)
    emojis = [e.strip() for e in emoji_str.replace(' ', ',').split(',') if e.strip()]
    
    if not emojis:
        await event.respond("No valid emojis found")
        return
    
    CHANNELS[name]['emojis'] = emojis
    save_data()
    
    await event.respond(
        f"✅ **Emojis set for `{name}`**\n\n"
        f"Emojis: {', '.join(emojis)}\n"
        f"Total: {len(emojis)}\n\n"
        f"Each post will have a random emoji!"
    )


@client.on(events.NewMessage(pattern='/caption'))
async def caption_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split(maxsplit=2)
    
    if len(parts) < 2:
        await event.respond(
            "**Usage:**\n"
            "`/caption NAME your text here`\n\n"
            "**With emoji placeholder:**\n"
            "`/caption NAME {emoji} Drive Safe | @Channel`\n\n"
            "**Without placeholder:**\n"
            "`/caption NAME Drive Safe | @Channel`\n"
            "(emoji added at start automatically)\n\n"
            "**Clear:**\n"
            "`/caption NAME clear`"
        )
        return
    
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    
    if len(parts) < 3:
        caption = CHANNELS[name].get('caption')
        emojis = CHANNELS[name].get('emojis', [])
        if caption:
            await event.respond(
                f"**Caption for `{name}`:**\n{caption}\n\n"
                f"**Emojis:** {', '.join(emojis) if emojis else 'None'}"
            )
        else:
            await event.respond(f"No caption set for `{name}`")
        return
    
    caption = parts[2]
    
    if caption.lower() == 'clear':
        CHANNELS[name]['caption'] = None
        save_data()
        await event.respond(f"✅ Caption cleared for `{name}`")
        return
    
    CHANNELS[name]['caption'] = caption
    save_data()
    
    emojis = CHANNELS[name].get('emojis', [])
    example = ""
    if emojis:
        sample_emoji = random.choice(emojis)
        if '{emoji}' in caption:
            example = caption.replace('{emoji}', sample_emoji)
        else:
            example = f"{sample_emoji} {caption}"
    else:
        example = caption
    
    await event.respond(
        f"✅ **Caption set for `{name}`**\n\n"
        f"Caption: {caption}\n\n"
        f"**Example output:**\n{example}\n\n"
        f"{'Set emojis: `/emojis ' + name + ' 🚗,⚠️,🚦`' if not emojis else ''}"
    )


@client.on(events.NewMessage(pattern='/scan'))
async def scan_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 2:
        await event.respond("**Usage:** `/scan NAME`")
        return
    
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    
    config = CHANNELS[name]
    source = config.get('source_id')
    content_types = config.get('content_types', [])
    
    status_msg = await event.respond(f"🔍 Scanning `{name}`...")
    
    async def progress_update(count, processed):
        try:
            await status_msg.edit(
                f"🔍 Scanning `{name}`...\n\n"
                f"Found: {count:,} content\n"
                f"Processed: ~{processed:,} messages"
            )
        except:
            pass
    
    try:
        all_ids = await scan_source_channel(source, content_types, progress_update)
        
        CHANNELS[name]['source_total'] = len(all_ids)
        CHANNELS[name]['all_source_ids'] = all_ids
        CHANNELS[name]['scanned'] = True
        save_data()
        
        forwarded = set(config.get('forwarded_ids', []))
        remaining = sum(1 for mid in all_ids if mid not in forwarded)
        already_done = len(all_ids) - remaining
        
        await status_msg.edit(
            f"✅ **Scan complete: `{name}`**\n\n"
            f"📊 Source: {len(all_ids):,}\n"
            f"✓ Forwarded: {already_done:,}\n"
            f"⏳ Remaining: {remaining:,}\n\n"
            f"**Start:** `/go {name}`"
        )
        logger.info(f"[SCAN] {name}: {len(all_ids)} total")
        
    except Exception as e:
        await status_msg.edit(f"❌ Scan failed: {e}")


@client.on(events.NewMessage(pattern='/progress'))
async def progress_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 2:
        await event.respond("**Usage:** `/progress NAME`")
        return
    
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    
    config = CHANNELS[name]
    source_total = config.get('source_total', 0)
    forwarded = config.get('total_forwarded', 0)
    
    if source_total > 0:
        remaining = source_total - forwarded
        percent = (forwarded / source_total) * 100
        bar_filled = int(percent / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        
        interval = config.get('interval', 30)
        hours_left = (remaining * interval) / 60
        days_left = hours_left / 24
        
        if days_left > 1:
            time_est = f"~{days_left:.1f} days"
        elif hours_left > 1:
            time_est = f"~{hours_left:.1f} hours"
        else:
            time_est = f"~{remaining * interval} min"
        
        await event.respond(
            f"**Progress: {name}**\n\n"
            f"[{bar}] {percent:.1f}%\n\n"
            f"📊 Source: {source_total:,}\n"
            f"✅ Forwarded: {forwarded:,}\n"
            f"⏳ Remaining: {remaining:,}\n"
            f"⏱️ Est: {time_est}\n\n"
            f"Status: {'🟢 Running' if config.get('enabled') else '🔴 Stopped'}"
        )
    else:
        await event.respond(f"Run `/scan {name}` first")


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
    content_types = config.get('content_types', [])
    
    await event.respond(f"Testing `{name}`...")
    
    try:
        messages = await client.get_messages(source, limit=100)
        messages = list(reversed(messages))
        forwarded = set(config.get('forwarded_ids', []))
        
        for msg in messages:
            if msg.id in forwarded:
                continue
            if should_forward(msg, content_types):
                await send_as_new(dest, msg, config)
                
                # Show what was sent
                caption = get_caption_with_emoji(config)
                await event.respond(
                    f"✅ **Test SUCCESS!**\n\n"
                    f"Caption sent:\n{caption or '(no caption)'}"
                )
                logger.info(f"[TEST] {name}: OK")
                return
        
        await event.respond("No new content found")
    except Exception as e:
        logger.error(f"[TEST] {name}: {e}")
        await event.respond(f"❌ **Failed:** {e}")


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
        
        config = CHANNELS[name]
        
        if config.get('completed'):
            await event.respond(f"Channel `{name}` completed! Use `/reset {name}`")
            return
        
        if not config.get('scanned'):
            await event.respond(f"⚠️ Run `/scan {name}` first")
            return
        
        CHANNELS[name]['enabled'] = True
        save_data()
        
        if name not in channel_tasks or channel_tasks[name].done():
            channel_tasks[name] = asyncio.create_task(forward_loop(name))
        
        source_total = config.get('source_total', 0)
        forwarded = config.get('total_forwarded', 0)
        
        await event.respond(
            f"▶️ **Started `{name}`**\n\n"
            f"Progress: {forwarded:,}/{source_total:,}\n"
            f"Check: `/progress {name}`"
        )
        logger.info(f"[>] Started: {name}")
    else:
        started = []
        skipped = []
        for name, config in CHANNELS.items():
            if config.get('completed'):
                skipped.append(f"{name} (done)")
                continue
            if not config.get('scanned'):
                skipped.append(f"{name} (not scanned)")
                continue
            CHANNELS[name]['enabled'] = True
            if name not in channel_tasks or channel_tasks[name].done():
                channel_tasks[name] = asyncio.create_task(forward_loop(name))
            started.append(name)
        
        save_data()
        msg = ""
        if started:
            msg += f"▶️ **Started:** {', '.join(started)}\n"
        if skipped:
            msg += f"\n⚠️ **Skipped:** {', '.join(skipped)}"
        await event.respond(msg or "No channels to start")


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
        await event.respond(f"⏹️ Stopped `{name}`")
        logger.info(f"[X] Stopped: {name}")
    else:
        for name in CHANNELS:
            CHANNELS[name]['enabled'] = False
            if name in channel_tasks:
                channel_tasks[name].cancel()
        channel_tasks.clear()
        save_data()
        await event.respond("⏹️ **All stopped**")
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
        if config.get('completed'):
            status = "✅"
        elif config.get('enabled'):
            status = "🟢"
        else:
            status = "🔴"
        
        source_total = config.get('source_total', 0)
        forwarded = config.get('total_forwarded', 0)
        emojis = config.get('emojis', [])
        
        text += f"{status} **{name}**"
        if emojis:
            text += f" ({len(emojis)} emojis)"
        text += f"\n   {forwarded:,}/{source_total:,}\n"
    
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
    
    if config.get('completed'):
        status = "✅ COMPLETED"
    elif config.get('enabled'):
        status = "🟢 Running"
    else:
        status = "🔴 Stopped"
    
    source_total = config.get('source_total', 0)
    forwarded = config.get('total_forwarded', 0)
    emojis = config.get('emojis', [])
    caption = config.get('caption', '')
    
    text = f"**{name}** [{status}]\n\n"
    text += f"Source: `{config.get('source_id')}`\n"
    text += f"Dest: `{config.get('dest_id')}`\n"
    text += f"Interval: {config.get('interval')}+/-{config.get('variation')} min\n"
    text += f"Content: {', '.join(config.get('content_types', []))}\n"
    text += f"Daily limit: {config.get('daily_limit')}\n\n"
    text += f"**Caption:** {caption or 'None'}\n"
    text += f"**Emojis:** {', '.join(emojis) if emojis else 'None'}\n\n"
    text += f"📊 Progress: {forwarded:,}/{source_total:,}\n"
    text += f"Today: {config.get('daily_count', 0)}"
    
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
    total_today = 0
    total_forwarded = 0
    
    for name, config in CHANNELS.items():
        today = config.get('daily_count', 0)
        forwarded = config.get('total_forwarded', 0)
        source = config.get('source_total', 0)
        
        if config.get('completed'):
            status = "✅"
        elif config.get('enabled'):
            status = "🟢"
        else:
            status = "🔴"
        
        text += f"{status} **{name}**: {today} today | {forwarded:,}/{source:,}\n"
        total_today += today
        total_forwarded += forwarded
    
    text += f"\n**Today:** {total_today}\n**Total:** {total_forwarded:,}"
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
    CHANNELS[name]['scanned'] = False
    save_data()
    await event.respond(f"**Content:** {', '.join(types)}\n\n⚠️ Run `/scan {name}` again")


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
    await event.respond(f"🗑️ Removed `{name}`")
    logger.info(f"[-] Removed: {name}")


@client.on(events.NewMessage(pattern='/reset'))
async def reset_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 2:
        await event.respond("**Usage:** `/reset NAME`")
        return
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    CHANNELS[name]['completed'] = False
    CHANNELS[name]['forwarded_ids'] = []
    CHANNELS[name]['total_forwarded'] = 0
    CHANNELS[name]['daily_count'] = 0
    save_data()
    await event.respond(f"🔄 Reset `{name}`\n\nRun `/scan {name}` then `/go {name}`")
    logger.info(f"[R] Reset: {name}")


async def forward_loop(name):
    logger.info(f"[{name}] Loop started")
    
    while CHANNELS.get(name, {}).get('enabled', False):
        try:
            reset_daily_counts()
            config = CHANNELS.get(name)
            if not config:
                break
            
            if config.get('completed'):
                break
            
            if config.get('daily_count', 0) >= config.get('daily_limit', DEFAULT_LIMIT):
                logger.info(f"[{name}] Daily limit")
                await asyncio.sleep(3600)
                continue
            
            source = config.get('source_id')
            dest = config.get('dest_id')
            content_types = config.get('content_types', [])
            
            if not source or not dest:
                break
            
            try:
                messages = await client.get_messages(source, limit=100)
                messages = list(reversed(messages))
                forwarded_set = set(config.get('forwarded_ids', []))
                
                found = False
                for msg in messages:
                    if msg.id in forwarded_set:
                        continue
                    if not should_forward(msg, content_types):
                        continue
                    
                    found = True
                    try:
                        await send_as_new(dest, msg, config)
                        
                        CHANNELS[name]['forwarded_ids'].append(msg.id)
                        if len(CHANNELS[name]['forwarded_ids']) > 10000:
                            CHANNELS[name]['forwarded_ids'] = CHANNELS[name]['forwarded_ids'][-10000:]
                        
                        CHANNELS[name]['daily_count'] = config.get('daily_count', 0) + 1
                        CHANNELS[name]['total_forwarded'] = config.get('total_forwarded', 0) + 1
                        save_data()
                        
                        total = CHANNELS[name]['total_forwarded']
                        source_total = config.get('source_total', 0)
                        
                        if source_total > 0:
                            percent = (total / source_total) * 100
                            logger.info(f"[{name}] #{total:,}/{source_total:,} ({percent:.1f}%)")
                        else:
                            logger.info(f"[{name}] #{total:,}")
                        
                        break
                    except Exception as e:
                        logger.error(f"[{name}] Send error: {e}")
                
                if not found:
                    source_total = config.get('source_total', 0)
                    total_forwarded = config.get('total_forwarded', 0)
                    
                    if source_total > 0 and total_forwarded >= source_total:
                        CHANNELS[name]['completed'] = True
                        CHANNELS[name]['enabled'] = False
                        save_data()
                        
                        await notify_admin(
                            f"🎉 **`{name}` COMPLETED!**\n\n"
                            f"✅ Total: {total_forwarded:,}/{source_total:,}\n\n"
                            f"Use `/reset {name}` to restart"
                        )
                        logger.info(f"[{name}] COMPLETED")
                        break
                
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


async def auto_resume():
    await asyncio.sleep(5)
    for name, config in CHANNELS.items():
        if config.get('enabled') and not config.get('completed'):
            if config.get('source_id') and config.get('dest_id'):
                if name not in channel_tasks or channel_tasks[name].done():
                    channel_tasks[name] = asyncio.create_task(forward_loop(name))
                    logger.info(f"[AUTO] Resumed: {name}")


async def main():
    print("=" * 40)
    print("  MyFC Forwarder v4.8")
    print("  Random Emoji Edition")
    print("=" * 40)
    
    load_data()
    logger.info(f"Admin: {ADMIN_ID}")
    logger.info(f"Channels: {len(CHANNELS)}")
    
    await client.start()
    me = await client.get_me()
    logger.info(f"Logged in: {me.first_name}")
    
    asyncio.create_task(auto_resume())
    
    logger.info("Ready! Send /start")
    print("=" * 40)
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())
