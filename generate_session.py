cat > ~/TelethonForwarder/generate_session.py << 'EOF'
#!/usr/bin/env python3
"""Generate Telethon Session String - Run locally"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

print("=" * 50)
print("  Session String Generator")
print("=" * 50)

API_ID = input("Enter API_ID: ").strip()
API_HASH = input("Enter API_HASH: ").strip()

print("\nConnecting... You'll get a code in Telegram.\n")

with TelegramClient(StringSession(), int(API_ID), API_HASH) as client:
    print("\n" + "=" * 50)
    print("✅ SESSION_STRING (copy this):")
    print("=" * 50)
    print(client.session.save())
    print("=" * 50)
EOF