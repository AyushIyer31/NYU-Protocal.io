#!/bin/bash
# EC2 Instance Startup Script for Protocols.io Backend
# Run this on EC2 instance initialization

set -e

echo "[$(date)] Starting Protocols.io backend setup..."

# Update system
sudo apt-get update
sudo apt-get install -y python3-pip nginx git

# Clone repository
cd /home/ubuntu
git clone git@github.com:AyushIyer31/NYU-Protocal.io.git
cd NYU-Protocal.io

# Setup Python virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r protocolsnerd-backend/requirements.txt

# Setup Nginx reverse proxy
sudo tee /etc/nginx/sites-available/protocols-io > /dev/null <<'NGINX'
server {
    listen 80;
    server_name protocolnerds.com www.protocolnerds.com;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "upgrade";
        proxy_set_header Upgrade $http_upgrade;
    }

    location /health {
        proxy_pass http://127.0.0.1:8001/health;
    }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/protocols-io /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx

# Setup systemd service for FastAPI
sudo tee /etc/systemd/system/protocols-backend.service > /dev/null <<'SYSTEMD'
[Unit]
Description=Protocols.io FastAPI Backend
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/NYU-Protocal.io/protocolsnerd-backend
Environment="PATH=/home/ubuntu/NYU-Protocal.io/venv/bin"
ExecStart=/home/ubuntu/NYU-Protocal.io/venv/bin/python3 -m uvicorn main:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SYSTEMD

# Setup systemd timer for incremental updates
sudo cp deploy/protocols-update.service /etc/systemd/system/
sudo cp deploy/protocols-update.timer /etc/systemd/system/

# Enable and start services
sudo systemctl daemon-reload
sudo systemctl enable protocols-backend.service
sudo systemctl start protocols-backend.service
sudo systemctl enable protocols-update.timer
sudo systemctl start protocols-update.timer

# Setup log directory
sudo mkdir -p /var/log/protocols-io
sudo chown ubuntu:ubuntu /var/log/protocols-io

echo "[$(date)] Setup complete!"
echo "[$(date)] Backend running at: http://127.0.0.1:8001"
echo "[$(date)] Check status: sudo systemctl status protocols-backend.service"
echo "[$(date)] View logs: tail -f /var/log/protocols-io/*.log"
