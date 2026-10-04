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
"""Generate an animated chat GIF from a JSON/TOML configuration file."""

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
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
        self.fps = int(config.get("fps", 15))
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
        return {
            "is_right": is_right,
            "avatar_letter": p.get("avatar_letter", sender[:1].upper() if sender else "?"),
            "avatar_color": p.get("avatar_color", default_avatar),
            "name_color": p.get("name_color", default_name),
            "bubble_color": p.get("bubble_color", default_bubble),
            "bubble_border_color": p.get("bubble_border_color", default_border),
            "text_color": p.get("text_color", "#f8fafc"),
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

    def generate(self, output_path):
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
        description="Generate an animated chat GIF from a configuration file."
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=str(default_config),
        help="Path to the JSON/TOML configuration file (default: chat-config.json)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Override output GIF path from configuration file",
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

    generator = ChatGifGenerator(config)
    final_path, frames, duration = generator.generate(out_path)
    size_kb = os.path.getsize(final_path) / 1024.0
    print(
        f"Generated {final_path} ({generator.width}x{generator.height}, {frames} frames, {duration:.1f}s, {size_kb:.1f} KB)"
    )


if __name__ == "__main__":
    main()
