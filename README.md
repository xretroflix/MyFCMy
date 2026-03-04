cat > ~/TelethonForwarder/README.md << 'EOF'
# MyFC - Safe Forwarder

Secure Telegram media forwarder with separate control bot.

## Features

- ✅ Separate control bot (admin-only, zero response to others)
- ✅ Photos + Videos forwarding
- ✅ 3 destination channels with random intervals
- ✅ Ultra-safe limits (no Telegram warnings)

## Environment Variables (Railway)

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Control bot token from @BotFather |
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API Hash |
| `SESSION_STRING` | Telethon session string |
| `ADMIN_ID` | Your Telegram user ID |

## Commands (via @MyFCMy_bot)

- `/start` - Show help
- `/setsource ID` - Set source channel
- `/setdest1 ID` - Premium Channel 1 (15min)
- `/setdest2 ID` - Premium Channel 2 (30min)
- `/setdest3 ID` - Free Channel (60min)
- `/setcaption3 TEXT` - Caption for Free
- `/startforward` - Start
- `/stopforward` - Stop
- `/status` - Config
- `/stats` - Statistics
EOF