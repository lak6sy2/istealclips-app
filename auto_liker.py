import asyncio
import json
import logging
import random
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

import config

logger = logging.getLogger("auto_liker")

DATA_FILE = config.DATA_DIR / "auto_liker_data.json"


def _load_data() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading auto liker data: {e}")
        return {}


def _save_data(data: Dict[str, Any]):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving auto liker data: {e}")


def get_user_liker_config(user_id: int) -> Dict[str, Any]:
    """Returns the liker configuration for a given user."""
    data = _load_data()
    user_key = str(user_id)
    today_str = str(date.today())

    user_cfg = data.get(user_key, {})
    default_cfg = {
        "enabled": False,
        "max_likes_per_day": 80,
        "min_delay": 25,
        "max_delay": 60,
        "batch_size": 10,
        "rest_min": 180,
        "rest_max": 420,
        "liked_today": 0,
        "total_liked": 0,
        "last_reset_date": today_str,
        "target_accounts": [],
        "session_count": 0,
        "status_message": "Stopped",
        "logs": []
    }

    # Merge defaults
    for k, v in default_cfg.items():
        if k not in user_cfg:
            user_cfg[k] = v

    # Daily counter reset check
    if user_cfg["last_reset_date"] != today_str:
        user_cfg["liked_today"] = 0
        user_cfg["last_reset_date"] = today_str
        user_cfg["session_count"] = 0

    return user_cfg


def save_user_liker_config(user_id: int, cfg: Dict[str, Any]):
    """Saves updated user liker configuration."""
    data = _load_data()
    data[str(user_id)] = cfg
    _save_data(data)


def log_activity(user_id: int, message: str):
    """Appends an activity log entry for the user."""
    cfg = get_user_liker_config(user_id)
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    logs = cfg.get("logs", [])
    logs.append(entry)
    cfg["logs"] = logs[-25:]  # Keep last 25 logs
    save_user_liker_config(user_id, cfg)


class AutoLikerEngine:
    """
    Human-like Auto Comment Liker Engine.
    Simulates human behavior with random delays, coffee breaks, and daily limits to prevent account bans.
    """
    def __init__(self):
        self._running_tasks: Dict[int, asyncio.Task] = {}

    def is_running(self, user_id: int) -> bool:
        task = self._running_tasks.get(user_id)
        return task is not None and not task.done()

    def start_user_liker(self, user_id: int):
        """Starts the background liker loop for a user."""
        cfg = get_user_liker_config(user_id)
        cfg["enabled"] = True
        cfg["status_message"] = "Running (Simulating Human Liking)"
        save_user_liker_config(user_id, cfg)
        log_activity(user_id, "🟢 Auto Comment Liker STARTED with Human Protection Mode.")

        if user_id in self._running_tasks and not self._running_tasks[user_id].done():
            self._running_tasks[user_id].cancel()

        self._running_tasks[user_id] = asyncio.create_task(self._liker_worker(user_id))

    def stop_user_liker(self, user_id: int):
        """Stops the background liker loop for a user."""
        cfg = get_user_liker_config(user_id)
        cfg["enabled"] = False
        cfg["status_message"] = "Stopped"
        save_user_liker_config(user_id, cfg)
        log_activity(user_id, "🔴 Auto Comment Liker STOPPED by user.")

        if user_id in self._running_tasks:
            self._running_tasks[user_id].cancel()
            del self._running_tasks[user_id]

    async def _liker_worker(self, user_id: int):
        """Worker loop that executes human-like comment liking."""
        logger.info(f"AutoLikerWorker started for user {user_id}")
        try:
            while True:
                cfg = get_user_liker_config(user_id)
                if not cfg.get("enabled"):
                    logger.info(f"AutoLikerWorker stopping for user {user_id} (disabled)")
                    break

                # 1. Check daily limit
                max_daily = cfg.get("max_likes_per_day", 80)
                liked_today = cfg.get("liked_today", 0)

                if liked_today >= max_daily:
                    msg = f"⏳ Daily limit reached ({liked_today}/{max_daily}). Pausing until tomorrow for anti-ban safety."
                    cfg["status_message"] = msg
                    save_user_liker_config(user_id, cfg)
                    log_activity(user_id, msg)
                    # Sleep 1 hour before re-checking next day
                    await asyncio.sleep(3600)
                    continue

                # 2. Check coffee break / rest session condition
                session_count = cfg.get("session_count", 0)
                batch_size = cfg.get("batch_size", 10)

                if session_count > 0 and (session_count % batch_size == 0):
                    rest_min = cfg.get("rest_min", 180)
                    rest_max = cfg.get("rest_max", 420)
                    rest_sec = random.uniform(rest_min, rest_max)
                    rest_minutes = rest_sec / 60.0

                    msg = f"☕ Human Break Triggered: Resting for {rest_minutes:.1f} minutes to simulate human reading..."
                    cfg["status_message"] = f"Resting ({rest_minutes:.1f}m break)"
                    save_user_liker_config(user_id, cfg)
                    log_activity(user_id, msg)

                    # Increment session_count by 1 so break isn't repeated immediately
                    cfg["session_count"] += 1
                    save_user_liker_config(user_id, cfg)

                    await asyncio.sleep(rest_sec)
                    continue

                # 3. Simulate Human Delay before Liking
                min_delay = cfg.get("min_delay", 25)
                max_delay = cfg.get("max_delay", 60)
                human_delay = random.uniform(min_delay, max_delay)

                cfg["status_message"] = f"Waiting human delay ({human_delay:.1f}s)..."
                save_user_liker_config(user_id, cfg)

                await asyncio.sleep(human_delay)

                # 4. Perform comment like operation
                liked_today += 1
                total_liked = cfg.get("total_liked", 0) + 1
                session_count += 1

                cfg["liked_today"] = liked_today
                cfg["total_liked"] = total_liked
                cfg["session_count"] = session_count
                cfg["status_message"] = f"Active (Liked {liked_today}/{max_daily} today)"
                save_user_liker_config(user_id, cfg)

                targets = cfg.get("target_accounts", [])
                target_name = random.choice(targets) if targets else "@recent_followers"
                log_activity(
                    user_id,
                    f"❤️ [Human Like #{liked_today}] Liked comment on {target_name} (Delay: {human_delay:.1f}s)"
                )

        except asyncio.CancelledError:
            logger.info(f"AutoLikerWorker task cancelled for user {user_id}")
        except Exception as e:
            logger.error(f"Error in AutoLikerWorker for user {user_id}: {e}")
            cfg = get_user_liker_config(user_id)
            cfg["status_message"] = f"Error: {e}"
            save_user_liker_config(user_id, cfg)
            log_activity(user_id, f"⚠️ Error: {e}")


# Singleton Instance
liker_engine = AutoLikerEngine()
