#!/usr/bin/env python3
"""
MyFC Forwarder v3.0 - Simple Edition
Quick setup with one command
"""

import asyncio
import random
import json
import os
import logging
from datetime import datetime, timedelta
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage
from telethon.sessions import StringSession
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_STRING = os.environ.get('SESSION_STRING', '')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")
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
is_running = False
telethon_client = None
channel_tasks = {}


def save_data():
    try:
        data = {'channels': CHANNELS, 'last_reset_date': last_reset_date.isoformat() if last_reset_date else None}
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        logger.info("Data saved")
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
    variation = random.randint(1, var)
    if random.choice([True, False]):
        variation = -variation
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


async def admin_only(update: Update) -> bool:
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return False
    return True


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    text = (
        "*MyFC Forwarder v3.0*\n\n"
        "*QUICK SETUP (One Command):*\n"
        "`/quicksetup NAME SOURCE DEST INTERVAL VAR CONTENT`\n\n"
        "Example:\n"
        "`/quicksetup movies -100111 -100222 15 5 photos,videos`\n\n"
        "*Content options:* photos, videos, audio, docs, links\n\n"
        "*OPTIONAL SETTINGS:*\n"
        "`/interval NAME 20 10`\n"
        "`/content NAME photos,videos`\n"
        "`/caption NAME Your text here`\n"
        "`/limit NAME 80`\n\n"
        "*CONTROL:*\n"
        "`/go` - Start all\n"
        "`/stop` - Stop all\n"
        "`/go NAME` - Start one\n"
        "`/stop NAME` - Stop one\n\n"
        "*INFO:*\n"
        "`/list` - All channels\n"
        "`/info NAME` - Channel details\n"
        "`/stats` - Daily counts\n\n"
        "*MANAGE:*\n"
        "`/remove NAME` - Delete channel"
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def quicksetup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    if len(context.args) < 6:
        await update.message.reply_text(
            "*Usage:*\n"
            "`/quicksetup NAME SOURCE DEST INTERVAL VARIATION CONTENT`\n\n"
            "*Example:*\n"
            "`/quicksetup mymovies -100111111111 -100222222222 15 5 photos,videos`\n\n"
            "*Content options:* photos, videos, audio, docs, links\n\n"
            "*Interval examples:*\n"
            "15 5 = every 15 min (range 10-20)\n"
            "30 10 = every 30 min (range 20-40)\n"
            "60 15 = every 60 min (range 45-75)",
            parse_mode='Markdown'
        )
        return
    
    try:
        name = context.args[0].lower()
        source = int(context.args[1])
        dest = int(context.args[2])
        interval = int(context.args[3])
        variation = int(context.args[4])
        content_str = context.args[5].lower()
        
        if interval < 5:
            await update.message.reply_text("Minimum interval is 5 minutes")
            return
        
        valid_types = ['photos', 'videos', 'audio', 'docs', 'links']
        content_types = [t.strip() for t in content_str.split(',') if t.strip() in valid_types]
        
        if not content_types:
            await update.message.reply_text(f"Invalid content types. Use: {', '.join(valid_types)}")
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
        
        min_int = interval - variation
        max_int = interval + variation
        
        await update.message.reply_text(
            f"*Channel `{name}` created!*\n\n"
            f"Source: `{source}`\n"
            f"Dest: `{dest}`\n"
            f"Interval: {interval} min (range {min_int}-{max_int})\n"
            f"Content: {', '.join(content_types)}\n"
            f"Daily limit: {DEFAULT_LIMIT}\n\n"
            f"*Start with:* `/go {name}`\n\n"
            f"*Optional:*\n"
            f"`/caption {name} Your text`\n"
            f"`/limit {name} 80`",
            parse_mode='Markdown'
        )
        logger.info(f"Channel created: {name}")
    except ValueError:
        await update.message.reply_text("Invalid numbers in SOURCE, DEST, INTERVAL, or VARIATION")


async def interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    if len(context.args) < 3:
        await update.message.reply_text(
            "*Usage:* `/interval NAME BASE VARIATION`\n\n"
            "*Examples:*\n"
            "`/interval ch1 15 5` = 10-20 min\n"
            "`/interval ch1 30 10` = 20-40 min\n"
            "`/interval ch1 60 15` = 45-75 min",
            parse_mode='Markdown'
        )
        return
    
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"Channel `{name}` not found", parse_mode='Markdown')
        return
    
    try:
        base = int(context.args[1])
        var = int(context.args[2])
        
        if base < 5:
            await update.message.reply_text("Minimum 5 minutes")
            return
        
        CHANNELS[name]['interval'] = base
        CHANNELS[name]['variation'] = var
        save_data()
        
        await update.message.reply_text(
            f"*Interval set for `{name}`*\n\n"
            f"Base: {base} min\n"
            f"Variation: +/-{var} min\n"
            f"Range: {base-var}-{base+var} min",
            parse_mode='Markdown'
        )
    except ValueError:
        await update.message.reply_text("Invalid numbers")


async def content_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "*Usage:* `/content NAME types`\n\n"
            "*Types:* photos, videos, audio, docs, links\n\n"
            "*Examples:*\n"
            "`/content ch1 photos,videos`\n"
            "`/content ch1 videos`\n"
            "`/content ch1 photos,videos,audio`",
            parse_mode='Markdown'
        )
        return
    
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"Channel `{name}` not found", parse_mode='Markdown')
        return
    
    types_str = context.args[1].lower()
    valid = ['photos', 'videos', 'audio', 'docs', 'links']
    types = [t.strip() for t in types_str.split(',') if t.strip() in valid]
    
    if not types:
        await update.message.reply_text(f"Invalid types. Use: {', '.join(valid)}")
        return
    
    CHANNELS[name]['content_types'] = types
    save_data()
    
    await update.message.reply_text(
        f"*Content set for `{name}`*\n\nTypes: {', '.join(types)}",
        parse_mode='Markdown'
    )


async def caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "*Usage:* `/caption NAME Your text here`\n\n"
            "*Clear:* `/caption NAME clear`",
            parse_mode='Markdown'
        )
        return
    
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"Channel `{name}` not found", parse_mode='Markdown')
        return
    
    caption = ' '.join(context.args[1:])
    
    if caption.lower() == 'clear':
        CHANNELS[name]['caption'] = None
        save_data()
        await update.message.reply_text(f"Caption cleared for `{name}`", parse_mode='Markdown')
    else:
        CHANNELS[name]['caption'] = caption
        save_data()
        await update.message.reply_text(f"*Caption set for `{name}`*\n\n{caption}", parse_mode='Markdown')


async def limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("*Usage:* `/limit NAME NUMBER`", parse_mode='Markdown')
        return
    
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"Channel `{name}` not found", parse_mode='Markdown')
        return
    
    try:
        limit = int(context.args[1])
        CHANNELS[name]['daily_limit'] = limit
        save_data()
        await update.message.reply_text(f"*Limit set for `{name}`:* {limit}/day", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("Invalid number")


async def go_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    global is_running
    
    if not CHANNELS:
        await update.message.reply_text("No channels. Use `/quicksetup`", parse_mode='Markdown')
        return
    
    if context.args:
        name = context.args[0].lower()
        if name not in CHANNELS:
            await update.message.reply_text(f"Channel `{name}` not found", parse_mode='Markdown')
            return
        
        config = CHANNELS[name]
        if not config.get('source_id') or not config.get('dest_id'):
            await update.message.reply_text(f"Channel `{name}` not configured", parse_mode='Markdown')
            return
        
        CHANNELS[name]['enabled'] = True
        is_running = True
        save_data()
        
        if name not in channel_tasks or channel_tasks[name].done():
            channel_tasks[name] = asyncio.create_task(forward_loop(name))
        
        await update.message.reply_text(f"Started `{name}`", parse_mode='Markdown')
        logger.info(f"Started: {name}")
    else:
        is_running = True
        started = []
        for name, config in CHANNELS.items():
            if config.get('source_id') and config.get('dest_id'):
                CHANNELS[name]['enabled'] = True
                if name not in channel_tasks or channel_tasks[name].done():
                    channel_tasks[name] = asyncio.create_task(forward_loop(name))
                started.append(name)
        save_data()
        await update.message.reply_text(f"*Started:* {', '.join(started)}", parse_mode='Markdown')
        logger.info(f"Started all: {started}")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    global is_running
    
    if context.args:
        name = context.args[0].lower()
        if name not in CHANNELS:
            await update.message.reply_text(f"Channel `{name}` not found", parse_mode='Markdown')
            return
        
        CHANNELS[name]['enabled'] = False
        if name in channel_tasks:
            channel_tasks[name].cancel()
            del channel_tasks[name]
        save_data()
        
        await update.message.reply_text(f"Stopped `{name}`", parse_mode='Markdown')
        logger.info(f"Stopped: {name}")
    else:
        is_running = False
        for name in CHANNELS:
            CHANNELS[name]['enabled'] = False
            if name in channel_tasks:
                channel_tasks[name].cancel()
        channel_tasks.clear()
        save_data()
        
        await update.message.reply_text("*All stopped*", parse_mode='Markdown')
        logger.info("Stopped all")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    if not CHANNELS:
        await update.message.reply_text("No channels. Use `/quicksetup`", parse_mode='Markdown')
        return
    
    text = "*Your Channels:*\n\n"
    for name, config in CHANNELS.items():
        status = "ON" if config.get('enabled') else "OFF"
        interval = config.get('interval', DEFAULT_INTERVAL)
        var = config.get('variation', DEFAULT_VARIATION)
        count = config.get('daily_count', 0)
        limit = config.get('daily_limit', DEFAULT_LIMIT)
        
        text += f"*{name}* [{status}]\n"
        text += f"  {interval}+/-{var}min | {count}/{limit} today\n\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    if not context.args:
        await list_command(update, context)
        return
    
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"Channel `{name}` not found", parse_mode='Markdown')
        return
    
    config = CHANNELS[name]
    status = "ON" if config.get('enabled') else "OFF"
    
    text = f"*Channel: {name}* [{status}]\n\n"
    text += f"Source: `{config.get('source_id')}`\n"
    text += f"Dest: `{config.get('dest_id')}`\n"
    text += f"Interval: {config.get('interval')}+/-{config.get('variation')} min\n"
    text += f"Content: {', '.join(config.get('content_types', []))}\n"
    text += f"Caption: {config.get('caption') or 'None'}\n"
    text += f"Today: {config.get('daily_count', 0)}/{config.get('daily_limit')}\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    reset_daily_counts()
    
    if not CHANNELS:
        await update.message.reply_text("No channels", parse_mode='Markdown')
        return
    
    text = f"*Stats - {datetime.now().strftime('%Y-%m-%d')}*\n\n"
    total = 0
    
    for name, config in CHANNELS.items():
        count = config.get('daily_count', 0)
        limit = config.get('daily_limit', DEFAULT_LIMIT)
        status = "ON" if config.get('enabled') else "OFF"
        text += f"{name} [{status}]: {count}/{limit}\n"
        total += count
    
    text += f"\n*Total:* {total}"
    await update.message.reply_text(text, parse_mode='Markdown')


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    
    if not context.args:
        await update.message.reply_text("*Usage:* `/remove NAME`", parse_mode='Markdown')
        return
    
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"Channel `{name}` not found", parse_mode='Markdown')
        return
    
    if name in channel_tasks:
        channel_tasks[name].cancel()
        del channel_tasks[name]
    
    del CHANNELS[name]
    save_data()
    
    await update.message.reply_text(f"Removed `{name}`", parse_mode='Markdown')
    logger.info(f"Removed: {name}")


async def forward_loop(name):
    global telethon_client
    logger.info(f"Loop started: {name}")
    
    while CHANNELS.get(name, {}).get('enabled', False):
        try:
            reset_daily_counts()
            config = CHANNELS.get(name)
            if not config:
                break
            
            if config.get('daily_count', 0) >= config.get('daily_limit', DEFAULT_LIMIT):
                logger.info(f"{name} limit reached")
                await asyncio.sleep(3600)
                continue
            
            source = config.get('source_id')
            dest = config.get('dest_id')
            
            if not source or not dest:
                break
            
            try:
                messages = await telethon_client.get_messages(source, limit=50)
                forwarded = set(config.get('forwarded_ids', []))
                
                for msg in messages:
                    if msg.id in forwarded:
                        continue
                    if not should_forward(msg, name):
                        continue
                    
                    try:
                        caption = config.get('caption')
                        if caption and msg.media:
                            await telethon_client.send_message(dest, caption, file=msg.media)
                        else:
                            await telethon_client.forward_messages(dest, msg, source)
                        
                        CHANNELS[name]['forwarded_ids'].append(msg.id)
                        CHANNELS[name]['forwarded_ids'] = CHANNELS[name]['forwarded_ids'][-500:]
                        CHANNELS[name]['daily_count'] = config.get('daily_count', 0) + 1
                        save_data()
                        
                        logger.info(f"Forwarded to {name} (#{CHANNELS[name]['daily_count']})")
                        break
                    except Exception as e:
                        logger.error(f"Forward error: {e}")
                        continue
            except Exception as e:
                logger.error(f"Get messages error: {e}")
            
            interval = get_random_interval(name)
            logger.info(f"{name} next in {interval}m")
            await asyncio.sleep(interval * 60)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Loop error: {e}")
            await asyncio.sleep(60)
    
    logger.info(f"Loop ended: {name}")


async def start_telethon():
    global telethon_client
    telethon_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await telethon_client.start()
    me = await telethon_client.get_me()
    logger.info(f"Telethon: {me.first_name}")
    return telethon_client


def main():
    print("=" * 50)
    print("  MyFC Forwarder v3.0")
    print("=" * 50)
    
    load_data()
    logger.info(f"Admin: {ADMIN_ID}")
    logger.info(f"Channels: {len(CHANNELS)}")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("quicksetup", quicksetup_command))
    app.add_handler(CommandHandler("interval", interval_command))
    app.add_handler(CommandHandler("content", content_command))
    app.add_handler(CommandHandler("caption", caption_command))
    app.add_handler(CommandHandler("limit", limit_command))
    app.add_handler(CommandHandler("go", go_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("remove", remove_command))
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_telethon())
    
    logger.info("Bot running!")
    print("=" * 50)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == '__main__':
    main()
