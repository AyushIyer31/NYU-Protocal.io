# Protocols.io RAG — AWS Deployment Guide

**Domain:** protocolnerds.com  
**Region:** us-west-1  
**Instance:** t3.medium (Ubuntu 22.04 LTS)  
**Updates:** Sunday 2 AM UTC (automatic)

---

## Part 1: AWS Console Setup (Manual — 30 min)

### Step 1: Create EC2 Instance

1. **Login to AWS Console**
   - Go to: https://console.aws.amazon.com
   - Region: **us-west-1** (N. California)

2. **Launch EC2 Instance**
   - EC2 Dashboard → "Launch instances"
   - **Name:** `protocols-io-backend`
   - **AMI:** Ubuntu 22.04 LTS (ami-0c2d3e4c0f9eeda82 in us-west-1)
   - **Instance Type:** `t3.medium` (~$0.0416/hour)
   - **Key Pair:** Create new, name: `protocols-io-key`, format: `.pem`
     - ⚠️ Download immediately and save securely
   - **Network:** Default VPC
   - **Subnet:** Any available
   - **Public IP:** Enable (auto-assign)

3. **Security Group**
   - Create new: `protocols-io-sg`
   - **Inbound Rules:**
     - SSH (22): Source `0.0.0.0/0` (or restrict to your IP)
     - HTTP (80): Source `0.0.0.0/0`
     - HTTPS (443): Source `0.0.0.0/0`
   - **Outbound:** All traffic allowed (default)

4. **Storage**
   - Root volume: 30 GB gp3 (sufficient for indexed corpus + backups)

5. **Launch** and note:
   - Public IPv4 address (e.g., `54.215.135.110`)
   - Public IPv4 DNS (e.g., `ec2-54-215-135-110.us-west-1.compute.amazonaws.com`)

---

### Step 2: Request SSL Certificate (AWS ACM)

1. **Navigate to Certificate Manager**
   - AWS Console → ACM (Certificate Manager)
   - **Region:** us-west-1 (or us-east-1 for CloudFront)

2. **Request Certificate**
   - "Request a certificate" → "Request a public certificate"
   - **Domain names:**
     - `protocolnerds.com`
     - `*.protocolnerds.com` (wildcard for www)
   - **Validation:** DNS validation (recommended)
   - **Request**

3. **Validate in Cloudflare**
   - Copy CNAME records from ACM
   - Go to Cloudflare → DNS Management
   - Add CNAME records (keep Proxy Status: DNS only)
   - Wait 5-10 minutes for validation

---

### Step 3: Cloudflare DNS Configuration

1. **Login to Cloudflare**
   - Domain: `protocolnerds.com`

2. **Add DNS Records**
   - **Type:** A
   - **Name:** `protocolnerds.com`
   - **Content:** EC2 Public IPv4 address (e.g., `54.215.135.110`)
   - **TTL:** Auto
   - **Proxy Status:** DNS only (grey cloud)

3. **Add WWW CNAME** (optional, for www.protocolnerds.com)
   - **Type:** CNAME
   - **Name:** `www`
   - **Content:** `protocolnerds.com`
   - **TTL:** Auto
   - **Proxy Status:** DNS only

4. **SSL/TLS Settings**
   - Overview → **Full (Strict)** mode
   - Caching → **Standard Caching**

---

## Part 2: EC2 Instance Setup (Automated — 5 min)

### Step 1: Connect via SSH

```bash
chmod 400 protocols-io-key.pem
ssh -i protocols-io-key.pem ubuntu@54.215.135.110
```

### Step 2: Run Startup Script

```bash
cd /home/ubuntu
git clone git@github.com:AyushIyer31/NYU-Protocal.io.git
cd NYU-Protocal.io
chmod +x deploy/backend_startup.sh
./deploy/backend_startup.sh
```

**What this does:**
- ✅ Updates system packages
- ✅ Clones your repository
- ✅ Creates Python virtual environment
- ✅ Installs dependencies
- ✅ Configures Nginx reverse proxy
- ✅ Starts FastAPI backend (port 8001)
- ✅ Enables systemd timer for Sunday updates

---

## Part 3: Verify Deployment

### Check Backend Status

```bash
# SSH into instance
ssh -i protocols-io-key.pem ubuntu@54.215.135.110

# Check backend service
sudo systemctl status protocols-backend.service

# Check Nginx
sudo systemctl status nginx

# Check update timer
sudo systemctl list-timers protocols-update.timer

# View logs
tail -f /var/log/protocols-io/update-*.log
journalctl -u protocols-backend.service -f
```

### Test Health Endpoint

```bash
# From local machine
curl https://protocolnerds.com/health

# Should return:
# {"status":"healthy","ollama":{"ok":true,...},...}
```

---

## Part 4: Sunday Updates (Automatic)

### Systemd Timer Configuration

The `protocols-update.timer` runs every Sunday at 2 AM UTC:

```
[Timer]
OnCalendar=Sun *-*-* 02:00:00
Persistent=true
```

### What Happens Each Sunday

1. **Fetch protocols** using 476 biology keywords
2. **Dedup** across keywords (skip already-indexed IDs)
3. **Backup** previous index to `/data/backups/`
4. **Update** main index with new protocols
5. **Log** results to `/var/log/protocols-io/update-YYYY-MM-DD.log`
6. **Health check** FastAPI is still running
7. **Report** count before/after + new additions

### Manual Update (Testing)

```bash
sudo systemctl start protocols-update.service
journalctl -u protocols-update.service -f
```

---

## Part 5: SSL & HTTPS Setup

### Use AWS Certificate Manager with Cloudflare

1. **Certificate already requested** (Step 2 above)
2. **Nginx config** uses HTTP (port 80)
3. **Cloudflare** handles HTTPS redirect (Full Strict mode)
4. **Result:** All traffic encrypted end-to-end

### Traffic Flow

```
User Browser (HTTPS)
    ↓
Cloudflare (proxy, SSL)
    ↓
EC2 Nginx (port 80)
    ↓
FastAPI (port 8001)
```

---

## Part 6: Cost Estimate

| Service | Cost/Month | Notes |
|---------|-----------|-------|
| EC2 t3.medium | ~$30 | Always on, includes 100 GB egress |
| EBS 30 GB | ~$3 | Root volume |
| Data transfer | ~$5 | Outbound to Cloudflare + API calls |
| **Total** | **~$38** | Without AWS free tier |

**With free tier:** $0-20/month for first year

---

## Part 7: Monitoring & Maintenance

### Daily Checks

```bash
# SSH in
ssh -i protocols-io-key.pem ubuntu@54.215.135.110

# Check everything
sudo systemctl status protocols-backend.service
sudo systemctl status nginx
curl http://127.0.0.1:8001/health
```

### Weekly After Update

```bash
# Check update ran successfully
tail -20 /var/log/protocols-io/update-*.log

# Verify index size grew
du -h /home/ubuntu/NYU-Protocal.io/data/protocols_index.json
```

### Backup Index

```bash
# Manual backup (before major changes)
sudo cp /home/ubuntu/NYU-Protocal.io/data/protocols_index.json \
        /home/ubuntu/NYU-Protocal.io/data/backups/manual-backup-$(date +%s).json
```

---

## Part 8: Troubleshooting

### Backend not responding

```bash
sudo systemctl restart protocols-backend.service
sudo journalctl -u protocols-backend.service -n 50
```

### Nginx errors

```bash
sudo nginx -t  # Test config
sudo systemctl restart nginx
```

### Update failed

```bash
# Check log
tail -100 /var/log/protocols-io/update-*.log

# Restore backup
sudo cp /home/ubuntu/NYU-Protocal.io/data/backups/protocols_index.json.backup.* \
        /home/ubuntu/NYU-Protocal.io/data/protocols_index.json

# Restart backend
sudo systemctl restart protocols-backend.service
```

### Disk space low

```bash
# Check usage
df -h /

# Clean old backups (keep 4 weeks)
ls -lrt /home/ubuntu/NYU-Protocal.io/data/backups/ | head -10
rm /home/ubuntu/NYU-Protocal.io/data/backups/old-backup-*.json
```

---

## Quick Reference

| Task | Command |
|------|---------|
| SSH in | `ssh -i protocols-io-key.pem ubuntu@<IP>` |
| Backend logs | `journalctl -u protocols-backend.service -f` |
| Update logs | `tail -f /var/log/protocols-io/update-*.log` |
| Restart backend | `sudo systemctl restart protocols-backend.service` |
| Check timer | `sudo systemctl list-timers protocols-update.timer` |
| Health check | `curl https://protocolnerds.com/health` |
| Disk usage | `df -h` |
| Index size | `du -h ~/NYU-Protocal.io/data/protocols_index.json` |

---

## Support

For issues:
1. Check logs first (commands above)
2. Review Nginx config: `/etc/nginx/sites-enabled/protocols-io`
3. Review systemd service: `/etc/systemd/system/protocols-backend.service`
4. SSH into instance and run manual tests

**Emergency:** Terminate EC2 instance in AWS Console, redeploy fresh.
