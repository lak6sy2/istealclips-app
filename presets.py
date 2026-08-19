import json
import uuid
import logging
from pathlib import Path
from typing import Dict, Any, Optional

import config

logger = logging.getLogger("presets")

PRESETS_FILE = config.DATA_DIR / "presets.json"
PRESETS_ASSETS_DIR = config.DATA_DIR / "preset_assets"

PRESETS_ASSETS_DIR.mkdir(parents=True, exist_ok=True)


def _load_all_data() -> Dict[str, Any]:
    if not PRESETS_FILE.exists():
        return {}
    try:
        with open(PRESETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read presets file: {e}")
        return {}


def _save_all_data(data: Dict[str, Any]):
    try:
        with open(PRESETS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save presets file: {e}")


def get_user_presets(user_id: Any = 1) -> Dict[str, Dict[str, Any]]:
    """Returns a dictionary of {preset_id: preset_data} for the given user, or global fallback presets."""
    data = _load_all_data()
    user_key = str(user_id)
    p = data.get(user_key, {})
    if not p and user_key != "1":
        p = data.get("1", {})
    return p


def get_preset(user_id: int, preset_id: str) -> Optional[Dict[str, Any]]:
    presets = get_user_presets(user_id)
    return presets.get(preset_id)


def create_preset(
    user_id: int,
    name: str,
    bg_bytes: Optional[bytes] = None,
    bg_ext: str = ".png",
    logo_bytes: Optional[bytes] = None
) -> Dict[str, Any]:
    """Creates a new preset persistently and saves image assets if provided."""
    data = _load_all_data()
    user_key = str(user_id)
    if user_key not in data:
        data[user_key] = {}

    preset_id = str(uuid.uuid4())[:8]
    user_dir = PRESETS_ASSETS_DIR / user_key
    user_dir.mkdir(parents=True, exist_ok=True)

    bg_filename = None
    if bg_bytes:
        bg_filename = f"{preset_id}_bg{bg_ext}"
        with open(user_dir / bg_filename, "wb") as f:
            f.write(bg_bytes)

    logo_filename = None
    if logo_bytes:
        logo_filename = f"{preset_id}_logo.png"
        with open(user_dir / logo_filename, "wb") as f:
            f.write(logo_bytes)

    preset_entry = {
        "id": preset_id,
        "name": name.strip(),
        "bg_filename": bg_filename,
        "logo_filename": logo_filename
    }

    data[user_key][preset_id] = preset_entry
    _save_all_data(data)
    logger.info(f"Created preset '{name}' ({preset_id}) for user {user_id}")
    return preset_entry


def update_preset_bg(user_id: int, preset_id: str, bg_bytes: bytes, bg_ext: str = ".png") -> bool:
    data = _load_all_data()
    user_key = str(user_id)
    if user_key in data and preset_id in data[user_key]:
        user_dir = PRESETS_ASSETS_DIR / user_key
        user_dir.mkdir(parents=True, exist_ok=True)
        bg_filename = f"{preset_id}_bg{bg_ext}"
        with open(user_dir / bg_filename, "wb") as f:
            f.write(bg_bytes)
        data[user_key][preset_id]["bg_filename"] = bg_filename
        _save_all_data(data)
        return True
    return False


def update_preset_logo(user_id: int, preset_id: str, logo_bytes: bytes) -> bool:
    data = _load_all_data()
    user_key = str(user_id)
    if user_key in data and preset_id in data[user_key]:
        user_dir = PRESETS_ASSETS_DIR / user_key
        user_dir.mkdir(parents=True, exist_ok=True)
        logo_filename = f"{preset_id}_logo.png"
        with open(user_dir / logo_filename, "wb") as f:
            f.write(logo_bytes)
        data[user_key][preset_id]["logo_filename"] = logo_filename
        _save_all_data(data)
        return True
    return False


def rename_preset(user_id: int, preset_id: str, new_name: str) -> bool:
    data = _load_all_data()
    user_key = str(user_id)
    if user_key in data and preset_id in data[user_key]:
        data[user_key][preset_id]["name"] = new_name.strip()
        _save_all_data(data)
        return True
    return False


def delete_preset(user_id: int, preset_id: str) -> bool:
    data = _load_all_data()
    user_key = str(user_id)
    if user_key in data and preset_id in data[user_key]:
        entry = data[user_key].pop(preset_id)
        _save_all_data(data)
        user_dir = PRESETS_ASSETS_DIR / user_key
        if entry.get("bg_filename"):
            bg_p = user_dir / entry["bg_filename"]
            if bg_p.exists():
                bg_p.unlink()
        if entry.get("logo_filename"):
            logo_p = user_dir / entry["logo_filename"]
            if logo_p.exists():
                logo_p.unlink()
        logger.info(f"Deleted preset {preset_id} for user {user_id}")
        return True
    return False


def get_preset_bg_path(user_id: int, preset_id: str) -> Optional[Path]:
    preset = get_preset(user_id, preset_id)
    if preset and preset.get("bg_filename"):
        p = PRESETS_ASSETS_DIR / str(user_id) / preset["bg_filename"]
        if p.exists():
            return p
    return None


def get_preset_logo_path(user_id: int, preset_id: str) -> Optional[Path]:
    preset = get_preset(user_id, preset_id)
    if preset and preset.get("logo_filename"):
        p = PRESETS_ASSETS_DIR / str(user_id) / preset["logo_filename"]
        if p.exists():
            return p
    return None
