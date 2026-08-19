import os
from pathlib import Path
from typing import List, Tuple
from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji
from pilmoji.source import AppleEmojiSource

import config

def get_text_font() -> ImageFont.FreeTypeFont:
    """Returns primary text font (Instagram / Caption font or system fallback)."""
    candidates = [
        config.FONTS_DIR / "instagram.ttf",
        config.FONTS_DIR / "caption.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(str(path), 54)
            except Exception:
                pass
    return ImageFont.load_default()


def generate_instagram_sticker(
    text: str,
    output_png_path: str,
    font_size: int = 54,
    max_text_width: int = 840,
    pad_x: int = 34,
    pad_y: int = 20,
    corner_radius: int = 26,
) -> bool:
    """
    Renders an Instagram-style white cloud sticker caption with Apple iOS emojis using Pilmoji.
    Saves transparent RGBA PNG to output_png_path.
    """
    text = text.strip()
    if not text:
        return False

    font = get_text_font()

    # Wrap words into lines
    words = text.split()
    lines: List[str] = []
    current_line: List[str] = []

    # Measurement canvas using Pilmoji to calculate widths including emojis
    meas_img = Image.new("RGBA", (1080, 200), (0, 0, 0, 0))

    with Pilmoji(meas_img, source=AppleEmojiSource) as pilmoji_meas:
        for word in words:
            test_line = " ".join(current_line + [word])
            # Measure width
            bbox = pilmoji_meas.getsize(test_line, font=font)
            w = bbox[0]
            if w > max_text_width and current_line:
                lines.append(" ".join(current_line))
                current_line = [word]
            else:
                current_line.append(word)
        if current_line:
            lines.append(" ".join(current_line))

    if not lines:
        return False

    # Calculate line dimensions
    line_data = []
    with Pilmoji(meas_img, source=AppleEmojiSource) as pilmoji_meas:
        for line in lines:
            size = pilmoji_meas.getsize(line, font=font)
            lw, lh = size[0], size[1]
            line_data.append((line, lw, lh))

    canvas_width = 1080
    step_y = font_size + pad_y + 4  # Merge line pills into cloud shape
    total_height = len(lines) * step_y + pad_y * 2 + 40

    sticker_img = Image.new("RGBA", (canvas_width, total_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sticker_img)

    # Step 1: Draw White Cloud Pill Backgrounds
    y_cursor = pad_y
    rects = []
    for line, lw, lh in line_data:
        b_width = lw + pad_x * 2
        b_height = font_size + pad_y * 2
        cx = canvas_width // 2
        x1 = cx - b_width // 2
        y1 = y_cursor
        x2 = cx + b_width // 2
        y2 = y1 + b_height

        rects.append((x1, y1, x2, y2))
        
        draw.rounded_rectangle(
            [x1, y1, x2, y2],
            radius=corner_radius,
            fill=(255, 255, 255, 255)
        )
        y_cursor += step_y

    # Bridge stacked pills to create continuous cloud bubble shape
    for i in range(len(rects) - 1):
        rx1, ry1, rx2, ry2 = rects[i]
        nx1, ny1, nx2, ny2 = rects[i+1]
        bridge_x1 = max(rx1, nx1)
        bridge_x2 = min(rx2, nx2)
        if bridge_x2 > bridge_x1:
            draw.rectangle(
                [bridge_x1, ry1 + corner_radius, bridge_x2, ny2 - corner_radius],
                fill=(255, 255, 255, 255)
            )
            draw.rounded_rectangle([rx1, ry1, rx2, ry2], radius=corner_radius, fill=(255, 255, 255, 255))
            draw.rounded_rectangle([nx1, ny1, nx2, ny2], radius=corner_radius, fill=(255, 255, 255, 255))

    # Step 2: Render Text + Apple iOS Color Emojis using Pilmoji
    y_cursor = pad_y
    with Pilmoji(sticker_img, source=AppleEmojiSource) as pilmoji:
        for line, lw, lh in line_data:
            cx = canvas_width // 2
            tx = cx - lw // 2
            ty = y_cursor + pad_y // 2 + 2

            pilmoji.text(
                (tx, ty),
                line,
                font=font,
                fill=(0, 0, 0, 255),
                emoji_scale_factor=1.1
            )
            y_cursor += step_y

    # Crop vertical transparent padding tightly
    bbox = sticker_img.getbbox()
    if bbox:
        sticker_img = sticker_img.crop((0, bbox[1], canvas_width, bbox[3]))

    sticker_img.save(output_png_path, "PNG")
    return True

if __name__ == "__main__":
    out = "c:/HELLO BRO/temp/test_pilmoji_result.png"
    ok = generate_instagram_sticker(
        'Bluface asks Neveah to shoutout Chrisean Rock and Neveah calls her "BABY DISABLER" 💀🔥',
        out
    )
    print("Generated Pilmoji sticker:", ok, out)
