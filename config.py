import os
import shutil
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("config")

# ── Directories ───────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).resolve().parent
DATA_DIR        = BASE_DIR / "data"
LOGOS_DIR       = DATA_DIR / "logos"
OVERLAYS_DIR    = DATA_DIR / "overlays"
BACKGROUNDS_DIR = DATA_DIR / "backgrounds"
TEMP_DIR        = BASE_DIR / "temp"
FONTS_DIR       = DATA_DIR / "fonts"
FONT_PATH       = FONTS_DIR / "caption.ttf"

for d in (DATA_DIR, LOGOS_DIR, OVERLAYS_DIR, BACKGROUNDS_DIR, TEMP_DIR, FONTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── Bot config ─────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
try:
    MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
except ValueError:
    MAX_CONCURRENT_JOBS = 2

# ── Colour palette ─────────────────────────────────────────────────────────────
COLOR_PALETTE = {
    "black":      "0x111827",   # Modern charcoal-black (default)
    "black_full": "0x000000",   # Pure black
    "navy":       "0x0F2F5E",   # Navy blue
    "blue":       "0x2563EB",   # Royal blue
    "purple":     "0x8B5CF6",   # Violet
    "pink":       "0xEC4899",   # Hot pink
    "red":        "0xDC2626",   # Red
    "green":      "0x10B981",   # Emerald
    "gray":       "0x4B5563",   # Dark gray
    "white":      "0xF9FAFB",   # Soft white
}

COLOR_LABELS = {
    "black":      "Black ⚫",
    "black_full": "Pure Black 🖤",
    "navy":       "Navy Blue 🌊",
    "blue":       "Blue 🔵",
    "purple":     "Purple 🔮",
    "pink":       "Pink 🌸",
    "red":        "Red 🔴",
    "green":      "Green 🟢",
    "gray":       "Gray 🩶",
    "white":      "White ⚪",
    "custom_bg":  "Custom Background 🖼️",
}

# ── Creator registry ───────────────────────────────────────────────────────────
CREATORS = {
    "bluesclues": "BluesClues",
    "chrisean":   "Chrisean Rock",
}

# ── Path helpers ───────────────────────────────────────────────────────────────
def get_logo_path(user_id: int) -> Path:
    return LOGOS_DIR / f"{user_id}.png"

def get_background_path(user_id: int) -> Path:
    return BACKGROUNDS_DIR / f"{user_id}.png"

def get_overlay_path(creator_key: str) -> Path:
    return OVERLAYS_DIR / f"{creator_key}.png"

def ensure_font_sync():
    """Copy a system font to a relative path so FFmpeg can use it without colon-escaping issues."""
    if FONT_PATH.exists():
        return
    candidates_win = [
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/verdana.ttf"),
    ]
    for src in candidates_win:
        if src.exists():
            try:
                shutil.copy2(src, FONT_PATH)
                logger.info(f"Font copied: {src.name} → {FONT_PATH}")
                return
            except Exception as e:
                logger.error(f"Font copy failed ({src}): {e}")
    linux_candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ]
    for src in linux_candidates:
        if src.exists():
            try:
                shutil.copy2(src, FONT_PATH)
                logger.info(f"Font copied: {src.name} → {FONT_PATH}")
                return
            except Exception as e:
                logger.error(f"Font copy failed ({src}): {e}")
    logger.warning("No system font found — captions will be unavailable.")
