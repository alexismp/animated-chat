# Animated Chat GIF Generator

Generate clean, realistic animated chat GIFs from declarative JSON, TOML, or YAML configuration files.

The tool renders a modern macOS-style chat window complete with realistic window chrome, drop shadows, participant avatars, typing indicators, bubble pop-in animations, link formatting, and emoji support.

![Animated Chat Demo](chat.gif)

## Features

- **macOS Window Aesthetics**: Realistic dark-themed chat interface with traffic light controls, header avatars, online status, and bottom input field.
- **Customizable Participants**: Configurable display names, avatar letters, bubble colors, border colors, and chat alignment (left/right).
- **Realistic Animation Flow**:
  - Configurable initial pause showing the idle chat window before conversation begins.
  - Realistic typing indicators (animated pulsing dots with configurable typing durations).
  - Smooth message pop-in with opacity and translation transitions.
  - Configurable hold duration per message.
- **Rich Content Support**: Automatic detection and styling of URLs/links and emoji characters.
- **High-Quality Optimized Output**: Uses FFmpeg two-pass palette generation (`palettegen` / `paletteuse`) for crisp colors and compact file size.
- **Single-Play or Loop Options**: Configured by default to play once without looping (`-loop -1`), or customizable to loop infinitely or a specified number of times.

## Dependencies & Prerequisites

### System Requirements

- **Operating System**: macOS (the script uses macOS system fonts: `/System/Library/Fonts/HelveticaNeue.ttc` and `/System/Library/Fonts/Apple Color Emoji.ttc`).
- **FFmpeg**: Required for compiling frames into an optimized GIF.
  ```bash
  brew install ffmpeg
  ```

### Python Dependencies

- **Python 3.10+**
- **Pillow**: Python imaging library for frame rendering.
  ```bash
  pip install Pillow
  ```
- **PyYAML** (Optional): Only needed if you prefer YAML configuration files over JSON or TOML.
  ```bash
  pip install pyyaml
  ```

## Usage

### Quick Start

Generate a GIF using the default configuration file (`chat-config.json`):

```bash
python3 generate_chat_gif.py
```

### CLI Arguments

```bash
python3 generate_chat_gif.py [config_file] [-o OUTPUT] [--loop LOOP] [--initial-delay INITIAL_DELAY]
```

- `config_file` *(optional)*: Path to the configuration file (defaults to `chat-config.json`). Supports `.json`, `.toml`, and `.yaml` / `.yml`.
- `-o`, `--output` *(optional)*: Override the output GIF file path specified in the configuration file.
- `--loop` *(optional)*: Number of loops (`-1` to play once, `0` for infinite loop, or a positive integer for a specific repetition count; default: `-1`).
- `--initial-delay` *(optional)*: Delay in seconds to display the empty window before the first message begins (default: `2.0`).

### Example

```bash
python3 generate_chat_gif.py chat-config.json -o my-chat.gif --initial-delay 1.5
```

## Configuration Reference

A sample `chat-config.json`:

```json
{
  "output": "chat.gif",
  "width": 1024,
  "height": 768,
  "fps": 15,
  "loop": -1,
  "initial_delay": 2.0,
  "window": {
    "title": "Bob & Alice",
    "subtitle": "Direct message • 2 participants online",
    "input_placeholder": "Type a message in the chat...",
    "typing_status_template": "{sender} is typing...",
    "typing_bubble_label_template": "{sender} is typing..."
  },
  "participants": {
    "Bob": {
      "side": "left",
      "avatar_letter": "B",
      "avatar_color": "#6366f1",
      "name_color": "#a5b4fc",
      "bubble_color": "#26344a",
      "bubble_border_color": "#3b4c69",
      "text_color": "#f8fafc"
    },
    "Alice": {
      "side": "right",
      "avatar_letter": "A",
      "avatar_color": "#10b981",
      "name_color": "#6ee7b7",
      "bubble_color": "#2563eb",
      "bubble_border_color": "#3b82f6",
      "text_color": "#ffffff"
    }
  },
  "messages": [
    {
      "sender": "Bob",
      "time": "17:24",
      "text": "Honestly, I don't get why we still keep expensive devs around when AI just coded everything for me in 20 minutes without a single bug! Your job is dead 💀",
      "typing_duration": 0.0,
      "hold_duration": 4.25
    },
    {
      "sender": "Alice",
      "time": "17:25",
      "text": "Oh really? Awesome, send me the link so I can test it!",
      "typing_duration": 2.5,
      "hold_duration": 1.9
    }
  ]
}
```

### Configuration Keys

| Parameter | Type | Description |
|---|---|---|
| `output` | string | Output file path for the generated GIF. |
| `width` | integer | Canvas width in pixels (e.g., `1024`). |
| `height` | integer | Canvas height in pixels (e.g., `768`). |
| `fps` | integer | Frame rate in frames per second (e.g., `15`). |
| `loop` | integer / bool | GIF loop count (`-1` = play once, `0` = infinite). Default is `-1`. |
| `initial_delay`| float | Duration in seconds before the conversation starts. Default is `2.0`. |
| `window` | object | Customization for the window title, subtitle, placeholders, and templates. |
| `participants` | object | Participant styling dictionary keyed by sender name (`avatar_letter`, `avatar_color`, `bubble_color`, `side`, etc.). |
| `messages` | array | Ordered list of messages with `sender`, `time`, `text`, `typing_duration`, and `hold_duration`. |

## License

This project is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.

## Disclaimer

This is not an officially supported Google product.
