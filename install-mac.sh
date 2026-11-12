#!/bin/bash
# Instala el bot en este Mac: arranca al iniciar sesión y se relanza si se cae.
set -e
D="$(cd "$(dirname "$0")" && pwd)"
[ -d "$D/.venv" ] || { python3 -m venv "$D/.venv"; "$D/.venv/bin/pip" install -q -r "$D/requirements.txt"; }
"$D/.venv/bin/python" -m playwright install chromium >/dev/null
X="$(printf '%s' "$D" | sed 's/&/\&amp;/g')"
P="$HOME/Library/LaunchAgents/com.rtx5080.bot.plist"
cat > "$P" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.rtx5080.bot</string>
  <key>ProgramArguments</key><array>
    <string>$X/.venv/bin/python</string><string>$X/bot.py</string><string>--loop</string><string>60</string>
  </array>
  <key>WorkingDirectory</key><string>$X</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$X/bot.log</string>
  <key>StandardErrorPath</key><string>$X/bot.log</string>
</dict></plist>
PLIST
launchctl unload "$P" 2>/dev/null || true
launchctl load "$P"
echo "Bot instalado y en marcha. Log: $D/bot.log"
