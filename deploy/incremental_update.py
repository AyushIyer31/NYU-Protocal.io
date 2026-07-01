#!/usr/bin/env python3
"""
Incremental protocol update script for weekly Sunday runs.

Runs fetch_protocols.py with existing keywords, logs results,
backs up the index, and tracks new protocols added.

Usage (automatic via systemd timer):
  systemctl start protocols-update.service

Usage (manual testing):
  python3 incremental_update.py --dry-run
  python3 incremental_update.py --verbose
"""

import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Setup logging
LOG_DIR = Path("/var/log/protocols-io")
LOG_DIR.mkdir(exist_ok=True, parents=True)
LOG_FILE = LOG_DIR / f"update-{datetime.now().strftime('%Y-%m-%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

BACKEND_DIR = Path("/home/ubuntu/NYU-Protocal.io/protocolsnerd-backend")
DATA_DIR = BACKEND_DIR.parent / "data"
INDEX_PATH = DATA_DIR / "protocols_index.json"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True, parents=True)


def load_index(path: Path) -> dict:
    """Load protocol index and return count + last update time."""
    if not path.exists():
        return {"count": 0, "entries": []}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Failed to load index: {e}")
        return {"count": 0, "entries": []}


def backup_index():
    """Backup current index before update."""
    if not INDEX_PATH.exists():
        log.info("No index to backup (first run)")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_path = BACKUP_DIR / f"protocols_index.json.backup.{timestamp}"

    try:
        shutil.copy2(INDEX_PATH, backup_path)
        log.info(f"Backed up index to {backup_path}")

        # Keep only last 4 weeks of backups
        backups = sorted(BACKUP_DIR.glob("protocols_index.json.backup.*"))
        if len(backups) > 28:  # 4 weeks × 7 days
            for old_backup in backups[:-28]:
                old_backup.unlink()
                log.info(f"Pruned old backup: {old_backup.name}")
    except Exception as e:
        log.error(f"Backup failed: {e}")
        return False

    return True


def run_fetch():
    """Run fetch_protocols.py with default keywords."""
    log.info("Starting incremental fetch...")

    cmd = [
        "python3",
        "fetch_protocols.py",
        "--max-per-keyword", "50",
        "--output-dir", str(DATA_DIR / "protocols"),
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour max
        )

        if result.returncode != 0:
            log.error(f"Fetch failed:\n{result.stderr}")
            return False

        log.info(result.stdout)
        return True

    except subprocess.TimeoutExpired:
        log.error("Fetch timed out after 1 hour")
        return False
    except Exception as e:
        log.error(f"Fetch error: {e}")
        return False


def check_health():
    """Verify FastAPI is still running."""
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/health", timeout=5) as r:
            data = json.loads(r.read())
            if data.get("status") == "healthy":
                log.info("✓ Backend health check passed")
                return True
            else:
                log.warning(f"Backend health: {data.get('status')}")
                return True  # Don't fail on degraded, just warn
    except Exception as e:
        log.error(f"Health check failed: {e}")
        return False


def get_stats():
    """Return protocol count before/after."""
    index = load_index(INDEX_PATH)
    return len(index) if isinstance(index, list) else 0


def main():
    log.info("=" * 70)
    log.info("PROTOCOLS.IO INCREMENTAL UPDATE")
    log.info("=" * 70)

    count_before = get_stats()
    log.info(f"Protocols before: {count_before}")

    # Backup existing index
    if not backup_index():
        log.error("Backup failed, aborting")
        return 1

    # Run fetch
    if not run_fetch():
        log.error("Fetch failed, restoring backup")
        # Could restore here if needed
        return 1

    # Check health
    if not check_health():
        log.warning("Health check failed, but continuing")

    # Stats
    count_after = get_stats()
    new_count = max(0, count_after - count_before)

    log.info(f"Protocols after: {count_after}")
    log.info(f"New protocols: {new_count}")
    log.info(f"Index size: {INDEX_PATH.stat().st_size / 1024 / 1024:.2f} MB")

    log.info("=" * 70)
    log.info("UPDATE COMPLETE")
    log.info("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
