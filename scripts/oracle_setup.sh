#!/bin/bash
# ==============================================================================
# ORACLE CLOUD AMPERE A1 (UBUNTU 22.04 LTS) - LILUSH BOT DEPLOYMENT SCRIPT
# ==============================================================================
# This script automates the installation of Docker, Docker Compose, Bun,
# and configures the Lilush Bot with Postgres 18 & Valkey.
# ==============================================================================

set -e

# ANSI Color Codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 Starting Oracle Cloud environment setup...${NC}"

# 1. Update system packages
echo -e "${GREEN}📦 Updating system packages...${NC}"
sudo apt-get update && sudo apt-get upgrade -y

# 2. Install essential utilities
echo -e "${GREEN}🛠 Installing essential utilities...${NC}"
sudo apt-get install -y curl wget git unzip zip htop ufw

# 3. Install Docker
if ! command -v docker &> /dev/null; then
    echo -e "${GREEN}🐳 Installing Docker...${NC}"
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    rm get-docker.sh
    echo -e "${GREEN}✅ Docker installed successfully.${NC}"
else
    echo -e "${YELLOW}🐳 Docker is already installed.${YELLOW}"
fi

# 4. Install Docker Compose
if ! command -v docker-compose &> /dev/null; then
    echo -e "${GREEN}🐳 Installing Docker Compose...${NC}"
    sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
    echo -e "${GREEN}✅ Docker Compose installed successfully.${NC}"
else
    echo -e "${YELLOW}🐳 Docker Compose is already installed.${NC}"
fi

# 5. Install Bun Runtime (ARM64 compatible, needed for Vibe projects)
if ! command -v bun &> /dev/null; then
    echo -e "${GREEN}🍞 Installing Bun Runtime (for Vibe projects)...${NC}"
    curl -fsSL https://bun.sh/install | bash
    export BUN_INSTALL="$HOME/.bun"
    export PATH="$BUN_INSTALL/bin:$PATH"
    # Add to shell profile
    echo 'export BUN_INSTALL="$HOME/.bun"' >> ~/.bashrc
    echo 'export PATH="$BUN_INSTALL/bin:$PATH"' >> ~/.bashrc
    echo -e "${GREEN}✅ Bun installed successfully.${NC}"
else
    echo -e "${YELLOW}🍞 Bun is already installed.${NC}"
fi

# 6. Configure Firewall (UFW) for Telegram Bot & Webhooks
echo -e "${GREEN}🔒 Configuring Firewall...${NC}"
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP
sudo ufw allow 443/tcp     # HTTPS
sudo ufw allow 8080/tcp    # Bot Port
sudo ufw allow 8000/tcp    # Master Room Web Panel
sudo ufw --force enable
echo -e "${GREEN}✅ Firewall configured. Ports 22, 80, 443, 8080, 8000 are open.${NC}"

# 7. Unzip the project
echo -e "${GREEN}📂 Setting up Lilush Bot files...${NC}"
mkdir -p ~/lilush-bot
if [ -f "lilush-bot-v17.18-vibe-completed.zip" ]; then
    unzip -o lilush-bot-v17.18-vibe-completed.zip -d ~/lilush-bot
    echo -e "${GREEN}✅ Project unzipped to ~/lilush-bot${NC}"
else
    echo -e "${YELLOW}⚠️ lilush-bot-v17.18-vibe-completed.zip not found in current directory.${NC}"
    echo -e "Make sure to upload the zip file to your server first."
fi

# 8. Create default env file
if [ ! -f "~/lilush-bot/.env" ]; then
    echo -e "${GREEN}📝 Creating default .env configuration...${NC}"
    cat << 'EOF' > ~/lilush-bot/.env
# === Lilush Bot Core Configuration ===
BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN_HERE"
ALLOWED_USER_IDS="YOUR_TELEGRAM_USER_ID_HERE"
EOF
    echo -e "${GREEN}✅ Default .env file created at ~/lilush-bot/.env${NC}"
    echo -e "${YELLOW}⚠️ PLEASE EDIT ~/lilush-bot/.env WITH YOUR REAL TOKENS BEFORE RUNNING!${NC}"
fi

# 9. Install Nous Research Hermes Agent (Optional, Self-learning AI Agent)
echo -e "${GREEN}🤖 Installing Nous Research Hermes Agent (Self-learning AI)...${NC}"
if ! command -v hermes &> /dev/null; then
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
    echo -e "${GREEN}✅ Hermes Agent installed successfully! Run with: hermes${NC}"
else
    echo -e "${YELLOW}🤖 Hermes Agent is already installed.${NC}"
fi

echo -e "${GREEN}==============================================================================${NC}"
echo -e "${GREEN}🎉 Setup complete! Your Oracle Cloud VM is ready.${NC}"
echo -e "${GREEN}==============================================================================${NC}"
echo -e "Next steps:"
echo -e "1. Apply shell environment changes: ${YELLOW}source ~/.bashrc${NC}"
echo -e "2. Edit your Telegram credentials: ${YELLOW}nano ~/lilush-bot/.env${NC}"
echo -e "3. Navigate to project: ${YELLOW}cd ~/lilush-bot${NC}"
echo -e "4. Launch the entire stack (Bot + Postgres + Valkey):"
echo -e "   ${YELLOW}docker-compose up -d --build${NC}"
echo -e "${GREEN}==============================================================================${NC}"
