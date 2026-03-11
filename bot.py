#!/usr/bin/env python3
"""
MyFC Forwarder v5.1 - Skip Command Added
- /skip command to skip already forwarded content
- Fixed daily count reset
- All previous features
"""

import asyncio
import random
import json
import os
import logging
from datetime import datetime, date
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

last_reset_date_str = None
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


def get_today_str():
    return datetime.now().strftime('%Y-%m-%d')


def save_data():
    global CHANNELS, last_reset_date_str
    try:
        save_channels = {}
        for name, config in CHANNELS.items():
            save_config = {k: v for k, v in config.items() if k != 'all_source_ids'}
            save_channels[name] = save_config
        
        data = {
            'id': 'main',
            'channels': save_channels,
            'last_reset_date': last_reset_date_str,
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
    global CHANNELS, last_reset_date_str
    try:
        url = f"{SUPABASE_URL}/rest/v1/forwarder_data?id=eq.main&select=*"
        with httpx.Client(timeout=30) as http:
            resp = http.get(url, headers=get_headers())
            if resp.status_code == 200:
                result = resp.json()
                if result and len(result) > 0:
                    data = result[0]
                    CHANNELS = data.get('channels', {})
                    last_reset_date_str = data.get('last_reset_date')
                    logger.info(f"[DB] Loaded {len(CHANNELS)} channels")
                else:
                    logger.info("[DB] No data, starting fresh")
    except Exception as e:
        logger.warning(f"[DB] Load error: {e}")


def reset_daily_counts():
    global last_reset_date_str
    today = get_today_str()
    
    if last_reset_date_str != today:
        logger.info(f"[RESET] New day! {last_reset_date_str} -> {today}")
        for name in CHANNELS:
            CHANNELS[name]['daily_count'] = 0
        last_reset_date_str = today
        save_data()
        return True
    return False


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
    caption = config.get('caption', '')
    emojis = config.get('emojis', [])
    
    if not caption:
        return ""
    
    if emojis:
        random_emoji = random.choice(emojis)
        if '{emoji}' in caption:
            return caption.replace('{emoji}', random_emoji)
        else:
            return f"{random_emoji} {caption}"
    
    return caption


async def send_as_new(dest, msg, config):
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


async def get_first_n_message_ids(source, content_types, n):
    """Get first N message IDs (oldest first) that match content types"""
    all_ids = []
    offset_id = 0
    batch_count = 0
    
    while len(all_ids) < n and batch_count < 500:
        try:
            messages = await client.get_messages(source, limit=100, offset_id=offset_id)
            if not messages:
                break
            
            for msg in messages:
                if should_forward(msg, content_types):
                    all_ids.append(msg.id)
            
            offset_id = messages[-1].id
            batch_count += 1
            
            if len(messages) < 100:
                break
            
            if batch_count % 10 == 0:
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Get IDs error: {e}")
            await asyncio.sleep(2)
            break
    
    # Reverse to get oldest first, then take first N
    all_ids.reverse()
    return all_ids[:n]


async def find_next_message_to_forward(source, content_types, forwarded_ids):
    forwarded_set = set(forwarded_ids)
    offset_id = 0
    batch_count = 0
    
    while batch_count < 100:
        try:
            if offset_id > 0:
                messages = await client.get_messages(source, limit=100, max_id=offset_id)
            else:
                messages = await client.get_messages(source, limit=100)
            
            if not messages:
                break
            
            for msg in reversed(messages):
                if msg.id in forwarded_set:
                    continue
                if should_forward(msg, content_types):
                    return msg
            
            offset_id = messages[-1].id
            batch_count += 1
            
            if len(messages) < 100:
                break
            
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Find next error: {e}")
            await asyncio.sleep(2)
            break
    
    return None


@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    await event.respond(
        "**MyFC Forwarder v5.1**\n\n"
        "**SETUP:**\n"
        "`/quicksetup NAME SOURCE DEST INT VAR CONTENT`\n\n"
        "**CAPTION & EMOJI:**\n"
        "`/caption NAME text`\n"
        "`/emojis NAME 🚗,⚠️,🚦`\n\n"
        "**CONTROL:**\n"
        "`/scan` `/test` `/go` `/stop`\n\n"
        "**INFO:**\n"
        "`/list` `/info` `/stats` `/progress`\n\n"
        "**SETTINGS:**\n"
        "`/interval` `/content` `/limit`\n\n"
        "**TROUBLESHOOT:**\n"
        "`/skip NAME 45` - Skip first 45 messages\n"
        "`/resetcount NAME` - Reset daily count\n"
        "`/debug NAME` - Check state\n"
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
            f"**Next:** `/scan {name}`"
        )
        logger.info(f"[+] Created: {name}")
    except Exception as e:
        await event.respond(f"Error: {e}")


@client.on(events.NewMessage(pattern='/skip'))
async def skip_handler(event):
    """Skip first N messages (mark as already forwarded)"""
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    
    if len(parts) < 3:
        await event.respond(
            "**Usage:** `/skip NAME NUMBER`\n\n"
            "**Example:** `/skip backup1 45`\n\n"
            "This marks the first 45 messages as forwarded,\n"
            "so bot will start from 46th message."
        )
        return
    
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    
    try:
        skip_count = int(parts[2])
    except:
        await event.respond("Invalid number")
        return
    
    if skip_count < 1:
        await event.respond("Number must be at least 1")
        return
    
    config = CHANNELS[name]
    source = config.get('source_id')
    content_types = config.get('content_types', [])
    
    # Stop if running
    if config.get('enabled'):
        CHANNELS[name]['enabled'] = False
        if name in channel_tasks:
            channel_tasks[name].cancel()
            del channel_tasks[name]
    
    status_msg = await event.respond(f"⏳ Finding first {skip_count} messages to skip...")
    
    try:
        # Get first N message IDs
        skip_ids = await get_first_n_message_ids(source, content_types, skip_count)
        
        if not skip_ids:
            await status_msg.edit("❌ No matching messages found in source")
            return
        
        actual_skip = len(skip_ids)
        
        # Add to forwarded_ids
        existing_ids = set(config.get('forwarded_ids', []))
        new_ids = [mid for mid in skip_ids if mid not in existing_ids]
        
        CHANNELS[name]['forwarded_ids'] = list(existing_ids) + new_ids
        CHANNELS[name]['total_forwarded'] = len(CHANNELS[name]['forwarded_ids'])
        save_data()
        
        await status_msg.edit(
            f"✅ **Skipped {actual_skip} messages for `{name}`**\n\n"
            f"Forwarded IDs count: {len(CHANNELS[name]['forwarded_ids'])}\n"
            f"Total forwarded: {CHANNELS[name]['total_forwarded']}\n\n"
            f"Bot will now start from message #{actual_skip + 1}\n\n"
            f"**Start:** `/go {name}`"
        )
        logger.info(f"[SKIP] {name}: Skipped {actual_skip} messages")
        
    except Exception as e:
        await status_msg.edit(f"❌ Error: {e}")
        logger.error(f"[SKIP] {name}: {e}")


@client.on(events.NewMessage(pattern='/resetcount'))
async def resetcount_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    
    if len(parts) < 2:
        await event.respond(
            "**Usage:** `/resetcount NAME`\n\n"
            "Resets daily count to 0 (keeps forwarded_ids intact).\n\n"
            "**Reset all:** `/resetcount all`"
        )
        return
    
    name = parts[1].lower()
    
    if name == 'all':
        for ch_name in CHANNELS:
            CHANNELS[ch_name]['daily_count'] = 0
        save_data()
        await event.respond("✅ Reset daily count for ALL channels")
        return
    
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    
    old_count = CHANNELS[name].get('daily_count', 0)
    CHANNELS[name]['daily_count'] = 0
    save_data()
    
    await event.respond(
        f"✅ **Reset daily count for `{name}`**\n\n"
        f"Before: {old_count} → After: 0\n"
        f"forwarded_ids: {len(CHANNELS[name].get('forwarded_ids', []))} (unchanged)"
    )


@client.on(events.NewMessage(pattern='/debug'))
async def debug_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    
    if len(parts) < 2:
        await event.respond(
            f"**Global Debug**\n\n"
            f"Today: `{get_today_str()}`\n"
            f"Last reset: `{last_reset_date_str}`\n"
            f"Channels: {len(CHANNELS)}\n"
            f"Active tasks: {len(channel_tasks)}\n\n"
            f"**Per channel:** `/debug NAME`"
        )
        return
    
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    
    config = CHANNELS[name]
    fwd_ids = config.get('forwarded_ids', [])
    
    await event.respond(
        f"**Debug: {name}**\n\n"
        f"enabled: `{config.get('enabled')}`\n"
        f"completed: `{config.get('completed')}`\n"
        f"scanned: `{config.get('scanned')}`\n\n"
        f"daily_count: `{config.get('daily_count', 0)}`\n"
        f"daily_limit: `{config.get('daily_limit', 100)}`\n"
        f"total_forwarded: `{config.get('total_forwarded', 0)}`\n"
        f"source_total: `{config.get('source_total', 0)}`\n\n"
        f"forwarded_ids count: `{len(fwd_ids)}`\n"
        f"Last 5 IDs: `{fwd_ids[-5:] if fwd_ids else 'empty'}`\n\n"
        f"Task active: `{name in channel_tasks and not channel_tasks[name].done()}`"
    )


@client.on(events.NewMessage(pattern='/emojis'))
async def emojis_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split(maxsplit=2)
    
    if len(parts) < 2:
        await event.respond(
            "**Usage:** `/emojis NAME emoji1,emoji2`\n\n"
            "**Clear:** `/emojis NAME clear`"
        )
        return
    
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    
    if len(parts) < 3:
        emojis = CHANNELS[name].get('emojis', [])
        await event.respond(f"**Emojis:** {', '.join(emojis) if emojis else 'None'}")
        return
    
    emoji_str = parts[2]
    
    if emoji_str.lower() == 'clear':
        CHANNELS[name]['emojis'] = []
        save_data()
        await event.respond("✅ Emojis cleared")
        return
    
    emojis = [e.strip() for e in emoji_str.replace(' ', ',').split(',') if e.strip()]
    CHANNELS[name]['emojis'] = emojis
    save_data()
    await event.respond(f"✅ **{len(emojis)} emojis set**")


@client.on(events.NewMessage(pattern='/caption'))
async def caption_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split(maxsplit=2)
    
    if len(parts) < 2:
        await event.respond("**Usage:** `/caption NAME text`\n**Clear:** `/caption NAME clear`")
        return
    
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    
    if len(parts) < 3:
        caption = CHANNELS[name].get('caption')
        await event.respond(f"**Caption:** {caption or 'None'}")
        return
    
    caption = parts[2]
    
    if caption.lower() == 'clear':
        CHANNELS[name]['caption'] = None
        save_data()
        await event.respond("✅ Caption cleared")
        return
    
    CHANNELS[name]['caption'] = caption
    save_data()
    await event.respond(f"✅ **Caption set:** {caption}")


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
            await status_msg.edit(f"🔍 Scanning... Found: {count:,}")
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
    daily_count = config.get('daily_count', 0)
    daily_limit = config.get('daily_limit', 100)
    
    if source_total > 0:
        remaining = source_total - forwarded
        percent = (forwarded / source_total) * 100
        bar_filled = int(percent / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        
        days_left = remaining / daily_limit if daily_limit > 0 else 0
        
        await event.respond(
            f"**Progress: {name}**\n\n"
            f"[{bar}] {percent:.1f}%\n\n"
            f"📊 Source: {source_total:,}\n"
            f"✅ Forwarded: {forwarded:,}\n"
            f"⏳ Remaining: {remaining:,}\n"
            f"📅 Est: ~{days_left:.0f} days\n\n"
            f"**Today:** {daily_count}/{daily_limit}\n"
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
    forwarded_ids = config.get('forwarded_ids', [])
    
    await event.respond(f"Testing `{name}`... (skipping {len(forwarded_ids)} already forwarded)")
    
    try:
        msg = await find_next_message_to_forward(source, content_types, forwarded_ids)
        
        if msg:
            await send_as_new(dest, msg, config)
            caption = get_caption_with_emoji(config)
            await event.respond(
                f"✅ **Test SUCCESS!**\n\n"
                f"Sent: {get_content_type(msg)} (msg ID: {msg.id})\n"
                f"Caption: {caption or '(none)'}"
            )
        else:
            await event.respond("No unforwarded content found")
    except Exception as e:
        await event.respond(f"❌ **Failed:** {e}")


@client.on(events.NewMessage(pattern='/go'))
async def go_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    
    if not CHANNELS:
        await event.respond("No channels. Use `/quicksetup`")
        return
    
    reset_daily_counts()
    
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
        daily_count = config.get('daily_count', 0)
        daily_limit = config.get('daily_limit', 100)
        fwd_ids_count = len(config.get('forwarded_ids', []))
        
        await event.respond(
            f"▶️ **Started `{name}`**\n\n"
            f"Progress: {forwarded:,}/{source_total:,}\n"
            f"Forwarded IDs: {fwd_ids_count}\n"
            f"Today: {daily_count}/{daily_limit}"
        )
        logger.info(f"[>] Started: {name} (fwd_ids: {fwd_ids_count})")
    else:
        started = []
        for name, config in CHANNELS.items():
            if config.get('completed') or not config.get('scanned'):
                continue
            CHANNELS[name]['enabled'] = True
            if name not in channel_tasks or channel_tasks[name].done():
                channel_tasks[name] = asyncio.create_task(forward_loop(name))
            started.append(name)
        save_data()
        await event.respond(f"▶️ **Started:** {', '.join(started)}" if started else "No channels to start")


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
    else:
        for name in CHANNELS:
            CHANNELS[name]['enabled'] = False
            if name in channel_tasks:
                channel_tasks[name].cancel()
        channel_tasks.clear()
        save_data()
        await event.respond("⏹️ **All stopped**")


@client.on(events.NewMessage(pattern='/list'))
async def list_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    if not CHANNELS:
        await event.respond("No channels")
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
        daily = config.get('daily_count', 0)
        limit = config.get('daily_limit', 100)
        
        text += f"{status} **{name}**: {forwarded:,}/{source_total:,} (Today: {daily}/{limit})\n"
    
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
    fwd_ids = len(config.get('forwarded_ids', []))
    
    text = f"**{name}** [{status}]\n\n"
    text += f"Source: `{config.get('source_id')}`\n"
    text += f"Dest: `{config.get('dest_id')}`\n"
    text += f"Interval: {config.get('interval')}+/-{config.get('variation')} min\n"
    text += f"Content: {', '.join(config.get('content_types', []))}\n\n"
    text += f"Caption: {config.get('caption') or 'None'}\n"
    text += f"Emojis: {len(config.get('emojis', []))} set\n\n"
    text += f"📊 Progress: {forwarded:,}/{source_total:,}\n"
    text += f"📋 Forwarded IDs: {fwd_ids}\n"
    text += f"📅 Today: {config.get('daily_count', 0)}/{config.get('daily_limit', 100)}"
    
    await event.respond(text)


@client.on(events.NewMessage(pattern='/stats'))
async def stats_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    reset_daily_counts()
    if not CHANNELS:
        await event.respond("No channels")
        return
    
    text = f"**Stats - {get_today_str()}**\n\n"
    for name, config in CHANNELS.items():
        today = config.get('daily_count', 0)
        forwarded = config.get('total_forwarded', 0)
        source = config.get('source_total', 0)
        limit = config.get('daily_limit', 100)
        
        if config.get('completed'):
            status = "✅"
        elif config.get('enabled'):
            status = "🟢"
        else:
            status = "🔴"
        
        text += f"{status} **{name}**: {today}/{limit} today | {forwarded:,}/{source:,}\n"
    
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
        CHANNELS[name]['interval'] = int(parts[2])
        CHANNELS[name]['variation'] = int(parts[3])
        save_data()
        await event.respond(f"**Interval:** {parts[2]}+/-{parts[3]} min")
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
        CHANNELS[name]['daily_limit'] = int(parts[2])
        save_data()
        await event.respond(f"**Limit:** {parts[2]}/day")
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


@client.on(events.NewMessage(pattern='/reset'))
async def reset_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 2:
        await event.respond("**Usage:** `/reset NAME`\n\nResets ALL progress (starts from beginning)")
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
    await event.respond(f"🔄 Reset `{name}` - All progress cleared\n\nRun `/scan {name}` then `/go {name}`")


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
            
            daily_count = config.get('daily_count', 0)
            daily_limit = config.get('daily_limit', DEFAULT_LIMIT)
            
            if daily_count >= daily_limit:
                logger.info(f"[{name}] Daily limit {daily_count}/{daily_limit}")
                await asyncio.sleep(3600)
                continue
            
            source = config.get('source_id')
            dest = config.get('dest_id')
            content_types = config.get('content_types', [])
            forwarded_ids = config.get('forwarded_ids', [])
            
            if not source or not dest:
                break
            
            try:
                msg = await find_next_message_to_forward(source, content_types, forwarded_ids)
                
                if msg:
                    try:
                        await send_as_new(dest, msg, config)
                        
                        CHANNELS[name]['forwarded_ids'].append(msg.id)
                        if len(CHANNELS[name]['forwarded_ids']) > 10000:
                            CHANNELS[name]['forwarded_ids'] = CHANNELS[name]['forwarded_ids'][-10000:]
                        
                        CHANNELS[name]['daily_count'] = config.get('daily_count', 0) + 1
                        CHANNELS[name]['total_forwarded'] = len(CHANNELS[name]['forwarded_ids'])
                        save_data()
                        
                        total = CHANNELS[name]['total_forwarded']
                        source_total = config.get('source_total', 0)
                        new_daily = CHANNELS[name]['daily_count']
                        
                        logger.info(f"[{name}] #{total:,}/{source_total:,} - Today: {new_daily}/{daily_limit}")
                        
                    except Exception as e:
                        logger.error(f"[{name}] Send error: {e}")
                else:
                    CHANNELS[name]['completed'] = True
                    CHANNELS[name]['enabled'] = False
                    save_data()
                    
                    await notify_admin(f"🎉 **`{name}` COMPLETED!**")
                    logger.info(f"[{name}] COMPLETED")
                    break
                
            except Exception as e:
                logger.error(f"[{name}] Error: {e}")
            
            interval = get_random_interval(name)
            logger.info(f"[{name}] Next in {interval}m")
            await asyncio.sleep(interval * 60)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[{name}] Loop error: {e}")
            await asyncio.sleep(60)
    
    logger.info(f"[{name}] Loop ended")


async def auto_resume():
    await asyncio.sleep(5)
    reset_daily_counts()
    for name, config in CHANNELS.items():
        if config.get('enabled') and not config.get('completed'):
            if config.get('source_id') and config.get('dest_id'):
                if name not in channel_tasks or channel_tasks[name].done():
                    channel_tasks[name] = asyncio.create_task(forward_loop(name))
                    logger.info(f"[AUTO] Resumed: {name}")


async def main():
    print("=" * 40)
    print("  MyFC Forwarder v5.1")
    print("  Skip Command Added")
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
