#!/bin/bash
# Secure terminal chat interface

CHAT_FILE="/home/pablo/.openclaw/workspace/sniper/.chat"

# Initialize chat
touch "$CHAT_FILE"
chmod 600 "$CHAT_FILE"

echo "===================================="
echo "SECURE TERMINAL CHAT"
echo "===================================="
echo ""
echo "HOW TO USE:"
echo "1. Type your message and press Enter"
echo "2. Wait for Pablo's response in Telegram"
echo "3. Check this screen for his replies"
echo "4. Type 'exit' to close"
echo "===================================="
echo ""

# Show last messages
echo "--- Chat History ---"
tail -20 "$CHAT_FILE" 2>/dev/null
echo ""
echo "--- Your turn ---"

# Chat loop
while true; do
    read -p "> " message
    
    if [ "$message" = "exit" ]; then
        echo "Chat closed."
        break
    fi
    
    # Add timestamp and message
    echo "[$(date '+%H:%M')] YOU: $message" >> "$CHAT_FILE"
    echo "Sent. Check Telegram for response."
    echo ""
done
