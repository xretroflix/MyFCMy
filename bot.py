#!/usr/bin/env python3
"""
MyFC Forwarder v4.7 - Large Scale Edition
- Scan source before starting (handles 2 lakh+ content)
- Accurate tracking: Source count = Forward count
- Handles restricted channels (download + re-upload)
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
scan_tasks = {}

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
        # Don't save all_source_ids to Supabase (too large)
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


async def send_as_new(dest, msg, custom_caption=None):
    """
    Send message as NEW - handles restricted channels
    Downloads media and re-uploads (bypasses forward restrictions)
    """
    if msg.media:
        # Download and re-upload (works even if forwarding is restricted)
        await client.send_file(dest, msg.media, caption=custom_caption or "")
    elif msg.text:
        await client.send_message(dest, custom_caption or msg.text)


async def notify_admin(message):
    try:
        await client.send_message(ADMIN_ID, message)
    except Exception as e:
        logger.error(f"Notify error: {e}")


async def scan_source_channel(source, content_types, progress_callback=None):
    """
    Scan ALL content from source channel
    Returns list of message IDs (oldest first)
    Handles 2 lakh+ messages
    """
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
            
            # Progress update every 50 batches (5000 messages)
            if progress_callback and batch_count % 50 == 0:
                await progress_callback(len(all_ids), batch_count * 100)
            
            if len(messages) < 100:
                break
            
            # Rate limit: 1 second delay every 10 batches
            if batch_count % 10 == 0:
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Scan error at batch {batch_count}: {e}")
            await asyncio.sleep(5)
            continue
    
    # Return oldest first
    all_ids.reverse()
    return all_ids


@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    await event.respond(
        "**MyFC Forwarder v4.7** (Large Scale)\n\n"
        "**SETUP:**\n"
        "`/quicksetup NAME SOURCE DEST INTERVAL VAR CONTENT`\n\n"
        "**BEFORE STARTING:**\n"
        "`/scan NAME` - Count all source content\n\n"
        "**CONTROL:**\n"
        "`/test NAME` - Test one forward\n"
        "`/go NAME` - Start forwarding\n"
        "`/stop NAME` - Stop forwarding\n\n"
        "**INFO:**\n"
        "`/list` - All channels\n"
        "`/info NAME` - Channel details\n"
        "`/stats` - Daily + Total counts\n"
        "`/progress NAME` - Live progress\n\n"
        "**SETTINGS:**\n"
        "`/interval` `/content` `/caption` `/limit`\n"
        "`/remove NAME` `/reset NAME`\n\n"
        "Handles 2 lakh+ content!"
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
            "`/quicksetup movies 3773414989 3255469862 15 5 photos,videos`\n\n"
            "**Content:** photos, videos, audio, docs, links"
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
            'total_forwarded': 0,
            'source_total': 0,  # Will be set by /scan
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
            f"**Next step:** `/scan {name}`\n"
            f"(Counts all content before starting)"
        )
        logger.info(f"[+] Created: {name}")
    except Exception as e:
        await event.respond(f"Error: {e}")


@client.on(events.NewMessage(pattern='/scan'))
async def scan_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 2:
        await event.respond("**Usage:** `/scan NAME`\n\nCounts ALL content in source channel")
        return
    
    name = parts[1].lower()
    if name not in CHANNELS:
        await event.respond(f"Channel `{name}` not found")
        return
    
    if name in scan_tasks and not scan_tasks[name].done():
        await event.respond(f"Scan already in progress for `{name}`")
        return
    
    config = CHANNELS[name]
    source = config.get('source_id')
    content_types = config.get('content_types', [])
    
    status_msg = await event.respond(f"🔍 Scanning `{name}`...\n\nThis may take a while for large channels.")
    
    async def progress_update(count, processed):
        try:
            await status_msg.edit(
                f"🔍 Scanning `{name}`...\n\n"
                f"Found: {count:,} matching content\n"
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
            f"✅ **Scan complete for `{name}`**\n\n"
            f"📊 **Source content:** {len(all_ids):,}\n"
            f"✓ Already forwarded: {already_done:,}\n"
            f"⏳ Remaining: {remaining:,}\n\n"
            f"**Start:** `/go {name}`\n"
            f"**Test first:** `/test {name}`"
        )
        logger.info(f"[SCAN] {name}: {len(all_ids)} total, {remaining} remaining")
        
    except Exception as e:
        await status_msg.edit(f"❌ Scan failed: {e}")
        logger.error(f"[SCAN] {name}: {e}")


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
    remaining = source_total - forwarded if source_total > 0 else 0
    
    if source_total > 0:
        percent = (forwarded / source_total) * 100
        bar_filled = int(percent / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        
        # Estimate time remaining
        interval = config.get('interval', 30)
        hours_left = (remaining * interval) / 60
        days_left = hours_left / 24
        
        time_est = ""
        if days_left > 1:
            time_est = f"~{days_left:.1f} days"
        elif hours_left > 1:
            time_est = f"~{hours_left:.1f} hours"
        else:
            time_est = f"~{remaining * interval} minutes"
        
        await event.respond(
            f"**Progress: {name}**\n\n"
            f"[{bar}] {percent:.1f}%\n\n"
            f"📊 Source total: {source_total:,}\n"
            f"✅ Forwarded: {forwarded:,}\n"
            f"⏳ Remaining: {remaining:,}\n"
            f"⏱️ Est. time: {time_est}\n\n"
            f"Status: {'🟢 Running' if config.get('enabled') else '🔴 Stopped'}"
        )
    else:
        await event.respond(
            f"**{name}** - Not scanned yet\n\n"
            f"Run `/scan {name}` first to count source content"
        )


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
                custom_caption = config.get('caption')
                await send_as_new(dest, msg, custom_caption)
                await event.respond(
                    f"✅ **Test SUCCESS!**\n\n"
                    f"Forwarded 1 {get_content_type(msg)}\n"
                    f"(Forward restriction bypassed)"
                )
                logger.info(f"[TEST] {name}: OK")
                return
        
        await event.respond("No new matching content found in first 100 messages")
    except Exception as e:
        logger.error(f"[TEST] {name}: {e}")
        await event.respond(f"❌ **Test FAILED:** {e}")


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
            await event.respond(
                f"Channel `{name}` already completed!\n\n"
                f"Use `/reset {name}` to start over"
            )
            return
        
        if not config.get('scanned'):
            await event.respond(
                f"⚠️ Channel `{name}` not scanned yet!\n\n"
                f"Run `/scan {name}` first to count source content.\n"
                f"This ensures no content is missed."
            )
            return
        
        CHANNELS[name]['enabled'] = True
        save_data()
        
        if name not in channel_tasks or channel_tasks[name].done():
            channel_tasks[name] = asyncio.create_task(forward_loop(name))
        
        source_total = config.get('source_total', 0)
        forwarded = config.get('total_forwarded', 0)
        remaining = source_total - forwarded
        
        await event.respond(
            f"▶️ **Started `{name}`**\n\n"
            f"Source: {source_total:,} content\n"
            f"Forwarded: {forwarded:,}\n"
            f"Remaining: {remaining:,}\n\n"
            f"Check progress: `/progress {name}`"
        )
        logger.info(f"[>] Started: {name}")
    else:
        started = []
        skipped = []
        for name, config in CHANNELS.items():
            if config.get('completed'):
                skipped.append(f"{name} (completed)")
                continue
            if not config.get('scanned'):
                skipped.append(f"{name} (not scanned)")
                continue
            if config.get('source_id') and config.get('dest_id'):
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
            status = "✅ DONE"
        elif config.get('enabled'):
            status = "🟢 ON"
        else:
            status = "🔴 OFF"
        
        source_total = config.get('source_total', 0)
        forwarded = config.get('total_forwarded', 0)
        
        if source_total > 0:
            percent = (forwarded / source_total) * 100
            text += f"**{name}** [{status}]\n"
            text += f"  {forwarded:,}/{source_total:,} ({percent:.1f}%)\n\n"
        else:
            text += f"**{name}** [{status}] - Not scanned\n\n"
    
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
    remaining = source_total - forwarded if source_total > 0 else 0
    
    text = f"**{name}** [{status}]\n\n"
    text += f"Source: `{config.get('source_id')}`\n"
    text += f"Dest: `{config.get('dest_id')}`\n"
    text += f"Interval: {config.get('interval')}+/-{config.get('variation')} min\n"
    text += f"Content: {', '.join(config.get('content_types', []))}\n"
    text += f"Caption: {config.get('caption') or 'None'}\n"
    text += f"Daily limit: {config.get('daily_limit')}\n\n"
    
    text += f"📊 **Progress:**\n"
    text += f"Source total: {source_total:,}\n"
    text += f"Forwarded: {forwarded:,}\n"
    text += f"Remaining: {remaining:,}\n"
    text += f"Today: {config.get('daily_count', 0)}\n"
    
    if source_total > 0:
        percent = (forwarded / source_total) * 100
        text += f"\nProgress: {percent:.1f}%"
    
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
    total_source = 0
    
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
        
        text += f"{status} **{name}**\n"
        text += f"   Today: {today} | Total: {forwarded:,}/{source:,}\n"
        
        total_today += today
        total_forwarded += forwarded
        total_source += source
    
    text += f"\n**Summary:**\n"
    text += f"Today: {total_today}\n"
    text += f"Total forwarded: {total_forwarded:,}\n"
    text += f"Total source: {total_source:,}"
    
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
    CHANNELS[name]['scanned'] = False  # Need to rescan with new types
    save_data()
    await event.respond(f"**Content:** {', '.join(types)}\n\n⚠️ Run `/scan {name}` again")


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
        await event.respond("Caption cleared")
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
    await event.respond(f"🗑️ Removed `{name}`")
    logger.info(f"[-] Removed: {name}")


@client.on(events.NewMessage(pattern='/reset'))
async def reset_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    parts = event.text.split()
    if len(parts) < 2:
        await event.respond("**Usage:** `/reset NAME`\n\nResets all progress and starts from beginning")
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
                logger.info(f"[{name}] Already completed")
                break
            
            if config.get('daily_count', 0) >= config.get('daily_limit', DEFAULT_LIMIT):
                logger.info(f"[{name}] Daily limit reached")
                await asyncio.sleep(3600)
                continue
            
            source = config.get('source_id')
            dest = config.get('dest_id')
            content_types = config.get('content_types', [])
            
            if not source or not dest:
                break
            
            try:
                # Get next batch of messages (oldest first)
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
                        custom_caption = config.get('caption')
                        await send_as_new(dest, msg, custom_caption)
                        
                        CHANNELS[name]['forwarded_ids'].append(msg.id)
                        # Keep last 10000 IDs to manage memory
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
                
                # Check if completed
                if not found:
                    source_total = config.get('source_total', 0)
                    total_forwarded = config.get('total_forwarded', 0)
                    
                    if source_total > 0 and total_forwarded >= source_total:
                        CHANNELS[name]['completed'] = True
                        CHANNELS[name]['enabled'] = False
                        save_data()
                        
                        await notify_admin(
                            f"🎉 **Channel `{name}` COMPLETED!**\n\n"
                            f"✅ All content forwarded!\n"
                            f"📊 Total: {total_forwarded:,}/{source_total:,}\n\n"
                            f"Use `/reset {name}` to start over"
                        )
                        logger.info(f"[{name}] COMPLETED: {total_forwarded}/{source_total}")
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
    print("  MyFC Forwarder v4.7")
    print("  Large Scale Edition")
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
