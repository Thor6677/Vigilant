#!/bin/bash
# Run this on your fresh Ubuntu 24.04 Digital Ocean VPS as root
set -e

echo "=== Vigilant VPS Setup ==="

# Update system
apt-get update && apt-get upgrade -y

# Install Docker
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Install git
apt-get install -y git

# Create app user
useradd -m -s /bin/bash vigilant || true
usermod -aG docker vigilant

# Create app directory
mkdir -p /opt/vigilant
chown vigilant:vigilant /opt/vigilant

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "1. Clone your repo: git clone https://github.com/YOUR_USERNAME/vigilant /opt/vigilant"
echo "2. Copy .env: cp /opt/vigilant/.env.example /opt/vigilant/.env && nano /opt/vigilant/.env"
echo "3. Get SSL cert (run as root, point DNS first):"
echo "   docker run --rm -p 80:80 certbot/certbot certonly --standalone -d yourdomain.com -d www.yourdomain.com --email YOUR_EMAIL --agree-tos"
echo "4. Start the app: cd /opt/vigilant && docker compose up -d"
