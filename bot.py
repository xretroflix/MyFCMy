cat > ~/TelethonForwarder/bot.py << 'EOF'
#!/usr/bin/env python3
"""
Safe Telethon Forwarder v2.0 - Railway Edition
- Separate Control Bot (Admin Only - Zero response to others)
- Telethon for forwarding (Videos + Photos)
- 3 destination channels with different intervals
- Ultra-safe limits (no Telegram warnings)

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
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
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
# CHANNEL CONFIGURATION
# =============================================================================

SOURCE_CHANNEL = None

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