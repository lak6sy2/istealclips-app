"""
iStealClips — Web Dashboard Backend (FastAPI)
Same auto-edit engine as Discord bot, but with a clean web UI.
"""

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

import config
import presets
import video_processor
import buffer_integration
import caption_sticker
import auto_liker
import auth

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("webapp")

# ── Directories ───────────────────────────────────────────────────────────────
RAW_CLIPS_DIR = config.BASE_DIR / "uploads" / "raw"
EDITED_CLIPS_DIR = config.BASE_DIR / "uploads" / "edited"
THUMBNAILS_DIR = config.BASE_DIR / "uploads" / "thumbnails"

for d in (RAW_CLIPS_DIR, EDITED_CLIPS_DIR, THUMBNAILS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── Processing State ─────────────────────────────────────────────────────────
# Tracks processing jobs: { job_id: { clips: { clip_id: { status, message } } } }
processing_jobs: Dict[str, Dict[str, Any]] = {}

# Tracks posting jobs: { job_id: { clips: { clip_id: { status, message } } } }
posting_jobs: Dict[str, Dict[str, Any]] = {}

# Default user_id for web (single-user mode)
WEB_USER_ID = 1


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_font_sync()
    logger.info("iStealClips Web Dashboard started!")
    yield
    logger.info("iStealClips Web Dashboard shutting down.")


# ── App Init ──────────────────────────────────────────────────────────────────
app = FastAPI(title="iStealClips", lifespan=lifespan)

# Mount static files
static_dir = config.BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Mount preset assets for serving background/logo previews
app.mount("/preset_assets", StaticFiles(directory=str(presets.PRESETS_ASSETS_DIR)), name="preset_assets")

# Templates
templates_dir = config.BASE_DIR / "templates"
templates_dir.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(templates_dir))


# ── Auth Middleware ───────────────────────────────────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path.startswith("/preset_assets") or path in ("/login", "/favicon.ico"):
        return await call_next(request)

    session_token = request.cookies.get("session_token")
    user = auth.get_session_user(session_token)

    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return RedirectResponse(url="/login", status_code=302)

    return await call_next(request)


# ══════════════════════════════════════════════════════════════════════════════
#                              PAGE ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/login", response_class=HTMLResponse)
async def page_login(request: Request):
    session_token = request.cookies.get("session_token")
    if auth.get_session_user(session_token):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html")


@app.post("/login")
async def process_login(request: Request, username: str = Form(...), password: str = Form(...), action: Optional[str] = Form(None)):
    if action == "register":
        ok = auth.register_user(username, password)
        if not ok:
            return templates.TemplateResponse(request=request, name="login.html", context={"error": "Username already exists or invalid!"})
    
    if not auth.verify_user(username, password):
        return templates.TemplateResponse(request=request, name="login.html", context={"error": "Invalid username or password!"})

    token = auth.create_session(username)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="session_token",
        value=token,
        max_age=31536000,      # 1 Year persistent
        expires=31536000,
        path="/",
        httponly=True,
        samesite="lax"
    )
    return response


@app.get("/logout")
async def process_logout(request: Request):
    token = request.cookies.get("session_token")
    if token:
        auth.delete_session(token)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key="session_token", path="/")
    return response

@app.get("/", response_class=HTMLResponse)
async def page_upload(request: Request):
    return templates.TemplateResponse(request=request, name="upload.html")


@app.get("/edit", response_class=HTMLResponse)
async def page_edit(request: Request):
    return templates.TemplateResponse(request=request, name="edit.html")


@app.get("/edited", response_class=HTMLResponse)
async def page_edited(request: Request):
    return templates.TemplateResponse(request=request, name="edited.html")


@app.get("/post", response_class=HTMLResponse)
async def page_post(request: Request):
    return templates.TemplateResponse(request=request, name="post.html")


@app.get("/presets", response_class=HTMLResponse)
async def page_presets(request: Request):
    return templates.TemplateResponse(request=request, name="presets.html")


@app.get("/accounts", response_class=HTMLResponse)
async def page_accounts(request: Request):
    return templates.TemplateResponse(request=request, name="accounts.html")


@app.get("/analytics", response_class=HTMLResponse)
async def page_analytics(request: Request):
    return templates.TemplateResponse(request=request, name="analytics.html")


@app.get("/autoliker", response_class=HTMLResponse)
async def page_autoliker(request: Request):
    return templates.TemplateResponse(request=request, name="autoliker.html")


# ══════════════════════════════════════════════════════════════════════════════
#                         API: RAW CLIP UPLOAD
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """Upload a single raw clip."""
    ext = Path(file.filename).suffix.lower()
    if ext not in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"):
        raise HTTPException(400, f"Unsupported file type: {ext}")

    clip_id = f"{uuid.uuid4().hex[:8]}"
    safe_name = f"{clip_id}{ext}"
    dest = RAW_CLIPS_DIR / safe_name

    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(1024 * 1024):  # 1MB chunks
                f.write(chunk)

        size_mb = dest.stat().st_size / (1024 * 1024)
        logger.info(f"Uploaded raw clip: {file.filename} -> {safe_name} ({size_mb:.1f}MB)")

        # Generate thumbnail
        _generate_thumbnail(str(dest), clip_id)

        return {
            "success": True,
            "clip_id": clip_id,
            "filename": file.filename,
            "saved_as": safe_name,
            "size_mb": round(size_mb, 1)
        }
    except Exception as e:
        logger.error(f"Upload failed for {file.filename}: {e}")
        raise HTTPException(500, f"Upload failed: {e}")


def _generate_thumbnail(video_path: str, clip_id: str):
    """Generate a thumbnail frame from video using ffmpeg."""
    thumb_path = THUMBNAILS_DIR / f"{clip_id}.jpg"
    try:
        import subprocess
        subprocess.run([
            "ffmpeg", "-y", "-ss", "00:00:01", "-i", video_path,
            "-vframes", "1", "-q:v", "5", "-vf", "scale=320:-1",
            str(thumb_path)
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except Exception as e:
        logger.warning(f"Thumbnail generation failed for {clip_id}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#                         API: LIST CLIPS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/clips/raw")
async def api_list_raw_clips():
    """List all raw uploaded clips."""
    clips = []
    for f in sorted(RAW_CLIPS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"):
            clip_id = f.stem
            st = f.stat()
            size_mb = st.st_size / (1024 * 1024)
            thumb_exists = (THUMBNAILS_DIR / f"{clip_id}.jpg").exists()
            clips.append({
                "clip_id": clip_id,
                "filename": f.name,
                "size_mb": round(size_mb, 1),
                "mtime": int(st.st_mtime),
                "has_thumbnail": thumb_exists
            })
    return {"clips": clips}


@app.get("/api/clips/edited")
async def api_list_edited_clips():
    """List all edited clips, automatically purging any 0-byte or corrupted failed artifacts."""
    clips = []
    for f in sorted(EDITED_CLIPS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".avi"):
            try:
                st = f.stat()
                if st.st_size < 100_000:  # Filter & purge corrupted/0-byte placeholders under 100KB
                    f.unlink(missing_ok=True)
                    continue
                clip_id = f.stem
                size_mb = st.st_size / (1024 * 1024)
                clips.append({
                    "clip_id": clip_id,
                    "filename": f.name,
                    "size_mb": round(size_mb, 1),
                    "mtime": int(st.st_mtime),
                })
            except Exception:
                pass
    return {"clips": clips}


# ══════════════════════════════════════════════════════════════════════════════
#                         API: SERVE CLIP FILES
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/clips/raw/{filename}/preview")
async def api_raw_preview(filename: str):
    """Serve a raw clip file for video preview."""
    path = RAW_CLIPS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(str(path), media_type="video/mp4")


@app.get("/api/clips/raw/{clip_id}/thumbnail")
async def api_raw_thumbnail(clip_id: str):
    """Serve thumbnail image for a raw clip."""
    path = THUMBNAILS_DIR / f"{clip_id}.jpg"
    if not path.exists():
        raise HTTPException(404, "Thumbnail not found")
    return FileResponse(str(path), media_type="image/jpeg")


@app.get("/api/clips/edited/{filename}/preview")
async def api_edited_preview(filename: str):
    """Serve an edited clip file for video preview."""
    path = EDITED_CLIPS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(str(path), media_type="video/mp4")


@app.get("/api/clips/edited/{filename}/download")
async def api_edited_download(filename: str):
    """Download an edited clip."""
    path = EDITED_CLIPS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(str(path), media_type="video/mp4", filename=filename)


@app.delete("/api/clips/raw/{clip_id}")
async def api_delete_raw(clip_id: str):
    """Delete a raw clip."""
    deleted = False
    for f in RAW_CLIPS_DIR.iterdir():
        if f.stem == clip_id:
            f.unlink()
            deleted = True
    # Also delete thumbnail
    thumb = THUMBNAILS_DIR / f"{clip_id}.jpg"
    if thumb.exists():
        thumb.unlink()
    if not deleted:
        raise HTTPException(404, "Clip not found")
    return {"success": True}


@app.delete("/api/clips/edited/{clip_id}")
async def api_delete_edited(clip_id: str):
    """Delete an edited clip."""
    deleted = False
    for f in EDITED_CLIPS_DIR.iterdir():
        if f.stem == clip_id:
            f.unlink()
            deleted = True
    if not deleted:
        raise HTTPException(404, "Clip not found")
    return {"success": True}


# ══════════════════════════════════════════════════════════════════════════════
#                       API: PROCESS CLIPS (AUTO-EDIT)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/process")
async def api_process(request: Request):
    """Start processing selected clips with one or multiple presets."""
    body = await request.json()
    clip_ids: List[str] = body.get("clip_ids", [])
    
    preset_ids: List[str] = body.get("preset_ids", [])
    if not preset_ids and body.get("preset_id"):
        preset_ids = [body["preset_id"]]

    caption: str = body.get("caption", "")

    if not clip_ids:
        raise HTTPException(400, "No clips selected")
    if not preset_ids:
        raise HTTPException(400, "No presets selected")

    # Validate presets
    for pid in preset_ids:
        preset_data = presets.get_preset(WEB_USER_ID, pid)
        if not preset_data:
            raise HTTPException(404, f"Preset '{pid}' not found")

    # Create job
    job_id = uuid.uuid4().hex[:8]
    job_clips = {}
    for cid in clip_ids:
        for pid in preset_ids:
            item_key = f"{cid}_{pid}"
            job_clips[item_key] = {
                "clip_id": cid,
                "preset_id": pid,
                "status": "pending",
                "message": ""
            }

    processing_jobs[job_id] = {
        "status": "running",
        "clips": job_clips
    }

    # Start background processing
    asyncio.create_task(_process_clips_task(job_id, clip_ids, preset_ids, caption))

    return {"job_id": job_id, "clip_count": len(clip_ids), "preset_count": len(preset_ids)}


async def _process_clips_task(job_id: str, clip_ids: List[str], preset_ids: List[str], caption: str):
    """Background task to process clips for each preset."""
    job = processing_jobs[job_id]

    for clip_id in clip_ids:
        # Find raw clip file
        raw_file = None
        for f in RAW_CLIPS_DIR.iterdir():
            if f.stem == clip_id:
                raw_file = f
                break

        if not raw_file:
            for pid in preset_ids:
                item_key = f"{clip_id}_{pid}"
                job["clips"][item_key]["status"] = "failed"
                job["clips"][item_key]["message"] = "Raw clip file not found"
            continue

        for pid in preset_ids:
            item_key = f"{clip_id}_{pid}"
            job["clips"][item_key]["status"] = "processing"

            bg_path = presets.get_preset_bg_path(WEB_USER_ID, pid)
            logo_path = presets.get_preset_logo_path(WEB_USER_ID, pid)
            preset_data = presets.get_preset(WEB_USER_ID, pid)
            preset_name_clean = preset_data.get("name", pid).replace(" ", "_") if preset_data else pid

            output_name = f"{clip_id}_{preset_name_clean}_edited.mp4"
            output_path = EDITED_CLIPS_DIR / output_name

            try:
                success, message = await video_processor.process_clip(
                    video_path=str(raw_file),
                    output_path=str(output_path),
                    bg_color_name="custom_bg" if bg_path else "black",
                    logo_path=str(logo_path) if logo_path else None,
                    custom_bg_path=str(bg_path) if bg_path else None,
                    overlay_path=None,
                    caption=caption,
                    mirror=False,
                    crop_blur=False,
                )

                if success:
                    job["clips"][item_key]["status"] = "done"
                    job["clips"][item_key]["message"] = "Edited successfully"
                    job["clips"][item_key]["edited_filename"] = output_name
                    logger.info(f"[{job_id}] Clip {clip_id} with preset {pid} processed successfully")
                else:
                    job["clips"][item_key]["status"] = "failed"
                    job["clips"][item_key]["message"] = message
                    logger.error(f"[{job_id}] Clip {clip_id} with preset {pid} failed: {message}")

            except Exception as e:
                job["clips"][item_key]["status"] = "failed"
                job["clips"][item_key]["message"] = str(e)
                logger.error(f"[{job_id}] Clip {clip_id} with preset {pid} error: {e}")

    all_done = all(c["status"] in ("done", "failed") for c in job["clips"].values())
    if all_done:
        job["status"] = "complete"
    logger.info(f"[{job_id}] Processing job complete")


@app.get("/api/process/status/{job_id}")
async def api_process_status(job_id: str):
    """Get processing job status."""
    if job_id not in processing_jobs:
        raise HTTPException(404, "Job not found")
    return processing_jobs[job_id]


# ══════════════════════════════════════════════════════════════════════════════
#                         API: PRESETS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/presets")
async def api_list_presets():
    """List all presets for the web user."""
    user_presets = presets.get_user_presets(WEB_USER_ID)
    preset_list = []
    for pid, pdata in user_presets.items():
        entry = {
            "id": pid,
            "name": pdata.get("name", "Unnamed"),
            "has_bg": bool(pdata.get("bg_filename")),
            "has_logo": bool(pdata.get("logo_filename")),
            "bg_url": f"/preset_assets/{WEB_USER_ID}/{pdata['bg_filename']}" if pdata.get("bg_filename") else None,
            "logo_url": f"/preset_assets/{WEB_USER_ID}/{pdata['logo_filename']}" if pdata.get("logo_filename") else None,
        }
        preset_list.append(entry)
    return {"presets": preset_list}


@app.post("/api/presets")
async def api_create_preset(
    name: str = Form(...),
    background: Optional[UploadFile] = File(None),
    logo: Optional[UploadFile] = File(None),
):
    """Create a new preset with optional background and logo."""
    bg_bytes = None
    bg_ext = ".png"
    if background and background.filename:
        bg_bytes = await background.read()
        bg_ext = Path(background.filename).suffix.lower() or ".png"

    logo_bytes = None
    if logo and logo.filename:
        logo_bytes = await logo.read()

    preset_data = presets.create_preset(
        user_id=WEB_USER_ID,
        name=name,
        bg_bytes=bg_bytes,
        bg_ext=bg_ext,
        logo_bytes=logo_bytes
    )
    return {"success": True, "preset": preset_data}


@app.put("/api/presets/{preset_id}")
async def api_update_preset(
    preset_id: str,
    name: Optional[str] = Form(None),
    background: Optional[UploadFile] = File(None),
    logo: Optional[UploadFile] = File(None),
):
    """Update a preset's name, background, and/or logo."""
    if name:
        presets.rename_preset(WEB_USER_ID, preset_id, name)

    if background and background.filename:
        bg_bytes = await background.read()
        bg_ext = Path(background.filename).suffix.lower() or ".png"
        presets.update_preset_bg(WEB_USER_ID, preset_id, bg_bytes, bg_ext)

    if logo and logo.filename:
        logo_bytes = await logo.read()
        presets.update_preset_logo(WEB_USER_ID, preset_id, logo_bytes)

    return {"success": True}


@app.delete("/api/presets/{preset_id}")
async def api_delete_preset(preset_id: str):
    """Delete a preset."""
    ok = presets.delete_preset(WEB_USER_ID, preset_id)
    if not ok:
        raise HTTPException(404, "Preset not found")
    return {"success": True}


# ══════════════════════════════════════════════════════════════════════════════
#                       API: BUFFER ACCOUNTS & POSTING
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/accounts")
async def api_get_accounts():
    """Get Buffer credentials and profiles."""
    cid, csec, ruri = buffer_integration.get_app_credentials()
    profiles = buffer_integration.get_user_profiles(WEB_USER_ID)
    token = buffer_integration.get_user_token(WEB_USER_ID)
    pub_url = buffer_integration.get_public_base_url()
    return {
        "has_credentials": bool(cid and csec),
        "has_token": bool(token),
        "client_id": cid or "",
        "public_base_url": pub_url,
        "profiles": profiles
    }


@app.post("/api/accounts/public-url")
async def api_set_public_url(request: Request):
    """Set public domain URL for Buffer video downloading."""
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "Public URL is required")
    buffer_integration.set_public_base_url(url)
    return {"success": True, "public_base_url": buffer_integration.get_public_base_url()}


@app.post("/api/accounts/credentials")
async def api_set_credentials(request: Request):
    """Set Buffer app credentials."""
    body = await request.json()
    client_id = body.get("client_id", "").strip()
    client_secret = body.get("client_secret", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(400, "Both Client ID and Client Secret are required")
    buffer_integration.set_app_credentials(client_id, client_secret)
    return {"success": True}


@app.get("/api/accounts/auth-url")
async def api_get_auth_url():
    """Get Buffer OAuth authorization URL."""
    cid, csec, ruri = buffer_integration.get_app_credentials()
    if not cid:
        raise HTTPException(400, "Set app credentials first")
    url = buffer_integration.get_oauth_url(cid, "", ruri)
    return {"auth_url": url}


@app.post("/api/accounts/exchange-code")
async def api_exchange_code(request: Request):
    """Exchange OAuth code for access token."""
    body = await request.json()
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(400, "Code is required")

    cid, csec, ruri = buffer_integration.get_app_credentials()
    if not cid:
        raise HTTPException(400, "Set app credentials first")

    ok, result = await buffer_integration.exchange_code_for_token(cid, csec, code, ruri)
    if ok:
        buffer_integration.set_user_token(WEB_USER_ID, result)
        return {"success": True, "message": "Token saved!"}
    else:
        raise HTTPException(400, f"Token exchange failed: {result}")


@app.post("/api/accounts/token")
async def api_set_token(request: Request):
    """Directly set Buffer access token."""
    body = await request.json()
    token = body.get("token", "").strip()
    if not token:
        raise HTTPException(400, "Token is required")
    buffer_integration.set_user_token(WEB_USER_ID, token)
    return {"success": True}


@app.get("/api/accounts/profiles")
async def api_get_profiles():
    """List all connected Buffer account profiles."""
    profiles = buffer_integration.get_user_profiles(WEB_USER_ID)
    return {"profiles": profiles}


@app.post("/api/accounts/profiles")
async def api_add_profile(request: Request):
    """Add an individual Buffer account profile with its own API credentials and schedule URL."""
    body = await request.json()
    name = body.get("name", "").strip()
    schedule_url = body.get("schedule_url", "").strip() or body.get("profile_id", "").strip()
    access_token = body.get("access_token", "").strip()
    client_id = body.get("client_id", "").strip()
    client_secret = body.get("client_secret", "").strip()

    if not name or not schedule_url:
        raise HTTPException(400, "Account Name and Schedule Link (or Profile ID) are required")

    clean_pid = buffer_integration.add_user_profile(
        user_id=WEB_USER_ID,
        name=name,
        schedule_url=schedule_url,
        access_token=access_token,
        client_id=client_id,
        client_secret=client_secret
    )
    return {"success": True, "profile_id": clean_pid}


@app.put("/api/accounts/profiles/{identifier}")
async def api_update_profile(identifier: str, request: Request):
    """Edit an existing Buffer account profile."""
    body = await request.json()
    name = body.get("name", "").strip()
    schedule_url = body.get("schedule_url", "").strip() or body.get("profile_id", "").strip()
    access_token = body.get("access_token", "").strip()
    client_id = body.get("client_id", "").strip()
    client_secret = body.get("client_secret", "").strip()

    if not name or not schedule_url:
        raise HTTPException(400, "Account Name and Schedule Link (or Profile ID) are required")

    clean_pid = buffer_integration.update_user_profile(
        user_id=WEB_USER_ID,
        original_identifier=identifier,
        name=name,
        schedule_url=schedule_url,
        access_token=access_token,
        client_id=client_id,
        client_secret=client_secret
    )
    return {"success": True, "profile_id": clean_pid}


@app.delete("/api/accounts/profiles/{identifier}")
async def api_remove_profile(identifier: str):
    """Remove a Buffer profile by ID or name."""
    ok = buffer_integration.remove_user_profile(WEB_USER_ID, identifier)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"success": True}


# ══════════════════════════════════════════════════════════════════════════════
#                       API: BULK POST TO BUFFER
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/post")
async def api_post(request: Request):
    """Bulk post selected edited clips to selected Buffer profiles."""
    body = await request.json()
    clip_ids: List[str] = body.get("clip_ids", [])
    account_targets: List[str] = body.get("account_ids") or body.get("profile_ids") or body.get("profile_names") or []
    caption: str = body.get("caption", "")

    if not clip_ids:
        raise HTTPException(400, "No clips selected")
    if not account_targets:
        raise HTTPException(400, "Please select at least one Buffer account")

    profiles = buffer_integration.get_user_profiles(WEB_USER_ID)
    global_token = buffer_integration.get_user_token(WEB_USER_ID)

    if not profiles and not global_token:
        raise HTTPException(400, "No Buffer accounts added. Go to Accounts page first.")

    # Create job
    job_id = uuid.uuid4().hex[:8]
    posting_jobs[job_id] = {
        "status": "running",
        "items": {}
    }

    # Build items: each clip × each target profile
    for clip_id in clip_ids:
        for target_id in account_targets:
            key = f"{clip_id}_{target_id}"
            posting_jobs[job_id]["items"][key] = {
                "clip_id": clip_id,
                "profile_id": target_id,
                "status": "pending",
                "message": ""
            }

    # Start background posting
    asyncio.create_task(_post_clips_task(job_id, clip_ids, account_targets, caption, global_token))

    return {"job_id": job_id}


async def _post_clips_task(
    job_id: str,
    clip_ids: List[str],
    account_targets: List[str],
    caption: str,
    global_token: Optional[str]
):
    """Background task to post clips to Buffer using per-account or global token."""
    job = posting_jobs[job_id]
    profiles = buffer_integration.get_user_profiles(WEB_USER_ID)

    # Build profile lookup by profile_id and by name
    profile_map = {}
    for p in profiles:
        if p.get("profile_id"):
            profile_map[p["profile_id"]] = p
        if p.get("name"):
            profile_map[p["name"]] = p

    for clip_id in clip_ids:
        # Find edited clip file
        clip_file = None
        for f in EDITED_CLIPS_DIR.iterdir():
            if f.stem == clip_id or f.stem.startswith(f"{clip_id}_"):
                clip_file = f
                break

        if not clip_file:
            for target_id in account_targets:
                key = f"{clip_id}_{target_id}"
                if key in job["items"]:
                    job["items"][key]["status"] = "failed"
                    job["items"][key]["message"] = "Edited clip file not found"
            continue

        for target_id in account_targets:
            key = f"{clip_id}_{target_id}"
            if key not in job["items"]:
                continue

            pdata = profile_map.get(target_id)
            if not pdata:
                job["items"][key]["status"] = "failed"
                job["items"][key]["message"] = f"Account '{target_id}' not found"
                continue

            pid = pdata.get("profile_id")
            pname = pdata.get("name", pid)
            job["items"][key]["account_name"] = pname
            job["items"][key]["clip_file"] = clip_file.name
            acc_token = pdata.get("access_token") or global_token

            if not acc_token:
                job["items"][key]["status"] = "failed"
                job["items"][key]["message"] = f"No Buffer API token found for '{pname}'"
                continue

            job["items"][key]["status"] = "posting"

            try:
                pub_base = buffer_integration.get_public_base_url()
                media_url = f"{pub_base}/api/clips/edited/{clip_file.name}/preview"

                ok, msg = await buffer_integration.post_to_buffer(
                    access_token=acc_token,
                    profile_id=pid,
                    caption=caption or "🔥 #reels #viral",
                    media_url=media_url
                )

                if ok:
                    job["items"][key]["status"] = "posted"
                    job["items"][key]["message"] = "✅ Posted!"
                else:
                    job["items"][key]["status"] = "failed"
                    job["items"][key]["message"] = msg

            except Exception as e:
                job["items"][key]["status"] = "failed"
                job["items"][key]["message"] = str(e)

            await asyncio.sleep(3.0)

    job["status"] = "complete"
    logger.info(f"[{job_id}] Posting job complete")


@app.get("/api/post/status/{job_id}")
async def api_post_status(job_id: str):
    """Get posting job status."""
    if job_id not in posting_jobs:
        raise HTTPException(404, "Job not found")
    return posting_jobs[job_id]


# ══════════════════════════════════════════════════════════════════════════════
#                       API: ANALYTICS & AUTO LIKER
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/analytics")
async def api_analytics(range: str = "24h"):
    """
    Returns analytics metrics filtered by time range: 
    1h, 12h, 24h, 3d, 7d, 15d, 30d
    Includes Views, Likes/Reactions, Followers, Reach, Comments, Shares, Saves.
    """
    import time
    from datetime import datetime, timezone, timedelta

    now = time.time()
    range_map = {
        "1h": 3600,
        "12h": 43200,
        "24h": 86400,
        "3d": 259200,
        "7d": 604800,
        "15d": 1296000,
        "30d": 2592000
    }
    seconds_limit = range_map.get(range.lower(), 86400)
    cutoff = now - seconds_limit

    # ISO cutoff for GraphQL filtering
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    cutoff_iso = cutoff_dt.isoformat()

    # Raw clips stats
    raw_count = 0
    raw_bytes = 0
    for f in RAW_CLIPS_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime >= cutoff:
            raw_count += 1
            raw_bytes += f.stat().st_size

    # Edited clips stats
    edited_count = 0
    edited_bytes = 0
    for f in EDITED_CLIPS_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime >= cutoff:
            edited_count += 1
            edited_bytes += f.stat().st_size

    # Posting jobs stats
    posted_count = 0
    failed_count = 0
    for job in posting_jobs.values():
        for item in job.get("items", {}).values():
            if item.get("status") == "posted":
                posted_count += 1
            elif item.get("status") == "failed":
                failed_count += 1

    # Fetch Social Engagement Metrics (Views, Likes, Followers, Reach, Comments)
    social = {
        "views": 0,
        "likes": 0,
        "comments": 0,
        "shares": 0,
        "saves": 0,
        "follows": 0,
        "reach": 0
    }
    profiles = buffer_integration.get_user_profiles()
    url_gql = "https://api.buffer.com/graphql"

    for prof in profiles:
        token = prof.get("access_token")
        pid = prof.get("profile_id")
        org_id = prof.get("client_id", "6a7d6d86bb615d4a32350f02")
        if not token or not pid:
            continue

        headers_gql = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        query = """
        query GetPosts($orgId: OrganizationId!, $channelId: ChannelId!) {
          posts(input: { organizationId: $orgId, filter: { channelIds: [$channelId] } }) {
            edges {
              node {
                id
                sentAt
                metrics {
                  name
                  value
                }
              }
            }
          }
        }
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url_gql,
                    headers=headers_gql,
                    json={"query": query, "variables": {"orgId": org_id, "channelId": pid}},
                    timeout=10
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        edges = data.get("data", {}).get("posts", {}).get("edges", [])
                        for edge in edges:
                            node = edge.get("node", {})
                            sent_at = node.get("sentAt", "")
                            if sent_at and sent_at >= cutoff_iso:
                                for m in node.get("metrics", []):
                                    name = m.get("name", "").lower()
                                    val = int(m.get("value", 0))
                                    if "view" in name:
                                        social["views"] += val
                                    elif "reaction" in name or "like" in name:
                                        social["likes"] += val
                                    elif "comment" in name:
                                        social["comments"] += val
                                    elif "share" in name:
                                        social["shares"] += val
                                    elif "save" in name:
                                        social["saves"] += val
                                    elif "follow" in name:
                                        social["follows"] += val
                                    elif "reach" in name:
                                        social["reach"] += val
        except Exception as e:
            logger.warning(f"Failed to fetch social metrics for {pid}: {e}")

    # Presets summary
    user_presets = presets.get_user_presets(WEB_USER_ID)
    preset_summary = []
    for pid, pdata in user_presets.items():
        preset_summary.append({
            "name": pdata.get("name", pid),
            "has_bg": bool(pdata.get("bg_filename")),
            "has_logo": bool(pdata.get("logo_filename"))
        })

    return {
        "range": range,
        "raw_count": raw_count,
        "raw_size_mb": round(raw_bytes / (1024 * 1024), 1),
        "edited_count": edited_count,
        "edited_size_mb": round(edited_bytes / (1024 * 1024), 1),
        "posted_count": posted_count,
        "failed_count": failed_count,
        "social": social,
        "presets": preset_summary
    }


@app.get("/api/autoliker/status")
async def api_autoliker_status():
    """Returns Auto Liker engine status for web user."""
    return auto_liker.get_user_liker_config(WEB_USER_ID)


@app.post("/api/autoliker/toggle")
async def api_autoliker_toggle(request: Request):
    """Starts or stops the human-simulated Auto Comment Liker engine."""
    body = await request.json()
    enable = body.get("enable", False)

    if enable:
        auto_liker.liker_engine.start_user_liker(WEB_USER_ID)
    else:
        auto_liker.liker_engine.stop_user_liker(WEB_USER_ID)

    return {"success": True, "enabled": enable}


# ══════════════════════════════════════════════════════════════════════════════
#                              MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
