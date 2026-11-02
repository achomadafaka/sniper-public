#!/bin/bash
# VS Code Setup Script for Sniper
# Run this on your local machine: curl -sS http://129.212.176.148:5000/setup-vscode | bash

set -e

echo "🚀 Setting up VS Code for Sniper VPS..."

# Create SSH config
mkdir -p ~/.ssh
chmod 700 ~/.ssh

if ! grep -q "Host sniper-vps" ~/.ssh/config 2>/dev/null; then
    cat >> ~/.ssh/config << 'EOF'

Host sniper-vps
    HostName 129.212.176.148
    User pablo
    Port 22
    IdentityFile ~/.ssh/id_rsa
    ServerAliveInterval 60
    ServerAliveCountMax 3
EOF
    chmod 600 ~/.ssh/config
    echo "✅ SSH config added"
else
    echo "ℹ️  SSH config already exists"
fi

# Create local sniper directory
mkdir -p ~/sniper-local/.vscode
cd ~/sniper-local

# Create SFTP config
cat > .vscode/sftp.json << 'EOF'
{
    "name": "Sniper VPS",
    "host": "129.212.176.148",
    "protocol": "sftp",
    "port": 22,
    "username": "pablo",
    "remotePath": "/home/pablo/.openclaw/workspace/sniper",
    "uploadOnSave": true,
    "downloadOnOpen": true,
    "syncMode": "update",
    "ignore": [
        ".vscode",
        ".git",
        "*.pyc",
        "__pycache__",
        ".venv",
        "*.log"
    ]
}
EOF

echo "✅ SFTP config created at ~/sniper-local/.vscode/sftp.json"

# Instructions
cat << 'EOF'

📋 NEXT STEPS:

1. Install VS Code extensions:
   - Remote - SSH (Microsoft)
   - SFTP (Natizyskunk)

2. For Remote-SSH (recommended):
   Cmd+Shift+P → "Remote-SSH: Connect to Host" → "sniper-vps"
   Then open: /home/pablo/.openclaw/workspace/sniper

3. For SFTP (auto-sync):
   Open ~/sniper-local in VS Code
   Edit files → Save → Auto-uploads to VPS

4. Test:
   Edit any file, save, and tell Pablo "check the file"

🔗 VS Code will now sync with your VPS instantly!
EOF
