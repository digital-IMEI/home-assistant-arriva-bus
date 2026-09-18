"""Named palette with backward-compatible custom RGB values."""

import re

PALETTE = {
    "green": "#4CAF50",
    "red": "#F44336",
    "orange": "#FF9800",
    "yellow": "#FFEB3B",
    "blue": "#2196F3",
    "purple": "#9C27B0",
    "pink": "#E91E63",
    "white": "#FFFFFF",
    "gray": "#9E9E9E",
}


def color_hex(value, fallback):
    if isinstance(value, str):
        if value in PALETTE:
            return PALETTE[value]
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            return value.upper()
    if (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(isinstance(v, (int, float)) and 0 <= v <= 255 for v in value)
    ):
        return "#" + "".join(f"{int(v):02X}" for v in value)
    return fallback


def color_choice(value, fallback):
    hex_value = color_hex(value, PALETTE[fallback])
    return next((name for name, color in PALETTE.items() if color == hex_value), hex_value)
