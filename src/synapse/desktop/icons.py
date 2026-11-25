"""16 px monochrome line icons (1.5 px stroke), drawn from embedded SVG so there is nothing to package or find.

The shapes are from Lucide (https://lucide.dev), ISC licence:

    Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part of Feather (MIT).
    All other copyright (c) for Lucide are held by Lucide Contributors 2022.
    Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby
    granted, provided that the above copyright notice and this permission notice appear in all copies.
    THE SOFTWARE IS PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND.

Icons are muted (text_secondary) by default and take the primary text colour when hovered or active, so they work in
both themes. State icons (see theme.STATES) are always shown with a text label next to them.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from synapse.desktop import theme

SIZE = 16
_PATHS = {
    "play": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "x-circle": '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
    "circle": '<circle cx="12" cy="12" r="9"/>',
    "shield-alert": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
    "lock": '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "minus": '<path d="M5 12h14"/>',
    "fit": '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-up": '<path d="m18 15-6-6-6 6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "loader": '<path d="M21 12a9 9 0 1 1-6.219-8.56"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    "sliders": '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
    "send": '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
    "stop": '<rect width="18" height="18" x="3" y="3" rx="2"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "branch": '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
    "pause": '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
}
NAMES = tuple(_PATHS)
_cache: dict[tuple, QIcon] = {}


def svg(name: str, color: str) -> bytes:
    body = _PATHS[name]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">{body}</svg>').encode()


def pixmap(name: str, color: str, size: int = SIZE, scale: float = 2.0) -> QPixmap:
    """The icon rendered at ``size`` logical pixels (and ``scale``x the pixels, for sharp edges on high-DPI screens)."""
    if name not in _PATHS:
        raise KeyError(name)
    pixels = int(size * scale)
    image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    QSvgRenderer(QByteArray(svg(name, QColor(color).name()))).render(painter, QRectF(0, 0, pixels, pixels))
    painter.end()
    pm = QPixmap.fromImage(image)
    pm.setDevicePixelRatio(scale)
    return pm


def icon(name: str, color: str | None = None, active: str | None = None, dark: bool | None = None) -> QIcon:
    """Muted by default (``color``), brighter in the Active (hover) state. Colours default to the theme's text roles."""
    p = theme.palette(dark)
    key = (name, color or p["text_secondary"], active or p["text_primary"])
    cached = _cache.get(key)
    if cached is None:
        cached = QIcon()
        cached.addPixmap(pixmap(name, key[1]), QIcon.Mode.Normal)
        cached.addPixmap(pixmap(name, key[2]), QIcon.Mode.Active)
        cached.addPixmap(pixmap(name, key[2]), QIcon.Mode.Selected)
        cached.addPixmap(pixmap(name, p["text_muted"]), QIcon.Mode.Disabled)
        _cache[key] = cached
    return cached


def state_icon(state: str, dark: bool | None = None) -> QIcon:
    """The icon of an operational state, in that state's colour (always paired with its text label by the caller)."""
    key, name, _label = theme.STATES.get(state, theme.STATES["idle"])
    colour = theme.palette(dark)[key]
    return icon(name, colour, colour, dark)


def state_label(state: str) -> str:
    return theme.STATES.get(state, theme.STATES["idle"])[2]
