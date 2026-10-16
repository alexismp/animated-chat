#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Generate an animated chat GIF and companion MP3 audio track from a JSON/TOML/YAML configuration file."""

import argparse
from datetime import datetime
import json
import math
import os
import re
import struct
import subprocess
import tempfile
import wave
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_PATH = "/System/Library/Fonts/HelveticaNeue.ttc"
EMOJI_FONT_PATH = "/System/Library/Fonts/Apple Color Emoji.ttc"

LINK_REGEX = re.compile(r"(https?://\S+|http//\S+)")


def parse_hex_color(hex_str, alpha=255):
    s = hex_str.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(alpha))


def is_emoji_char(ch):
    cp = ord(ch)
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0xFE00 <= cp <= 0xFE0F
    )


def has_emoji(token):
    return any(is_emoji_char(c) for c in token)


class ChatGifGenerator:
    def __init__(self, config):
        self.config = config

        # Theme ("dark" vs "light")
        raw_theme = str(config.get("theme", "dark")).strip().lower()
        self.theme = "light" if raw_theme == "light" else "dark"

        # Orientation / portrait mode
        orientation = str(config.get("orientation", "")).strip().lower()
        portrait_cfg = config.get("portrait", False)
        if isinstance(portrait_cfg, str):
            portrait_flag = portrait_cfg.strip().lower() in ("true", "1", "yes", "on", "portrait")
        else:
            portrait_flag = bool(portrait_cfg)
        self.portrait = portrait_flag or orientation == "portrait"

        raw_w = config.get("width")
        raw_h = config.get("height")
        if self.portrait:
            if raw_w is not None and raw_h is not None and int(raw_w) < int(raw_h):
                self.width = int(raw_w)
                self.height = int(raw_h)
            else:
                self.width = 576
                self.height = 960
        else:
            self.width = int(raw_w if raw_w is not None else 1024)
            self.height = int(raw_h if raw_h is not None else 768)
            if self.width < self.height and orientation != "landscape":
                self.portrait = True

        self.fps = int(config.get("fps", 20))
        loop_cfg = config.get("loop", -1)
        if isinstance(loop_cfg, bool):
            self.loop = 0 if loop_cfg else -1
        elif isinstance(loop_cfg, str):
            if loop_cfg.lower() in ("false", "once", "no", "none", "off"):
                self.loop = -1
            elif loop_cfg.lower() in ("true", "infinite", "forever", "always", "yes", "on"):
                self.loop = 0
            else:
                self.loop = int(loop_cfg)
        elif loop_cfg is None:
            self.loop = -1
        else:
            self.loop = int(loop_cfg)
        self.initial_delay = float(config.get("initial_delay", config.get("start_delay", 2.0)))
        self.window_cfg = config.get("window", {})
        self.audio_cfg = config.get("audio", {}) if isinstance(config.get("audio"), dict) else {}
        self.participants = config.get("participants", {})
        self.messages = config.get("messages", [])

        text_size = 19 if self.portrait else 21
        self.line_h = 26 if self.portrait else 29
        self.font_reg = ImageFont.truetype(FONT_PATH, text_size, index=0)
        self.font_bold = ImageFont.truetype(FONT_PATH, text_size, index=1)
        self.font_name = ImageFont.truetype(FONT_PATH, 15 if self.portrait else 16, index=10)
        self.font_time = ImageFont.truetype(FONT_PATH, 13 if self.portrait else 14, index=0)
        self.font_header_title = ImageFont.truetype(FONT_PATH, 18 if self.portrait else 20, index=1)
        self.font_header_sub = ImageFont.truetype(FONT_PATH, 13 if self.portrait else 14, index=0)
        self.font_avatar = ImageFont.truetype(FONT_PATH, 18 if self.portrait else 20, index=1)
        self.font_input = ImageFont.truetype(FONT_PATH, 16 if self.portrait else 17, index=0)
        self.font_typing_bar = ImageFont.truetype(FONT_PATH, 14 if self.portrait else 15, index=2)
        self.font_status = ImageFont.truetype(FONT_PATH, 14, index=1)
        self.font_emoji = ImageFont.truetype(EMOJI_FONT_PATH, 32)

        self._emoji_cache = {}

    def get_layout_metrics(self):
        win_x = 24 if self.portrait else 40
        win_y = 24 if self.portrait else 32
        win_w = self.width - 2 * win_x
        win_h = self.height - 2 * win_y
        win_r = 36 if self.portrait else (26 if self.theme == "light" else 20)
        status_h = 34 if self.portrait else 0
        header_h = (66 if self.portrait else 74) + status_h
        footer_h = 78 if self.portrait else 68
        pad_x = 18 if self.portrait else 28
        avatar_size = 38 if self.portrait else 44
        max_text_w = win_w - 2 * pad_x - avatar_size - (48 if self.portrait else 204)
        max_text_w = max(220, min(640, max_text_w))
        return {
            "win_x": win_x,
            "win_y": win_y,
            "win_w": win_w,
            "win_h": win_h,
            "win_r": win_r,
            "status_h": status_h,
            "header_h": header_h,
            "footer_h": footer_h,
            "pad_x": pad_x,
            "avatar_size": avatar_size,
            "max_text_w": max_text_w,
            "y_start": win_y + header_h + (18 if self.portrait else 22),
            "y_bottom_limit": win_y + win_h - footer_h - 18,
            "y_typing_bottom_limit": win_y + win_h - footer_h - 32,
        }

    def get_emoji_image(self, emoji_str, target_size=24):
        key = (emoji_str, target_size)
        if key in self._emoji_cache:
            return self._emoji_cache[key]

        canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)
        d.text((8, 8), emoji_str, font=self.font_emoji, embedded_color=True)
        bbox = canvas.getbbox()
        if bbox:
            cropped = canvas.crop(bbox)
            w, h = cropped.size
            scale = target_size / max(w, h)
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))
            img = cropped.resize((new_w, new_h), Image.Resampling.LANCZOS)
        else:
            img = canvas.resize((target_size, target_size), Image.Resampling.LANCZOS)

        self._emoji_cache[key] = img
        return img

    def measure_line(self, draw, line):
        total_w = 0.0
        idx = 0
        emoji_adv = 24.0 if self.portrait else 26.0
        while idx < len(line):
            ch = line[idx]
            if is_emoji_char(ch):
                total_w += emoji_adv
                idx += 1
                if idx < len(line) and ord(line[idx]) == 0xFE0F:
                    idx += 1
            else:
                start = idx
                while idx < len(line) and not is_emoji_char(line[idx]):
                    idx += 1
                total_w += draw.textlength(line[start:idx], font=self.font_reg)
        return total_w

    def wrap_text(self, draw, text, max_width):
        raw_words = text.split(" ")
        words = []
        for w in raw_words:
            if w in ("!", "?", ":", ";") and words:
                words[-1] = words[-1] + " " + w
            else:
                words.append(w)

        lines = []
        current = ""
        for w in words:
            test = (current + " " + w).strip()
            if self.measure_line(draw, test) <= max_width or not current:
                current = test
            else:
                lines.append(current)
                current = w
        if current:
            lines.append(current)
        return lines

    def get_participant_style(self, sender):
        p = self.participants.get(sender, {})
        if "side" in p:
            side = p["side"]
        else:
            seen_senders = []
            for m in self.messages:
                s = m.get("sender", "User")
                if s not in seen_senders:
                    seen_senders.append(s)
            side = "right" if (sender in seen_senders and seen_senders.index(sender) % 2 == 1) else "left"
        is_right = side.lower() == "right"
        default_sound = (
            self.audio_cfg.get("right_sound", "sent")
            if is_right
            else self.audio_cfg.get("left_sound", "received")
        )

        # Dark defaults for comparison
        dark_defaults = {
            "avatar_color": "#10b981" if is_right else "#6366f1",
            "name_color": "#6ee7b7" if is_right else "#a5b4fc",
            "bubble_color": "#2563eb" if is_right else "#26344a",
            "bubble_border_color": "#3b82f6" if is_right else "#3b4c69",
            "text_color": "#ffffff" if is_right else "#f8fafc",
        }

        if self.theme == "light":
            light_defaults = {
                "avatar_color": "#0ea5e9" if is_right else "#6366f1",
                "name_color": "#0284c7" if is_right else "#4f46e5",
                "bubble_color": "#4f46e5" if is_right else "#f1f5f9",
                "bubble_border_color": "#4338ca" if is_right else "#e2e8f0",
                "text_color": "#ffffff" if is_right else "#0f172a",
            }

            def resolve_color(key):
                val = p.get(key)
                if not val or val.lower() == dark_defaults[key].lower():
                    return light_defaults[key]
                return val

            return {
                "is_right": is_right,
                "avatar_letter": p.get("avatar_letter", sender[:1].upper() if sender else "?"),
                "avatar_color": resolve_color("avatar_color"),
                "name_color": resolve_color("name_color"),
                "bubble_color": resolve_color("bubble_color"),
                "bubble_border_color": resolve_color("bubble_border_color"),
                "text_color": resolve_color("text_color"),
                "sound": p.get("sound", default_sound),
            }

        return {
            "is_right": is_right,
            "avatar_letter": p.get("avatar_letter", sender[:1].upper() if sender else "?"),
            "avatar_color": p.get("avatar_color", dark_defaults["avatar_color"]),
            "name_color": p.get("name_color", dark_defaults["name_color"]),
            "bubble_color": p.get("bubble_color", dark_defaults["bubble_color"]),
            "bubble_border_color": p.get("bubble_border_color", dark_defaults["bubble_border_color"]),
            "text_color": p.get("text_color", dark_defaults["text_color"]),
            "sound": p.get("sound", default_sound),
        }

    def _draw_status_bar(self, wdraw, win_x, win_y, win_w, is_light):
        """Draw mobile portrait status bar (time, dynamic island, signal/battery)."""
        fg = (15, 23, 42, 230) if is_light else (248, 250, 252, 230)
        muted = (100, 116, 139, 180) if is_light else (148, 163, 184, 180)

        # Time on left
        wdraw.text((win_x + 28, win_y + 12), "9:41", font=self.font_status, fill=fg)

        # Dynamic island pill in center
        island_w, island_h = 86, 20
        ix1 = win_x + (win_w - island_w) // 2
        iy1 = win_y + 9
        wdraw.rounded_rectangle(
            [ix1, iy1, ix1 + island_w, iy1 + island_h],
            radius=10,
            fill=(15, 23, 42, 255) if is_light else (9, 13, 22, 255),
        )

        # Right status icons: signal bars + battery
        rx = win_x + win_w - 28
        by = win_y + 13
        # Battery body
        wdraw.rounded_rectangle(
            [rx - 22, by, rx - 3, by + 11],
            radius=3,
            outline=muted,
            width=1,
        )
        wdraw.rectangle([rx - 2, by + 3, rx, by + 8], fill=muted)
        wdraw.rounded_rectangle(
            [rx - 20, by + 2, rx - 6, by + 9],
            radius=1,
            fill=fg,
        )
        # Signal bars
        sx = rx - 46
        for b_idx in range(4):
            bh = 4 + b_idx * 2
            bx = sx + b_idx * 5
            wdraw.rectangle([bx, by + 11 - bh, bx + 3, by + 11], fill=fg)

    def create_base_window(self):
        m = self.get_layout_metrics()
        win_x, win_y = m["win_x"], m["win_y"]
        win_w, win_h = m["win_w"], m["win_h"]
        win_r = m["win_r"]
        status_h = m["status_h"]
        header_h = m["header_h"]
        footer_h = m["footer_h"]
        fy = win_y + win_h - footer_h
        is_light = self.theme == "light"

        bg = Image.new("RGBA", (self.width, self.height), (15, 23, 42, 255))
        draw = ImageDraw.Draw(bg)

        for y in range(self.height):
            t = y / self.height
            if is_light:
                r = int(238 - 18 * t)
                g = int(242 - 12 * t)
                b = int(255 - 5 * t)
            else:
                r = int(13 + 8 * t)
                g = int(19 + 12 * t)
                b = int(35 + 22 * t)
            draw.line([(0, y), (self.width, y)], fill=(r, g, b, 255))

        shadow = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow)
        sdraw.rounded_rectangle(
            [win_x + 2, win_y + 8, win_x + win_w + 2, win_y + win_h + 10],
            radius=win_r,
            fill=(15, 23, 42, 42) if is_light else (0, 0, 0, 140),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(16 if is_light else 14))
        bg = Image.alpha_composite(bg, shadow)

        win_canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        wdraw = ImageDraw.Draw(win_canvas)

        # Window body background
        body_fill = (255, 255, 255, 255) if is_light else (22, 30, 46, 255)
        header_fill = (248, 250, 252, 255) if is_light else (30, 41, 59, 255)
        divider_col = (226, 232, 240, 255) if is_light else (51, 65, 85, 255)

        wdraw.rectangle(
            [win_x, win_y, win_x + win_w, win_y + win_h],
            fill=body_fill,
        )

        wdraw.rectangle(
            [win_x, win_y, win_x + win_w, win_y + header_h],
            fill=header_fill,
        )
        wdraw.line(
            [(win_x, win_y + header_h), (win_x + win_w, win_y + header_h)],
            fill=divider_col,
            width=1,
        )

        if self.portrait:
            self._draw_status_bar(wdraw, win_x, win_y, win_w, is_light)
        else:
            # Desktop window controls
            tl_y = win_y + 24
            if is_light:
                # Subtle light macOS controls
                for i, col in enumerate([(252, 165, 165), (253, 224, 71), (134, 239, 172)]):
                    cx = win_x + 28 + i * 22
                    wdraw.ellipse(
                        [cx - 6, tl_y - 6, cx + 6, tl_y + 6],
                        fill=col,
                        outline=(203, 213, 225, 255),
                        width=1,
                    )
            else:
                for i, col in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
                    cx = win_x + 28 + i * 22
                    wdraw.ellipse([cx - 6, tl_y - 6, cx + 6, tl_y + 6], fill=col)

        # Header participant avatars
        p_names = list(self.participants.keys())
        if not p_names:
            seen = []
            for m in self.messages:
                s = m.get("sender", "User")
                if s not in seen:
                    seen.append(s)
            p_names = seen[:2]

        base_ax = (win_x + 42) if self.portrait else (win_x + 125)
        ay = win_y + status_h + (32 if self.portrait else 37)
        av_r = 17 if self.portrait else 19
        for idx, pname in enumerate(p_names[:2]):
            st = self.get_participant_style(pname)
            cx = base_ax + idx * (24 if self.portrait else 28)
            if idx > 0:
                wdraw.ellipse(
                    [cx - (av_r + 2), ay - (av_r + 2), cx + (av_r + 2), ay + (av_r + 2)],
                    fill=header_fill,
                )
            wdraw.ellipse(
                [cx - av_r, ay - av_r, cx + av_r, ay + av_r],
                fill=parse_hex_color(st["avatar_color"]),
            )
            letter = st["avatar_letter"]
            lw = wdraw.textlength(letter, font=self.font_header_title)
            wdraw.text(
                (cx - lw / 2, ay - (10 if self.portrait else 11)),
                letter,
                font=self.font_header_title,
                fill=(255, 255, 255, 255),
            )
            if idx == len(p_names[:2]) - 1:
                wdraw.ellipse(
                    [cx + 7, ay + 7, cx + 17, ay + 17],
                    fill=(16, 185, 129, 255) if is_light else (34, 197, 94, 255),
                    outline=header_fill,
                    width=2,
                )

        title = self.window_cfg.get("title", " & ".join(p_names))
        subtitle = self.window_cfg.get(
            "subtitle", f"Direct message • {len(p_names)} participants online"
        )
        if self.portrait:
            title_x = win_x + (94 if len(p_names) >= 2 else 70)
        else:
            title_x = win_x + (186 if len(p_names) >= 2 else 158)

        title_col = (15, 23, 42, 255) if is_light else (248, 250, 252, 255)
        sub_col = (100, 116, 139, 255) if is_light else (148, 163, 184, 255)
        wdraw.text(
            (title_x, win_y + status_h + (12 if self.portrait else 16)),
            title,
            font=self.font_header_title,
            fill=title_col,
        )
        wdraw.text(
            (title_x, win_y + status_h + (36 if self.portrait else 42)),
            subtitle,
            font=self.font_header_sub,
            fill=sub_col,
        )

        # Footer bar
        footer_fill = (248, 250, 252, 255) if is_light else (26, 35, 53, 255)
        wdraw.rectangle(
            [win_x, fy, win_x + win_w, win_y + win_h],
            fill=footer_fill,
        )
        wdraw.line(
            [(win_x, fy), (win_x + win_w, fy)],
            fill=divider_col,
            width=1,
        )

        pill_pad = 16 if self.portrait else 24
        pill_x1, pill_y1 = win_x + pill_pad, fy + 12
        pill_x2, pill_y2 = win_x + win_w - pill_pad, fy + 52
        pill_fill = (255, 255, 255, 255) if is_light else (15, 23, 42, 255)
        pill_outline = (203, 213, 225, 255) if is_light else (51, 65, 85, 255)
        wdraw.rounded_rectangle(
            [pill_x1, pill_y1, pill_x2, pill_y2],
            radius=20,
            fill=pill_fill,
            outline=pill_outline,
            width=1,
        )
        placeholder = self.window_cfg.get(
            "input_placeholder", "Type a message in the chat..."
        )
        wdraw.text(
            (pill_x1 + 18, pill_y1 + 9),
            placeholder,
            font=self.font_input,
            fill=(148, 163, 184, 255) if is_light else (100, 116, 139, 255),
        )

        sb_cx, sb_cy = pill_x2 - 22, (pill_y1 + pill_y2) // 2
        sb_col = (79, 70, 229, 255) if is_light else (37, 99, 235, 255)
        wdraw.ellipse(
            [sb_cx - 14, sb_cy - 14, sb_cx + 14, sb_cy + 14],
            fill=sb_col,
        )
        wdraw.polygon(
            [
                (sb_cx - 5, sb_cy - 6),
                (sb_cx + 7, sb_cy),
                (sb_cx - 5, sb_cy + 6),
                (sb_cx - 2, sb_cy),
            ],
            fill=(255, 255, 255, 255),
        )

        # Bottom home indicator bar in portrait mode
        if self.portrait:
            ind_w = 120
            ind_x1 = win_x + (win_w - ind_w) // 2
            ind_y1 = win_y + win_h - 11
            ind_col = (148, 163, 184, 180) if is_light else (100, 116, 139, 180)
            wdraw.rounded_rectangle(
                [ind_x1, ind_y1, ind_x1 + ind_w, ind_y1 + 5],
                radius=3,
                fill=ind_col,
            )

        win_mask = Image.new("L", (self.width, self.height), 0)
        mdraw = ImageDraw.Draw(win_mask)
        mdraw.rounded_rectangle(
            [win_x, win_y, win_x + win_w, win_y + win_h], radius=win_r, fill=255
        )

        clipped_win = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        clipped_win.paste(win_canvas, (0, 0), win_mask)
        bg = Image.alpha_composite(bg, clipped_win)

        draw = ImageDraw.Draw(bg)
        outer_border = (203, 213, 225, 255) if is_light else (64, 80, 104, 255)
        draw.rounded_rectangle(
            [win_x, win_y, win_x + win_w, win_y + win_h],
            radius=win_r,
            outline=outer_border,
            width=2,
        )
        return bg

    def draw_avatar(self, draw, x, y, letter, color_rgba, size=44):
        r = size // 2
        draw.ellipse([x, y, x + 2 * r, y + 2 * r], fill=color_rgba)
        w = draw.textlength(letter, font=self.font_avatar)
        draw.text(
            (x + r - w / 2, y + (8 if size < 42 else 10)),
            letter,
            font=self.font_avatar,
            fill=(255, 255, 255, color_rgba[3]),
        )

    def draw_rich_line(self, layer, draw, line, tx, ty, text_rgba, alpha, links, is_right=False):
        # Check if any link is in this line
        matched_link = None
        for lnk in links:
            if lnk and lnk in line:
                matched_link = lnk
                break

        if matched_link:
            prefix, suffix = line.split(matched_link, 1)
            cur_x = self.draw_rich_line(
                layer, draw, prefix, tx, ty, text_rgba, alpha, [], is_right=is_right
            )
            link_w = draw.textlength(matched_link, font=self.font_reg)
            if self.theme == "light" and not is_right:
                link_col = (37, 99, 235, int(255 * alpha))
            elif self.theme == "light" and is_right:
                link_col = (224, 242, 254, int(255 * alpha))
            else:
                link_col = (56, 189, 248, int(255 * alpha))
            draw.text((cur_x, ty), matched_link, font=self.font_reg, fill=link_col)
            underline_y = ty + (21 if self.portrait else 24)
            draw.line(
                [(cur_x, underline_y), (cur_x + link_w, underline_y)],
                fill=link_col,
                width=2,
            )
            cur_x += link_w
            if suffix:
                cur_x = self.draw_rich_line(
                    layer, draw, suffix, cur_x, ty, text_rgba, alpha, links, is_right=is_right
                )
            return cur_x

        cur_x = float(tx)
        idx = 0
        emoji_size = 22 if self.portrait else 24
        emoji_adv = 24.0 if self.portrait else 26.0
        while idx < len(line):
            ch = line[idx]
            if is_emoji_char(ch):
                emoji_str = ch
                idx += 1
                if idx < len(line) and ord(line[idx]) == 0xFE0F:
                    emoji_str += line[idx]
                    idx += 1
                emoji_img = self.get_emoji_image(emoji_str, target_size=emoji_size)
                ex = int(round(cur_x + 1))
                ey = int(round(ty + 1))
                if alpha >= 0.99:
                    layer.paste(emoji_img, (ex, ey), emoji_img)
                else:
                    faded = emoji_img.copy()
                    r, g, b, a = faded.split()
                    a = a.point(lambda p: int(p * alpha))
                    faded = Image.merge("RGBA", (r, g, b, a))
                    layer.paste(faded, (ex, ey), faded)
                cur_x += emoji_adv
            else:
                start = idx
                while idx < len(line) and not is_emoji_char(line[idx]):
                    idx += 1
                seg = line[start:idx]
                draw.text((cur_x, ty), seg, font=self.font_reg, fill=text_rgba)
                cur_x += draw.textlength(seg, font=self.font_reg)
        return cur_x

    def _draw_bubble_box(self, draw, box, bub_col, bdr_col, is_right):
        bx1, by1, bx2, by2 = box
        if self.theme == "light":
            r = 20
            draw.rounded_rectangle(
                [bx1, by1, bx2, by2],
                radius=r,
                fill=bub_col,
                outline=bdr_col,
                width=1,
                corners=(True, True, not is_right, is_right),
            )
        else:
            draw.rounded_rectangle(
                [bx1, by1, bx2, by2],
                radius=16,
                fill=bub_col,
                outline=bdr_col,
                width=1,
            )

    def _measure_message_height(self, draw, msg, show_header, max_text_w):
        lines = self.wrap_text(draw, msg.get("text", ""), max_text_w)
        pad_by = 12 if self.portrait else 13
        bubble_h = len(lines) * self.line_h + pad_by * 2 - 4
        header_offset = 24 if show_header else 0
        return header_offset + bubble_h

    def draw_message_bubble(
        self,
        img,
        msg,
        y_top,
        show_header=True,
        alpha=1.0,
        y_offset=0,
    ):
        sender = msg.get("sender", "User")
        time_str = msg.get("time", "")
        text = msg.get("text", "")
        st = self.get_participant_style(sender)
        is_right = st["is_right"]

        explicit_links = msg.get("links", [])
        auto_links = LINK_REGEX.findall(text)
        links = list(dict.fromkeys(explicit_links + auto_links))

        layer = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        m = self.get_layout_metrics()
        win_x = m["win_x"]
        win_w = m["win_w"]
        pad_x = m["pad_x"]
        avatar_size = m["avatar_size"]
        max_text_w = m["max_text_w"]

        lines = self.wrap_text(draw, text, max_text_w)
        line_h = self.line_h
        pad_bx = 16 if self.portrait else 18
        pad_by = 12 if self.portrait else 13

        max_line_w = max((self.measure_line(draw, ln) for ln in lines), default=40)
        bubble_w = int(math.ceil(max_line_w)) + pad_bx * 2
        bubble_h = len(lines) * line_h + pad_by * 2 - 4

        y_cur = int(y_top + y_offset)
        header_offset = 24 if show_header else 0

        av_col = parse_hex_color(st["avatar_color"], 255 * alpha)
        name_col = parse_hex_color(st["name_color"], 255 * alpha)
        bub_col = parse_hex_color(st["bubble_color"], 255 * alpha)
        bdr_col = parse_hex_color(st["bubble_border_color"], 255 * alpha)
        txt_col = parse_hex_color(st["text_color"], 255 * alpha)
        time_col = (100, 116, 139, int(255 * alpha))

        if not is_right:
            av_x = win_x + pad_x
            av_y = y_cur + header_offset
            if show_header:
                self.draw_avatar(draw, av_x, av_y, st["avatar_letter"], av_col, size=avatar_size)
                draw.text(
                    (av_x + avatar_size + 12, y_cur + 2),
                    sender,
                    font=self.font_name,
                    fill=name_col,
                )
                sw = draw.textlength(sender, font=self.font_name)
                draw.text(
                    (av_x + avatar_size + 12 + sw + 10, y_cur + 4),
                    time_str,
                    font=self.font_time,
                    fill=time_col,
                )

            bx1 = av_x + avatar_size + 10
            by1 = y_cur + header_offset
            bx2 = bx1 + bubble_w
            by2 = by1 + bubble_h
        else:
            av_x = win_x + win_w - pad_x - avatar_size
            av_y = y_cur + header_offset
            if show_header:
                self.draw_avatar(draw, av_x, av_y, st["avatar_letter"], av_col, size=avatar_size)
                tw = draw.textlength(time_str, font=self.font_time)
                sw = draw.textlength(sender, font=self.font_name)
                right_edge = av_x - 12
                draw.text(
                    (right_edge - tw, y_cur + 4),
                    time_str,
                    font=self.font_time,
                    fill=time_col,
                )
                draw.text(
                    (right_edge - tw - 10 - sw, y_cur + 2),
                    sender,
                    font=self.font_name,
                    fill=name_col,
                )

            bx2 = av_x - 10
            bx1 = bx2 - bubble_w
            by1 = y_cur + header_offset
            by2 = by1 + bubble_h

        self._draw_bubble_box(draw, (bx1, by1, bx2, by2), bub_col, bdr_col, is_right)

        for idx, line in enumerate(lines):
            ty = by1 + pad_by + idx * line_h
            tx = bx1 + pad_bx
            self.draw_rich_line(layer, draw, line, tx, ty, txt_col, alpha, links, is_right=is_right)

        img.alpha_composite(layer)
        return y_top + header_offset + bubble_h

    def draw_typing_indicator(self, img, sender, y_top, phase_t, show_header=True, status_img=None):
        st = self.get_participant_style(sender)
        is_right = st["is_right"]

        layer = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        m = self.get_layout_metrics()
        win_x = m["win_x"]
        win_w = m["win_w"]
        pad_x = m["pad_x"]
        avatar_size = m["avatar_size"]

        header_offset = 24 if show_header else 0
        bubble_w = 88
        bubble_h = 42 if self.portrait else 44

        av_col = parse_hex_color(st["avatar_color"], 255)
        name_col = parse_hex_color(st["name_color"], 255)
        bub_col = parse_hex_color(st["bubble_color"], 255)
        bdr_col = parse_hex_color(st["bubble_border_color"], 255)

        label_tmpl = self.window_cfg.get("typing_bubble_label_template", "{sender} is typing...")
        label = label_tmpl.format(sender=sender)

        if not is_right:
            av_x = win_x + pad_x
            av_y = y_top + header_offset
            if show_header:
                self.draw_avatar(draw, av_x, av_y, st["avatar_letter"], av_col, size=avatar_size)
                draw.text(
                    (av_x + avatar_size + 12, y_top + 2),
                    label,
                    font=self.font_name,
                    fill=name_col,
                )
            bx1 = av_x + avatar_size + 10
            by1 = y_top + header_offset
            bx2 = bx1 + bubble_w
            by2 = by1 + bubble_h
        else:
            av_x = win_x + win_w - pad_x - avatar_size
            av_y = y_top + header_offset
            if show_header:
                self.draw_avatar(draw, av_x, av_y, st["avatar_letter"], av_col, size=avatar_size)
                lw = draw.textlength(label, font=self.font_name)
                draw.text(
                    (av_x - 12 - lw, y_top + 2),
                    label,
                    font=self.font_name,
                    fill=name_col,
                )
            bx2 = av_x - 10
            bx1 = bx2 - bubble_w
            by1 = y_top + header_offset
            by2 = by1 + bubble_h

        self._draw_bubble_box(draw, (bx1, by1, bx2, by2), bub_col, bdr_col, is_right)

        center_y = (by1 + by2) / 2
        start_x = bx1 + 24
        spacing = 20
        for i in range(3):
            angle = phase_t * 2.0 * math.pi * 1.8 - i * 0.85
            bounce = max(0.0, math.sin(angle))
            dy = -5.5 * bounce
            r = 5.0 + 1.0 * bounce
            alpha_dot = int(150 + 105 * bounce)
            cx = start_x + i * spacing
            cy = center_y + dy
            if self.theme == "light" and not is_right:
                dot_col = (100, 116, 139, alpha_dot)
            elif is_right:
                dot_col = (248, 250, 252, alpha_dot)
            else:
                dot_col = (148, 163, 184, alpha_dot)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=dot_col)

        status_tmpl = self.window_cfg.get(
            "typing_status_template", "{sender} is typing..."
        )
        status_text = status_tmpl.format(sender=sender)
        status_col = (100, 116, 139, 220) if self.theme == "light" else (148, 163, 184, 220)
        status_draw = ImageDraw.Draw(status_img) if status_img is not None else draw
        status_draw.text(
            (win_x + (22 if self.portrait else 32), m["win_y"] + m["win_h"] - m["footer_h"] - 24),
            status_text,
            font=self.font_typing_bar,
            fill=status_col,
        )

        img.alpha_composite(layer)

    def build_timeline(self):
        """Construct timeline segments for each message."""
        timeline = []
        t_cur = self.initial_delay
        prev_sender = None

        for idx, msg in enumerate(self.messages):
            sender = msg.get("sender", "User")
            default_typing = 0.0 if idx == 0 else 2.5
            default_hold = 7.5 if idx == len(self.messages) - 1 else 3.5
            typing_dur = float(msg.get("typing_duration", default_typing))
            pop_dur = float(msg.get("pop_in_duration", 0.25))
            hold_dur = float(msg.get("hold_duration", default_hold))

            if "show_header" in msg:
                show_header = bool(msg["show_header"])
            else:
                show_header = sender != prev_sender

            typing_start = t_cur
            typing_end = typing_start + typing_dur
            msg_start = typing_end
            msg_ready = msg_start + pop_dur
            msg_end = msg_ready + hold_dur

            timeline.append(
                {
                    "msg": msg,
                    "sender": sender,
                    "show_header": show_header,
                    "typing_start": typing_start,
                    "typing_end": typing_end,
                    "msg_start": msg_start,
                    "pop_dur": pop_dur,
                    "msg_end": msg_end,
                }
            )
            t_cur = msg_end
            prev_sender = sender

        return timeline, t_cur

    def render_frame(self, t, base_win, timeline):
        frame = base_win.copy()
        m = self.get_layout_metrics()
        dummy_draw = ImageDraw.Draw(frame)

        # First pass: measure total vertical extent at time t to auto-scroll if needed
        y_measure = m["y_start"]
        max_y_extent = y_measure
        is_typing = False
        for idx, item in enumerate(timeline):
            if t < item["typing_start"]:
                break
            if item["typing_start"] <= t < item["typing_end"]:
                header_offset = 24 if item["show_header"] else 0
                bubble_h = 42 if self.portrait else 44
                max_y_extent = y_measure + header_offset + bubble_h
                is_typing = True
                break
            msg_h = self._measure_message_height(
                dummy_draw, item["msg"], item["show_header"], m["max_text_w"]
            )
            y_after = y_measure + msg_h
            max_y_extent = y_after
            next_same_sender = (
                idx + 1 < len(timeline)
                and timeline[idx + 1]["sender"] == item["sender"]
                and not timeline[idx + 1]["show_header"]
            )
            y_measure = y_after + ((8 if self.portrait else 10) if next_same_sender else (18 if self.portrait else 22))

        active_limit = m["y_typing_bottom_limit"] if is_typing else m["y_bottom_limit"]
        scroll_offset = max(0, max_y_extent - active_limit)
        y_cursor = m["y_start"] - scroll_offset

        msg_layer = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        status_layer = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))

        for idx, item in enumerate(timeline):
            if t < item["typing_start"]:
                break

            if item["typing_start"] <= t < item["typing_end"]:
                self.draw_typing_indicator(
                    msg_layer,
                    item["sender"],
                    y_cursor,
                    t - item["typing_start"],
                    show_header=item["show_header"],
                    status_img=status_layer,
                )
                break

            # Message is visible
            pop_dur = max(0.01, item["pop_dur"])
            alpha = min(1.0, max(0.15, (t - item["msg_start"]) / pop_dur))
            y_offset = int((1.0 - alpha) * 10)
            y_after = self.draw_message_bubble(
                msg_layer,
                item["msg"],
                y_cursor,
                show_header=item["show_header"],
                alpha=alpha,
                y_offset=y_offset,
            )

            # Determine spacing before next message
            next_same_sender = (
                idx + 1 < len(timeline)
                and timeline[idx + 1]["sender"] == item["sender"]
                and not timeline[idx + 1]["show_header"]
            )
            y_cursor = y_after + ((8 if self.portrait else 10) if next_same_sender else (18 if self.portrait else 22))

        # Clip message bubbles strictly to the chat viewport between header and footer
        clip_mask = Image.new("L", (self.width, self.height), 0)
        cdraw = ImageDraw.Draw(clip_mask)
        cdraw.rectangle(
            [
                m["win_x"] + 2,
                m["win_y"] + m["header_h"] + 1,
                m["win_x"] + m["win_w"] - 2,
                m["win_y"] + m["win_h"] - m["footer_h"] - 1,
            ],
            fill=255,
        )
        clipped_msgs = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        clipped_msgs.paste(msg_layer, (0, 0), clip_mask)
        frame = Image.alpha_composite(frame, clipped_msgs)
        frame = Image.alpha_composite(frame, status_layer)

        return frame

    def _load_external_sound(self, sound_path, sample_rate=44100):
        """Decode an external audio file into mono float samples [-1.0, 1.0]."""
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(sound_path),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "pipe:1",
        ]
        res = subprocess.run(
            cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        raw = res.stdout
        count = len(raw) // 2
        ints = struct.unpack(f"<{count}h", raw[: count * 2])
        return [s / 32768.0 for s in ints]

    def _synthesize_sound(self, sound_name, sample_rate=44100):
        """Synthesize a crisp UI message pop/notification sound in pure Python."""
        if not sound_name or str(sound_name).lower() in ("none", "false", "off", "silent"):
            return []

        name = str(sound_name).strip()
        # Support custom file paths or macOS system sounds
        candidate_paths = [
            Path(name),
            Path(f"/System/Library/Sounds/{name}.aiff"),
            Path(f"/System/Library/Sounds/{name.capitalize()}.aiff"),
        ]
        for cpath in candidate_paths:
            if cpath.is_file():
                return self._load_external_sound(cpath, sample_rate=sample_rate)

        preset = name.lower()
        if preset in ("received", "incoming", "left"):
            # Warm incoming chat bubble pop + two-note rising chime (D5 -> A5)
            duration = 0.34
            n_samples = int(duration * sample_rate)
            samples = [0.0] * n_samples
            phase_pop = 0.0
            for i in range(n_samples):
                t = i / sample_rate
                val = 0.0
                # Soft organic bubble pop transient (0 -> 65ms, pitch sweep 290Hz -> 640Hz)
                if t < 0.065:
                    freq_pop = 290.0 + 350.0 * ((t / 0.065) ** 0.65)
                    phase_pop += 2.0 * math.pi * freq_pop / sample_rate
                    env_pop = math.sin(math.pi * t / 0.065) * math.exp(-16.0 * t)
                    val += 0.42 * env_pop * math.sin(phase_pop)

                # First warm note: D5 (587.33 Hz)
                if t < 0.18:
                    attack1 = min(1.0, t / 0.005)
                    env1 = attack1 * math.exp(-18.0 * t)
                    f1 = 587.33
                    val += 0.45 * env1 * (
                        math.sin(2.0 * math.pi * f1 * t)
                        + 0.22 * math.sin(2.0 * math.pi * (f1 * 2.0) * t)
                    )

                # Second warm note: A5 (880.0 Hz) starting at 55ms
                if t >= 0.055:
                    lt = t - 0.055
                    attack2 = min(1.0, lt / 0.005)
                    env2 = attack2 * math.exp(-13.5 * lt)
                    f2 = 880.0
                    val += 0.52 * env2 * (
                        math.sin(2.0 * math.pi * f2 * t)
                        + 0.18 * math.sin(2.0 * math.pi * (f2 * 2.0) * t)
                    )

                samples[i] = max(-1.0, min(1.0, val))
            return samples

        if preset in ("sent", "outgoing", "right"):
            # Crisp sent-message upward pitch-glide pop (C5 -> C6 with G6 sparkle)
            duration = 0.24
            n_samples = int(duration * sample_rate)
            samples = [0.0] * n_samples
            phase = 0.0
            for i in range(n_samples):
                t = i / sample_rate
                # Smooth exponential pitch glide from 523.25 Hz (C5) to 1046.50 Hz (C6) over 70ms
                glide = min(1.0, t / 0.070)
                freq = 523.25 * (2.0 ** (glide ** 0.75))
                phase += 2.0 * math.pi * freq / sample_rate
                attack = min(1.0, t / 0.004)
                env = attack * math.exp(-17.5 * t)
                val = 0.58 * env * (math.sin(phase) + 0.20 * math.sin(2.0 * phase))

                # Subtle high harmonic sparkle at 40ms (G6 = 1567.98 Hz)
                if t >= 0.040:
                    lt = t - 0.040
                    s_attack = min(1.0, lt / 0.004)
                    s_env = s_attack * math.exp(-24.0 * lt)
                    val += 0.24 * s_env * math.sin(2.0 * math.pi * 1567.98 * lt)

                samples[i] = max(-1.0, min(1.0, val))
            return samples

        if preset == "chime":
            # Gentle two-tone chime (E5 -> B5)
            duration = 0.36
            n_samples = int(duration * sample_rate)
            samples = [0.0] * n_samples
            for i in range(n_samples):
                t = i / sample_rate
                val = 0.0
                if t < 0.20:
                    env1 = min(1.0, t / 0.006) * math.exp(-15.0 * t)
                    val += 0.48 * env1 * math.sin(2.0 * math.pi * 659.25 * t)
                if t >= 0.065:
                    lt = t - 0.065
                    env2 = min(1.0, lt / 0.006) * math.exp(-11.5 * lt)
                    val += 0.52 * env2 * math.sin(2.0 * math.pi * 987.77 * lt)
                samples[i] = max(-1.0, min(1.0, val))
            return samples

        if preset == "ding":
            # Single resonant bell tone (G5 = 783.99 Hz)
            duration = 0.32
            n_samples = int(duration * sample_rate)
            samples = [0.0] * n_samples
            for i in range(n_samples):
                t = i / sample_rate
                env = min(1.0, t / 0.004) * math.exp(-13.0 * t)
                val = 0.60 * env * (
                    math.sin(2.0 * math.pi * 783.99 * t)
                    + 0.25 * math.exp(-25.0 * t) * math.sin(2.0 * math.pi * 1567.98 * t)
                )
                samples[i] = max(-1.0, min(1.0, val))
            return samples

        # Default "pop": classic crisp chat bubble pop (pitch sweep 310Hz -> 780Hz)
        duration = 0.14
        n_samples = int(duration * sample_rate)
        samples = [0.0] * n_samples
        phase = 0.0
        for i in range(n_samples):
            t = i / sample_rate
            sweep = min(1.0, t / 0.055)
            freq = 310.0 + 470.0 * (sweep ** 0.6)
            phase += 2.0 * math.pi * freq / sample_rate
            env = min(1.0, t / 0.003) * math.exp(-24.0 * t)
            samples[i] = max(-1.0, min(1.0, 0.68 * env * math.sin(phase)))
        return samples

    def _synthesize_keystroke(self, pitch_scale=1.0, sample_rate=44100):
        """Synthesize a subtle tactile typing tick."""
        duration = 0.025
        n_samples = int(duration * sample_rate)
        samples = [0.0] * n_samples
        f_click = 1100.0 * pitch_scale
        f_body = 320.0 * pitch_scale
        for i in range(n_samples):
            t = i / sample_rate
            env = min(1.0, t / 0.001) * math.exp(-160.0 * t)
            val = 0.18 * env * (
                0.6 * math.sin(2.0 * math.pi * f_click * t)
                + 0.4 * math.sin(2.0 * math.pi * f_body * t)
            )
            samples[i] = val
        return samples

    def _write_audio_wav(self, wav_path, timeline=None, total_duration=None):
        """Synthesize the stereo audio track and write it to a WAV file."""
        if timeline is None or total_duration is None:
            timeline, total_duration = self.build_timeline()

        total_frames = max(1, int(round(total_duration * self.fps)))
        exact_duration = total_frames / self.fps
        sample_rate = 44100
        total_samples = max(1, int(round(exact_duration * sample_rate)))

        left_buf = [0.0] * total_samples
        right_buf = [0.0] * total_samples

        master_volume = float(self.audio_cfg.get("volume", 0.85))
        enable_typing = bool(self.audio_cfg.get("typing_sound", False))
        sound_cache = {}

        for idx, item in enumerate(timeline):
            msg = item["msg"]
            sender = item["sender"]
            st = self.get_participant_style(sender)
            is_right = st["is_right"]

            # Stereo placement: subtle left for left-side sender, subtle right for right-side sender
            pan = 0.22 if is_right else -0.22
            gain_l = master_volume * min(1.0, 1.0 - pan)
            gain_r = master_volume * min(1.0, 1.0 + pan)

            # Optional typing clicks during typing indicator phase
            if enable_typing and item["typing_end"] > item["typing_start"]:
                t_cursor = item["typing_start"] + 0.08
                step_idx = 0
                while t_cursor < item["typing_end"] - 0.08:
                    pitch_var = 0.94 + 0.04 * ((step_idx * 7 + idx * 3) % 4)
                    tick = self._synthesize_keystroke(pitch_scale=pitch_var, sample_rate=sample_rate)
                    start_sample = int(round(t_cursor * sample_rate))
                    for k, s_val in enumerate(tick):
                        pos = start_sample + k
                        if 0 <= pos < total_samples:
                            left_buf[pos] += s_val * gain_l
                            right_buf[pos] += s_val * gain_r
                    interval = 0.11 + 0.03 * ((step_idx * 5 + idx) % 3)
                    t_cursor += interval
                    step_idx += 1

            # Message pop-up sound at msg_start
            sound_spec = msg.get("sound", st["sound"])
            if sound_spec not in sound_cache:
                sound_cache[sound_spec] = self._synthesize_sound(
                    sound_spec, sample_rate=sample_rate
                )
            pop_samples = sound_cache[sound_spec]

            msg_volume = float(msg.get("volume", 1.0))
            start_idx = int(round(item["msg_start"] * sample_rate))
            for k, s_val in enumerate(pop_samples):
                pos = start_idx + k
                if 0 <= pos < total_samples:
                    left_buf[pos] += s_val * gain_l * msg_volume
                    right_buf[pos] += s_val * gain_r * msg_volume

        pcm_bytes = bytearray(total_samples * 4)
        for i in range(total_samples):
            l_int = int(max(-1.0, min(1.0, left_buf[i])) * 32767.0)
            r_int = int(max(-1.0, min(1.0, right_buf[i])) * 32767.0)
            struct.pack_into("<hh", pcm_bytes, i * 4, l_int, r_int)

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)

        return exact_duration

    def generate_audio(self, audio_output_path, timeline=None, total_duration=None, wav_path=None):
        """Generate an MP3 audio file with message pop-up sounds synchronized to the timeline."""
        if wav_path is not None:
            exact_duration = max(1, int(round((total_duration or 0.0) * self.fps))) / self.fps
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(wav_path),
                "-codec:a",
                "libmp3lame",
                "-qscale:a",
                "2",
                str(audio_output_path),
            ]
            subprocess.run(
                cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            return audio_output_path, exact_duration

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_wav = os.path.join(tmp_dir, "track.wav")
            exact_duration = self._write_audio_wav(
                tmp_wav, timeline=timeline, total_duration=total_duration
            )
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                tmp_wav,
                "-codec:a",
                "libmp3lame",
                "-qscale:a",
                "2",
                str(audio_output_path),
            ]
            subprocess.run(
                cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )

        return audio_output_path, exact_duration

    def generate(self, output_path, audio_output_path=None, video_output_path=None, video_with_audio=True):
        base_win = self.create_base_window()
        timeline, total_duration = self.build_timeline()
        total_frames = max(1, int(round(total_duration * self.fps)))

        with tempfile.TemporaryDirectory() as tmp_dir:
            frames_pattern = os.path.join(tmp_dir, "frame_%04d.png")
            for i in range(total_frames):
                t = i / self.fps
                img = self.render_frame(t, base_win, timeline)
                img.convert("RGB").save(os.path.join(tmp_dir, f"frame_{i:04d}.png"))

            cmd = [
                "ffmpeg",
                "-y",
                "-framerate",
                str(self.fps),
                "-i",
                frames_pattern,
                "-vf",
                "split[s0][s1];[s0]palettegen=stats_mode=full:max_colors=256[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                "-loop",
                str(self.loop),
                str(output_path),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            wav_path = None
            if audio_output_path is not None or (video_output_path is not None and video_with_audio):
                wav_path = os.path.join(tmp_dir, "track.wav")
                self._write_audio_wav(wav_path, timeline=timeline, total_duration=total_duration)

            if audio_output_path is not None:
                self.generate_audio(
                    audio_output_path,
                    timeline=timeline,
                    total_duration=total_duration,
                    wav_path=wav_path,
                )

            if video_output_path is not None:
                v_cmd = [
                    "ffmpeg",
                    "-y",
                    "-framerate",
                    str(self.fps),
                    "-i",
                    frames_pattern,
                ]
                if wav_path is not None and video_with_audio:
                    v_cmd.extend(["-i", wav_path])
                v_cmd.extend([
                    "-vf",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    "-c:v",
                    "libx264",
                    "-tune",
                    "animation",
                    "-preset",
                    "veryslow",
                    "-crf",
                    "28",
                    "-g",
                    "1000",
                    "-pix_fmt",
                    "yuv420p",
                ])
                if wav_path is not None and video_with_audio:
                    v_cmd.extend(["-c:a", "aac", "-b:a", "48k", "-shortest"])
                v_cmd.extend(["-movflags", "+faststart", str(video_output_path)])
                subprocess.run(v_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        return output_path, total_frames, total_duration


def load_config(config_path):
    path = Path(config_path)
    suffix = path.suffix.lower()
    if suffix == ".toml":
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    elif suffix in (".yaml", ".yml"):
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    else:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def main():
    script_dir = Path(__file__).resolve().parent
    default_config = script_dir / "chat-config.json"

    parser = argparse.ArgumentParser(
        description="Generate an animated chat GIF, companion MP3 audio file, and optional MP4 video from a configuration file."
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=str(default_config),
        help="Path to the JSON/TOML/YAML configuration file (default: chat-config.json)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Override output GIF path from configuration file",
    )
    parser.add_argument(
        "-a",
        "--audio-output",
        help="Override output MP3 path from configuration file (default: matches output GIF basename with .mp3)",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Disable MP3 audio generation",
    )
    parser.add_argument(
        "-v",
        "--video",
        nargs="?",
        const=True,
        default=None,
        metavar="VIDEO_OUTPUT",
        help="Generate a compact MP4 video with synchronized sound (optionally specify output .mp4 path)",
    )
    parser.add_argument(
        "--no-video",
        action="store_true",
        help="Disable MP4 video generation if enabled in the configuration file",
    )
    parser.add_argument(
        "-t",
        "--theme",
        choices=["dark", "light"],
        default=None,
        help="Visual look and feel: 'dark' (default) or 'light'",
    )
    orient_group = parser.add_mutually_exclusive_group()
    orient_group.add_argument(
        "-p",
        "--portrait",
        action="store_true",
        help="Render in portrait mode (vertical mobile layout, default 576x960)",
    )
    orient_group.add_argument(
        "--landscape",
        action="store_true",
        help="Render in landscape mode (desktop window layout, default 1024x768)",
    )
    parser.add_argument(
        "--loop",
        type=int,
        default=None,
        help="Number of loops (-1 for play once, 0 for infinite, default: -1)",
    )
    parser.add_argument(
        "--initial-delay",
        type=float,
        default=None,
        help="Initial delay in seconds before messages start (default: 2.0)",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = load_config(config_path)

    if args.theme is not None:
        config["theme"] = args.theme
    if args.portrait:
        config["portrait"] = True
        config["orientation"] = "portrait"
    elif args.landscape:
        config["portrait"] = False
        config["orientation"] = "landscape"
    if args.loop is not None:
        config["loop"] = args.loop
    if args.initial_delay is not None:
        config["initial_delay"] = args.initial_delay

    ts = datetime.now().strftime("%H%M%S")
    if args.output:
        out_path = Path(args.output).resolve()
    else:
        base_out = Path(config.get("output", "chat.gif"))
        out_path = base_out.with_name(f"{base_out.stem}-{ts}{base_out.suffix or '.gif'}")
        if not out_path.is_absolute():
            out_path = config_path.parent / out_path

    audio_cfg = config.get("audio", {})
    audio_enabled = not args.no_audio and (
        audio_cfg.get("enabled", True) if isinstance(audio_cfg, dict) else bool(audio_cfg)
    )

    audio_path = None
    if audio_enabled:
        if args.audio_output:
            audio_path = Path(args.audio_output).resolve()
        elif args.output:
            audio_path = out_path.with_suffix(".mp3")
        else:
            raw_audio = (
                config.get("audio_output")
                or (audio_cfg.get("output") if isinstance(audio_cfg, dict) else None)
            )
            if raw_audio:
                base_audio = Path(raw_audio)
                audio_path = base_audio.with_name(
                    f"{base_audio.stem}-{ts}{base_audio.suffix or '.mp3'}"
                )
                if not audio_path.is_absolute():
                    audio_path = config_path.parent / audio_path
            else:
                audio_path = out_path.with_suffix(".mp3")

    video_cfg = config.get("video", False)
    video_enabled = not args.no_video and (
        args.video is not None
        or bool(config.get("video_output"))
        or (video_cfg.get("enabled", False) if isinstance(video_cfg, dict) else bool(video_cfg))
    )

    video_path = None
    if video_enabled:
        if isinstance(args.video, str):
            video_path = Path(args.video).resolve()
        elif args.output:
            video_path = out_path.with_suffix(".mp4")
        else:
            raw_video = (
                config.get("video_output")
                or (video_cfg.get("output") if isinstance(video_cfg, dict) else None)
                or (video_cfg if isinstance(video_cfg, str) else None)
            )
            if raw_video:
                base_video = Path(raw_video)
                video_path = base_video.with_name(
                    f"{base_video.stem}-{ts}{base_video.suffix or '.mp4'}"
                )
                if not video_path.is_absolute():
                    video_path = config_path.parent / video_path
            else:
                video_path = out_path.with_suffix(".mp4")

    generator = ChatGifGenerator(config)
    final_path, frames, duration = generator.generate(
        out_path,
        audio_output_path=audio_path,
        video_output_path=video_path,
        video_with_audio=audio_enabled,
    )
    size_kb = os.path.getsize(final_path) / 1024.0
    mode_label = "portrait" if generator.portrait else "landscape"
    print(
        f"Generated {final_path} ({generator.width}x{generator.height} {mode_label}, theme={generator.theme}, {frames} frames, {duration:.1f}s, {size_kb:.1f} KB)"
    )
    if audio_path is not None and audio_path.exists():
        audio_kb = os.path.getsize(audio_path) / 1024.0
        print(f"Generated {audio_path} ({duration:.1f}s, {audio_kb:.1f} KB)")
    if video_path is not None and video_path.exists():
        video_kb = os.path.getsize(video_path) / 1024.0
        print(f"Generated {video_path} ({duration:.1f}s, {video_kb:.1f} KB)")


if __name__ == "__main__":
    main()
