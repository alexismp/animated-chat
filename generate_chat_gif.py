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
        self.width = int(config.get("width", 1024))
        self.height = int(config.get("height", 768))
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

        self.font_reg = ImageFont.truetype(FONT_PATH, 21, index=0)
        self.font_bold = ImageFont.truetype(FONT_PATH, 21, index=1)
        self.font_name = ImageFont.truetype(FONT_PATH, 16, index=10)
        self.font_time = ImageFont.truetype(FONT_PATH, 14, index=0)
        self.font_header_title = ImageFont.truetype(FONT_PATH, 20, index=1)
        self.font_header_sub = ImageFont.truetype(FONT_PATH, 14, index=0)
        self.font_avatar = ImageFont.truetype(FONT_PATH, 20, index=1)
        self.font_input = ImageFont.truetype(FONT_PATH, 17, index=0)
        self.font_typing_bar = ImageFont.truetype(FONT_PATH, 15, index=2)
        self.font_emoji = ImageFont.truetype(EMOJI_FONT_PATH, 32)

        self._emoji_cache = {}

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
        while idx < len(line):
            ch = line[idx]
            if is_emoji_char(ch):
                total_w += 26.0
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
        side = p.get("side", "left")
        is_right = side.lower() == "right"
        default_avatar = "#10b981" if is_right else "#6366f1"
        default_name = "#6ee7b7" if is_right else "#a5b4fc"
        default_bubble = "#2563eb" if is_right else "#26344a"
        default_border = "#3b82f6" if is_right else "#3b4c69"
        default_sound = (
            self.audio_cfg.get("right_sound", "sent")
            if is_right
            else self.audio_cfg.get("left_sound", "received")
        )
        return {
            "is_right": is_right,
            "avatar_letter": p.get("avatar_letter", sender[:1].upper() if sender else "?"),
            "avatar_color": p.get("avatar_color", default_avatar),
            "name_color": p.get("name_color", default_name),
            "bubble_color": p.get("bubble_color", default_bubble),
            "bubble_border_color": p.get("bubble_border_color", default_border),
            "text_color": p.get("text_color", "#f8fafc"),
            "sound": p.get("sound", default_sound),
        }

    def create_base_window(self):
        bg = Image.new("RGBA", (self.width, self.height), (15, 23, 42, 255))
        draw = ImageDraw.Draw(bg)

        for y in range(self.height):
            t = y / self.height
            r = int(13 + 8 * t)
            g = int(19 + 12 * t)
            b = int(35 + 22 * t)
            draw.line([(0, y), (self.width, y)], fill=(r, g, b, 255))

        win_x, win_y = 40, 32
        win_w, win_h = self.width - 80, self.height - 64
        win_r = 20

        shadow = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow)
        sdraw.rounded_rectangle(
            [win_x + 4, win_y + 10, win_x + win_w + 4, win_y + win_h + 10],
            radius=win_r,
            fill=(0, 0, 0, 140),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(14))
        bg = Image.alpha_composite(bg, shadow)

        win_canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        wdraw = ImageDraw.Draw(win_canvas)

        wdraw.rectangle(
            [win_x, win_y, win_x + win_w, win_y + win_h],
            fill=(22, 30, 46, 255),
        )

        header_h = 74
        footer_h = 68
        fy = win_y + win_h - footer_h

        wdraw.rectangle(
            [win_x, win_y, win_x + win_w, win_y + header_h],
            fill=(30, 41, 59, 255),
        )
        wdraw.line(
            [(win_x, win_y + header_h), (win_x + win_w, win_y + header_h)],
            fill=(51, 65, 85, 255),
            width=1,
        )

        # macOS traffic lights
        tl_y = win_y + 24
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

        base_ax = win_x + 125
        ay = win_y + 37
        for idx, pname in enumerate(p_names[:2]):
            st = self.get_participant_style(pname)
            cx = base_ax + idx * 28
            if idx > 0:
                wdraw.ellipse(
                    [cx - 21, ay - 21, cx + 21, ay + 21],
                    fill=(30, 41, 59, 255),
                )
            wdraw.ellipse(
                [cx - 19, ay - 19, cx + 19, ay + 19],
                fill=parse_hex_color(st["avatar_color"]),
            )
            letter = st["avatar_letter"]
            lw = wdraw.textlength(letter, font=self.font_header_title)
            wdraw.text(
                (cx - lw / 2, ay - 11),
                letter,
                font=self.font_header_title,
                fill=(255, 255, 255, 255),
            )
            if idx == len(p_names[:2]) - 1:
                wdraw.ellipse(
                    [cx + 8, ay + 8, cx + 18, ay + 18],
                    fill=(34, 197, 94, 255),
                    outline=(30, 41, 59, 255),
                    width=2,
                )

        title = self.window_cfg.get("title", " & ".join(p_names))
        subtitle = self.window_cfg.get(
            "subtitle", f"Direct message • {len(p_names)} participants online"
        )
        title_x = win_x + 186 if len(p_names) >= 2 else win_x + 158
        wdraw.text(
            (title_x, win_y + 16),
            title,
            font=self.font_header_title,
            fill=(248, 250, 252, 255),
        )
        wdraw.text(
            (title_x, win_y + 42),
            subtitle,
            font=self.font_header_sub,
            fill=(148, 163, 184, 255),
        )

        # Footer bar
        wdraw.rectangle(
            [win_x, fy, win_x + win_w, win_y + win_h],
            fill=(26, 35, 53, 255),
        )
        wdraw.line(
            [(win_x, fy), (win_x + win_w, fy)],
            fill=(51, 65, 85, 255),
            width=1,
        )

        pill_x1, pill_y1 = win_x + 24, fy + 14
        pill_x2, pill_y2 = win_x + win_w - 24, fy + 54
        wdraw.rounded_rectangle(
            [pill_x1, pill_y1, pill_x2, pill_y2],
            radius=20,
            fill=(15, 23, 42, 255),
            outline=(51, 65, 85, 255),
            width=1,
        )
        placeholder = self.window_cfg.get(
            "input_placeholder", "Type a message in the chat..."
        )
        wdraw.text(
            (pill_x1 + 20, pill_y1 + 9),
            placeholder,
            font=self.font_input,
            fill=(100, 116, 139, 255),
        )

        sb_cx, sb_cy = pill_x2 - 22, (pill_y1 + pill_y2) // 2
        wdraw.ellipse(
            [sb_cx - 14, sb_cy - 14, sb_cx + 14, sb_cy + 14],
            fill=(37, 99, 235, 255),
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

        win_mask = Image.new("L", (self.width, self.height), 0)
        mdraw = ImageDraw.Draw(win_mask)
        mdraw.rounded_rectangle(
            [win_x, win_y, win_x + win_w, win_y + win_h], radius=win_r, fill=255
        )

        clipped_win = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        clipped_win.paste(win_canvas, (0, 0), win_mask)
        bg = Image.alpha_composite(bg, clipped_win)

        draw = ImageDraw.Draw(bg)
        draw.rounded_rectangle(
            [win_x, win_y, win_x + win_w, win_y + win_h],
            radius=win_r,
            outline=(64, 80, 104, 255),
            width=2,
        )
        return bg

    def draw_avatar(self, draw, x, y, letter, color_rgba):
        r = 22
        draw.ellipse([x, y, x + 2 * r, y + 2 * r], fill=color_rgba)
        w = draw.textlength(letter, font=self.font_avatar)
        draw.text(
            (x + r - w / 2, y + 10),
            letter,
            font=self.font_avatar,
            fill=(255, 255, 255, color_rgba[3]),
        )

    def draw_rich_line(self, layer, draw, line, tx, ty, text_rgba, alpha, links):
        # Check if any link is in this line
        matched_link = None
        for lnk in links:
            if lnk and lnk in line:
                matched_link = lnk
                break

        if matched_link:
            prefix, suffix = line.split(matched_link, 1)
            cur_x = self.draw_rich_line(
                layer, draw, prefix, tx, ty, text_rgba, alpha, []
            )
            link_w = draw.textlength(matched_link, font=self.font_reg)
            link_col = (56, 189, 248, int(255 * alpha))
            draw.text((cur_x, ty), matched_link, font=self.font_reg, fill=link_col)
            draw.line(
                [(cur_x, ty + 24), (cur_x + link_w, ty + 24)],
                fill=link_col,
                width=2,
            )
            cur_x += link_w
            if suffix:
                cur_x = self.draw_rich_line(
                    layer, draw, suffix, cur_x, ty, text_rgba, alpha, links
                )
            return cur_x

        cur_x = float(tx)
        idx = 0
        while idx < len(line):
            ch = line[idx]
            if is_emoji_char(ch):
                emoji_str = ch
                idx += 1
                if idx < len(line) and ord(line[idx]) == 0xFE0F:
                    emoji_str += line[idx]
                    idx += 1
                emoji_img = self.get_emoji_image(emoji_str, target_size=24)
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
                cur_x += 26.0
            else:
                start = idx
                while idx < len(line) and not is_emoji_char(line[idx]):
                    idx += 1
                seg = line[start:idx]
                draw.text((cur_x, ty), seg, font=self.font_reg, fill=text_rgba)
                cur_x += draw.textlength(seg, font=self.font_reg)
        return cur_x

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

        win_x = 40
        win_w = self.width - 80
        pad_x = 28
        avatar_size = 44
        max_text_w = 560

        lines = self.wrap_text(draw, text, max_text_w)
        line_h = 29
        pad_bx = 18
        pad_by = 13

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

        if not is_right:
            av_x = win_x + pad_x
            av_y = y_cur + header_offset
            if show_header:
                self.draw_avatar(draw, av_x, av_y, st["avatar_letter"], av_col)
                draw.text(
                    (av_x + avatar_size + 14, y_cur + 2),
                    sender,
                    font=self.font_name,
                    fill=name_col,
                )
                sw = draw.textlength(sender, font=self.font_name)
                draw.text(
                    (av_x + avatar_size + 14 + sw + 10, y_cur + 4),
                    time_str,
                    font=self.font_time,
                    fill=(100, 116, 139, int(255 * alpha)),
                )

            bx1 = av_x + avatar_size + 12
            by1 = y_cur + header_offset
            bx2 = bx1 + bubble_w
            by2 = by1 + bubble_h
        else:
            av_x = win_x + win_w - pad_x - avatar_size
            av_y = y_cur + header_offset
            if show_header:
                self.draw_avatar(draw, av_x, av_y, st["avatar_letter"], av_col)
                tw = draw.textlength(time_str, font=self.font_time)
                sw = draw.textlength(sender, font=self.font_name)
                right_edge = av_x - 14
                draw.text(
                    (right_edge - tw, y_cur + 4),
                    time_str,
                    font=self.font_time,
                    fill=(100, 116, 139, int(255 * alpha)),
                )
                draw.text(
                    (right_edge - tw - 10 - sw, y_cur + 2),
                    sender,
                    font=self.font_name,
                    fill=name_col,
                )

            bx2 = av_x - 12
            bx1 = bx2 - bubble_w
            by1 = y_cur + header_offset
            by2 = by1 + bubble_h

        draw.rounded_rectangle(
            [bx1, by1, bx2, by2],
            radius=16,
            fill=bub_col,
            outline=bdr_col,
            width=1,
        )

        for idx, line in enumerate(lines):
            ty = by1 + pad_by + idx * line_h
            tx = bx1 + pad_bx
            self.draw_rich_line(layer, draw, line, tx, ty, txt_col, alpha, links)

        img.alpha_composite(layer)
        return y_top + header_offset + bubble_h

    def draw_typing_indicator(self, img, sender, y_top, phase_t, show_header=True):
        st = self.get_participant_style(sender)
        is_right = st["is_right"]

        layer = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        win_x = 40
        win_w = self.width - 80
        pad_x = 28
        avatar_size = 44

        header_offset = 24 if show_header else 0
        bubble_w = 88
        bubble_h = 44

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
                self.draw_avatar(draw, av_x, av_y, st["avatar_letter"], av_col)
                draw.text(
                    (av_x + avatar_size + 14, y_top + 2),
                    label,
                    font=self.font_name,
                    fill=name_col,
                )
            bx1 = av_x + avatar_size + 12
            by1 = y_top + header_offset
            bx2 = bx1 + bubble_w
            by2 = by1 + bubble_h
        else:
            av_x = win_x + win_w - pad_x - avatar_size
            av_y = y_top + header_offset
            if show_header:
                self.draw_avatar(draw, av_x, av_y, st["avatar_letter"], av_col)
                lw = draw.textlength(label, font=self.font_name)
                draw.text(
                    (av_x - 14 - lw, y_top + 2),
                    label,
                    font=self.font_name,
                    fill=name_col,
                )
            bx2 = av_x - 12
            bx1 = bx2 - bubble_w
            by1 = y_top + header_offset
            by2 = by1 + bubble_h

        draw.rounded_rectangle(
            [bx1, by1, bx2, by2],
            radius=16,
            fill=bub_col,
            outline=bdr_col,
            width=1,
        )

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
            dot_col = (
                (248, 250, 252, alpha_dot)
                if is_right
                else (148, 163, 184, alpha_dot)
            )
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=dot_col)

        status_tmpl = self.window_cfg.get(
            "typing_status_template", "{sender} is typing..."
        )
        status_text = status_tmpl.format(sender=sender)
        draw.text(
            (win_x + 32, self.height - 32 - 68 - 26),
            status_text,
            font=self.font_typing_bar,
            fill=(148, 163, 184, 220),
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
            default_hold = 4.0 if idx == len(self.messages) - 1 else 2.0
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
        y_cursor = 128

        for idx, item in enumerate(timeline):
            if t < item["typing_start"]:
                break

            if item["typing_start"] <= t < item["typing_end"]:
                self.draw_typing_indicator(
                    frame,
                    item["sender"],
                    y_cursor,
                    t - item["typing_start"],
                    show_header=item["show_header"],
                )
                break

            # Message is visible
            pop_dur = max(0.01, item["pop_dur"])
            alpha = min(1.0, max(0.15, (t - item["msg_start"]) / pop_dur))
            y_offset = int((1.0 - alpha) * 10)
            y_after = self.draw_message_bubble(
                frame,
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
            y_cursor = y_after + (10 if next_same_sender else 22)

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

    def generate_audio(self, audio_output_path, timeline=None, total_duration=None):
        """Generate an MP3 audio file with message pop-up sounds synchronized to the timeline."""
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

        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = os.path.join(tmp_dir, "track.wav")
            pcm_bytes = bytearray(total_samples * 4)
            for i in range(total_samples):
                l_int = int(max(-1.0, min(1.0, left_buf[i])) * 32767.0)
                r_int = int(max(-1.0, min(1.0, right_buf[i])) * 32767.0)
                struct.pack_into("<hh", pcm_bytes, i * 4, l_int, r_int)

            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm_bytes)

            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                wav_path,
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

    def generate(self, output_path, audio_output_path=None):
        base_win = self.create_base_window()
        timeline, total_duration = self.build_timeline()
        total_frames = max(1, int(round(total_duration * self.fps)))

        with tempfile.TemporaryDirectory() as tmp_dir:
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
                os.path.join(tmp_dir, "frame_%04d.png"),
                "-vf",
                "split[s0][s1];[s0]palettegen=stats_mode=full:max_colors=256[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                "-loop",
                str(self.loop),
                str(output_path),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        if audio_output_path is not None:
            self.generate_audio(
                audio_output_path, timeline=timeline, total_duration=total_duration
            )

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
        description="Generate an animated chat GIF and companion MP3 audio file from a configuration file."
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

    if args.loop is not None:
        config["loop"] = args.loop
    if args.initial_delay is not None:
        config["initial_delay"] = args.initial_delay

    out_file = args.output or config.get("output", "chat.gif")
    out_path = Path(out_file)
    if not out_path.is_absolute():
        out_path = config_path.parent / out_path

    audio_cfg = config.get("audio", {})
    audio_enabled = not args.no_audio and (
        audio_cfg.get("enabled", True) if isinstance(audio_cfg, dict) else bool(audio_cfg)
    )

    audio_path = None
    if audio_enabled:
        if args.audio_output:
            audio_file = args.audio_output
        elif args.output:
            audio_file = str(Path(args.output).with_suffix(".mp3"))
        else:
            audio_file = (
                config.get("audio_output")
                or (audio_cfg.get("output") if isinstance(audio_cfg, dict) else None)
                or str(out_path.with_suffix(".mp3"))
            )
        audio_path = Path(audio_file)
        if not audio_path.is_absolute():
            audio_path = config_path.parent / audio_path

    generator = ChatGifGenerator(config)
    final_path, frames, duration = generator.generate(out_path, audio_output_path=audio_path)
    size_kb = os.path.getsize(final_path) / 1024.0
    print(
        f"Generated {final_path} ({generator.width}x{generator.height}, {frames} frames, {duration:.1f}s, {size_kb:.1f} KB)"
    )
    if audio_path is not None and audio_path.exists():
        audio_kb = os.path.getsize(audio_path) / 1024.0
        print(f"Generated {audio_path} ({duration:.1f}s, {audio_kb:.1f} KB)")


if __name__ == "__main__":
    main()
