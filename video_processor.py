import asyncio
import json
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Tuple, Dict, Any, Optional

import config
from config import COLOR_PALETTE, FONT_PATH

logger = logging.getLogger(__name__)

def get_video_metadata(video_path: str) -> Dict[str, Any]:
    """Retrieves video metadata (width, height, duration, audio presence) using ffprobe."""
    cmd_dims = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration:format=duration",
        "-of", "json", video_path
    ]
    cmd_audio = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "json", video_path
    ]
    metadata = {"width": 0, "height": 0, "duration": 0.0, "has_audio": False}
    try:
        r = subprocess.run(cmd_dims, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        res = json.loads(r.stdout)
        streams = res.get("streams", [])
        if streams:
            metadata["width"]  = int(streams[0].get("width", 0))
            metadata["height"] = int(streams[0].get("height", 0))
            dur_str = streams[0].get("duration")
            if dur_str:
                try:
                    metadata["duration"] = float(dur_str)
                except ValueError:
                    pass

        if metadata["duration"] == 0.0 and res.get("format", {}).get("duration"):
            try:
                metadata["duration"] = float(res["format"]["duration"])
            except ValueError:
                pass

        r2 = subprocess.run(cmd_audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        metadata["has_audio"] = len(json.loads(r2.stdout).get("streams", [])) > 0
    except Exception as e:
        logger.error(f"Error reading metadata from {video_path}: {e}")
    return metadata


def detect_text_position(video_path: str) -> str:
    """
    Samples a frame from the video and analyzes luminance density to detect if 
    main text/caption is in the top half or bottom half of the frame.
    Returns "TOP_TEXT" or "BOTTOM_TEXT".
    """
    temp_frame = config.TEMP_DIR / f"frame_{uuid.uuid4().hex[:6]}.png"
    try:
        cmd = [
            "ffmpeg", "-y", "-ss", "00:00:02",
            "-i", video_path, "-vframes", "1",
            "-q:v", "2", str(temp_frame)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if not temp_frame.exists():
            return "TOP_TEXT"

        from PIL import Image
        img = Image.open(temp_frame).convert("L")
        w, h = img.size
        threshold = 200
        top_count = 0
        bottom_count = 0

        pixels = img.load()
        # Check top 28% region for title box vs bottom title region (72% to 90%), ignoring middle frame/people
        top_region_end = int(h * 0.28)
        bottom_region_start = int(h * 0.72)
        bottom_region_end = int(h * 0.90)

        for y in range(0, h, 2):
            bright_in_row = sum(1 for x in range(0, w, 4) if pixels[x, y] >= threshold)
            if y <= top_region_end:
                top_count += bright_in_row
            elif bottom_region_start <= y <= bottom_region_end:
                bottom_count += bright_in_row

        logger.info(f"Text position detection | Top title bright pixels: {top_count}, Bottom title bright pixels: {bottom_count}")
        if bottom_count > top_count * 1.5:
            return "BOTTOM_TEXT"
        else:
            return "TOP_TEXT"
    except Exception as e:
        logger.error(f"Error in detect_text_position: {e}")
        return "TOP_TEXT"
    finally:
        if temp_frame.exists():
            try:
                temp_frame.unlink()
            except Exception:
                pass


async def process_clip(
    video_path: str,
    output_path: str,
    bg_color_name: str = "black",
    logo_path: Optional[str] = None,       # User's personal watermark PNG
    custom_bg_path: Optional[str] = None,  # User's custom background image
    overlay_path: Optional[str] = None,    # Creator's Kick overlay PNG (unmirrored)
    caption: Optional[str] = None,         # Caption text to render at the top
    mirror: bool = False,
    crop_blur: bool = False,               # Cut off bottom blurred video bar
) -> Tuple[bool, str]:
    """
    Builds and executes an FFmpeg job that:
    1. Handles background (Custom Image if available/selected, or Solid Hex Color).
    2. (Optionally) hflips the video clip.
    3. (Optionally) crops off bottom blurred padding.
    4. Scales + pads it onto a 1080x1920 canvas.
    5. Overlays the user's personal logo/watermark dynamically (top/bottom based on text detection).
    6. Overlays the creator's Kick overlay PNG.
    7. Renders an Instagram cloud sticker caption.
    8. Preserves audio.
    """
    if not Path(video_path).exists():
        return False, "Input video file does not exist."

    meta      = get_video_metadata(video_path)
    W         = meta["width"]
    H         = meta["height"]
    has_audio = meta["has_audio"]

    if W == 0 or H == 0:
        return False, "Failed to read video dimensions."

    # ── Crop old platform overlay / top header / bottom blurred padding ────────
    if crop_blur:
        # Cut off both top header bar (~14%) and bottom blurred overlay (~25%)
        crop_top    = int(H * 0.14)
        crop_bottom = int(H * 0.25)
    else:
        # Keep 100% full original video content (do NOT crop bottom Kick banner)
        crop_top    = 0
        crop_bottom = 0

    H_effective = H - crop_top - crop_bottom
    if H_effective <= 0:
        H_effective = H

    # Full 1080 width video scaling (zero zoom out)
    scale   = min(1080 / W, 1920 / H_effective)
    W_prime = int(round(W * scale))
    H_prime = int(round(H_effective * scale))
    if W_prime % 2: W_prime -= 1
    if H_prime % 2: H_prime -= 1

    bg_hex          = COLOR_PALETTE.get(bg_color_name.lower(), COLOR_PALETTE["black"])
    top_of_video    = int((1920 - H_prime) / 2)
    bottom_of_video = int((1920 + H_prime) / 2)

    # Detect text position to decide whether logo goes at TOP or BOTTOM
    text_pos = detect_text_position(video_path)
    logger.info(f"Video text position detected: {text_pos}")

    inputs = ["-i", video_path]
    in_idx = 0
    fc_parts = []

    # ── Step 1 : Video Prep (Crop / Mirror) ───────────────────────────────────
    if mirror:
        # 1. Flip full video body
        # 2. Crop unmirrored top text region (~25% of height to cover full caption card)
        # 3. Overlay unmirrored top text card over the flipped video
        txt_h = int(H_effective * 0.25)
        if txt_h % 2: txt_h -= 1

        fc_parts.append(
            f"[0:v]crop=iw:{H_effective}:0:{crop_top},split[v_orig][v_to_flip]; "
            f"[v_to_flip]hflip[v_flipped]; "
            f"[v_orig]crop=iw:{txt_h}:0:0[top_text]; "
            f"[v_flipped][top_text]overlay=x=0:y=0[vid_prep]"
        )
        in_stream = "[vid_prep]"
    elif crop_blur:
        fc_parts.append(f"[0:v]crop=iw:{H_effective}:0:{crop_top}[vid_prep]")
        in_stream = "[vid_prep]"
    else:
        in_stream = "[0:v]"

    # ── Step 2 : Scale Video (Full 1080 Width Preserved, Zero Zoom Out) ───────
    scale_expr = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "scale=w='trunc(iw/2)*2':h='trunc(ih/2)*2',setsar=1"
    )
    fc_parts.append(f"{in_stream}{scale_expr}[vid_scaled]")
    in_stream = "[vid_scaled]"

    # ── Step 3 : Creator Kick Overlay PNG (Attached directly to bottom of video) ──
    if overlay_path and Path(overlay_path).exists():
        in_idx += 1
        inputs.extend(["-i", overlay_path])
        # Scale Kick overlay to match video width 1080
        fc_parts.append(f"[{in_idx}:v]scale=1080:-2,scale=w='trunc(iw/2)*2':h='trunc(ih/2)*2'[kov]")
        # Overlay directly at the bottom edge of the video clip (y=H-h)
        fc_parts.append(f"{in_stream}[kov]overlay=x=0:y=H-h[vid_with_kov]")
        in_stream = "[vid_with_kov]"

    # ── Step 4 : Pad / Overlay Video Block onto 1080x1920 Canvas ─────────────
    use_custom_bg = bool(custom_bg_path and Path(custom_bg_path).exists())

    if use_custom_bg:
        in_idx += 1
        inputs.extend(["-i", custom_bg_path])
        # Background canvas: custom image scaled/cropped to 1080x1920
        fc_parts.append(f"[{in_idx}:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[bg_canvas]")
        # Overlay video block (with attached Kick overlay) centered on canvas
        fc_parts.append(f"[bg_canvas]{in_stream}overlay=x=(1080-w)/2:y=(1920-h)/2[bg]")
        current_v = "[bg]"
    else:
        # Pad video block centered with solid hex background
        fc_parts.append(
            f"{in_stream}pad=1080:1920:(1080-iw)/2:(1920-ih)/2:color={bg_hex}[bg]"
        )
        current_v = "[bg]"

    # ── Step 5 : User Watermark Logo (EXTREME CANVAS BOUNDARY ANCHORING & LARGE SIZE) ──
    if logo_path and Path(logo_path).exists():
        in_idx += 1
        inputs.extend(["-i", logo_path])

        max_lw = 950
        max_lh = 280

        if text_pos == "BOTTOM_TEXT":
            # Text is at BOTTOM -> Push logo to EXTREME TOP EDGE of canvas!
            ov_expr = "x=(1080-w)/2:y=15"
        else:
            # Text is at TOP -> Push logo to EXTREME BOTTOM EDGE of canvas!
            ov_expr = "x=(1080-w)/2:y=1920-h-15"

        fc_parts.append(
            f"[{in_idx}:v]scale={max_lw}:{max_lh}:force_original_aspect_ratio=decrease,"
            f"scale=w='trunc(iw/2)*2':h='trunc(ih/2)*2'[logo]"
        )
        fc_parts.append(f"{current_v}[logo]overlay={ov_expr}[bg_logo]")
        current_v = "[bg_logo]"

    # ── Step 6 : Instagram Cloud Sticker Caption ──────────────────────────────
    sticker_path = None
    if caption and caption.strip():
        from caption_sticker import generate_instagram_sticker
        job_id_prefix = Path(output_path).stem
        sticker_path = config.TEMP_DIR / f"{job_id_prefix}_sticker.png"
        ok = generate_instagram_sticker(caption, str(sticker_path))
        if ok and sticker_path.exists():
            in_idx += 1
            inputs.extend(["-i", str(sticker_path)])
            if top_of_video >= 100:
                sticker_y = max(30, top_of_video - 120)
            else:
                sticker_y = 40
            fc_parts.append(f"{current_v}[{in_idx}:v]overlay=x=0:y={sticker_y}[outv]")
            current_v = "[outv]"

    video_map = current_v

    # ── Calculate dynamic target maxrate based on video duration ──────────────
    duration = meta.get("duration", 0.0)
    if duration > 0:
        # Target max size: 20 MB (20 * 1024 * 1024 * 8 bits = 167,772,160 bits)
        target_bits = 20 * 1024 * 1024 * 8
        calc_bitrate_bps = int(target_bits / duration)
        v_bitrate_bps = max(400_000, calc_bitrate_bps - 128_000)
        v_bitrate_k = f"{min(4500, v_bitrate_bps // 1000)}k"
        buf_size_k = f"{min(9000, (v_bitrate_bps * 2) // 1000)}k"
    else:
        v_bitrate_k = "3.5M"
        buf_size_k = "7M"

    # ── Build FFmpeg command ──────────────────────────────────────────────────
    filter_complex = "; ".join(fc_parts)

    cmd = ["ffmpeg", "-y"]
    cmd.extend(inputs)
    cmd.extend(["-filter_complex", filter_complex, "-map", video_map])
    if has_audio:
        cmd.extend(["-map", "0:a?", "-af", "volume=1.3", "-c:a", "aac", "-b:a", "128k"])
    cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
                "-maxrate", v_bitrate_k, "-bufsize", buf_size_k,
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", output_path])

    logger.info(f"Running FFmpeg (Duration: {duration:.1f}s, MaxRate: {v_bitrate_k}): {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            out_file = Path(output_path)
            if out_file.exists():
                size_mb = out_file.stat().st_size / (1024 * 1024)
                logger.info(f"Processed output file size: {size_mb:.2f} MB")
                if size_mb > 23.0:
                    logger.warning(f"File size ({size_mb:.2f}MB) exceeds 23MB Discord upload limit. Auto-recompressing...")
                    temp_recomp = out_file.with_name(f"{out_file.stem}_recomp.mp4")
                    recomp_cmd = [
                        "ffmpeg", "-y", "-i", str(out_file),
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                        "-maxrate", "2.5M", "-bufsize", "5M",
                        "-c:a", "copy", str(temp_recomp)
                    ]
                    r_proc = await asyncio.create_subprocess_exec(
                        *recomp_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await r_proc.communicate()
                    if temp_recomp.exists() and temp_recomp.stat().st_size > 0:
                        temp_recomp.replace(out_file)
                        logger.info(f"Auto-recompressed file size: {out_file.stat().st_size / (1024*1024):.2f} MB")
            return True, "Video processed successfully."
        err = stderr.decode(errors="replace")
        logger.error(f"FFmpeg error: {err}")
        return False, f"FFmpeg failed (exit {proc.returncode}): {err[-300:]}"
    except Exception as e:
        logger.error(f"Failed to launch FFmpeg: {e}")
        return False, f"Exception: {e}"
    finally:
        if sticker_path and Path(sticker_path).exists():
            try:
                Path(sticker_path).unlink()
            except Exception:
                pass
