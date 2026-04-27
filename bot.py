#!/usr/bin/env python3
"""MyFC Forwarder v6.0 - Proper Bot + User Session"""

import asyncio, random, os, logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage
from telethon.sessions import StringSession
import httpx

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('MyFC')

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_STRING = os.environ.get('SESSION_STRING', '')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

CHANNELS = {}
last_reset_date = None
channel_tasks = {}
tg = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

def headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

def fix_id(cid):
    s = str(cid).replace(" ", "")
    if s.startswith("-100"): return int(s)
    s = s.lstrip("-")
    return int(f"-100{s}") if len(s) >= 10 else int(f"-{s}")

def today(): return datetime.now().strftime('%Y-%m-%d')

def save():
    global CHANNELS, last_reset_date
    if not SUPABASE_URL: return
    try:
        ch = {n: {k: v for k, v in c.items() if k != 'all_source_ids'} for n, c in CHANNELS.items()}
        data = {'id': 'main', 'channels': ch, 'last_reset_date': last_reset_date}
        with httpx.Client(timeout=30) as h:
            r = h.patch(f"{SUPABASE_URL}/rest/v1/forwarder_data?id=eq.main", json=data, headers=headers())
            if r.status_code in [404, 400]: h.post(f"{SUPABASE_URL}/rest/v1/forwarder_data", json=data, headers=headers())
    except Exception as e: logger.error(f"Save: {e}")

def load():
    global CHANNELS, last_reset_date
    if not SUPABASE_URL: return
    try:
        with httpx.Client(timeout=30) as h:
            r = h.get(f"{SUPABASE_URL}/rest/v1/forwarder_data?id=eq.main&select=*", headers=headers())
            if r.status_code == 200 and r.json():
                d = r.json()[0]
                CHANNELS = d.get('channels', {})
                last_reset_date = d.get('last_reset_date')
                logger.info(f"Loaded {len(CHANNELS)} channels")
    except Exception as e: logger.error(f"Load: {e}")

def reset_daily():
    global last_reset_date
    t = today()
    if last_reset_date != t:
        for n in CHANNELS: CHANNELS[n]['daily_count'] = 0
        last_reset_date = t
        save()

def get_interval(n):
    c = CHANNELS.get(n, {})
    return max(5, c.get('interval', 30) + random.randint(-c.get('variation', 10), c.get('variation', 10)))

def content_type(m):
    if not m: return None
    if m.photo or isinstance(m.media, MessageMediaPhoto): return 'photos'
    if isinstance(m.media, MessageMediaDocument) and m.media.document:
        mime = m.media.document.mime_type or ''
        if 'video' in mime: return 'videos'
        if 'audio' in mime: return 'audio'
        if 'image' in mime: return 'photos'
        return 'docs'
    if m.text and ('http://' in m.text or 'https://' in m.text): return 'links'
    if isinstance(m.media, MessageMediaWebPage): return 'links'
    return None

def should_fwd(m, types):
    t = content_type(m)
    return t in types if t else False

def caption(cfg):
    cap, em = cfg.get('caption', ''), cfg.get('emojis', [])
    if not cap: return ""
    if em:
        e = random.choice(em)
        return cap.replace('{emoji}', e) if '{emoji}' in cap else f"{e} {cap}"
    return cap

async def send(dest, m, cfg):
    cap = caption(cfg)
    if m.media: await tg.send_file(dest, m.media, caption=cap or "")
    elif m.text: await tg.send_message(dest, cap or m.text)

async def scan(src, types, cb=None):
    ids, off, batch = [], 0, 0
    while True:
        msgs = await tg.get_messages(src, limit=100, offset_id=off)
        if not msgs: break
        for m in msgs:
            if should_fwd(m, types): ids.append(m.id)
        off = msgs[-1].id
        batch += 1
        if cb and batch % 50 == 0: await cb(len(ids))
        if len(msgs) < 100: break
        if batch % 10 == 0: await asyncio.sleep(1)
    ids.reverse()
    return ids

async def get_first_n(src, types, n):
    ids, off, batch = [], 0, 0
    while len(ids) < n and batch < 500:
        msgs = await tg.get_messages(src, limit=100, offset_id=off)
        if not msgs: break
        for m in msgs:
            if should_fwd(m, types): ids.append(m.id)
        off = msgs[-1].id
        batch += 1
        if len(msgs) < 100: break
        if batch % 10 == 0: await asyncio.sleep(1)
    ids.reverse()
    return ids[:n]

async def find_next(src, types, fwd):
    fset, off, batch = set(fwd), 0, 0
    while batch < 100:
        msgs = await tg.get_messages(src, limit=100, max_id=off) if off else await tg.get_messages(src, limit=100)
        if not msgs: break
        for m in reversed(msgs):
            if m.id not in fset and should_fwd(m, types): return m
        off = msgs[-1].id
        batch += 1
        if len(msgs) < 100: break
        await asyncio.sleep(0.5)
    return None

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return
        return await func(update, context)
    return wrapper

@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "**MyFC Forwarder v6.0**\n\n"
        "`/setup NAME SRC DST INT VAR TYPE`\n"
        "`/scan` `/go` `/stop` `/list` `/info`\n"
        "`/progress` `/stats` `/test`\n"
        "`/caption` `/emojis` `/interval` `/limit`\n"
        "`/skip` `/resetcount` `/reset` `/remove`",
        parse_mode='Markdown')

@admin_only
async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 6:
        await update.message.reply_text("`/setup NAME SRC DST INT VAR TYPE`\nEx: `/setup ch1 123 456 30 8 photos,videos`", parse_mode='Markdown')
        return
    name, src, dst = args[0].lower(), fix_id(args[1]), fix_id(args[2])
    intv, var = int(args[3]), int(args[4])
    types = [t for t in args[5].lower().split(',') if t in ['photos','videos','audio','docs','links']]
    if intv < 5 or not types:
        await update.message.reply_text("Min 5 min, valid types: photos,videos,audio,docs,links")
        return
    CHANNELS[name] = {'source_id': src, 'dest_id': dst, 'interval': intv, 'variation': var,
        'daily_limit': 100, 'content_types': types, 'caption': None, 'emojis': [],
        'enabled': False, 'daily_count': 0, 'total_forwarded': 0, 'source_total': 0,
        'forwarded_ids': [], 'completed': False, 'scanned': False}
    save()
    await update.message.reply_text(f"✅ `{name}` created\n\nNext: `/scan {name}`", parse_mode='Markdown')

@admin_only
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("`/scan NAME`", parse_mode='Markdown')
        return
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    cfg = CHANNELS[name]
    msg = await update.message.reply_text(f"🔍 Scanning `{name}`...", parse_mode='Markdown')
    ids = await scan(cfg['source_id'], cfg['content_types'])
    CHANNELS[name]['source_total'] = len(ids)
    CHANNELS[name]['scanned'] = True
    save()
    fwd = set(cfg.get('forwarded_ids', []))
    rem = sum(1 for i in ids if i not in fwd)
    await msg.edit_text(f"✅ `{name}`\nSource: {len(ids):,}\nRemaining: {rem:,}\n\n`/go {name}`", parse_mode='Markdown')

@admin_only
async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("`/skip NAME NUMBER`", parse_mode='Markdown')
        return
    name, n = context.args[0].lower(), int(context.args[1])
    if name not in CHANNELS:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    cfg = CHANNELS[name]
    if cfg.get('enabled'):
        CHANNELS[name]['enabled'] = False
        if name in channel_tasks: channel_tasks[name].cancel()
    msg = await update.message.reply_text(f"⏳ Skipping {n}...")
    ids = await get_first_n(cfg['source_id'], cfg['content_types'], n)
    existing = set(cfg.get('forwarded_ids', []))
    new = [i for i in ids if i not in existing]
    CHANNELS[name]['forwarded_ids'] = list(existing) + new
    CHANNELS[name]['total_forwarded'] = len(CHANNELS[name]['forwarded_ids'])
    save()
    await msg.edit_text(f"✅ Skipped {len(ids)}. Start from #{len(ids)+1}\n\n`/go {name}`", parse_mode='Markdown')

@admin_only
async def cmd_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_daily()
    if context.args:
        name = context.args[0].lower()
        if name not in CHANNELS:
            await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
            return
        c = CHANNELS[name]
        if c.get('completed'):
            await update.message.reply_text(f"`{name}` done. `/reset {name}`", parse_mode='Markdown')
            return
        if not c.get('scanned'):
            await update.message.reply_text(f"Run `/scan {name}` first", parse_mode='Markdown')
            return
        CHANNELS[name]['enabled'] = True
        save()
        if name not in channel_tasks or channel_tasks[name].done():
            channel_tasks[name] = asyncio.create_task(fwd_loop(name))
        await update.message.reply_text(f"▶️ `{name}` started", parse_mode='Markdown')
    else:
        started = []
        for n, c in CHANNELS.items():
            if c.get('completed') or not c.get('scanned'): continue
            CHANNELS[n]['enabled'] = True
            if n not in channel_tasks or channel_tasks[n].done():
                channel_tasks[n] = asyncio.create_task(fwd_loop(n))
            started.append(n)
        save()
        await update.message.reply_text(f"▶️ Started: {', '.join(started)}" if started else "Nothing to start")

@admin_only
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        name = context.args[0].lower()
        if name in CHANNELS:
            CHANNELS[name]['enabled'] = False
            if name in channel_tasks: channel_tasks[name].cancel()
            save()
            await update.message.reply_text(f"⏹️ `{name}` stopped", parse_mode='Markdown')
    else:
        for n in CHANNELS:
            CHANNELS[n]['enabled'] = False
            if n in channel_tasks: channel_tasks[n].cancel()
        channel_tasks.clear()
        save()
        await update.message.reply_text("⏹️ All stopped")

@admin_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not CHANNELS:
        await update.message.reply_text("No channels")
        return
    txt = "**Channels:**\n"
    for n, c in CHANNELS.items():
        s = "✅" if c.get('completed') else ("🟢" if c.get('enabled') else "🔴")
        txt += f"{s} `{n}`: {c.get('total_forwarded',0):,}/{c.get('source_total',0):,}\n"
    await update.message.reply_text(txt, parse_mode='Markdown')

@admin_only
async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await cmd_list(update, context)
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    c = CHANNELS[name]
    s = "✅ Done" if c.get('completed') else ("🟢 ON" if c.get('enabled') else "🔴 OFF")
    await update.message.reply_text(
        f"**{name}** [{s}]\n"
        f"Src: `{c.get('source_id')}`\n"
        f"Dst: `{c.get('dest_id')}`\n"
        f"Int: {c.get('interval')}±{c.get('variation')}m\n"
        f"Types: {','.join(c.get('content_types',[]))}\n"
        f"Cap: {c.get('caption') or 'None'}\n"
        f"Progress: {c.get('total_forwarded',0):,}/{c.get('source_total',0):,}\n"
        f"Today: {c.get('daily_count',0)}/{c.get('daily_limit',100)}", parse_mode='Markdown')

@admin_only
async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("`/progress NAME`", parse_mode='Markdown')
        return
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    c = CHANNELS[name]
    src, fwd = c.get('source_total', 0), c.get('total_forwarded', 0)
    if src == 0:
        await update.message.reply_text(f"Run `/scan {name}` first", parse_mode='Markdown')
        return
    pct = (fwd/src)*100
    bar = "█"*int(pct/5) + "░"*(20-int(pct/5))
    days = (src-fwd)/c.get('daily_limit',50)
    await update.message.reply_text(
        f"**{name}**\n[{bar}] {pct:.1f}%\n\n"
        f"{fwd:,}/{src:,}\nToday: {c.get('daily_count',0)}/{c.get('daily_limit',100)}\n"
        f"Est: ~{days:.0f} days", parse_mode='Markdown')

@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_daily()
    txt = f"**Stats - {today()}**\n"
    for n, c in CHANNELS.items():
        s = "✅" if c.get('completed') else ("🟢" if c.get('enabled') else "🔴")
        txt += f"{s} `{n}`: {c.get('daily_count',0)}/{c.get('daily_limit',100)} | {c.get('total_forwarded',0):,}\n"
    await update.message.reply_text(txt, parse_mode='Markdown')

@admin_only
async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("`/test NAME`", parse_mode='Markdown')
        return
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    cfg = CHANNELS[name]
    m = await find_next(cfg['source_id'], cfg['content_types'], cfg.get('forwarded_ids', []))
    if m:
        await send(cfg['dest_id'], m, cfg)
        await update.message.reply_text(f"✅ Sent {content_type(m)}")
    else:
        await update.message.reply_text("No unforwarded content")

@admin_only
async def cmd_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("`/caption NAME text` or `/caption NAME clear`", parse_mode='Markdown')
        return
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    if len(context.args) < 2:
        await update.message.reply_text(f"Caption: {CHANNELS[name].get('caption') or 'None'}")
        return
    txt = ' '.join(context.args[1:])
    CHANNELS[name]['caption'] = None if txt.lower() == 'clear' else txt
    save()
    await update.message.reply_text("✅ Caption updated")

@admin_only
async def cmd_emojis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("`/emojis NAME emoji1,emoji2`", parse_mode='Markdown')
        return
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    if len(context.args) < 2:
        em = CHANNELS[name].get('emojis', [])
        await update.message.reply_text(f"Emojis: {', '.join(em) if em else 'None'}")
        return
    txt = context.args[1]
    if txt.lower() == 'clear':
        CHANNELS[name]['emojis'] = []
    else:
        CHANNELS[name]['emojis'] = [e.strip() for e in txt.replace(' ',',').split(',') if e.strip()]
    save()
    await update.message.reply_text(f"✅ {len(CHANNELS[name]['emojis'])} emojis set")

@admin_only
async def cmd_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("`/interval NAME BASE VAR`", parse_mode='Markdown')
        return
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    CHANNELS[name]['interval'] = int(context.args[1])
    CHANNELS[name]['variation'] = int(context.args[2])
    save()
    await update.message.reply_text(f"✅ {context.args[1]}±{context.args[2]} min")

@admin_only
async def cmd_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("`/limit NAME NUMBER`", parse_mode='Markdown')
        return
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    CHANNELS[name]['daily_limit'] = int(context.args[1])
    save()
    await update.message.reply_text(f"✅ {context.args[1]}/day")

@admin_only
async def cmd_resetcount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("`/resetcount NAME` or `/resetcount all`", parse_mode='Markdown')
        return
    name = context.args[0].lower()
    if name == 'all':
        for n in CHANNELS: CHANNELS[n]['daily_count'] = 0
    elif name in CHANNELS:
        CHANNELS[name]['daily_count'] = 0
    else:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    save()
    await update.message.reply_text("✅ Daily count reset")

@admin_only
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("`/reset NAME` - Clears ALL progress", parse_mode='Markdown')
        return
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    CHANNELS[name].update({'completed': False, 'forwarded_ids': [], 'total_forwarded': 0, 'daily_count': 0})
    save()
    await update.message.reply_text(f"🔄 `{name}` reset. Run `/scan {name}`", parse_mode='Markdown')

@admin_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("`/remove NAME`", parse_mode='Markdown')
        return
    name = context.args[0].lower()
    if name not in CHANNELS:
        await update.message.reply_text(f"`{name}` not found", parse_mode='Markdown')
        return
    if name in channel_tasks: channel_tasks[name].cancel()
    del CHANNELS[name]
    save()
    await update.message.reply_text(f"🗑️ `{name}` removed", parse_mode='Markdown')

async def fwd_loop(name):
    logger.info(f"[{name}] Loop start")
    while CHANNELS.get(name, {}).get('enabled'):
        try:
            reset_daily()
            c = CHANNELS.get(name)
            if not c or c.get('completed'): break
            if c.get('daily_count', 0) >= c.get('daily_limit', 100):
                logger.info(f"[{name}] Limit")
                await asyncio.sleep(3600)
                continue
            m = await find_next(c['source_id'], c['content_types'], c.get('forwarded_ids', []))
            if m:
                await send(c['dest_id'], m, c)
                CHANNELS[name]['forwarded_ids'].append(m.id)
                if len(CHANNELS[name]['forwarded_ids']) > 10000:
                    CHANNELS[name]['forwarded_ids'] = CHANNELS[name]['forwarded_ids'][-10000:]
                CHANNELS[name]['daily_count'] = c.get('daily_count', 0) + 1
                CHANNELS[name]['total_forwarded'] = len(CHANNELS[name]['forwarded_ids'])
                save()
                logger.info(f"[{name}] #{CHANNELS[name]['total_forwarded']} Today:{CHANNELS[name]['daily_count']}")
            else:
                CHANNELS[name]['completed'] = True
                CHANNELS[name]['enabled'] = False
                save()
                logger.info(f"[{name}] DONE")
                break
            await asyncio.sleep(get_interval(name) * 60)
        except asyncio.CancelledError: break
        except Exception as e:
            logger.error(f"[{name}] {e}")
            await asyncio.sleep(60)
    logger.info(f"[{name}] Loop end")

async def post_init(app):
    await tg.start()
    logger.info(f"Telethon: {(await tg.get_me()).first_name}")
    load()
    for n, c in CHANNELS.items():
        if c.get('enabled') and not c.get('completed'):
            channel_tasks[n] = asyncio.create_task(fwd_loop(n))
            logger.info(f"Auto: {n}")

def main():
    print("="*40 + "\n  MyFC Forwarder v6.0\n  Proper Bot Edition\n" + "="*40)
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("go", cmd_go))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("caption", cmd_caption))
    app.add_handler(CommandHandler("emojis", cmd_emojis))
    app.add_handler(CommandHandler("interval", cmd_interval))
    app.add_handler(CommandHandler("limit", cmd_limit))
    app.add_handler(CommandHandler("resetcount", cmd_resetcount))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("remove", cmd_remove))
    logger.info("Starting bot...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
