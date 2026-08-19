import json
import logging
import os
import re
import aiohttp
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
import config

logger = logging.getLogger("buffer_integration")

BUFFER_CONFIG_FILE = config.DATA_DIR / "buffer_config.json"


# ── Config Persistence ────────────────────────────────────────────────────────

def _load_buffer_config() -> Dict[str, Any]:
    if not BUFFER_CONFIG_FILE.exists():
        return {}
    try:
        with open(BUFFER_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read buffer config: {e}")
        return {}


def _save_buffer_config(data: Dict[str, Any]):
    try:
        with open(BUFFER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save buffer config: {e}")


def _extract_profile_id(raw: str) -> str:
    """Extract the hex profile/channel ID from a raw string or full Buffer URL."""
    raw = raw.strip().rstrip("/")
    match = re.search(r'[/=]([0-9a-f]{16,32})(?:[/\s?#]|$)', raw, re.IGNORECASE)
    if match:
        return match.group(1)
    if re.fullmatch(r'[0-9a-f]{16,32}', raw, re.IGNORECASE):
        return raw
    return raw


# ── Buffer OAuth App Credentials ──────────────────────────────────────────────

def set_app_credentials(client_id: str, client_secret: str, redirect_uri: str = "https://buffer.com"):
    """Saves Buffer OAuth App credentials globally."""
    data = _load_buffer_config()
    data["app_credentials"] = {
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "redirect_uri": redirect_uri.strip()
    }
    _save_buffer_config(data)


def set_public_base_url(url: str):
    """Saves the public base URL for Buffer video fetching."""
    data = _load_buffer_config()
    data["public_base_url"] = url.strip().rstrip("/")
    _save_buffer_config(data)


def get_public_base_url() -> str:
    """Returns the saved public base URL, environment variable, or default."""
    import os
    env_url = os.getenv("PUBLIC_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN")
    if env_url:
        if not env_url.startswith("http"):
            return f"https://{env_url}".rstrip("/")
        return env_url.rstrip("/")

    data = _load_buffer_config()
    saved_url = data.get("public_base_url", "")
    if saved_url:
        return saved_url.rstrip("/")
    return "http://localhost:8000"


def get_app_credentials() -> Tuple[Optional[str], Optional[str], str]:
    """Returns saved (client_id, client_secret, redirect_uri)."""
    data = _load_buffer_config()
    app_cfg = data.get("app_credentials", {})
    return (
        app_cfg.get("client_id"),
        app_cfg.get("client_secret"),
        app_cfg.get("redirect_uri", "https://buffer.com")
    )


def get_oauth_url(client_id: str, state: str = "", redirect_uri: str = "https://buffer.com") -> str:
    """Generates the official Buffer OAuth authorization URL."""
    from urllib.parse import urlencode
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code"
    }
    if state:
        params["state"] = state
    return f"https://buffer.com/oauth2/authorize?{urlencode(params)}"


async def exchange_code_for_token(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str = "https://buffer.com"
) -> Tuple[bool, str]:
    """
    Exchanges an OAuth code for an access token via Buffer API.
    """
    code_clean = code.strip()
    match = re.search(r'[?&]code=([^&]+)', code_clean)
    if match:
        code_clean = match.group(1)

    url = "https://api.bufferapp.com/1/oauth2/token.json"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code_clean,
        "grant_type": "authorization_code"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=payload, timeout=30) as resp:
                res_text = await resp.text()
                if resp.status == 200:
                    res_json = json.loads(res_text)
                    token = res_json.get("access_token")
                    if token:
                        return True, token
                    return False, f"No access_token in response: {res_text}"
                return False, f"HTTP {resp.status}: {res_text}"
    except Exception as e:
        logger.error(f"Failed to exchange OAuth code: {e}")
        return False, f"Network error: {e}"


# ── Access Token & Profiles ───────────────────────────────────────────────────

def get_user_token(user_id: int) -> Optional[str]:
    """Returns the saved Buffer access token for a user, or None."""
    data = _load_buffer_config()
    return data.get(str(user_id), {}).get("access_token")


def set_user_token(user_id: int, access_token: str):
    """Saves the Buffer access token for a user."""
    data = _load_buffer_config()
    user_key = str(user_id)
    if user_key not in data:
        data[user_key] = {}
    data[user_key]["access_token"] = access_token.strip()
    _save_buffer_config(data)


def get_user_profiles(user_id: Any = 1) -> List[Dict[str, str]]:
    """Returns saved Buffer profiles for a user."""
    data = _load_buffer_config()
    user_key = str(user_id)
    profiles = data.get(user_key, {}).get("profiles", [])
    if not profiles and user_key != "1":
        profiles = data.get("1", {}).get("profiles", [])
    return profiles


def add_user_profile(
    user_id: int,
    name: str,
    schedule_url: str,
    access_token: str = "",
    client_id: str = "",
    client_secret: str = ""
) -> str:
    """Adds an individual Buffer account profile with its own API credentials and schedule link."""
    data = _load_buffer_config()
    user_key = str(user_id)
    if user_key not in data:
        data[user_key] = {}
    if "profiles" not in data[user_key]:
        data[user_key]["profiles"] = []

    clean_pid = _extract_profile_id(schedule_url)
    profile_entry = {
        "name": name.strip(),
        "schedule_url": schedule_url.strip(),
        "profile_id": clean_pid,
        "access_token": access_token.strip(),
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip()
    }

    data[user_key]["profiles"] = [
        p for p in data[user_key]["profiles"] if p["name"].lower() != name.strip().lower()
    ]
    data[user_key]["profiles"].append(profile_entry)
    _save_buffer_config(data)
    return clean_pid


def update_user_profile(
    user_id: int,
    original_identifier: str,
    name: str,
    schedule_url: str,
    access_token: str = "",
    client_id: str = "",
    client_secret: str = ""
) -> str:
    """Updates an existing Buffer account profile or adds it if not found."""
    data = _load_buffer_config()
    user_key = str(user_id)
    if user_key not in data or "profiles" not in data[user_key]:
        return add_user_profile(user_id, name, schedule_url, access_token, client_id, client_secret)

    clean_pid = _extract_profile_id(schedule_url)
    target = original_identifier.strip().lower()

    found = False
    for p in data[user_key]["profiles"]:
        if p.get("profile_id", "").lower() == target or p.get("name", "").lower() == target:
            p["name"] = name.strip()
            p["schedule_url"] = schedule_url.strip()
            p["profile_id"] = clean_pid
            if access_token.strip():
                p["access_token"] = access_token.strip()
            if client_id.strip():
                p["client_id"] = client_id.strip()
            if client_secret.strip():
                p["client_secret"] = client_secret.strip()
            found = True
            break

    if not found:
        return add_user_profile(user_id, name, schedule_url, access_token, client_id, client_secret)

    _save_buffer_config(data)
    return clean_pid


def remove_user_profile(user_id: int, identifier: str) -> bool:
    """Removes a Buffer profile by profile_id or name."""
    data = _load_buffer_config()
    user_key = str(user_id)
    if user_key not in data or "profiles" not in data[user_key]:
        return False
    target = identifier.strip().lower()
    before = len(data[user_key]["profiles"])
    data[user_key]["profiles"] = [
        p for p in data[user_key]["profiles"]
        if p.get("name", "").lower() != target and p.get("profile_id", "").lower() != target
    ]
    if len(data[user_key]["profiles"]) != before:
        _save_buffer_config(data)
        return True
    return False


# ── Backward Compatibility Aliases ─────────────────────────────────────────────

def get_user_buffer_token(user_id: int) -> Optional[str]:
    return get_user_token(user_id)

def set_user_buffer_token(user_id: int, access_token: str):
    set_user_token(user_id, access_token)


# ── Post to Buffer via GraphQL ────────────────────────────────────────────────

async def upload_clip_to_discord_cdn(file_path: str) -> Optional[str]:
    """
    Uploads a local video clip to Discord CDN via Discord Bot API.
    Returns a high-speed, Meta-verified cdn.discordapp.com video URL.
    """
    token = os.getenv("DISCORD_TOKEN", "MTUzNzQxMDA2MDQ3MTUwOTAwMg.G1-xp8.BJ6_zeSkjCwS2XWMutvUFkIMdWnZKKkGd2tgx8")
    if not token:
        return None

    file_p = Path(file_path)
    if not file_p.exists():
        logger.error(f"File for Discord CDN upload does not exist: {file_path}")
        return None

    headers_discord = {"Authorization": f"Bot {token}"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://discord.com/api/v10/users/@me/guilds", headers=headers_discord) as r:
                if r.status != 200:
                    logger.error(f"Failed to fetch bot guilds: {r.status}")
                    return None
                guilds = await r.json()

            if not guilds:
                logger.error("Bot is not in any Discord guild")
                return None

            target_channel_id = None
            guild_id = guilds[0]["id"]
            async with session.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels", headers=headers_discord) as r:
                if r.status == 200:
                    channels = await r.json()
                    for ch in channels:
                        if ch.get("type") == 0:
                            target_channel_id = ch["id"]
                            break

            if not target_channel_id:
                logger.error("No valid text channel found in bot guild")
                return None

            form = aiohttp.FormData()
            form.add_field("file", open(file_p, "rb"), filename=file_p.name)

            async with session.post(
                f"https://discord.com/api/v10/channels/{target_channel_id}/messages",
                headers=headers_discord,
                data=form,
                timeout=90
            ) as r:
                if r.status == 200:
                    msg_res = await r.json()
                    attachments = msg_res.get("attachments", [])
                    if attachments:
                        cdn_url = attachments[0].get("url")
                        logger.info(f"Discord CDN upload successful: {cdn_url}")
                        return cdn_url
                else:
                    logger.error(f"Discord message upload failed with status {r.status}")

    except Exception as e:
        logger.error(f"Error uploading clip to Discord CDN: {e}")

    return None


async def ensure_public_media_url(file_path_or_url: str) -> str:
    """
    Ensures media URL is accessible to external Buffer API servers by uploading 
    local files to Discord CDN (or Catbox/Tmpfiles fallback).
    """
    if file_path_or_url.startswith("http") and "localhost" not in file_path_or_url and "127.0.0.1" not in file_path_or_url:
        return file_path_or_url

    local_path = file_path_or_url
    if "localhost" in file_path_or_url or "127.0.0.1" in file_path_or_url:
        parts = [p for p in file_path_or_url.split("/") if p]
        fname = parts[-2] if parts[-1] == "preview" else parts[-1]
        local_path = str(config.BASE_DIR / "uploads" / "edited" / fname)

    if not Path(local_path).exists():
        logger.error(f"Local clip file not found for public upload: {local_path}")
        return file_path_or_url

    logger.info(f"Uploading clip to Discord CDN for Buffer API: {local_path}")

    # 1. Upload to Discord CDN (100% verified compatibility with Meta/Buffer)
    discord_cdn_url = await upload_clip_to_discord_cdn(local_path)
    if discord_cdn_url:
        return discord_cdn_url

    # 2. Fallback: Catbox.moe
    try:
        url_catbox = "https://catbox.moe/user/api.php"
        data = aiohttp.FormData()
        data.add_field("reqtype", "fileupload")
        data.add_field("fileToUpload", open(local_path, "rb"), filename=Path(local_path).name)

        async with aiohttp.ClientSession() as session:
            async with session.post(url_catbox, data=data, timeout=60) as resp:
                if resp.status == 200:
                    res_url = (await resp.text()).strip()
                    if res_url.startswith("http"):
                        logger.info(f"Catbox upload successful for Buffer: {res_url}")
                        return res_url
    except Exception as e:
        logger.warning(f"Catbox upload failed: {e}")

    # 3. Fallback: Tmpfiles.org
    try:
        url_tmp = "https://tmpfiles.org/api/v1/upload"
        data = aiohttp.FormData()
        data.add_field("file", open(local_path, "rb"), filename=Path(local_path).name)

        async with aiohttp.ClientSession() as session:
            async with session.post(url_tmp, data=data, timeout=60) as resp:
                if resp.status == 200:
                    res_json = await resp.json()
                    raw_url = res_json.get("data", {}).get("url")
                    if raw_url:
                        dl_url = raw_url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
                        logger.info(f"Tmpfiles upload successful for Buffer: {dl_url}")
                        return dl_url
    except Exception as e:
        logger.warning(f"Tmpfiles upload failed: {e}")

    return file_path_or_url


async def post_to_buffer(
    access_token: str,
    profile_id: str,
    caption: str,
    media_url: str,
) -> Tuple[bool, str]:
    """
    Posts a video Reel to Buffer for a single profile_id via GraphQL API immediately.
    Auto-uploads local clips to a public HTTPS host so Buffer can read them.
    """
    token = access_token.strip()
    clean_pid = _extract_profile_id(profile_id)
    if not clean_pid:
        return False, "No valid Buffer Profile/Channel ID provided."
    if not media_url:
        return False, "No media URL provided."

    # Ensure media_url is publicly downloadable by Buffer's API servers
    media_url = await ensure_public_media_url(media_url)

    post_text = caption or "🔥 New Reel! #reels #viral"
    url_gql = "https://api.buffer.com/graphql"

    headers_gql = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    query = """
    mutation CreatePost($channelId: ChannelId!, $text: String!, $mediaUrl: String!) {
      createPost(input: {
        channelId: $channelId,
        text: $text,
        mode: shareNow,
        schedulingType: automatic,
        assets: [{ video: { url: $mediaUrl } }],
        metadata: {
          instagram: {
            type: reel,
            shouldShareToFeed: true
          }
        }
      }) {
        ... on PostActionSuccess {
          post { id }
        }
        ... on MutationError {
          message
        }
      }
    }
    """

    variables = {
        "channelId": clean_pid,
        "text": post_text,
        "mediaUrl": media_url
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url_gql, headers=headers_gql, json={"query": query, "variables": variables}, timeout=45) as resp:
                res_text = await resp.text()
                logger.info(f"Buffer GraphQL Reel post HTTP {resp.status}: {res_text[:300]}")
                if resp.status == 200:
                    res_json = json.loads(res_text)
                    if res_json.get("data") and res_json["data"].get("createPost"):
                        cp_res = res_json["data"]["createPost"]
                        if cp_res.get("post") and cp_res["post"].get("id"):
                            return True, "✅ Reel posted to Buffer!"
                        elif cp_res.get("message"):
                            return False, f"Buffer API message: {cp_res['message']}"
                    elif "errors" in res_json and res_json["errors"]:
                        err_msg = res_json["errors"][0].get("message", "Unknown GraphQL error")
                        return False, f"Buffer API error: {err_msg}"
                return False, f"Buffer API HTTP error ({resp.status}): {res_text[:150]}"
    except Exception as e:
        logger.error(f"GraphQL post request failed: {e}")
        return False, f"Request failed: {e}"
