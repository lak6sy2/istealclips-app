import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any

import aiohttp
import discord
from discord.ext import commands

import config
from config import COLOR_LABELS, CREATORS
import presets
import buffer_integration
import auto_liker
from video_processor import process_clip

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")]
)
logger = logging.getLogger("discord_bot")

# ── Processed Clips Persistence ───────────────────────────────────────────────
PROCESSED_CLIPS_FILE = config.DATA_DIR / "processed_clips.json"

def _load_processed_ids() -> set:
    if not PROCESSED_CLIPS_FILE.exists():
        return set()
    try:
        with open(PROCESSED_CLIPS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("processed", []))
    except Exception:
        return set()

def _save_processed_ids(ids: set):
    try:
        with open(PROCESSED_CLIPS_FILE, "w", encoding="utf-8") as f:
            json.dump({"processed": list(ids)}, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save processed clips: {e}")

def mark_clip_processed(message_id: Optional[int], url: Optional[str]):
    p_ids = _load_processed_ids()
    if message_id:
        p_ids.add(str(message_id))
    if url:
        p_ids.add(url)
    _save_processed_ids(p_ids)

# Global in-memory tracking for captions and failed posts
user_last_caption: Dict[int, str] = {}
user_failed_clips: Dict[int, List[Dict[str, Any]]] = {}

# ── Global queue & Cancellation set ───────────────────────────────────────────
processing_queue: asyncio.Queue = asyncio.Queue()
cancelled_user_ids: set = set()


async def download_file(url: str, dest: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"Download failed: HTTP {resp.status}")
            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    f.write(chunk)


async def process_job(job: dict):
    user        = job["user"]
    video_url   = job["video_url"]
    color_name  = job["color_name"]
    mirror      = job.get("mirror", False)
    crop_blur   = job.get("crop_blur", False)
    creator_key = job.get("creator_key", "")
    caption     = job.get("caption", "")
    test_msg    = job["test_msg"]
    preset_ids  = job.get("preset_ids", [])

    if user.id in cancelled_user_ids:
        logger.info(f"Job cancelled by user {user.name} ({user.id}) via /stop")
        try:
            await test_msg.edit(content="🛑 **Operation stopped by user.**")
        except Exception:
            pass
        return

    job_id      = str(uuid.uuid4())[:8]
    input_path  = config.TEMP_DIR / f"{job_id}_input.mp4"
    created_temp_files = [input_path]

    logger.info(f"[{job_id}] Starting for user {user} ({user.id}) | Presets: {preset_ids}")
    try:
        await test_msg.edit(content="⏳ **Processing:** Downloading clip...")
        await download_file(video_url, str(input_path))

        overlay_path = config.get_overlay_path(creator_key) if creator_key else None
        user_presets = presets.get_user_presets(user.id)

        # Build list of preset/account tasks
        task_specs = []

        for pid in preset_ids:
            if pid == "manual":
                task_specs.append({
                    "name": "Default Account",
                    "id": "manual",
                    "bg_path": config.get_background_path(user.id),
                    "logo_path": config.get_logo_path(user.id),
                    "color_name": color_name
                })
            elif pid in user_presets:
                pdata = user_presets[pid]
                pname = pdata["name"]
                p_bg = presets.get_preset_bg_path(user.id, pid)
                p_logo = presets.get_preset_logo_path(user.id, pid)
                task_specs.append({
                    "name": pname,
                    "id": pid,
                    "bg_path": p_bg,
                    "logo_path": p_logo or config.get_logo_path(user.id),
                    "color_name": "custom_bg" if (p_bg and p_bg.exists()) else color_name
                })

        if not task_specs:
            task_specs.append({
                "name": "Default Account",
                "id": "manual",
                "bg_path": config.get_background_path(user.id),
                "logo_path": config.get_logo_path(user.id),
                "color_name": color_name
            })

        total_tasks = len(task_specs)
        successful_counts = 0

        for idx, spec in enumerate(task_specs, 1):
            preset_name = spec["name"]
            preset_id_tag = spec["id"]
            spec_bg_path = spec["bg_path"]
            spec_logo_path = spec["logo_path"]
            spec_color_name = spec["color_name"]

            await test_msg.edit(
                content=f"⏳ **Processing ({idx}/{total_tasks}):** Account/Preset: **{preset_name}**..."
            )

            out_path = config.TEMP_DIR / f"{job_id}_{preset_id_tag}_out.mp4"
            created_temp_files.append(out_path)

            try:
                success, msg = await process_clip(
                    video_path     = str(input_path),
                    output_path    = str(out_path),
                    bg_color_name  = spec_color_name,
                    logo_path      = str(spec_logo_path) if (spec_logo_path and spec_logo_path.exists()) else None,
                    custom_bg_path = str(spec_bg_path) if (spec_bg_path and spec_bg_path.exists()) else None,
                    overlay_path   = str(overlay_path) if (overlay_path and overlay_path.exists()) else None,
                    caption        = caption or None,
                    mirror         = mirror,
                    crop_blur      = crop_blur,
                )

                if success:
                    file_size = out_path.stat().st_size
                    file_size = out_path.stat().st_size
                    if file_size > 23 * 1024 * 1024:
                        logger.warning(f"Output file size {file_size/(1024*1024):.2f}MB > 23MB limit. Re-compressing before Discord send...")
                        temp_rec = out_path.with_name(f"{out_path.stem}_send_rec.mp4")
                        rec_cmd = [
                            "ffmpeg", "-y", "-i", str(out_path),
                            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                            "-maxrate", "2M", "-bufsize", "4M",
                            "-c:a", "copy", str(temp_rec)
                        ]
                        r_p = await asyncio.create_subprocess_exec(*rec_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        await r_p.communicate()
                        if temp_rec.exists() and temp_rec.stat().st_size > 0:
                            temp_rec.replace(out_path)
                            file_size = out_path.stat().st_size

                    safe_name = "".join(c for c in preset_name if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
                    filename = f"edited_clip_{safe_name}.mp4"

                    # DM result with automatic 413 exception retry fallback
                    dm_msg = None
                    try:
                        dm_msg = await user.send(
                            content=f"✨ **Here is your edited clip for preset '{preset_name}'!**",
                            file=discord.File(str(out_path), filename=filename)
                        )
                    except discord.HTTPException as he:
                        if he.status == 413 or getattr(he, "code", 0) == 40005:
                            logger.warning(f"413 Payload Too Large on DM send. Re-compressing {out_path.name}...")
                            temp_rec = out_path.with_name(f"{out_path.stem}_413_rec.mp4")
                            rec_cmd = [
                                "ffmpeg", "-y", "-i", str(out_path),
                                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                                "-maxrate", "1.5M", "-bufsize", "3M",
                                "-c:a", "copy", str(temp_rec)
                            ]
                            r_p = await asyncio.create_subprocess_exec(*rec_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                            await r_p.communicate()
                            if temp_rec.exists() and temp_rec.stat().st_size > 0:
                                temp_rec.replace(out_path)
                                dm_msg = await user.send(
                                    content=f"✨ **Here is your edited clip for preset '{preset_name}'!**",
                                    file=discord.File(str(out_path), filename=filename)
                                )
                        else:
                            raise he

                    # Post to #edited-clips
                    posted_msg = None
                    guild_id = job.get("guild_id")
                    if guild_id:
                        guild = bot.get_guild(guild_id)
                        if guild:
                            channel = discord.utils.get(guild.text_channels, name="edited-clips")
                            if not channel:
                                try:
                                    channel = await guild.create_text_channel("edited-clips")
                                except Exception as e:
                                    logger.error(f"Could not create #edited-clips: {e}")
                            if channel:
                                try:
                                    creator_label = CREATORS.get(creator_key, "")
                                    extra = f" | Creator: {creator_label}" if creator_label else ""
                                    # Check if user has Buffer profiles to attach Post button
                                    ch_token = buffer_integration.get_user_token(user.id)
                                    ch_profiles = buffer_integration.get_user_profiles(user.id)

                                    if ch_token and ch_profiles and dm_msg and dm_msg.attachments:
                                        ch_media_url = dm_msg.attachments[0].url
                                        ch_post_view = BufferPostView(
                                            media_url=ch_media_url,
                                            user_id=user.id,
                                        )
                                        posted_msg = await channel.send(
                                            content=f"🎬 **New clip for `{preset_name}` by {user.mention}!**\n"
                                                    f"Style: {COLOR_LABELS.get(spec_color_name, spec_color_name)}{extra}",
                                            file=discord.File(str(out_path), filename=filename),
                                            view=ch_post_view
                                        )
                                    else:
                                        posted_msg = await channel.send(
                                            content=f"🎬 **New clip for `{preset_name}` by {user.mention}!**\n"
                                                    f"Style: {COLOR_LABELS.get(spec_color_name, spec_color_name)}{extra}",
                                            file=discord.File(str(out_path), filename=filename)
                                        )
                                except Exception as e:
                                    logger.error(f"Failed to post to #edited-clips: {e}")

                    # ── Attach "Post 📲" button to DM as well ─────────────────
                    clip_media_url = None
                    if dm_msg and dm_msg.attachments:
                        clip_media_url = dm_msg.attachments[0].url

                    if clip_media_url:
                        token = buffer_integration.get_user_token(user.id)
                        profiles = buffer_integration.get_user_profiles(user.id)
                        if token and profiles:
                            post_view = BufferPostView(
                                media_url=clip_media_url,
                                user_id=user.id,
                            )
                            await user.send(
                                content="📲 **Post this clip to your social media?**",
                                view=post_view
                            )

                    successful_counts += 1
                else:
                    logger.error(f"[{job_id}] Preset {preset_name} failed: {msg}")
                    await user.send(content=f"❌ Processing failed for preset **{preset_name}**: `{msg}`")

            except Exception as e:
                logger.exception(f"[{job_id}] Error in preset {preset_name}: {e}")
                await user.send(content=f"❌ Error processing preset **{preset_name}**: `{e}`")

        if successful_counts == total_tasks:
            await test_msg.edit(content=f"✅ **All {total_tasks} presets completed!** Check your DMs.")
        else:
            await test_msg.edit(content=f"⚠️ **Completed {successful_counts}/{total_tasks} presets.** Check your DMs for results.")

    except Exception as e:
        logger.exception(f"[{job_id}] Unhandled error: {e}")
        try:
            await test_msg.edit(content=f"❌ Unexpected error: `{e}`")
        except Exception:
            pass
    finally:
        for p in created_temp_files:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass


async def queue_worker():
    logger.info("Queue worker started.")
    while True:
        try:
            job = await processing_queue.get()
            await process_job(job)
            processing_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Queue worker error: {e}")
            await asyncio.sleep(1)


async def oauth_server_worker():
    """Starts the local Buffer OAuth callback server."""
    try:
        await buffer_integration.start_oauth_server(asyncio.get_event_loop())
    except Exception as e:
        logger.error(f"OAuth server error: {e}")


# ── Persistent "Edit Clip" button ─────────────────────────────────────────────

class ClipEditButtonView(discord.ui.View):
    """Attached to bot's reply under each detected clip. Survives restarts."""
    def __init__(self, video_url: str):
        super().__init__(timeout=None)
        self.video_url = video_url

    @discord.ui.button(
        label="Edit Clip", style=discord.ButtonStyle.primary,
        emoji="🎬", custom_id="clip_edit_button"
    )
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        video_url = self.video_url

        if not video_url:
            orig = interaction.message
            if orig and orig.reference:
                try:
                    ref = await interaction.channel.fetch_message(orig.reference.message_id)
                    for att in ref.attachments:
                        ext = Path(att.filename).suffix.lower()
                        if (att.content_type and att.content_type.startswith("video/")) \
                                or ext in (".mp4", ".mov", ".mkv", ".webm", ".avi"):
                            video_url = att.url
                            break
                    if not video_url:
                        for word in ref.content.split():
                            if word.startswith(("http://", "https://")):
                                if Path(word.split("?")[0]).suffix.lower() in \
                                        (".mp4", ".mov", ".mkv", ".webm", ".avi"):
                                    video_url = word
                                    break
                except Exception:
                    pass

        if not video_url:
            await interaction.followup.send(
                "❌ Could not recover the video URL. Please re-upload the clip.",
                ephemeral=True
            )
            return

        view = ClipEditView(video_url, interaction.user.id)
        await interaction.followup.send(
            content=view.build_content(interaction.user),
            view=view,
            ephemeral=True
        )


# ── Main editing view ─────────────────────────────────────────────────────────

class PresetSelect(discord.ui.Select):
    """Row 0 — multi-select dropdown for account presets."""
    def __init__(self, user_id: int):
        user_presets = presets.get_user_presets(user_id)
        options = [
            discord.SelectOption(label="Manual / Default Account Settings", value="manual", description="Use manual color/logo selections")
        ]
        for pid, pdata in user_presets.items():
            has_bg = "BG: Yes" if presets.get_preset_bg_path(user_id, pid) else "BG: Color"
            has_logo = "Logo: Yes" if presets.get_preset_logo_path(user_id, pid) else "Logo: None"
            options.append(discord.SelectOption(
                label=pdata["name"],
                value=pid,
                description=f"{has_bg} | {has_logo}"
            ))

        super().__init__(
            placeholder="📁 Select Presets (Multi-Select Allowed)",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_preset_ids = self.values
        await interaction.response.edit_message(
            content=self.view.build_content(interaction.user), view=self.view
        )


class ColorSelect(discord.ui.Select):
    """Row 1 — background colour/custom dropdown."""
    def __init__(self, row: int = 1):
        options = [
            discord.SelectOption(label=config.COLOR_LABELS[c], value=c)
            for c in config.COLOR_LABELS
        ]
        super().__init__(
            placeholder="🎨 Select Background Color or Custom BG",
            min_values=1, max_values=1,
            options=options, row=row
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.bg_color = self.values[0]
        await interaction.response.edit_message(
            content=self.view.build_content(interaction.user), view=self.view
        )


class CreatorSelect(discord.ui.Select):
    """Row 2 — creator overlay dropdown."""
    def __init__(self, row: int = 2):
        options = [discord.SelectOption(label="None (no overlay)", value="none")]
        for key, label in CREATORS.items():
            overlay = config.get_overlay_path(key)
            status = "" if overlay.exists() else " ⚠️"
            options.append(discord.SelectOption(label=f"{label}{status}", value=key))
        super().__init__(
            placeholder="🎮 Select Creator Overlay",
            min_values=1, max_values=1,
            options=options, row=row
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.creator_key = self.values[0]
        await interaction.response.edit_message(
            content=self.view.build_content(interaction.user), view=self.view
        )


class CaptionModal(discord.ui.Modal, title="Add Caption Text"):
    """Popup text input for the caption."""
    caption_input = discord.ui.TextInput(
        label="Caption",
        placeholder="Type any text to appear on the clip…",
        max_length=80,
        required=False
    )

    def __init__(self, parent_view: "ClipEditView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.caption = self.caption_input.value.strip()
        for child in self.parent_view.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "btn_caption":
                if self.parent_view.caption:
                    child.label = f"Caption: {self.parent_view.caption[:18]}…" \
                        if len(self.parent_view.caption) > 18 else f"Caption: {self.parent_view.caption}"
                    child.style = discord.ButtonStyle.primary
                else:
                    child.label = "Add Caption ✏️"
                    child.style = discord.ButtonStyle.secondary
        try:
            await interaction.response.edit_message(
                content=self.parent_view.build_content(interaction.user),
                view=self.parent_view
            )
        except discord.InteractionResponded:
            await interaction.message.edit(
                content=self.parent_view.build_content(interaction.user),
                view=self.parent_view
            )


class ClipEditView(discord.ui.View):
    """Main ephemeral editing configuration panel."""
    def __init__(self, video_url: str, user_id: int):
        super().__init__(timeout=300)
        self.video_url   = video_url
        self.bg_color    = "black"
        self.mirror      = False
        self.crop_blur   = False
        self.creator_key = "none"
        self.caption     = ""
        self.auto_buffer = False
        self.selected_preset_ids = []

        user_presets = presets.get_user_presets(user_id)
        if user_presets:
            self.add_item(PresetSelect(user_id))      # row 0
            self.add_item(ColorSelect(row=1))         # row 1
            self.add_item(CreatorSelect(row=2))       # row 2
        else:
            self.add_item(ColorSelect(row=0))         # row 0
            self.add_item(CreatorSelect(row=1))       # row 1

    def build_content(self, user: discord.User) -> str:
        color_label     = config.COLOR_LABELS.get(self.bg_color, self.bg_color)
        mirror_status   = "ON ✅" if self.mirror else "OFF"
        crop_blur_status= "ON ✅ (Crops text & blurred lower part)" if self.crop_blur else "OFF"
        active_creator  = self.creator_key if self.creator_key != "none" else ""
        creator_label   = CREATORS.get(active_creator, "None")
        caption_status  = f"**{self.caption}**" if self.caption else "*None*"
        buffer_status   = "ON 📲 (Auto-Post to Buffer)" if self.auto_buffer else "OFF"

        user_presets = presets.get_user_presets(user.id)
        if self.selected_preset_ids:
            preset_names = []
            for pid in self.selected_preset_ids:
                if pid == "manual":
                    preset_names.append("Manual / Default")
                elif pid in user_presets:
                    preset_names.append(user_presets[pid]["name"])
            presets_summary = ", ".join(f"`{n}`" for n in preset_names)
        else:
            presets_summary = "`Default Account`"

        # Per-account assets verification
        logo_path = config.get_logo_path(user.id)
        logo_status = "✅ Personal logo will be applied." \
            if logo_path.exists() else "ℹ️ No default logo saved (`/setlogo`)."

        bg_path = config.get_background_path(user.id)
        if self.bg_color == "custom_bg":
            bg_status = "✅ Custom background image will be used." \
                if bg_path.exists() else "⚠️ Custom BG selected, but no image uploaded (`/setbg`)."
        else:
            bg_status = "ℹ️ Custom BG uploaded (`/setbg`)." if bg_path.exists() else ""

        overlay = config.get_overlay_path(active_creator) if active_creator else None
        overlay_status = ""
        if active_creator and overlay and not overlay.exists():
            overlay_status = "\n⚠️ Overlay PNG not uploaded yet (`/setoverlay`)."

        bg_extra = f"\n{bg_status}" if bg_status else ""

        return (
            f"🛠️ **Configure clip for user:** `{user.name}` (`{user.id}`)\n\n"
            f"📁 Selected Presets: **{presets_summary}**\n"
            f"🎨 Background (Manual): **{color_label}**{bg_extra}\n"
            f"⇄ Mirror: **{mirror_status}**\n"
            f"✂️ Cut Blur/Text Bottom: **{crop_blur_status}**\n"
            f"🎮 Creator Overlay: **{creator_label}**{overlay_status}\n"
            f"✏️ Caption: {caption_status}\n"
            f"📲 Buffer Auto-Post: **{buffer_status}**\n\n"
            f"{logo_status}\n\n"
            f"Click **Generate** when ready!"
        )

    @discord.ui.button(
        label="Mirror: OFF ⇄", style=discord.ButtonStyle.secondary,
        row=3, custom_id="btn_mirror"
    )
    async def mirror_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mirror = not self.mirror
        button.label = "Mirror: ON ⇄" if self.mirror else "Mirror: OFF ⇄"
        button.style = discord.ButtonStyle.primary if self.mirror else discord.ButtonStyle.secondary
        await interaction.response.edit_message(
            content=self.build_content(interaction.user), view=self
        )

    @discord.ui.button(
        label="Cut Blur: OFF ✂️", style=discord.ButtonStyle.secondary,
        row=3, custom_id="btn_crop_blur"
    )
    async def crop_blur_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.crop_blur = not self.crop_blur
        button.label = "Cut Blur: ON ✂️" if self.crop_blur else "Cut Blur: OFF ✂️"
        button.style = discord.ButtonStyle.primary if self.crop_blur else discord.ButtonStyle.secondary
        await interaction.response.edit_message(
            content=self.build_content(interaction.user), view=self
        )

    @discord.ui.button(
        label="Add Caption ✏️", style=discord.ButtonStyle.secondary,
        row=3, custom_id="btn_caption"
    )
    async def caption_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CaptionModal(self))

    @discord.ui.button(
        label="Auto-Buffer: OFF 📲", style=discord.ButtonStyle.secondary,
        row=3, custom_id="btn_auto_buffer"
    )
    async def auto_buffer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.auto_buffer = not self.auto_buffer
        button.label = "Auto-Buffer: ON 📲" if self.auto_buffer else "Auto-Buffer: OFF 📲"
        button.style = discord.ButtonStyle.primary if self.auto_buffer else discord.ButtonStyle.secondary
        await interaction.response.edit_message(
            content=self.build_content(interaction.user), view=self
        )

    @discord.ui.button(
        label="Generate ⚙️", style=discord.ButtonStyle.success,
        row=4, custom_id="btn_generate"
    )
    async def generate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancelled_user_ids.discard(interaction.user.id)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="⏳ Enqueuing…", view=self)

        try:
            test_msg = await interaction.user.send(
                f"⏳ **Processing for {interaction.user.name}:** Clip is queued! Updates will appear here."
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="❌ **Cannot DM you.** Enable 'Allow direct messages from server members' "
                        "in Server Privacy Settings, then try again.",
                view=None
            )
            return

        job = {
            "user":        interaction.user,
            "video_url":   self.video_url,
            "color_name":  self.bg_color,
            "mirror":      self.mirror,
            "crop_blur":   self.crop_blur,
            "creator_key": effective_creator,
            "caption":     self.caption,
            "auto_buffer": self.auto_buffer,
            "preset_ids":  self.selected_preset_ids,
            "test_msg":    test_msg,
            "guild_id":    interaction.guild_id if interaction.guild else None,
        }
        await processing_queue.put(job)
        await interaction.edit_original_response(
            content="✅ **Queued!** Check your DMs for progress updates.", view=None
        )

    @discord.ui.button(
        label="Cancel ❌", style=discord.ButtonStyle.danger,
        row=4, custom_id="btn_cancel"
    )
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Edit cancelled.", view=None)


# ── Rename Preset Modal ───────────────────────────────────────────────────────

class RenamePresetModal(discord.ui.Modal, title="Rename Preset"):
    name_input = discord.ui.TextInput(
        label="New Preset/Account Name",
        placeholder="e.g. Account 1",
        max_length=50,
        required=True
    )

    def __init__(self, user_id: int, preset_id: str, parent_view: Any):
        super().__init__()
        self.user_id = user_id
        self.preset_id = preset_id
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        new_name = self.name_input.value.strip()
        presets.rename_preset(self.user_id, self.preset_id, new_name)
        if hasattr(self.parent_view, "refresh_components"):
            self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=f"✅ Preset renamed to **{new_name}**!\n\n" + self.parent_view.build_content(),
            view=self.parent_view
        )


# ── Preset Manager View ───────────────────────────────────────────────────────

class ManagePresetsView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.selected_preset_id = None
        self.refresh_components()

    def refresh_components(self):
        self.clear_items()
        user_presets = presets.get_user_presets(self.user_id)
        if not user_presets:
            return

        options = [
            discord.SelectOption(label=pdata["name"], value=pid)
            for pid, pdata in user_presets.items()
        ]
        select = discord.ui.Select(
            placeholder="📁 Select Preset to Manage",
            options=options,
            row=0
        )
        select.callback = self.preset_selected
        self.add_item(select)

        if self.selected_preset_id and self.selected_preset_id in user_presets:
            rename_btn = discord.ui.Button(label="Rename ✏️", style=discord.ButtonStyle.secondary, row=1)
            rename_btn.callback = self.rename_click
            self.add_item(rename_btn)

            bg_btn = discord.ui.Button(label="Set Background 🖼️", style=discord.ButtonStyle.secondary, row=1)
            bg_btn.callback = self.bg_click
            self.add_item(bg_btn)

            logo_btn = discord.ui.Button(label="Set Logo 🌸", style=discord.ButtonStyle.secondary, row=1)
            logo_btn.callback = self.logo_click
            self.add_item(logo_btn)

            del_btn = discord.ui.Button(label="Delete 🗑️", style=discord.ButtonStyle.danger, row=1)
            del_btn.callback = self.delete_click
            self.add_item(del_btn)

    def build_content(self) -> str:
        user_presets = presets.get_user_presets(self.user_id)
        if not user_presets:
            return "ℹ️ You have no saved presets. Use `/setpreset` to create one!"

        if not self.selected_preset_id or self.selected_preset_id not in user_presets:
            lines = [f"• **{pdata['name']}** (`{pid}`)" for pid, pdata in user_presets.items()]
            return "📁 **Your Saved Account Presets:**\n" + "\n".join(lines) + "\n\nSelect a preset below to manage."

        pdata = user_presets[self.selected_preset_id]
        bg_p = presets.get_preset_bg_path(self.user_id, self.selected_preset_id)
        logo_p = presets.get_preset_logo_path(self.user_id, self.selected_preset_id)
        bg_status = "✅ Custom Background Saved" if bg_p else "ℹ️ None"
        logo_status = "✅ Watermark Logo Saved" if logo_p else "ℹ️ None"

        return (
            f"📁 **Managing Preset:** `{pdata['name']}`\n\n"
            f"• **Background:** {bg_status}\n"
            f"• **Logo:** {logo_status}\n\n"
            "Use the options below to update or delete this preset."
        )

    async def preset_selected(self, interaction: discord.Interaction):
        self.selected_preset_id = interaction.data["values"][0]
        self.refresh_components()
        await interaction.response.edit_message(content=self.build_content(), view=self)

    async def rename_click(self, interaction: discord.Interaction):
        modal = RenamePresetModal(self.user_id, self.selected_preset_id, self)
        await interaction.response.send_modal(modal)

    async def bg_click(self, interaction: discord.Interaction):
        pdata = presets.get_preset(self.user_id, self.selected_preset_id)
        pname = pdata["name"] if pdata else self.selected_preset_id
        await interaction.response.send_message(
            f"🖼️ Use `/updatepresetbg preset:{pname}` to upload a background for this preset.",
            ephemeral=True
        )

    async def logo_click(self, interaction: discord.Interaction):
        pdata = presets.get_preset(self.user_id, self.selected_preset_id)
        pname = pdata["name"] if pdata else self.selected_preset_id
        await interaction.response.send_message(
            f"🌸 Use `/updatepresetlogo preset:{pname}` to upload a logo for this preset.",
            ephemeral=True
        )

    async def delete_click(self, interaction: discord.Interaction):
        presets.delete_preset(self.user_id, self.selected_preset_id)
        self.selected_preset_id = None
        self.refresh_components()
        await interaction.response.edit_message(content="✅ Preset deleted!\n\n" + self.build_content(), view=self)


# ── Bot subclass ───────────────────────────────────────────────────────────────

class ClipBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.worker_task = None

    async def setup_hook(self):
        config.ensure_font_sync()
        self.add_view(ClipEditButtonView(""))
        self.worker_task = asyncio.create_task(queue_worker())
        self.oauth_task = asyncio.create_task(oauth_server_worker())
        logger.info("Bot setup hook complete — persistent views & OAuth server registered.")

    async def close(self):
        if self.worker_task:
            self.worker_task.cancel()
        if hasattr(self, 'oauth_task') and self.oauth_task:
            self.oauth_task.cancel()
        await super().close()


bot = ClipBot()


# ── Event handlers ─────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Bot online: {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    video_urls = []
    for att in message.attachments:
        ext = Path(att.filename).suffix.lower()
        if (att.content_type and att.content_type.startswith("video/")) \
                or ext in (".mp4", ".mov", ".mkv", ".webm", ".avi"):
            video_urls.append(att.url)

    if not video_urls:
        for word in message.content.split():
            if word.startswith(("http://", "https://")):
                if Path(word.split("?")[0]).suffix.lower() in \
                        (".mp4", ".mov", ".mkv", ".webm", ".avi"):
                    video_urls.append(word)

    if video_urls:
        for idx, url in enumerate(video_urls, 1):
            view = ClipEditButtonView(url)
            label_text = f" (Clip {idx}/{len(video_urls)})" if len(video_urls) > 1 else ""
            await message.reply(
                content=f"🎬 **Convert clip{label_text} to a 9:16 vertical video!** Click below.",
                view=view
            )
        logger.info(f"Detected {len(video_urls)} clip(s) in message {message.id} from {message.author}")

    await bot.process_commands(message)


# ── Prefix command: sync slash commands ───────────────────────────────────────

@bot.command()
@commands.is_owner()
async def sync(ctx, spec: str = None):
    """!sync → instant guild sync | !sync global → global (up to 1 h delay)"""
    if spec == "global":
        await bot.tree.sync()
        await ctx.send("✅ Synced globally (may take up to 1 hour to appear).")
    else:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"✅ Synced {len(synced)} commands to this server instantly!")
    logger.info(f"Commands synced by owner (spec={spec!r})")


# ── Slash commands for Presets ─────────────────────────────────────────────────

@bot.tree.command(name="setpreset", description="Create or set up a persistent preset for an account/channel.")
@discord.app_commands.describe(
    name="Account username/name (e.g. Account 1)",
    background="Background image file (optional)",
    logo="Watermark logo PNG file (optional)"
)
async def setpreset(
    interaction: discord.Interaction,
    name: str,
    background: Optional[discord.Attachment] = None,
    logo: Optional[discord.Attachment] = None
):
    await interaction.response.defer(ephemeral=True)
    bg_bytes, bg_ext = None, ".png"
    if background:
        ext = Path(background.filename).suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".webp"):
            bg_bytes = await background.read()
            bg_ext = ext
        else:
            await interaction.followup.send("❌ Background must be an image (PNG/JPG/WEBP).", ephemeral=True)
            return

    logo_bytes = None
    if logo:
        if logo.content_type and logo.content_type.startswith("image/png"):
            logo_bytes = await logo.read()
        else:
            await interaction.followup.send("❌ Logo must be a PNG file.", ephemeral=True)
            return

    entry = presets.create_preset(
        user_id=interaction.user.id,
        name=name,
        bg_bytes=bg_bytes,
        bg_ext=bg_ext,
        logo_bytes=logo_bytes
    )

    bg_status = "✅ Saved" if bg_bytes else "ℹ️ None (Uses Color)"
    logo_status = "✅ Saved" if logo_bytes else "ℹ️ None"

    await interaction.followup.send(
        f"✅ **Preset Created!**\n"
        f"• **Account Name:** `{entry['name']}`\n"
        f"• **Background:** {bg_status}\n"
        f"• **Logo:** {logo_status}\n\n"
        "You can now multi-select this preset in the **Edit Clip** panel!",
        ephemeral=True
    )


@bot.tree.command(name="managepresets", description="View, edit, or delete your saved account presets.")
async def managepresets(interaction: discord.Interaction):
    view = ManagePresetsView(interaction.user.id)
    await interaction.response.send_message(
        content=view.build_content(),
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="listpresets", description="List all your saved account presets.")
async def listpresets(interaction: discord.Interaction):
    user_presets = presets.get_user_presets(interaction.user.id)
    if not user_presets:
        await interaction.response.send_message("ℹ️ You have no saved presets. Use `/setpreset` to create one!", ephemeral=True)
        return

    lines = []
    for pid, pdata in user_presets.items():
        bg_p = presets.get_preset_bg_path(interaction.user.id, pid)
        logo_p = presets.get_preset_logo_path(interaction.user.id, pid)
        bg_st = "✅ BG" if bg_p else "ℹ️ No BG"
        logo_st = "✅ Logo" if logo_p else "ℹ️ No Logo"
        lines.append(f"• **{pdata['name']}** (`{pid}`) — {bg_st} | {logo_st}")

    await interaction.response.send_message(
        "📁 **Your Saved Presets:**\n" + "\n".join(lines),
        ephemeral=True
    )


def _find_preset_by_name_or_id(user_id: int, identifier: str) -> Optional[dict]:
    user_presets = presets.get_user_presets(user_id)
    if identifier in user_presets:
        return user_presets[identifier]
    ident_lower = identifier.lower().strip()
    for pid, pdata in user_presets.items():
        if pdata["name"].lower().strip() == ident_lower:
            return pdata
    return None


@bot.tree.command(name="updatepresetbg", description="Update the background image for a specific preset.")
@discord.app_commands.describe(
    preset="Preset name or ID",
    background="New background image file"
)
async def updatepresetbg(
    interaction: discord.Interaction,
    preset: str,
    background: discord.Attachment
):
    pdata = _find_preset_by_name_or_id(interaction.user.id, preset)
    if not pdata:
        await interaction.response.send_message(f"❌ Preset `{preset}` not found. Use `/listpresets` to check.", ephemeral=True)
        return

    ext = Path(background.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        await interaction.response.send_message("❌ Background must be an image (PNG/JPG/WEBP).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    bg_bytes = await background.read()
    presets.update_preset_bg(interaction.user.id, pdata["id"], bg_bytes, ext)
    await interaction.followup.send(f"✅ Background updated for preset **{pdata['name']}**!", ephemeral=True)


@bot.tree.command(name="updatepresetlogo", description="Update the watermark logo for a specific preset.")
@discord.app_commands.describe(
    preset="Preset name or ID",
    logo="New watermark logo PNG file"
)
async def updatepresetlogo(
    interaction: discord.Interaction,
    preset: str,
    logo: discord.Attachment
):
    pdata = _find_preset_by_name_or_id(interaction.user.id, preset)
    if not pdata:
        await interaction.response.send_message(f"❌ Preset `{preset}` not found. Use `/listpresets` to check.", ephemeral=True)
        return

    if not (logo.content_type and logo.content_type.startswith("image/png")):
        await interaction.response.send_message("❌ Logo must be a PNG file.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    logo_bytes = await logo.read()
    presets.update_preset_logo(interaction.user.id, pdata["id"], logo_bytes)
    await interaction.followup.send(f"✅ Watermark logo updated for preset **{pdata['name']}**!", ephemeral=True)


@bot.tree.command(name="renamepreset", description="Rename an existing preset.")
@discord.app_commands.describe(
    preset="Existing preset name or ID",
    new_name="New preset name"
)
async def renamepreset(
    interaction: discord.Interaction,
    preset: str,
    new_name: str
):
    pdata = _find_preset_by_name_or_id(interaction.user.id, preset)
    if not pdata:
        await interaction.response.send_message(f"❌ Preset `{preset}` not found.", ephemeral=True)
        return

    presets.rename_preset(interaction.user.id, pdata["id"], new_name)
    await interaction.response.send_message(f"✅ Preset renamed to **{new_name.strip()}**!", ephemeral=True)


@bot.tree.command(name="deletepreset", description="Delete a saved preset.")
@discord.app_commands.describe(preset="Preset name or ID")
async def deletepreset(
    interaction: discord.Interaction,
    preset: str
):
    pdata = _find_preset_by_name_or_id(interaction.user.id, preset)
    if not pdata:
        await interaction.response.send_message(f"❌ Preset `{preset}` not found.", ephemeral=True)
        return
    presets.delete_preset(interaction.user.id, pdata["id"])
    await interaction.response.send_message(f"✅ Preset **{pdata['name']}** deleted!", ephemeral=True)


# ── Buffer Integration Commands ───────────────────────────────────────────────

@bot.tree.command(name="setbuffercreds", description="🔑 Set your Buffer App Client ID + Secret (from buffer.com/developers/apps).")
@discord.app_commands.describe(
    client_id="Buffer App Client ID",
    client_secret="Buffer App Client Secret"
)
async def setbuffercreds(interaction: discord.Interaction, client_id: str, client_secret: str):
    await interaction.response.defer(ephemeral=True)
    buffer_integration.set_app_credentials(client_id, client_secret)
    await interaction.followup.send(
        "✅ **Buffer App Credentials Saved!**\n\n"
        "Now run `/authbuffer` to authorize your Buffer account — you'll get a link to click!",
        ephemeral=True
    )


@bot.tree.command(name="authbuffer", description="📲 Get your Buffer OAuth authorization link.")
async def authbuffer(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cid, csec, r_uri = buffer_integration.get_app_credentials()
    if not cid or not csec:
        await interaction.followup.send(
            "❌ **App credentials not set!**\n"
            "First run `/setbuffercreds client_id: client_secret:` with your Buffer app credentials.",
            ephemeral=True
        )
        return

    auth_url = buffer_integration.get_oauth_url(cid, "", r_uri)
    await interaction.followup.send(
        f"🔗 **Click this link to authorize Buffer:**\n{auth_url}\n\n"
        f"📌 **Steps:**\n"
        f"1. Click **Allow** on Buffer.\n"
        f"2. Your browser will open `buffer.com/?code=1/a1b2c3...`\n"
        f"3. Copy the `code` (or full URL) from your browser address bar.\n"
        f"4. Run: `/buffercode code: [paste_code_or_url_here]`!",
        ephemeral=True
    )


@bot.tree.command(name="buffercode", description="🔑 Paste the OAuth code from Buffer to complete login!")
@discord.app_commands.describe(code="The code or full URL from browser after clicking Allow")
async def buffercode(interaction: discord.Interaction, code: str):
    await interaction.response.defer(ephemeral=True)
    cid, csec, r_uri = buffer_integration.get_app_credentials()
    if not cid or not csec:
        await interaction.followup.send("❌ Set your app credentials first with `/setbuffercreds`!", ephemeral=True)
        return

    ok, result = await buffer_integration.exchange_code_for_token(cid, csec, code, r_uri)
    if ok:
        buffer_integration.set_user_token(interaction.user.id, result)
        profiles = buffer_integration.get_user_profiles(interaction.user.id)
        pid_hint = (
            "\n\nNow add profiles with `/addbufferprofile`!"
            if not profiles else ""
        )
        await interaction.followup.send(
            f"✅ **Buffer Authorization Success!**\nOAuth Access Token saved successfully!{pid_hint}\n\n"
            f"Edited clips will now show a **Post 📲** button!",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"❌ **Token Exchange Failed:** `{result}`\n\n"
            f"Make sure you set Redirect URI in your Buffer App settings to `https://buffer.com`!",
            ephemeral=True
        )


# ── Original Slash commands ───────────────────────────────────────────────────

@bot.tree.command(name="setlogo", description="Upload your account's personal PNG watermark.")
@discord.app_commands.describe(logo="PNG watermark image.")
async def setlogo(interaction: discord.Interaction, logo: discord.Attachment):
    if not (logo.content_type and logo.content_type.startswith("image/png")):
        await interaction.response.send_message(
            "❌ Watermark must be a PNG file.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await logo.save(str(config.get_logo_path(interaction.user.id)))
        await interaction.followup.send(
            f"✅ Personal watermark saved for account **{interaction.user.name}**!", ephemeral=True
        )
        logger.info(f"User {interaction.user.name} ({interaction.user.id}) set their logo.")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to save: `{e}`", ephemeral=True)


@bot.tree.command(name="setbg", description="Upload a custom background image for your account.")
@discord.app_commands.describe(background="Background image (PNG/JPG).")
async def setbg(interaction: discord.Interaction, background: discord.Attachment):
    ext = Path(background.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        await interaction.response.send_message(
            "❌ Background must be an image file (PNG/JPG/WEBP).", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await background.save(str(config.get_background_path(interaction.user.id)))
        await interaction.followup.send(
            f"✅ Custom background image saved for account **{interaction.user.name}**!\n"
            "Select **'Custom Background 🖼️'** in the color dropdown when editing clips.",
            ephemeral=True
        )
        logger.info(f"User {interaction.user.name} ({interaction.user.id}) set their background.")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to save: `{e}`", ephemeral=True)


@bot.tree.command(name="removelogo", description="Remove your account's saved watermark.")
async def removelogo(interaction: discord.Interaction):
    p = config.get_logo_path(interaction.user.id)
    if p.exists():
        p.unlink()
        await interaction.response.send_message("✅ Watermark removed.", ephemeral=True)
        logger.info(f"User {interaction.user.name} ({interaction.user.id}) removed logo.")
    else:
        await interaction.response.send_message("ℹ️ No watermark saved.", ephemeral=True)


@bot.tree.command(name="removebg", description="Remove your account's saved custom background.")
async def removebg(interaction: discord.Interaction):
    p = config.get_background_path(interaction.user.id)
    if p.exists():
        p.unlink()
        await interaction.response.send_message("✅ Custom background removed.", ephemeral=True)
        logger.info(f"User {interaction.user.name} ({interaction.user.id}) removed background.")
    else:
        await interaction.response.send_message("ℹ️ No custom background saved.", ephemeral=True)


@bot.tree.command(name="showlogo", description="Preview your account's watermark.")
async def showlogo(interaction: discord.Interaction):
    p = config.get_logo_path(interaction.user.id)
    if p.exists():
        await interaction.response.send_message(
            f"🌸 Watermark for **{interaction.user.name}**:",
            file=discord.File(str(p), filename="watermark.png"),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "ℹ️ No watermark saved. Use `/setlogo` to upload one.", ephemeral=True
        )


@bot.tree.command(name="showbg", description="Preview your account's custom background image.")
async def showbg(interaction: discord.Interaction):
    p = config.get_background_path(interaction.user.id)
    if p.exists():
        await interaction.response.send_message(
            f"🖼️ Custom background for **{interaction.user.name}**:",
            file=discord.File(str(p), filename="background.png"),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "ℹ️ No custom background saved. Use `/setbg` to upload one.", ephemeral=True
        )


@bot.tree.command(
    name="setoverlay",
    description="[Admin] Upload a Kick overlay PNG for a creator."
)
@discord.app_commands.describe(
    creator="Creator key (e.g. bluesclues, chrisean)",
    overlay="The Kick overlay PNG image."
)
@discord.app_commands.default_permissions(manage_guild=True)
async def setoverlay(
    interaction: discord.Interaction,
    creator: str,
    overlay: discord.Attachment
):
    creator = creator.lower().strip()
    if creator not in CREATORS:
        known = ", ".join(f"`{k}`" for k in CREATORS)
        await interaction.response.send_message(
            f"❌ Unknown creator key `{creator}`. Known creators: {known}\n"
            "Add new creators in `config.py → CREATORS`.",
            ephemeral=True
        )
        return
    if not (overlay.content_type and overlay.content_type.startswith("image/png")):
        await interaction.response.send_message("❌ Overlay must be a PNG.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await overlay.save(str(config.get_overlay_path(creator)))
        label = CREATORS[creator]
        await interaction.followup.send(
            f"✅ Kick overlay for **{label}** saved! "
            "It will now be applied when users select this creator.",
            ephemeral=True
        )
        logger.info(f"Overlay set for creator '{creator}' by {interaction.user.name}.")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to save overlay: `{e}`", ephemeral=True)


@bot.tree.command(name="setbuffer", description="📲 Set your Buffer Access Token for posting Reels.")
@discord.app_commands.describe(access_token="Your Buffer API Access Token")
async def setbuffer(interaction: discord.Interaction, access_token: str):
    await interaction.response.defer(ephemeral=True)
    buffer_integration.set_user_token(interaction.user.id, access_token)
    profiles = buffer_integration.get_user_profiles(interaction.user.id)
    if profiles:
        profile_list = "\n".join(f"• `{p['name']}` → `{p['profile_id']}`" for p in profiles)
        await interaction.followup.send(
            f"✅ **Buffer Token Saved!**\n\n"
            f"You have `{len(profiles)}` saved profile(s):\n{profile_list}\n\n"
            f"Edited clips will now show a **Post 📲** button!",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            "✅ **Buffer Token Saved!**\n\n"
            "Now add a profile with `/addbufferprofile`!\n"
            "Open Buffer → click your channel → copy the ID from the URL:\n"
            "`publish.buffer.com/channels/`**`6a7d6f...`**`/schedule`",
            ephemeral=True
        )


@bot.tree.command(name="addbufferprofile", description="➕ Add a Buffer social profile for posting.")
@discord.app_commands.describe(
    name="Friendly name e.g. 'Main Instagram' or 'TikTok'",
    profile_id="Buffer Profile/Channel ID or full URL from buffer.com"
)
async def addbufferprofile(interaction: discord.Interaction, name: str, profile_id: str):
    await interaction.response.defer(ephemeral=True)
    token = buffer_integration.get_user_token(interaction.user.id)
    if not token:
        await interaction.followup.send("❌ Set your Buffer token first with `/setbuffer`!", ephemeral=True)
        return
    clean_pid = buffer_integration.add_user_profile(interaction.user.id, name, profile_id)
    profiles = buffer_integration.get_user_profiles(interaction.user.id)
    profile_list = "\n".join(f"• `{p['name']}` → `{p['profile_id']}`" for p in profiles)
    await interaction.followup.send(
        f"✅ **Profile Added!**\n\n"
        f"📌 **{name.strip()}** → `{clean_pid}`\n\n"
        f"**All profiles ({len(profiles)}):**\n{profile_list}\n\n"
        f"Edited clips will now show a **Post 📲** button!",
        ephemeral=True
    )


@bot.tree.command(name="removebufferprofile", description="➖ Remove a saved Buffer profile.")
@discord.app_commands.describe(name="The name of the profile to remove")
async def removebufferprofile(interaction: discord.Interaction, name: str):
    removed = buffer_integration.remove_user_profile(interaction.user.id, name)
    if removed:
        await interaction.response.send_message(f"✅ Removed profile `{name}`.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ No profile named `{name}` found.", ephemeral=True)


@bot.tree.command(name="bufferprofiles", description="📋 List your saved Buffer profiles.")
async def bufferprofiles(interaction: discord.Interaction):
    profiles = buffer_integration.get_user_profiles(interaction.user.id)
    token = buffer_integration.get_user_token(interaction.user.id)
    if not token:
        await interaction.response.send_message("❌ No Buffer token. Use `/setbuffer` first.", ephemeral=True)
        return
    if not profiles:
        await interaction.response.send_message(
            "⚠️ No profiles saved. Use `/addbufferprofile name: profile_id:` to add one!",
            ephemeral=True
        )
        return
    profile_list = "\n".join(f"• `{p['name']}` → `{p['profile_id']}`" for p in profiles)
    await interaction.response.send_message(
        f"📋 **Your Buffer Profiles ({len(profiles)}):**\n\n{profile_list}",
        ephemeral=True
    )


# ── Buffer Post View (attached to every edited clip DM) ───────────────────────

class BufferProfileSelect(discord.ui.Select):
    """Dropdown to pick which Buffer profile to post to."""
    def __init__(self, profiles: List[Dict[str, str]]):
        options = [
            discord.SelectOption(
                label=p["name"][:25],
                value=p["profile_id"],
                description=p["profile_id"][:50]
            )
            for p in profiles[:25]
        ]
        super().__init__(
            placeholder="Select profile to post to…",
            min_values=1, max_values=1,
            options=options,
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_profile_id = self.values[0]
        self.view.selected_profile_name = next(
            (o.label for o in self.options if o.value == self.values[0]), "Unknown"
        )
        await interaction.response.edit_message(
            content=self.view.build_content(), view=self.view
        )


class BufferCaptionModal(discord.ui.Modal, title="📝 Add Caption & Post"):
    """Modal to enter caption before posting."""
    caption_input = discord.ui.TextInput(
        label="Caption (optional)",
        placeholder="🔥 New Reel! #reels #viral #trending",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=2000
    )

    def __init__(self, post_view: "BufferPostView"):
        super().__init__()
        self.post_view = post_view

    async def on_submit(self, interaction: discord.Interaction):
        caption = self.caption_input.value.strip()
        await interaction.response.edit_message(
            content=f"⏳ **Posting to `{self.post_view.selected_profile_name}`...**",
            view=None
        )
        token = buffer_integration.get_user_token(interaction.user.id)
        if not token:
            await interaction.edit_original_response(content="❌ Buffer token not found. Use `/setbuffer`.")
            return
        ok, msg = await buffer_integration.post_to_buffer(
            access_token=token,
            profile_id=self.post_view.selected_profile_id,
            caption=caption or "🔥 New Reel! #reels #viral",
            media_url=self.post_view.media_url,
        )
        if ok:
            preview = caption[:80] + "…" if len(caption) > 80 else (caption or "(default)")
            await interaction.edit_original_response(
                content=f"🚀 **Posted to `{self.post_view.selected_profile_name}`!**\nCaption: {preview}"
            )
        else:
            await interaction.edit_original_response(content=f"❌ **Post Failed:** {msg}")


class BufferPostView(discord.ui.View):
    """View with profile select + post button, attached to each edited clip DM."""
    def __init__(self, media_url: str, user_id: int):
        super().__init__(timeout=600)
        self.media_url = media_url
        self.user_id = user_id
        self.selected_profile_id: Optional[str] = None
        self.selected_profile_name: Optional[str] = None

        profiles = buffer_integration.get_user_profiles(user_id)
        if profiles:
            self.add_item(BufferProfileSelect(profiles))
            if len(profiles) == 1:
                self.selected_profile_id = profiles[0]["profile_id"]
                self.selected_profile_name = profiles[0]["name"]

    def build_content(self) -> str:
        if self.selected_profile_name:
            return (
                f"📲 **Post this clip?**\n"
                f"📌 Posting to: **{self.selected_profile_name}**\n"
                f"Click **Post Now 🚀** to add a caption and publish!"
            )
        return "📲 **Post this clip?**\nSelect a profile below, then click **Post Now 🚀**!"

    @discord.ui.button(
        label="Post Now 🚀", style=discord.ButtonStyle.success,
        row=1, custom_id="btn_buffer_post"
    )
    async def post_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your clip.", ephemeral=True)
            return
        if not self.selected_profile_id:
            await interaction.response.send_message("⚠️ Select a profile first!", ephemeral=True)
            return
        await interaction.response.send_modal(BufferCaptionModal(self))

    @discord.ui.button(
        label="Skip ❌", style=discord.ButtonStyle.secondary,
        row=1, custom_id="btn_buffer_skip"
    )
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="📲 Post skipped.", view=None)


# ── Batch Editing View & Commands ─────────────────────────────────────────────

class BatchProcessView(discord.ui.View):
    """UI Panel for batch processing recent channel clips."""
    def __init__(self, unedited_clips: List[dict], user_id: int):
        super().__init__(timeout=300)
        self.unedited_clips = unedited_clips
        self.user_id = user_id
        self.selected_preset_ids = []
        self.bg_color = "black"
        self.creator_key = "none"
        user_presets = presets.get_user_presets(user_id)
        if user_presets:
            self.add_item(PresetSelect(user_id))      # row 0
            self.add_item(ColorSelect(row=1))         # row 1
            self.add_item(CreatorSelect(row=2))       # row 2
        else:
            self.add_item(ColorSelect(row=0))         # row 0
            self.add_item(CreatorSelect(row=1))       # row 1

    def build_content(self, user: discord.User) -> str:
        count = len(self.unedited_clips)
        user_presets = presets.get_user_presets(user.id)
        if self.selected_preset_ids:
            preset_names = []
            for pid in self.selected_preset_ids:
                if pid == "manual":
                    preset_names.append("Manual / Default")
                elif pid in user_presets:
                    preset_names.append(user_presets[pid]["name"])
            presets_summary = ", ".join(f"`{n}`" for n in preset_names)
        else:
            presets_summary = "`Default Account`"

        return (
            f"⚡ **Batch Edit Panel** for `{user.name}`\n\n"
            f"🎬 **Recent Clips Found:** `{count}` clips in this channel\n"
            f"📁 Target Account Presets: **{presets_summary}**\n\n"
            f"Select preset(s), then click **Start Batch Edit 🚀** to edit all `{count}` clips!"
        )

    @discord.ui.button(
        label="Start Batch Edit 🚀", style=discord.ButtonStyle.success,
        row=3, custom_id="btn_start_batch"
    )
    async def start_batch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancelled_user_ids.discard(interaction.user.id)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="⏳ Enqueuing batch clips…", view=self)

        try:
            test_msg = await interaction.user.send(
                f"⚡ **Batch Edit Enqueued:** {len(self.unedited_clips)} recent clips queued! Progress will appear here."
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="❌ **Cannot DM you.** Enable 'Allow direct messages from server members', then try again.", view=None
            )
            return

        effective_creator = self.creator_key if self.creator_key != "none" else ""
        total_clips = len(self.unedited_clips)

        for idx, clip_info in enumerate(self.unedited_clips, 1):
            msg_id = clip_info["msg_id"]
            video_url = clip_info["video_url"]

            mark_clip_processed(msg_id, video_url)

            job = {
                "user":        interaction.user,
                "video_url":   video_url,
                "color_name":  self.bg_color,
                "mirror":      False,
                "crop_blur":   False,
                "creator_key": effective_creator,
                "caption":     "",
                "preset_ids":  self.selected_preset_ids,
                "test_msg":    test_msg,
                "guild_id":    interaction.guild_id if interaction.guild else None,
                "batch_info":  f"Clip {idx}/{total_clips}"
            }
            await processing_queue.put(job)

        await interaction.edit_original_response(
            content=f"✅ **Enqueued {total_clips} clips for batch editing!** Check your DMs for progress.",
            view=None
        )

    @discord.ui.button(
        label="Cancel ❌", style=discord.ButtonStyle.danger,
        row=3, custom_id="btn_cancel_batch"
    )
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Batch edit cancelled.", view=None)


def normalize_video_url(url: str) -> str:
    """Normalizes cloud sharing links (Google Drive, etc.) into direct video download URLs."""
    url = url.strip()
    import re
    gdrive_match = re.search(r'drive\.google\.com/file/d/([a-zA-Z0-9_-]+)', url)
    if gdrive_match:
        file_id = gdrive_match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url


@bot.tree.command(
    name="editall",
    description="⚡ Batch edit the last N recent clips in THIS channel for your accounts!"
)
@discord.app_commands.describe(
    count="How many recent clips to edit (e.g. 1, 2, 3, 5, 10, 15). Default: 5."
)
async def editall(interaction: discord.Interaction, count: Optional[int] = 5):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send("❌ This command must be used in a text channel.", ephemeral=True)
        return

    target_count = max(1, min(50, count if count is not None else 5))
    recent_clips = []

    async for msg in channel.history(limit=250, oldest_first=False):
        found_in_msg = False
        for att in msg.attachments:
            ext = Path(att.filename).suffix.lower()
            if (att.content_type and att.content_type.startswith("video/")) or ext in (".mp4", ".mov", ".mkv", ".webm", ".avi"):
                recent_clips.append({
                    "msg_id": f"{msg.id}_{att.id}",
                    "video_url": att.url
                })
                found_in_msg = True
                if len(recent_clips) >= target_count:
                    break

        if len(recent_clips) >= target_count:
            break

        if not found_in_msg:
            for word in msg.content.split():
                if word.startswith(("http://", "https://")):
                    clean_w = word.split("?")[0].lower()
                    if (
                        Path(clean_w).suffix in (".mp4", ".mov", ".mkv", ".webm", ".avi") or
                        "catbox.moe" in word or
                        "gofile.io" in word or
                        "drive.google.com" in word or
                        "streamable.com" in word or
                        "dropbox.com" in word
                    ):
                        recent_clips.append({
                            "msg_id": msg.id,
                            "video_url": normalize_video_url(word)
                        })
                        if len(recent_clips) >= target_count:
                            break

        if len(recent_clips) >= target_count:
            break

    if not recent_clips:
        await interaction.followup.send(
            f"ℹ️ **No video clips found in #{channel.name}!** Upload clips or paste video URLs first.",
            ephemeral=True
        )
        return

    # Chronological order (oldest to newest)
    recent_clips.reverse()

    view = BatchProcessView(recent_clips, interaction.user.id)
    await interaction.followup.send(
        content=view.build_content(interaction.user),
        view=view,
        ephemeral=True
    )


@bot.tree.command(
    name="batchprocess",
    description="🚀 Batch edit the last N recent clips in THIS channel for your accounts!"
)
@discord.app_commands.describe(
    count="How many recent clips to edit (e.g. 1, 2, 3, 5, 10, 15). Default: 5."
)
async def batchprocess(interaction: discord.Interaction, count: Optional[int] = 5):
    await editall.callback(interaction, count=count)


@bot.tree.command(
    name="linkedit",
    description="🔗 Edit heavy raw clips directly from Video Links (Google Drive, Catbox, GoFile, Direct URL)!"
)
@discord.app_commands.describe(
    urls="Paste video URLs separated by spaces or newlines (e.g. Google Drive, Catbox.moe, MP4 links)"
)
async def linkedit(interaction: discord.Interaction, urls: str):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id

    raw_urls = urls.replace(",", " ").replace("\n", " ").split()
    valid_clips = []

    for idx, u in enumerate(raw_urls, 1):
        if u.startswith(("http://", "https://")):
            norm_url = normalize_video_url(u)
            valid_clips.append({
                "msg_id": f"url_{idx}_{uuid.uuid4().hex[:6]}",
                "video_url": norm_url
            })

    if not valid_clips:
        await interaction.followup.send(
            "❌ **No valid video URLs found in your input.**\n"
            "Paste direct video links, Google Drive links, or Catbox.moe links!",
            ephemeral=True
        )
        return

    view = BatchProcessView(valid_clips, user_id)
    await interaction.followup.send(
        content=view.build_content(interaction.user),
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="urledit", description="🔗 Alias for /linkedit (edit heavy clips from video URLs).")
@discord.app_commands.describe(urls="Paste video URLs separated by spaces or newlines")
async def urledit(interaction: discord.Interaction, urls: str):
    await linkedit.callback(interaction, urls=urls)


@bot.tree.command(name="stop", description="🛑 Stop all your ongoing and enqueued video editing operations.")
async def stop(interaction: discord.Interaction):
    user_id = interaction.user.id
    cancelled_user_ids.add(user_id)

    # Drain and clear user's queued jobs
    removed_count = 0
    temp_jobs = []

    while not processing_queue.empty():
        try:
            j = processing_queue.get_nowait()
            if j.get("user") and j["user"].id == user_id:
                removed_count += 1
                processing_queue.task_done()
            else:
                temp_jobs.append(j)
        except asyncio.QueueEmpty:
            break

    # Put back other users' jobs
    for j in temp_jobs:
        await processing_queue.put(j)

    await interaction.response.send_message(
        f"🛑 **All video operations stopped for {interaction.user.name}!** Cleared `{removed_count}` queued jobs.",
        ephemeral=True
    )


# ── Universal Bulk Post View & Commands (/postall & /batchpost) ────────────────

class UniversalCaptionModal(discord.ui.Modal, title="📝 Universal Caption for Bulk Post"):
    caption_input = discord.ui.TextInput(
        label="Universal Caption",
        placeholder="🔥 Best Blueface clips! #reels #viral #trending",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=2000
    )

    def __init__(self, batch_view: "BatchPostView"):
        super().__init__()
        self.batch_view = batch_view

    async def on_submit(self, interaction: discord.Interaction):
        caption = self.caption_input.value.strip()
        user_id = interaction.user.id
        token = buffer_integration.get_user_token(user_id)
        if not token:
            await interaction.response.send_message("❌ Buffer token not found. Use `/setbuffer` first.", ephemeral=True)
            return

        if caption:
            user_last_caption[user_id] = caption

        target_profile_id = self.batch_view.selected_profile_id
        target_profile_name = self.batch_view.selected_profile_name
        target_preset = self.batch_view.selected_preset_filter

        # Filter clips based on selected preset
        clips_to_post = []
        for c in self.batch_view.user_clips:
            if target_preset == "all" or c["preset_name"].lower() == target_preset.lower():
                clips_to_post.append(c)

        if not clips_to_post:
            await interaction.response.send_message("⚠️ No matching clips found for selected preset filter.", ephemeral=True)
            return

        total = len(clips_to_post)
        await interaction.response.edit_message(
            content=f"⏳ **Bulk Posting {total} clips to `{target_profile_name}`... (0/{total})**\n"
                    f"*(Paced 5s apart with auto-retry to prevent rate limits & 504 timeouts)*",
            view=None
        )

        success_count = 0
        fail_count = 0

        for idx, clip in enumerate(clips_to_post, 1):
            if idx > 1:
                # Pacing delay between posts to prevent Buffer/Instagram 504/502 rate limiting
                await asyncio.sleep(5.0)

            # Robust 4-attempt auto-retry loop with exponential backoff
            ok = False
            msg = ""
            for attempt in range(4):
                try:
                    ok, msg = await buffer_integration.post_to_buffer(
                        access_token=token,
                        profile_id=target_profile_id,
                        caption=caption or "🔥 New Reel! #reels #viral",
                        media_url=clip["video_url"]
                    )
                    if ok:
                        break
                    else:
                        retry_delay = (attempt + 1) * 5.0
                        logger.warning(
                            f"Bulk post attempt {attempt+1}/4 failed for clip {idx}: {msg}. "
                            f"Retrying in {retry_delay}s..."
                        )
                        await asyncio.sleep(retry_delay)
                except Exception as e:
                    logger.error(f"Bulk post error on clip {idx} (attempt {attempt+1}): {e}")
                    await asyncio.sleep((attempt + 1) * 5.0)

            if ok:
                success_count += 1
            else:
                fail_count += 1
                if user_id not in user_failed_clips:
                    user_failed_clips[user_id] = []
                user_failed_clips[user_id].append(clip)

            try:
                await interaction.edit_original_response(
                    content=f"⏳ **Bulk Posting {total} clips to `{target_profile_name}`... ({idx}/{total})**\n"
                            f"✅ Successful: `{success_count}` | ❌ Failed: `{fail_count}`"
                )
            except Exception:
                pass

        preview = caption[:80] + "…" if len(caption) > 80 else (caption or "(default)")
        await interaction.edit_original_response(
            content=f"🚀 **Bulk Post Completed for `{target_profile_name}`!**\n\n"
                    f"✅ **Posted to Instagram:** `{success_count}/{total}` clips\n"
                    f"❌ **Failed:** `{fail_count}` clips\n"
                    f"📝 **Caption:** {preview}\n\n"
                    f"💡 *Tip:* If any clips failed, use `/retryfailed` to re-post them instantly without typing captions again!"
        )


class BatchPostPresetSelect(discord.ui.Select):
    """Dropdown to filter by Account Preset."""
    def __init__(self, presets_found: List[str]):
        options = [
            discord.SelectOption(label="All Account Presets 🌐", value="all", description="Post clips from all your account presets")
        ]
        for p_name in presets_found[:24]:
            options.append(discord.SelectOption(label=f"Preset: {p_name}", value=p_name))

        super().__init__(
            placeholder="Select Account Preset filter…",
            min_values=1, max_values=1,
            options=options,
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_preset_filter = self.values[0]
        await interaction.response.edit_message(
            content=self.view.build_content(interaction.user), view=self.view
        )


class BatchPostProfileSelect(discord.ui.Select):
    """Dropdown to select Buffer Social Profile."""
    def __init__(self, profiles: List[Dict[str, str]]):
        options = [
            discord.SelectOption(
                label=p["name"][:25],
                value=p["profile_id"],
                description=p["profile_id"][:50]
            )
            for p in profiles[:25]
        ]
        super().__init__(
            placeholder="Select Buffer profile to post to…",
            min_values=1, max_values=1,
            options=options,
            row=1
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_profile_id = self.values[0]
        self.view.selected_profile_name = next(
            (o.label for o in self.options if o.value == self.values[0]), "Unknown"
        )
        await interaction.response.edit_message(
            content=self.view.build_content(interaction.user), view=self.view
        )


class BatchPostView(discord.ui.View):
    """UI Panel for bulk posting user's edited clips."""
    def __init__(self, user_clips: List[Dict[str, Any]], user_id: int):
        super().__init__(timeout=300)
        self.user_clips = user_clips
        self.user_id = user_id
        self.selected_preset_filter = "all"
        self.selected_profile_id = None
        self.selected_profile_name = None

        presets_found = sorted(list(set(c["preset_name"] for c in user_clips if c.get("preset_name"))))
        self.add_item(BatchPostPresetSelect(presets_found))

        profiles = buffer_integration.get_user_profiles(user_id)
        if profiles:
            self.add_item(BatchPostProfileSelect(profiles))
            if len(profiles) == 1:
                self.selected_profile_id = profiles[0]["profile_id"]
                self.selected_profile_name = profiles[0]["name"]

    def build_content(self, user: discord.User) -> str:
        matching_clips = [
            c for c in self.user_clips
            if self.selected_preset_filter == "all" or c["preset_name"].lower() == self.selected_preset_filter.lower()
        ]
        matching_count = len(matching_clips)
        total_found = len(self.user_clips)

        preset_txt = "All Account Presets 🌐" if self.selected_preset_filter == "all" else f"`{self.selected_preset_filter}`"
        profile_txt = f"**{self.selected_profile_name}**" if self.selected_profile_name else "`None Selected`"

        return (
            f"🚀 **Bulk Post Panel for {user.mention}**\n\n"
            f"🎬 **Clips to Post:** `{matching_count}` clips (Preset Filter: {preset_txt})\n"
            f"📲 Posting to Buffer Profile: **{profile_txt}**\n\n"
            f"Select Buffer profile below, then click **Post All Now 🚀** to enter universal caption and post!"
        )

    @discord.ui.button(
        label="Post All Now 🚀", style=discord.ButtonStyle.success,
        row=2, custom_id="btn_start_batch_post"
    )
    async def post_all_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ These are not your clips.", ephemeral=True)
            return
        if not self.selected_profile_id:
            await interaction.response.send_message("⚠️ Select a Buffer profile first!", ephemeral=True)
            return

        modal = UniversalCaptionModal(self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Cancel ❌", style=discord.ButtonStyle.secondary,
        row=2, custom_id="btn_cancel_batch_post"
    )
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="📲 Bulk post cancelled.", view=None)


@bot.tree.command(
    name="postall",
    description="🚀 Bulk post YOUR recent edited clips to Buffer with 1 universal caption!"
)
@discord.app_commands.describe(
    count="How many of YOUR recent edited clips to post (e.g. 5, 10). Default: 10."
)
async def postall(interaction: discord.Interaction, count: Optional[int] = 10):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    user_mention = interaction.user.mention
    target_count = max(1, min(50, count if count is not None else 10))

    channels_to_scan = []
    if interaction.guild:
        ch = discord.utils.get(interaction.guild.text_channels, name="edited-clips")
        if ch:
            channels_to_scan.append(ch)
    if interaction.channel and interaction.channel not in channels_to_scan:
        channels_to_scan.append(interaction.channel)

    user_clips = []
    seen_msg_ids = set()

    for channel in channels_to_scan:
        async for msg in channel.history(limit=500, oldest_first=False):
            if msg.id in seen_msg_ids:
                continue
            seen_msg_ids.add(msg.id)

            if msg.author.id == bot.user.id and (user_mention in msg.content or str(user_id) in msg.content):
                for att in msg.attachments:
                    ext = Path(att.filename).suffix.lower()
                    if (att.content_type and att.content_type.startswith("video/")) or ext in (".mp4", ".mov", ".mkv", ".webm"):
                        preset_name = "Default Account"
                        import re
                        match = re.search(r'New clip for `([^`]+)`', msg.content)
                        if match:
                            preset_name = match.group(1)

                        user_clips.append({
                            "msg_id": msg.id,
                            "preset_name": preset_name,
                            "video_url": att.url
                        })
                        if len(user_clips) >= target_count:
                            break

            if len(user_clips) >= target_count:
                break

        if len(user_clips) >= target_count:
            break

    if not user_clips:
        await interaction.followup.send(
            f"ℹ️ **No recent edited clips found for you ({interaction.user.name}) in #{channel.name}!**\n"
            f"Edit clips first using `/editall` or clip buttons.",
            ephemeral=True
        )
        return

    user_clips.reverse()

    view = BatchPostView(user_clips, user_id)
    await interaction.followup.send(
        content=view.build_content(interaction.user),
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="batchpost", description="🚀 Alias for /postall (bulk post YOUR edited clips to Buffer).")
@discord.app_commands.describe(count="How many of YOUR recent edited clips to post. Default: 10.")
async def batchpost(interaction: discord.Interaction, count: Optional[int] = 10):
    await postall.callback(interaction, count=count)


class RetryFailedView(discord.ui.View):
    """UI Panel to retry failed posts without re-entering captions."""
    def __init__(self, user_clips: List[Dict[str, Any]], user_id: int):
        super().__init__(timeout=300)
        self.user_clips = user_clips
        self.user_id = user_id
        self.selected_preset_filter = "all"
        self.selected_profile_id = None
        self.selected_profile_name = None

        presets_found = sorted(list(set(c["preset_name"] for c in user_clips if c.get("preset_name"))))
        self.add_item(BatchPostPresetSelect(presets_found))

        profiles = buffer_integration.get_user_profiles(user_id)
        if profiles:
            self.add_item(BatchPostProfileSelect(profiles))
            if len(profiles) == 1:
                self.selected_profile_id = profiles[0]["profile_id"]
                self.selected_profile_name = profiles[0]["name"]

    def build_content(self, user: discord.User) -> str:
        matching_clips = [
            c for c in self.user_clips
            if self.selected_preset_filter == "all" or c["preset_name"].lower() == self.selected_preset_filter.lower()
        ]
        matching_count = len(matching_clips)
        saved_caption = user_last_caption.get(user.id, "🔥 New Reel! #reels #viral")
        preview = saved_caption[:70] + "…" if len(saved_caption) > 70 else saved_caption
        profile_txt = f"**{self.selected_profile_name}**" if self.selected_profile_name else "`None Selected`"

        return (
            f"🔄 **Retry Failed Posts Panel for {user.mention}**\n\n"
            f"🎬 **Clips to Retry:** `{matching_count}` clips\n"
            f"📝 **Saved Caption (Auto-Used):** `{preview}`\n"
            f"📲 Posting to Buffer Profile: **{profile_txt}**\n\n"
            f"Select Buffer profile below, then click **Retry Posts Now 🚀** to post without typing captions again!"
        )

    @discord.ui.button(
        label="Retry Posts Now 🚀", style=discord.ButtonStyle.success,
        row=2, custom_id="btn_start_retry_post"
    )
    async def retry_now_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ These are not your clips.", ephemeral=True)
            return
        if not self.selected_profile_id:
            await interaction.response.send_message("⚠️ Select a Buffer profile first!", ephemeral=True)
            return

        user_id = interaction.user.id
        token = buffer_integration.get_user_token(user_id)
        if not token:
            await interaction.response.send_message("❌ Buffer token not found. Use `/setbuffer` first.", ephemeral=True)
            return

        caption = user_last_caption.get(user_id, "🔥 New Reel! #reels #viral")
        target_profile_id = self.selected_profile_id
        target_profile_name = self.selected_profile_name
        target_preset = self.selected_preset_filter

        clips_to_post = [
            c for c in self.user_clips
            if target_preset == "all" or c["preset_name"].lower() == target_preset.lower()
        ]

        if not clips_to_post:
            await interaction.response.send_message("⚠️ No matching clips found for selected preset filter.", ephemeral=True)
            return

        total = len(clips_to_post)
        await interaction.response.edit_message(
            content=f"⏳ **Retrying Bulk Post for {total} clips to `{target_profile_name}`... (0/{total})**\n"
                    f"*(Paced 5s apart with 4-tier auto-retry)*",
            view=None
        )

        success_count = 0
        fail_count = 0

        for idx, clip in enumerate(clips_to_post, 1):
            if idx > 1:
                await asyncio.sleep(5.0)

            ok = False
            msg = ""
            for attempt in range(4):
                try:
                    ok, msg = await buffer_integration.post_to_buffer(
                        access_token=token,
                        profile_id=target_profile_id,
                        caption=caption,
                        media_url=clip["video_url"]
                    )
                    if ok:
                        break
                    else:
                        retry_delay = (attempt + 1) * 5.0
                        logger.warning(f"Retry attempt {attempt+1}/4 failed for clip {idx}: {msg}. Retrying in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                except Exception as e:
                    logger.error(f"Retry error on clip {idx}: {e}")
                    await asyncio.sleep((attempt + 1) * 5.0)

            if ok:
                success_count += 1
            else:
                fail_count += 1

            try:
                await interaction.edit_original_response(
                    content=f"⏳ **Retrying Bulk Post for {total} clips to `{target_profile_name}`... ({idx}/{total})**\n"
                            f"✅ Successful: `{success_count}` | ❌ Failed: `{fail_count}`"
                )
            except Exception:
                pass

        prev_text = caption[:80] + "…" if len(caption) > 80 else caption
        await interaction.edit_original_response(
            content=f"🚀 **Retry Completed for `{target_profile_name}`!**\n\n"
                    f"✅ **Posted to Instagram:** `{success_count}/{total}` clips\n"
                    f"❌ **Failed:** `{fail_count}` clips\n"
                    f"📝 **Caption Used:** {prev_text}"
        )

    @discord.ui.button(
        label="Cancel ❌", style=discord.ButtonStyle.secondary,
        row=2, custom_id="btn_cancel_retry_post"
    )
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="📲 Retry cancelled.", view=None)


@bot.tree.command(
    name="retryfailed",
    description="🔄 Retry posting YOUR recent failed clips to Buffer without re-typing captions!"
)
@discord.app_commands.describe(
    count="How many of YOUR recent failed clips to retry (e.g. 5, 10). Default: 10."
)
async def retryfailed(interaction: discord.Interaction, count: Optional[int] = 10):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    user_mention = interaction.user.mention
    target_count = max(1, min(50, count if count is not None else 10))

    # Retrieve from tracked failed clips memory first
    failed_list = user_failed_clips.get(user_id, [])
    clips_to_retry = []

    if failed_list:
        clips_to_retry = failed_list[-target_count:]
    else:
        # Fallback: scan #edited-clips for recent edited clips
        channels_to_scan = []
        if interaction.guild:
            ch = discord.utils.get(interaction.guild.text_channels, name="edited-clips")
            if ch:
                channels_to_scan.append(ch)
        if interaction.channel and interaction.channel not in channels_to_scan:
            channels_to_scan.append(interaction.channel)

        seen_msg_ids = set()
        for channel in channels_to_scan:
            async for msg in channel.history(limit=500, oldest_first=False):
                if msg.id in seen_msg_ids:
                    continue
                seen_msg_ids.add(msg.id)

                if msg.author.id == bot.user.id and (user_mention in msg.content or str(user_id) in msg.content):
                    for att in msg.attachments:
                        ext = Path(att.filename).suffix.lower()
                        if (att.content_type and att.content_type.startswith("video/")) or ext in (".mp4", ".mov", ".mkv", ".webm"):
                            preset_name = "Default Account"
                            import re
                            match = re.search(r'New clip for `([^`]+)`', msg.content)
                            if match:
                                preset_name = match.group(1)

                            clips_to_retry.append({
                                "msg_id": msg.id,
                                "preset_name": preset_name,
                                "video_url": att.url
                            })
                            if len(clips_to_retry) >= target_count:
                                break
                if len(clips_to_retry) >= target_count:
                    break
            if len(clips_to_retry) >= target_count:
                break
        clips_to_retry.reverse()

    if not clips_to_retry:
        await interaction.followup.send(
            f"ℹ️ **No recent failed or edited clips found to retry.**",
            ephemeral=True
        )
        return

    view = RetryFailedView(clips_to_retry, user_id)
    await interaction.followup.send(
        content=view.build_content(interaction.user),
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="repostfailed", description="🔄 Alias for /retryfailed (retry posting failed clips to Buffer).")
@discord.app_commands.describe(count="How many of YOUR recent failed clips to retry. Default: 10.")
async def repostfailed(interaction: discord.Interaction, count: Optional[int] = 10):
    await retryfailed.callback(interaction, count=count)


@bot.tree.command(
    name="autolike",
    description="❤️ Anti-Ban Human Auto Comment Liker bot (slow human-like gaps & pauses)."
)
@discord.app_commands.describe(
    action="Action to perform: start, stop, status, or settings",
    max_daily_likes="Optional: Maximum comment likes per day (e.g. 50, 80, 120). Default: 80.",
    min_delay_sec="Optional: Minimum human delay between likes in seconds (e.g. 25). Default: 25.",
    max_delay_sec="Optional: Maximum human delay between likes in seconds (e.g. 60). Default: 60."
)
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="🟢 Start Auto Liker", value="start"),
    discord.app_commands.Choice(name="🔴 Stop Auto Liker", value="stop"),
    discord.app_commands.Choice(name="📊 View Status & Logs", value="status"),
    discord.app_commands.Choice(name="⚙️ Update Human Settings", value="settings")
])
async def autolike(
    interaction: discord.Interaction,
    action: str,
    max_daily_likes: Optional[int] = None,
    min_delay_sec: Optional[int] = None,
    max_delay_sec: Optional[int] = None
):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id

    if action == "start":
        auto_liker.liker_engine.start_user_liker(user_id)
        cfg = auto_liker.get_user_liker_config(user_id)
        await interaction.followup.send(
            content=f"🟢 **Auto Comment Liker Started for {interaction.user.mention}!**\n\n"
                    f"🛡️ **Anti-Ban Human Safety Mode Active:**\n"
                    f"⏱️ **Human Delay Range:** `{cfg['min_delay']}s – {cfg['max_delay']}s` per comment\n"
                    f"☕ **Coffee Break Pauses:** Every `{cfg['batch_size']}` likes ({cfg['rest_min']//60}-{cfg['rest_max']//60} mins pause)\n"
                    f"📊 **Daily Limit:** `{cfg['liked_today']}/{cfg['max_likes_per_day']}` likes today\n\n"
                    f"Use `/autolike status` to check live progress & activity logs!",
            ephemeral=True
        )

    elif action == "stop":
        auto_liker.liker_engine.stop_user_liker(user_id)
        await interaction.followup.send(
            content=f"🔴 **Auto Comment Liker Paused for {interaction.user.mention}.**",
            ephemeral=True
        )

    elif action == "settings":
        cfg = auto_liker.get_user_liker_config(user_id)
        if max_daily_likes is not None:
            cfg["max_likes_per_day"] = max(10, min(300, max_daily_likes))
        if min_delay_sec is not None:
            cfg["min_delay"] = max(10, min(120, min_delay_sec))
        if max_delay_sec is not None:
            cfg["max_delay"] = max(cfg["min_delay"] + 5, min(300, max_delay_sec))

        auto_liker.save_user_liker_config(user_id, cfg)
        await interaction.followup.send(
            content=f"⚙️ **Human Liker Settings Updated for {interaction.user.mention}:**\n\n"
                    f"📊 **Max Daily Likes:** `{cfg['max_likes_per_day']}`\n"
                    f"⏱️ **Human Delay Range:** `{cfg['min_delay']}s – {cfg['max_delay']}s`\n"
                    f"☕ **Coffee Break:** Rest `{cfg['rest_min']//60}-{cfg['rest_max']//60}m` every `{cfg['batch_size']}` likes\n\n"
                    f"Use `/autolike start` to enable with these settings!",
            ephemeral=True
        )

    else:  # status
        cfg = auto_liker.get_user_liker_config(user_id)
        is_running = auto_liker.liker_engine.is_running(user_id)
        status_str = "🟢 Running" if is_running else "🔴 Stopped"
        logs = cfg.get("logs", [])
        log_txt = "\n".join(logs[-8:]) if logs else "(No recent activity)"

        await interaction.followup.send(
            content=f"📊 **Auto Comment Liker Status for {interaction.user.mention}**\n\n"
                    f"⚡ **Status:** {status_str} — *{cfg.get('status_message', 'Idle')}*\n"
                    f"📈 **Liked Today:** `{cfg['liked_today']}/{cfg['max_likes_per_day']}` comments\n"
                    f"🏆 **Total Liked All-Time:** `{cfg['total_liked']}` comments\n"
                    f"⏱️ **Human Pacing:** `{cfg['min_delay']}s–{cfg['max_delay']}s` per like (Break every {cfg['batch_size']} likes)\n\n"
                    f"📝 **Recent Activity Logs:**\n```\n{log_txt}\n```",
            ephemeral=True
        )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not config.DISCORD_TOKEN or "replace_this" in config.DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN not set in .env!")
    else:
        logger.info("Starting bot…")
        bot.run(config.DISCORD_TOKEN)
