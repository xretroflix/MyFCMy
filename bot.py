#!/usr/bin/env python3
"""
Safe Telethon Forwarder v3.0 - Enhanced Edition
- Unlimited channels with custom names
- Different source per destination
- Custom intervals per channel
- Content type filtering (photos, videos, audio, docs, links)
- Optional captions per channel
- Admin-only control bot

Environment Variables Required:
- BOT_TOKEN: Control bot token from @BotFather
- API_ID: Telegram API ID
- API_HASH: Telegram API Hash
- SESSION_STRING: Telethon session string
- ADMIN_ID: Your Telegram user ID
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

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION FROM ENVIRONMENT VARIABLES
# =============================================================================

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_STRING = os.environ.get('SESSION_STRING', '')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))

# Validate required vars
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable must be set!")
if not API_ID or not API_HASH:
    raise ValueError("❌ API_ID and API_HASH environment variables must be set!")
if not SESSION_STRING:
    raise ValueError("❌ SESSION_STRING environment variable must be set!")
if not ADMIN_ID:
    raise ValueError("❌ ADMIN_ID environment variable must be set!")

# =============================================================================
# DEFAULT SETTINGS
# =============================================================================

DEFAULT_INTERVAL = 30  # minutes
DEFAULT_VARIATION = 10  # ±10 minutes
DEFAULT_DAILY_LIMIT = 100
DEFAULT_CONTENT_TYPES = ['photos', 'videos']  # Default: photos + videos

# =============================================================================
# CHANNEL CONFIGURATION (Dynamic)
# =============================================================================

# Structure:
# CHANNELS = {
#     'channel_name': {
#         'source_id': -100xxx,
#         'dest_id': -100xxx,
#         'interval': 30,
#         'variation': 10,
#         'daily_limit': 100,
#         'content_types': ['photos', 'videos'],
#         'caption': None or "text",
#         'enabled': True/False,
#         'daily_count': 0,
#         'forwarded_ids': [],
#     }
# }

CHANNELS = {}

# =============================================================================
# PERSISTENT STORAGE
# =============================================================================

DATA_DIR = "/app/data"
if not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR)
        logger.info(f"✅ Created data directory: {DATA_DIR}")
    except:
        DATA_DIR = "."
        logger.warning("⚠️ Using current directory for storage")

DATA_FILE = os.path.join(DATA_DIR, "forwarder_data.json")

# Runtime
last_reset_date = None
is_running = False
telethon_client = None
channel_tasks = {}  # Store asyncio tasks for each channel

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def save_data():
    """Save persistent data to JSON file"""
    try:
        data = {
            'channels': CHANNELS,
            'last_reset_date': last_reset_date.isoformat() if last_reset_date else None,
        }
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        logger.info("✅ Data saved")
    except Exception as e:
        logger.error(f"❌ Save error: {e}")


def load_data():
    """Load persistent data from JSON file"""
    global CHANNELS, last_reset_date
    
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
            
            CHANNELS = data.get('channels', {})
            
            if data.get('last_reset_date'):
                last_reset_date = datetime.fromisoformat(data['last_reset_date'])
            
            logger.info(f"✅ Data loaded - {len(CHANNELS)} channels configured")
    except Exception as e:
        logger.warning(f"⚠️ Load error (starting fresh): {e}")


def reset_daily_counts_if_needed():
    """Reset daily counts at midnight"""
    global last_reset_date
    
    today = datetime.now().date()
    
    if last_reset_date is None or last_reset_date.date() < today:
        for name in CHANNELS:
            CHANNELS[name]['daily_count'] = 0
        last_reset_date = datetime.now()
        logger.info(f"🔄 Daily counts reset for {today}")
        save_data()


def get_random_interval(channel_name):
    """Get randomized interval for a channel"""
    config = CHANNELS.get(channel_name, {})
    base = config.get('interval', DEFAULT_INTERVAL)
    var = config.get('variation', DEFAULT_VARIATION)
    
    # Random variation (can be positive or negative)
    variation = random.randint(1, var)
    if random.choice([True, False]):
        variation = -variation
    
    interval = max(5, base + variation)  # Minimum 5 minutes
    return interval


def get_content_type(message):
    """
    Determine content type of a message
    Returns: 'photos', 'videos', 'audio', 'docs', 'links', or None
    """
    if not message:
        return None
    
    # Check for photo
    if message.photo or isinstance(message.media, MessageMediaPhoto):
        return 'photos'
    
    # Check for document types
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
    
    # Check for links in text
    if message.text or message.message:
        text = message.text or message.message
        if 'http://' in text or 'https://' in text:
            return 'links'
    
    # Check for web page preview
    if isinstance(message.media, MessageMediaWebPage):
        return 'links'
    
    return None


def should_forward(message, channel_name):
    """Check if message should be forwarded based on content type settings"""
    config = CHANNELS.get(channel_name, {})
    allowed_types = config.get('content_types', DEFAULT_CONTENT_TYPES)
    
    content_type = get_content_type(message)
    
    if content_type is None:
        return False
    
    return content_type in allowed_types


# =============================================================================
# ADMIN CHECK - COMPLETE LOCKDOWN
# =============================================================================

async def admin_only(update: Update) -> bool:
    """Check if user is admin. Non-admins get ZERO response."""
    if not update.effective_user:
        return False
    
    if update.effective_user.id != ADMIN_ID:
        logger.warning(f"🚫 Blocked: {update.effective_user.id}")
        return False
    
    return True


# =============================================================================
# TELEGRAM BOT COMMAND HANDLERS
# =============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    if not await admin_only(update):
        return
    
    await update.message.reply_text(
        f"🤖 **MyFC Forwarder v3.0**\n\n"
        f"Admin: `{ADMIN_ID}` ✅\n\n"
        f"**📋 Channel Setup:**\n"
        f"`/addchannel NAME` - Add new channel\n"
        f"`/removechannel NAME` - Remove channel\n"
        f"`/setsource NAME ID` - Set source\n"
        f"`/setdest NAME ID` - Set destination\n"
        f"`/setinterval NAME BASE VAR` - Set timing\n"
        f"`/setcontent NAME types` - Set content types\n"
        f"`/setcaption NAME text` - Set caption\n"
        f"`/clearcaption NAME` - Remove caption\n"
        f"`/setlimit NAME NUM` - Set daily limit\n\n"
        f"**🎮 Control:**\n"
        f"`/startforward` - Start all channels\n"
        f"`/stopforward` - Stop all channels\n"
        f"`/startchannel NAME` - Start one\n"
        f"`/stopchannel NAME` - Stop one\n\n"
        f"**📊 Info:**\n"
        f"`/listchannels` - List all channels\n"
        f"`/status NAME` - Channel details\n"
        f"`/stats` - Daily statistics\n\n"
        f"**📝 Content Types:**\n"
        f"`photos` `videos` `audio` `docs` `links`\n\n"
        f"**⏰ Default Settings:**\n"
        f"Interval: {DEFAULT_INTERVAL} min ±{DEFAULT_VARIATION}\n"
        f"Content: photos + videos\n"
        f"Limit: {DEFAULT_DAILY_LIMIT}/day",
        parse_mode='Markdown'
    )


async def addchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new channel configuration"""
    if not await admin_only(update):
        return
    
    if not context.args:
        await update.message.reply_text(
            "**Usage:** `/addchannel NAME`\n\n"
            "Example: `/addchannel MyPremium`\n\n"
            "Name should be one word, no spaces.",
            parse_mode='Markdown'
        )
        return
    
    name = context.args[0].lower()
    
    if name in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` already exists!", parse_mode='Markdown')
        return
    
    CHANNELS[name] = {
        'source_id': None,
        'dest_id': None,
        'interval': DEFAULT_INTERVAL,
        'variation': DEFAULT_VARIATION,
        'daily_limit': DEFAULT_DAILY_LIMIT,
        'content_types': DEFAULT_CONTENT_TYPES.copy(),
        'caption': None,
        'enabled': False,
        'daily_count': 0,
        'forwarded_ids': [],
    }
    save_data()
    
    await update.message.reply_text(
        f"✅ **Channel `{name}` created!**\n\n"
        f"Now configure it:\n"
        f"1. `/setsource {name} CHANNEL_ID`\n"
        f"2. `/setdest {name} CHANNEL_ID`\n"
        f"3. `/setinterval {name} 30 10` (optional)\n"
        f"4. `/setcontent {name} photos videos` (optional)\n"
        f"5. `/setcaption {name} Your text` (optional)\n\n"
        f"Then start: `/startchannel {name}`",
        parse_mode='Markdown'
    )
    logger.info(f"✅ Channel added: {name}")


async def removechannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a channel configuration"""
    if not await admin_only(update):
        return
    
    if not context.args:
        await update.message.reply_text("**Usage:** `/removechannel NAME`", parse_mode='Markdown')
        return
    
    name = context.args[0].lower()
    
    if name not in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` not found!", parse_mode='Markdown')
        return
    
    # Stop if running
    if name in channel_tasks:
        channel_tasks[name].cancel()
        del channel_tasks[name]
    
    del CHANNELS[name]
    save_data()
    
    await update.message.reply_text(f"✅ Channel `{name}` removed!", parse_mode='Markdown')
    logger.info(f"✅ Channel removed: {name}")


async def setsource_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set source channel for a configuration"""
    if not await admin_only(update):
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "**Usage:** `/setsource NAME CHANNEL_ID`\n\n"
            "Example: `/setsource MyPremium -1001234567890`",
            parse_mode='Markdown'
        )
        return
    
    name = context.args[0].lower()
    
    if name not in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` not found! Use `/addchannel {name}` first.", parse_mode='Markdown')
        return
    
    try:
        source_id = int(context.args[1])
        CHANNELS[name]['source_id'] = source_id
        save_data()
        
        await update.message.reply_text(
            f"✅ **Source set for `{name}`**\n\nSource ID: `{source_id}`",
            parse_mode='Markdown'
        )
        logger.info(f"✅ Source set for {name}: {source_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID - must be a number")


async def setdest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set destination channel for a configuration"""
    if not await admin_only(update):
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "**Usage:** `/setdest NAME CHANNEL_ID`\n\n"
            "Example: `/setdest MyPremium -1001234567890`",
            parse_mode='Markdown'
        )
        return
    
    name = context.args[0].lower()
    
    if name not in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` not found!", parse_mode='Markdown')
        return
    
    try:
        dest_id = int(context.args[1])
        CHANNELS[name]['dest_id'] = dest_id
        save_data()
        
        await update.message.reply_text(
            f"✅ **Destination set for `{name}`**\n\nDest ID: `{dest_id}`",
            parse_mode='Markdown'
        )
        logger.info(f"✅ Dest set for {name}: {dest_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID - must be a number")


async def setinterval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set interval for a channel"""
    if not await admin_only(update):
        return
    
    if len(context.args) < 3:
        await update.message.reply_text(
            "**Usage:** `/setinterval NAME BASE VARIATION`\n\n"
            "Example: `/setinterval MyPremium 20 10`\n"
            "→ Posts every 20 min ±10 min (10-30 min range)\n\n"
            "**Examples:**\n"
            "• `/setinterval ch1 15 5` → 10-20 min\n"
            "• `/setinterval ch2 30 10` → 20-40 min\n"
            "• `/setinterval ch3 60 15` → 45-75 min",
            parse_mode='Markdown'
        )
        return
    
    name = context.args[0].lower()
    
    if name not in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` not found!", parse_mode='Markdown')
        return
    
    try:
        base = int(context.args[1])
        variation = int(context.args[2])
        
        if base < 5:
            await update.message.reply_text("❌ Minimum interval is 5 minutes (safety)")
            return
        
        if variation >= base:
            await update.message.reply_text("❌ Variation must be less than base interval")
            return
        
        CHANNELS[name]['interval'] = base
        CHANNELS[name]['variation'] = variation
        save_data()
        
        min_time = base - variation
        max_time = base + variation
        
        await update.message.reply_text(
            f"✅ **Interval set for `{name}`**\n\n"
            f"Base: {base} min\n"
            f"Variation: ±{variation} min\n"
            f"Range: {min_time}-{max_time} min",
            parse_mode='Markdown'
        )
        logger.info(f"✅ Interval set for {name}: {base}±{variation}")
    except ValueError:
        await update.message.reply_text("❌ Invalid numbers")


async def setcontent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set content types to forward"""
    if not await admin_only(update):
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "**Usage:** `/setcontent NAME type1 type2 ...`\n\n"
            "**Available types:**\n"
            "• `photos` - Images\n"
            "• `videos` - Videos\n"
            "• `audio` - Audio files\n"
            "• `docs` - Documents\n"
            "• `links` - Links/URLs\n\n"
            "**Examples:**\n"
            "• `/setcontent ch1 photos videos`\n"
            "• `/setcontent ch2 videos`\n"
            "• `/setcontent ch3 photos videos audio docs links`",
            parse_mode='Markdown'
        )
        return
    
    name = context.args[0].lower()
    
    if name not in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` not found!", parse_mode='Markdown')
        return
    
    valid_types = ['photos', 'videos', 'audio', 'docs', 'links']
    content_types = []
    
    for arg in context.args[1:]:
        arg_lower = arg.lower()
        if arg_lower in valid_types:
            content_types.append(arg_lower)
    
    if not content_types:
        await update.message.reply_text(
            f"❌ No valid content types!\n\nValid: `{', '.join(valid_types)}`",
            parse_mode='Markdown'
        )
        return
    
    CHANNELS[name]['content_types'] = content_types
    save_data()
    
    await update.message.reply_text(
        f"✅ **Content types set for `{name}`**\n\n"
        f"Will forward: `{', '.join(content_types)}`",
        parse_mode='Markdown'
    )
    logger.info(f"✅ Content types set for {name}: {content_types}")


async def setcaption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set caption for a channel"""
    if not await admin_only(update):
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "**Usage:** `/setcaption NAME Your caption text`\n\n"
            "Example: `/setcaption free ⚡ Upgrade for faster updates!`",
            parse_mode='Markdown'
        )
        return
    
    name = context.args[0].lower()
    
    if name not in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` not found!", parse_mode='Markdown')
        return
    
    caption = ' '.join(context.args[1:])
    CHANNELS[name]['caption'] = caption
    save_data()
    
    await update.message.reply_text(
        f"✅ **Caption set for `{name}`**\n\n{caption}",
        parse_mode='Markdown'
    )
    logger.info(f"✅ Caption set for {name}")


async def clearcaption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear caption for a channel"""
    if not await admin_only(update):
        return
    
    if not context.args:
        await update.message.reply_text("**Usage:** `/clearcaption NAME`", parse_mode='Markdown')
        return
    
    name = context.args[0].lower()
    
    if name not in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` not found!", parse_mode='Markdown')
        return
    
    CHANNELS[name]['caption'] = None
    save_data()
    
    await update.message.reply_text(f"✅ Caption cleared for `{name}`", parse_mode='Markdown')


async def setlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set daily limit for a channel"""
    if not await admin_only(update):
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "**Usage:** `/setlimit NAME NUMBER`\n\n"
            "Example: `/setlimit MyPremium 80`",
            parse_mode='Markdown'
        )
        return
    
    name = context.args[0].lower()
    
    if name not in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` not found!", parse_mode='Markdown')
        return
    
    try:
        limit = int(context.args[1])
        
        if limit < 1:
            await update.message.reply_text("❌ Limit must be at least 1")
            return
        
        if limit > 200:
            await update.message.reply_text("⚠️ Warning: High limit may trigger Telegram restrictions!")
        
        CHANNELS[name]['daily_limit'] = limit
        save_data()
        
        await update.message.reply_text(
            f"✅ **Daily limit set for `{name}`**\n\nLimit: {limit}/day",
            parse_mode='Markdown'
        )
        logger.info(f"✅ Limit set for {name}: {limit}")
    except ValueError:
        await update.message.reply_text("❌ Invalid number")


async def listchannels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all configured channels"""
    if not await admin_only(update):
        return
    
    if not CHANNELS:
        await update.message.reply_text(
            "No channels configured yet.\n\nUse `/addchannel NAME` to create one.",
            parse_mode='Markdown'
        )
        return
    
    text = "📋 **Configured Channels:**\n\n"
    
    for name, config in CHANNELS.items():
        status = "🟢" if config.get('enabled') else "🔴"
        source = config.get('source_id') or 'Not set'
        dest = config.get('dest_id') or 'Not set'
        
        text += f"{status} **{name}**\n"
        text += f"   Source: `{source}`\n"
        text += f"   Dest: `{dest}`\n"
        text += f"   Interval: {config.get('interval', DEFAULT_INTERVAL)}±{config.get('variation', DEFAULT_VARIATION)} min\n"
        text += f"   Content: {', '.join(config.get('content_types', DEFAULT_CONTENT_TYPES))}\n"
        text += f"   Today: {config.get('daily_count', 0)}/{config.get('daily_limit', DEFAULT_DAILY_LIMIT)}\n\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed status of a channel"""
    if not await admin_only(update):
        return
    
    if not context.args:
        # Show all channels summary
        await listchannels_command(update, context)
        return
    
    name = context.args[0].lower()
    
    if name not in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` not found!", parse_mode='Markdown')
        return
    
    config = CHANNELS[name]
    status = "🟢 Running" if config.get('enabled') else "🔴 Stopped"
    
    min_time = config.get('interval', DEFAULT_INTERVAL) - config.get('variation', DEFAULT_VARIATION)
    max_time = config.get('interval', DEFAULT_INTERVAL) + config.get('variation', DEFAULT_VARIATION)
    
    text = f"📊 **Channel: {name}**\n\n"
    text += f"**Status:** {status}\n\n"
    text += f"**Source:** `{config.get('source_id') or 'Not set'}`\n"
    text += f"**Destination:** `{config.get('dest_id') or 'Not set'}`\n\n"
    text += f"**Timing:**\n"
    text += f"   Base: {config.get('interval', DEFAULT_INTERVAL)} min\n"
    text += f"   Variation: ±{config.get('variation', DEFAULT_VARIATION)} min\n"
    text += f"   Range: {min_time}-{max_time} min\n\n"
    text += f"**Content Types:**\n"
    text += f"   {', '.join(config.get('content_types', DEFAULT_CONTENT_TYPES))}\n\n"
    text += f"**Caption:**\n"
    text += f"   {config.get('caption') or 'None'}\n\n"
    text += f"**Daily:**\n"
    text += f"   Count: {config.get('daily_count', 0)}/{config.get('daily_limit', DEFAULT_DAILY_LIMIT)}\n"
    text += f"   Forwarded IDs: {len(config.get('forwarded_ids', []))}"
    
    await update.message.reply_text(text, parse_mode='Markdown')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show daily statistics"""
    if not await admin_only(update):
        return
    
    reset_daily_counts_if_needed()
    
    if not CHANNELS:
        await update.message.reply_text("No channels configured yet.", parse_mode='Markdown')
        return
    
    text = f"📈 **Daily Statistics**\n📅 {datetime.now().strftime('%Y-%m-%d')}\n\n"
    
    total_count = 0
    total_limit = 0
    
    for name, config in CHANNELS.items():
        count = config.get('daily_count', 0)
        limit = config.get('daily_limit', DEFAULT_DAILY_LIMIT)
        pct = (count / limit * 100) if limit > 0 else 0
        
        bar = '█' * int(pct / 10) + '░' * (10 - int(pct / 10))
        status = "🟢" if config.get('enabled') else "🔴"
        
        text += f"{status} **{name}**\n[{bar}] {count}/{limit}\n\n"
        
        total_count += count
        total_limit += limit
    
    text += f"**Total:** {total_count}/{total_limit}"
    
    await update.message.reply_text(text, parse_mode='Markdown')


async def startforward_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start forwarding for all channels"""
    if not await admin_only(update):
        return
    
    global is_running
    
    if not CHANNELS:
        await update.message.reply_text("❌ No channels configured! Use `/addchannel NAME` first.", parse_mode='Markdown')
        return
    
    # Check if any channel is ready
    ready_channels = []
    not_ready = []
    
    for name, config in CHANNELS.items():
        if config.get('source_id') and config.get('dest_id'):
            ready_channels.append(name)
        else:
            not_ready.append(name)
    
    if not ready_channels:
        await update.message.reply_text(
            "❌ No channels are fully configured!\n\n"
            "Each channel needs source and destination set.",
            parse_mode='Markdown'
        )
        return
    
    is_running = True
    
    # Start each ready channel
    started = []
    for name in ready_channels:
        CHANNELS[name]['enabled'] = True
        if name not in channel_tasks or channel_tasks[name].done():
            channel_tasks[name] = asyncio.create_task(channel_forward_loop(name))
            started.append(name)
    
    save_data()
    
    text = f"✅ **Forwarding Started!**\n\n"
    text += f"**Running:** {', '.join(started)}\n"
    if not_ready:
        text += f"**Not ready:** {', '.join(not_ready)}\n"
    text += f"\nUse `/stopforward` to stop all"
    
    await update.message.reply_text(text, parse_mode='Markdown')
    logger.info(f"🚀 Started channels: {started}")


async def stopforward_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop forwarding for all channels"""
    if not await admin_only(update):
        return
    
    global is_running
    is_running = False
    
    stopped = []
    for name in CHANNELS:
        if CHANNELS[name].get('enabled'):
            CHANNELS[name]['enabled'] = False
            stopped.append(name)
        if name in channel_tasks:
            channel_tasks[name].cancel()
    
    channel_tasks.clear()
    save_data()
    
    await update.message.reply_text(
        f"🛑 **All Forwarding Stopped!**\n\nStopped: {', '.join(stopped) if stopped else 'None were running'}",
        parse_mode='Markdown'
    )
    logger.info("🛑 All forwarding stopped")


async def startchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start forwarding for a specific channel"""
    if not await admin_only(update):
        return
    
    if not context.args:
        await update.message.reply_text("**Usage:** `/startchannel NAME`", parse_mode='Markdown')
        return
    
    name = context.args[0].lower()
    
    if name not in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` not found!", parse_mode='Markdown')
        return
    
    config = CHANNELS[name]
    
    if not config.get('source_id') or not config.get('dest_id'):
        await update.message.reply_text(
            f"❌ Channel `{name}` not fully configured!\n\n"
            f"Source: `{config.get('source_id') or 'Not set'}`\n"
            f"Dest: `{config.get('dest_id') or 'Not set'}`",
            parse_mode='Markdown'
        )
        return
    
    if config.get('enabled'):
        await update.message.reply_text(f"⚠️ Channel `{name}` is already running!", parse_mode='Markdown')
        return
    
    CHANNELS[name]['enabled'] = True
    save_data()
    
    if name not in channel_tasks or channel_tasks[name].done():
        channel_tasks[name] = asyncio.create_task(channel_forward_loop(name))
    
    await update.message.reply_text(f"✅ Channel `{name}` started!", parse_mode='Markdown')
    logger.info(f"🚀 Started channel: {name}")


async def stopchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop forwarding for a specific channel"""
    if not await admin_only(update):
        return
    
    if not context.args:
        await update.message.reply_text("**Usage:** `/stopchannel NAME`", parse_mode='Markdown')
        return
    
    name = context.args[0].lower()
    
    if name not in CHANNELS:
        await update.message.reply_text(f"❌ Channel `{name}` not found!", parse_mode='Markdown')
        return
    
    CHANNELS[name]['enabled'] = False
    
    if name in channel_tasks:
        channel_tasks[name].cancel()
        del channel_tasks[name]
    
    save_data()
    
    await update.message.reply_text(f"🛑 Channel `{name}` stopped!", parse_mode='Markdown')
    logger.info(f"🛑 Stopped channel: {name}")


# =============================================================================
# FORWARDING LOOP (PER CHANNEL)
# =============================================================================

async def channel_forward_loop(channel_name):
    """Forwarding loop for a specific channel"""
    global telethon_client
    
    logger.info(f"📡 Loop started for: {channel_name}")
    
    while CHANNELS.get(channel_name, {}).get('enabled', False):
        try:
            reset_daily_counts_if_needed()
            
            config = CHANNELS.get(channel_name)
            if not config:
                break
            
            # Check daily limit
            if config.get('daily_count', 0) >= config.get('daily_limit', DEFAULT_DAILY_LIMIT):
                logger.info(f"⚠️ {channel_name} daily limit reached")
                await asyncio.sleep(3600)  # Wait 1 hour
                continue
            
            source_id = config.get('source_id')
            dest_id = config.get('dest_id')
            
            if not source_id or not dest_id:
                logger.error(f"❌ {channel_name} missing source/dest")
                break
            
            # Get messages from source
            try:
                messages = await telethon_client.get_messages(source_id, limit=50)
                
                forwarded_ids = set(config.get('forwarded_ids', []))
                
                for msg in messages:
                    if msg.id in forwarded_ids:
                        continue
                    
                    if not should_forward(msg, channel_name):
                        continue
                    
                    # Forward the message
                    try:
                        caption = config.get('caption')
                        
                        if caption and msg.media:
                            # Send with custom caption
                            await telethon_client.send_message(
                                dest_id,
                                caption,
                                file=msg.media
                            )
                        else:
                            # Simple forward
                            await telethon_client.forward_messages(
                                dest_id,
                                msg,
                                source_id
                            )
                        
                        # Update tracking
                        CHANNELS[channel_name]['forwarded_ids'].append(msg.id)
                        # Keep only last 500 IDs
                        CHANNELS[channel_name]['forwarded_ids'] = CHANNELS[channel_name]['forwarded_ids'][-500:]
                        CHANNELS[channel_name]['daily_count'] = config.get('daily_count', 0) + 1
                        save_data()
                        
                        logger.info(f"✅ → {channel_name} (#{CHANNELS[channel_name]['daily_count']})")
                        break  # Only one per cycle
                        
                    except Exception as e:
                        logger.error(f"❌ Forward error ({channel_name}): {e}")
                        continue
                
            except Exception as e:
                logger.error(f"❌ Get messages error ({channel_name}): {e}")
            
            # Wait with random interval
            interval = get_random_interval(channel_name)
            logger.info(f"⏰ {channel_name} next in {interval}m")
            await asyncio.sleep(interval * 60)
            
        except asyncio.CancelledError:
            logger.info(f"📡 Loop cancelled for: {channel_name}")
            break
        except Exception as e:
            logger.error(f"❌ Loop error ({channel_name}): {e}")
            await asyncio.sleep(60)
    
    logger.info(f"📡 Loop ended for: {channel_name}")


# =============================================================================
# TELETHON CLIENT
# =============================================================================

async def start_telethon():
    """Start Telethon client"""
    global telethon_client
    
    telethon_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await telethon_client.start()
    
    me = await telethon_client.get_me()
    logger.info(f"✅ Telethon: {me.first_name} (@{me.username})")
    
    return telethon_client


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Main function"""
    print("=" * 60)
    print("  🤖 MyFC Forwarder v3.0 - Enhanced Edition")
    print("  📷 Multi-channel | Custom intervals | Content filters")
    print("=" * 60)
    
    # Load saved data
    load_data()
    
    logger.info(f"👤 Admin ID: {ADMIN_ID}")
    logger.info(f"📋 Channels configured: {len(CHANNELS)}")
    
    # Create bot application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("addchannel", addchannel_command))
    app.add_handler(CommandHandler("removechannel", removechannel_command))
    app.add_handler(CommandHandler("setsource", setsource_command))
    app.add_handler(CommandHandler("setdest", setdest_command))
    app.add_handler(CommandHandler("setinterval", setinterval_command))
    app.add_handler(CommandHandler("setcontent", setcontent_command))
    app.add_handler(CommandHandler("setcaption", setcaption_command))
    app.add_handler(CommandHandler("clearcaption", clearcaption_command))
    app.add_handler(CommandHandler("setlimit", setlimit_command))
    app.add_handler(CommandHandler("listchannels", listchannels_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("startforward", startforward_command))
    app.add_handler(CommandHandler("stopforward", stopforward_command))
    app.add_handler(CommandHandler("startchannel", startchannel_command))
    app.add_handler(CommandHandler("stopchannel", stopchannel_command))
    
    # Start Telethon in background
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_telethon())
    
    logger.info("🚀 Bot is running!")
    print("=" * 60)
    
    # Run bot
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == '__main__':
    main()
```

5. Click **Commit changes**

---

## New Commands Reference

| Command | Purpose |
|---------|---------|
| `/addchannel NAME` | Create new channel |
| `/removechannel NAME` | Delete channel |
| `/setsource NAME ID` | Set source for channel |
| `/setdest NAME ID` | Set destination |
| `/setinterval NAME BASE VAR` | Set timing (e.g., 20 10 = 20±10 min) |
| `/setcontent NAME types` | Set content (photos videos audio docs links) |
| `/setcaption NAME text` | Set caption |
| `/clearcaption NAME` | Remove caption |
| `/setlimit NAME NUM` | Set daily limit |
| `/listchannels` | Show all channels |
| `/status NAME` | Channel details |
| `/stats` | Daily statistics |
| `/startforward` | Start all |
| `/stopforward` | Stop all |
| `/startchannel NAME` | Start one |
| `/stopchannel NAME` | Stop one |

---

## Example Setup
```
/addchannel premium1
/setsource premium1 -1001234567890
/setdest premium1 -1009876543210
/setinterval premium1 20 10
/setcontent premium1 photos videos
/startchannel premium1
DESTINATIONS = {
    'channel_1': {
        'id': None,
        'name': 'Premium 1',
        'base_interval': 15,
        'variation': (1, 10),
        'daily_limit': 80,
        'caption': None,
    },
    'channel_2': {
        'id': None,
        'name': 'Premium 2',
        'base_interval': 30,
        'variation': (1, 10),
        'daily_limit': 45,
        'caption': None,
    },
    'channel_3': {
        'id': None,
        'name': 'Free',
        'base_interval': 60,
        'variation': (1, 10),
        'daily_limit': 20,
        'caption': '⚡ Upgrade to Premium for faster updates!',
    },
}

# =============================================================================
# PERSISTENT STORAGE
# =============================================================================

DATA_DIR = "/app/data"
if not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR)
        logger.info(f"✅ Created data directory: {DATA_DIR}")
    except:
        DATA_DIR = "."
        logger.warning("⚠️ Using current directory for storage")

DATA_FILE = os.path.join(DATA_DIR, "forwarder_data.json")

# Runtime data
daily_counts = {'channel_1': 0, 'channel_2': 0, 'channel_3': 0}
last_reset_date = None
forwarded_ids = set()
is_running = False
telethon_client = None

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def save_data():
    """Save persistent data to JSON file"""
    global SOURCE_CHANNEL, DESTINATIONS
    try:
        data = {
            'source_channel': SOURCE_CHANNEL,
            'destinations': {k: {'id': v['id'], 'caption': v['caption']} for k, v in DESTINATIONS.items()},
            'daily_counts': daily_counts,
            'last_reset_date': last_reset_date.isoformat() if last_reset_date else None,
            'forwarded_ids': list(forwarded_ids)[-1000:],
        }
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        logger.info("✅ Data saved")
    except Exception as e:
        logger.error(f"❌ Save error: {e}")


def load_data():
    """Load persistent data from JSON file"""
    global SOURCE_CHANNEL, DESTINATIONS, daily_counts, last_reset_date, forwarded_ids
    
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
            
            SOURCE_CHANNEL = data.get('source_channel')
            daily_counts = data.get('daily_counts', daily_counts)
            forwarded_ids = set(data.get('forwarded_ids', []))
            
            if data.get('last_reset_date'):
                last_reset_date = datetime.fromisoformat(data['last_reset_date'])
            
            saved_dests = data.get('destinations', {})
            for key in DESTINATIONS:
                if key in saved_dests:
                    DESTINATIONS[key]['id'] = saved_dests[key].get('id')
                    if saved_dests[key].get('caption'):
                        DESTINATIONS[key]['caption'] = saved_dests[key]['caption']
            
            logger.info(f"✅ Data loaded - Source: {SOURCE_CHANNEL}")
    except Exception as e:
        logger.warning(f"⚠️ Load error (starting fresh): {e}")


def reset_daily_counts_if_needed():
    """Reset daily counts at midnight"""
    global daily_counts, last_reset_date
    
    today = datetime.now().date()
    
    if last_reset_date is None or last_reset_date.date() < today:
        daily_counts = {'channel_1': 0, 'channel_2': 0, 'channel_3': 0}
        last_reset_date = datetime.now()
        logger.info(f"🔄 Daily counts reset for {today}")
        save_data()


def get_random_interval(channel_key):
    """Get randomized interval for a channel"""
    config = DESTINATIONS[channel_key]
    base = config['base_interval']
    var_min, var_max = config['variation']
    
    variation = random.randint(var_min, var_max)
    if random.choice([True, False]):
        variation = -variation
    
    interval = max(5, base + variation)
    return interval


def is_media_message(message):
    """Check if message contains photo or video"""
    if not message.media:
        return False
    
    if isinstance(message.media, MessageMediaPhoto):
        return True
    
    if isinstance(message.media, MessageMediaDocument):
        if message.media.document:
            mime = message.media.document.mime_type or ''
            if mime.startswith('video/') or mime.startswith('image/'):
                return True
    
    return False


# =============================================================================
# ADMIN CHECK - COMPLETE LOCKDOWN
# =============================================================================

def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id == ADMIN_ID


async def admin_only(update: Update) -> bool:
    """
    Check if user is admin.
    Returns True if admin, False otherwise.
    NON-ADMINS GET ZERO RESPONSE - Complete silence.
    """
    if not update.effective_user:
        return False
    
    if update.effective_user.id != ADMIN_ID:
        # Complete silence - no response, no acknowledgment
        logger.warning(f"🚫 Blocked: {update.effective_user.id} tried /{update.message.text.split()[0] if update.message else 'unknown'}")
        return False
    
    return True


# =============================================================================
# TELEGRAM BOT COMMAND HANDLERS
# =============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    if not await admin_only(update):
        return
    
    await update.message.reply_text(
        f"🤖 **MyFC Control Bot**\n\n"
        f"Admin: `{ADMIN_ID}` ✅\n\n"
        f"**📋 Setup Commands:**\n"
        f"`/setsource ID` - Set source channel\n"
        f"`/setdest1 ID` - Premium Channel 1 (15min)\n"
        f"`/setdest2 ID` - Premium Channel 2 (30min)\n"
        f"`/setdest3 ID` - Free Channel (60min)\n"
        f"`/setcaption3 TEXT` - Caption for Free\n\n"
        f"**🎮 Control Commands:**\n"
        f"`/startforward` - Start forwarding\n"
        f"`/stopforward` - Stop forwarding\n"
        f"`/status` - View config\n"
        f"`/stats` - View daily stats\n\n"
        f"**📊 Safe Limits:**\n"
        f"• Premium 1: 80/day (15min ±1-10)\n"
        f"• Premium 2: 45/day (30min ±1-10)\n"
        f"• Free: 20/day (60min ±1-10)",
        parse_mode='Markdown'
    )


async def setsource_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set source channel"""
    if not await admin_only(update):
        return
    
    global SOURCE_CHANNEL
    
    if not context.args:
        await update.message.reply_text(
            "**Usage:** `/setsource CHANNEL_ID`\n\n"
            "Example: `/setsource -1001234567890`\n\n"
            f"Current: `{SOURCE_CHANNEL or 'Not set'}`",
            parse_mode='Markdown'
        )
        return
    
    try:
        channel_id = int(context.args[0])
        SOURCE_CHANNEL = channel_id
        save_data()
        
        await update.message.reply_text(
            f"✅ **Source channel set!**\n\nID: `{channel_id}`",
            parse_mode='Markdown'
        )
        logger.info(f"✅ Source set: {channel_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID - must be a number")


async def setdest1_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set destination channel 1"""
    if not await admin_only(update):
        return
    
    if not context.args:
        await update.message.reply_text(
            "**Usage:** `/setdest1 CHANNEL_ID`\n\n"
            f"Current: `{DESTINATIONS['channel_1']['id'] or 'Not set'}`",
            parse_mode='Markdown'
        )
        return
    
    try:
        channel_id = int(context.args[0])
        DESTINATIONS['channel_1']['id'] = channel_id
        save_data()
        
        await update.message.reply_text(
            f"✅ **Premium Channel 1 set!**\n\n"
            f"ID: `{channel_id}`\n"
            f"Interval: 15 min ±1-10 min\n"
            f"Daily limit: 80 posts",
            parse_mode='Markdown'
        )
        logger.info(f"✅ Dest 1 set: {channel_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID")


async def setdest2_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set destination channel 2"""
    if not await admin_only(update):
        return
    
    if not context.args:
        await update.message.reply_text(
            "**Usage:** `/setdest2 CHANNEL_ID`\n\n"
            f"Current: `{DESTINATIONS['channel_2']['id'] or 'Not set'}`",
            parse_mode='Markdown'
        )
        return
    
    try:
        channel_id = int(context.args[0])
        DESTINATIONS['channel_2']['id'] = channel_id
        save_data()
        
        await update.message.reply_text(
            f"✅ **Premium Channel 2 set!**\n\n"
            f"ID: `{channel_id}`\n"
            f"Interval: 30 min ±1-10 min\n"
            f"Daily limit: 45 posts",
            parse_mode='Markdown'
        )
        logger.info(f"✅ Dest 2 set: {channel_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID")


async def setdest3_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set destination channel 3"""
    if not await admin_only(update):
        return
    
    if not context.args:
        await update.message.reply_text(
            "**Usage:** `/setdest3 CHANNEL_ID`\n\n"
            f"Current: `{DESTINATIONS['channel_3']['id'] or 'Not set'}`",
            parse_mode='Markdown'
        )
        return
    
    try:
        channel_id = int(context.args[0])
        DESTINATIONS['channel_3']['id'] = channel_id
        save_data()
        
        await update.message.reply_text(
            f"✅ **Free Channel 3 set!**\n\n"
            f"ID: `{channel_id}`\n"
            f"Interval: 60 min ±1-10 min\n"
            f"Daily limit: 20 posts\n"
            f"Caption: {DESTINATIONS['channel_3']['caption']}",
            parse_mode='Markdown'
        )
        logger.info(f"✅ Dest 3 set: {channel_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID")


async def setcaption3_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set caption for channel 3"""
    if not await admin_only(update):
        return
    
    if not context.args:
        await update.message.reply_text(
            "**Usage:** `/setcaption3 Your caption here`\n\n"
            f"Current: {DESTINATIONS['channel_3']['caption']}",
            parse_mode='Markdown'
        )
        return
    
    caption = ' '.join(context.args)
    DESTINATIONS['channel_3']['caption'] = caption
    save_data()
    
    await update.message.reply_text(f"✅ **Caption updated!**\n\n{caption}")
    logger.info("✅ Caption 3 updated")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current configuration"""
    if not await admin_only(update):
        return
    
    status_text = (
        f"📊 **Configuration**\n\n"
        f"**Source:**\n`{SOURCE_CHANNEL or 'Not set'}`\n\n"
        f"**Destinations:**\n"
    )
    
    for key, config in DESTINATIONS.items():
        status_text += (
            f"\n🔹 **{config['name']}**\n"
            f"   ID: `{config['id'] or 'Not set'}`\n"
            f"   Interval: {config['base_interval']}m ±1-10m\n"
            f"   Limit: {config['daily_limit']}/day\n"
            f"   Today: {daily_counts.get(key, 0)}\n"
        )
        if config['caption']:
            cap = config['caption'][:25] + '...' if len(config['caption']) > 25 else config['caption']
            status_text += f"   Caption: {cap}\n"
    
    status_text += f"\n**Status:** {'🟢 Running' if is_running else '🔴 Stopped'}"
    
    await update.message.reply_text(status_text, parse_mode='Markdown')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show daily statistics"""
    if not await admin_only(update):
        return
    
    reset_daily_counts_if_needed()
    
    stats_text = f"📈 **Daily Stats**\n📅 {datetime.now().strftime('%Y-%m-%d')}\n\n"
    
    for key, config in DESTINATIONS.items():
        count = daily_counts.get(key, 0)
        limit = config['daily_limit']
        pct = (count / limit * 100) if limit > 0 else 0
        bar = '█' * int(pct / 10) + '░' * (10 - int(pct / 10))
        
        stats_text += f"**{config['name']}**\n[{bar}] {count}/{limit}\n\n"
    
    total = sum(daily_counts.values())
    total_limit = sum(d['daily_limit'] for d in DESTINATIONS.values())
    stats_text += f"**Total:** {total}/{total_limit} posts"
    
    await update.message.reply_text(stats_text, parse_mode='Markdown')


async def startforward_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start forwarding"""
    if not await admin_only(update):
        return
    
    global is_running
    
    if not SOURCE_CHANNEL:
        await update.message.reply_text("❌ Set source first: `/setsource ID`", parse_mode='Markdown')
        return
    
    missing = [d['name'] for k, d in DESTINATIONS.items() if not d['id']]
    if missing:
        await update.message.reply_text(f"❌ Missing: {', '.join(missing)}")
        return
    
    if is_running:
        await update.message.reply_text("⚠️ Already running!")
        return
    
    is_running = True
    
    await update.message.reply_text(
        f"✅ **Forwarding Started!**\n\n"
        f"📥 Source: `{SOURCE_CHANNEL}`\n"
        f"📤 3 destinations active\n"
        f"🎯 Photos + Videos\n"
        f"⏰ Random intervals\n\n"
        f"Use /stopforward to stop",
        parse_mode='Markdown'
    )
    
    logger.info("🚀 Forwarding STARTED")
    asyncio.create_task(forwarding_loop())


async def stopforward_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop forwarding"""
    if not await admin_only(update):
        return
    
    global is_running
    
    if not is_running:
        await update.message.reply_text("⚠️ Not currently running!")
        return
    
    is_running = False
    await update.message.reply_text("🛑 **Forwarding stopped!**", parse_mode='Markdown')
    logger.info("🛑 Forwarding STOPPED")


# =============================================================================
# FORWARDING LOOP (TELETHON)
# =============================================================================

async def forwarding_loop():
    """Main forwarding loop using Telethon"""
    global is_running, telethon_client
    
    logger.info("📡 Forwarding loop started")
    
    next_forward = {
        'channel_1': datetime.now(),
        'channel_2': datetime.now(),
        'channel_3': datetime.now(),
    }
    
    while is_running:
        try:
            reset_daily_counts_if_needed()
            
            for channel_key in ['channel_1', 'channel_2', 'channel_3']:
                if not is_running:
                    break
                
                config = DESTINATIONS[channel_key]
                
                if not config['id']:
                    continue
                
                if datetime.now() < next_forward[channel_key]:
                    continue
                
                if daily_counts[channel_key] >= config['daily_limit']:
                    logger.info(f"⚠️ {config['name']} limit reached")
                    next_forward[channel_key] = datetime.now() + timedelta(hours=1)
                    continue
                
                try:
                    messages = await telethon_client.get_messages(SOURCE_CHANNEL, limit=50)
                    
                    for msg in messages:
                        if msg.id in forwarded_ids:
                            continue
                        
                        if not is_media_message(msg):
                            continue
                        
                        try:
                            if config['caption']:
                                await telethon_client.send_message(
                                    config['id'],
                                    config['caption'],
                                    file=msg.media
                                )
                            else:
                                await telethon_client.forward_messages(
                                    config['id'],
                                    msg,
                                    SOURCE_CHANNEL
                                )
                            
                            forwarded_ids.add(msg.id)
                            daily_counts[channel_key] += 1
                            save_data()
                            
                            logger.info(f"✅ → {config['name']} (#{daily_counts[channel_key]})")
                            break
                            
                        except Exception as e:
                            logger.error(f"❌ Forward error: {e}")
                            continue
                    
                except Exception as e:
                    logger.error(f"❌ Get messages error: {e}")
                
                interval = get_random_interval(channel_key)
                next_forward[channel_key] = datetime.now() + timedelta(minutes=interval)
                logger.info(f"⏰ {config['name']} next in {interval}m")
            
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"❌ Loop error: {e}")
            await asyncio.sleep(60)
    
    logger.info("📡 Forwarding loop ended")


# =============================================================================
# MAIN
# =============================================================================

async def start_telethon():
    """Start Telethon client"""
    global telethon_client
    
    telethon_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await telethon_client.start()
    
    me = await telethon_client.get_me()
    logger.info(f"✅ Telethon: {me.first_name} (@{me.username})")
    
    return telethon_client


def main():
    """Main function"""
    print("=" * 60)
    print("  🤖 MyFC - Safe Forwarder v2.0")
    print("  📷 Photos + Videos | 🔒 Admin Only | ⚡ Ultra-Safe")
    print("=" * 60)
    
    # Load saved data
    load_data()
    
    logger.info(f"👤 Admin ID: {ADMIN_ID}")
    logger.info(f"📥 Source: {SOURCE_CHANNEL or 'Not set'}")
    
    # Create bot application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("setsource", setsource_command))
    app.add_handler(CommandHandler("setdest1", setdest1_command))
    app.add_handler(CommandHandler("setdest2", setdest2_command))
    app.add_handler(CommandHandler("setdest3", setdest3_command))
    app.add_handler(CommandHandler("setcaption3", setcaption3_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("startforward", startforward_command))
    app.add_handler(CommandHandler("stopforward", stopforward_command))
    
    # Start Telethon in background
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_telethon())
    
    logger.info("🚀 Bot is running! Send /start to @MyFCMy_bot")
    print("=" * 60)
    
    # Run bot
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == '__main__':
    main()
EOF
