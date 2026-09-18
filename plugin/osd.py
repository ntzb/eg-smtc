# -*- coding: utf-8 -*-
"""A now-playing overlay, in the spirit of the Windows 10 media flyout.

Windows 11 removed that flyout: the volume OSD became a standalone indicator
and media controls moved into Quick Settings, so pressing a media key no
longer shows anything. Nothing in the OS will draw this any more, and the
third-party replacements cost 50-200 MB of resident memory for a panel that
appears for two seconds.

This draws it in EventGhost's own process instead, out of the metadata and
artwork the plugin already has, for the cost of one wx frame.

It renders through a layered window with per-pixel alpha, which is what makes
the rounded corners smooth and allows a soft shadow. A shaped frame with a
region mask, the approach EventGhost's own ShowOSD uses, can only produce
hard-edged corners because a region is all-or-nothing per pixel. The layered
path needs a little ctypes, so there is a fallback to the shaped frame if any
of it fails: a slightly jagged overlay beats none.

wxPython here is 3.0.2 classic, as bundled with EventGhost, so the old
spellings (wx.EmptyBitmap, wx.BitmapFromImage, wx.RegionFromBitmap) are the
correct ones rather than legacy aliases.
"""

import ctypes
import os
import threading
from ctypes import wintypes

import wx

# Layout, echoing the Windows 10 flyout: square artwork on the left, app name
# then title then artist on the right.
ART_SIZE = 72
PADDING = 16
GUTTER = 14
CORNER = 10
SHADOW = 12
MIN_TEXT_WIDTH = 200
MAX_TEXT_WIDTH = 340

TOP_COLOUR = (48, 48, 50)
BOTTOM_COLOUR = (28, 28, 30)
# A lighter line along the top inside edge. The DC has no alpha, so this is
# a solid colour picked to read as a highlight against TOP_COLOUR rather than
# a border.
HIGHLIGHT_COLOUR = (72, 72, 76)
TITLE_COLOUR = (255, 255, 255)
ARTIST_COLOUR = (176, 176, 180)
APP_COLOUR = (128, 128, 134)
ACCENT_COLOUR = (120, 190, 255)

SHADOW_ALPHA = 110

FACE = "Segoe UI"

# Must not occur in the rendered panel: it becomes the transparency mask on
# the fallback path.
MASK_COLOUR = (255, 0, 255)

# Win32 bits for the layered window.
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _font(size, bold=False):
    return wx.Font(
        size,
        wx.FONTFAMILY_SWISS,
        wx.FONTSTYLE_NORMAL,
        wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL,
        face=FACE,
    )


def _elide(dc, text, limit):
    """Shorten text with an ellipsis until it fits limit pixels."""
    if not text:
        return u""
    if dc.GetTextExtent(text)[0] <= limit:
        return text
    ellipsis = u"…"
    trimmed = text
    while trimmed and dc.GetTextExtent(trimmed + ellipsis)[0] > limit:
        trimmed = trimmed[:-1]
    return (trimmed + ellipsis) if trimmed else ellipsis


def _load_artwork(path):
    """Return a square bitmap for path, or None if it is unusable.

    The bytes come straight from whichever app published them, so a decode
    failure is an expected outcome rather than a bug: fall back to a
    text-only panel instead of failing the whole overlay.
    """
    if not path or not os.path.exists(path):
        return None
    log = wx.LogNull()  # suppress wx's modal "unknown image format" dialog
    try:
        image = wx.Image(path)
        if not image.IsOk():
            return None
        width, height = image.GetWidth(), image.GetHeight()
        if width <= 0 or height <= 0:
            return None
        # Cover the square, then centre-crop, so artwork is never stretched.
        scale = float(ART_SIZE) / min(width, height)
        image = image.Scale(
            max(ART_SIZE, int(round(width * scale))),
            max(ART_SIZE, int(round(height * scale))),
            wx.IMAGE_QUALITY_HIGH,
        )
        left = max(0, (image.GetWidth() - ART_SIZE) // 2)
        top = max(0, (image.GetHeight() - ART_SIZE) // 2)
        image = image.GetSubImage(wx.Rect(left, top, ART_SIZE, ART_SIZE))
        return wx.BitmapFromImage(image)
    except Exception:
        return None
    finally:
        del log


def _status_marks(dc, x, y, status, colour):
    """A small play triangle or pause bars, drawn rather than typed.

    A glyph would depend on the font actually carrying it; Segoe UI does not
    reliably, and a missing glyph renders as a box.
    """
    dc.SetPen(wx.Pen(colour, 1))
    dc.SetBrush(wx.Brush(colour, wx.SOLID))
    if status == u"Playing":
        dc.DrawPolygon([(x, y), (x, y + 10), (x + 9, y + 5)])
    elif status in (u"Paused", u"Changing"):
        dc.DrawRectangle(x, y, 3, 10)
        dc.DrawRectangle(x + 5, y, 3, 10)
    else:
        dc.DrawRectangle(x, y + 1, 8, 8)


class Panel(object):
    """The rendered overlay: an RGB bitmap plus its size."""

    def __init__(self, bitmap, width, height):
        self.bitmap = bitmap
        self.width = width
        self.height = height


def _render(title, artist, app, status, artwork):
    """Draw the panel, inset by SHADOW on every side for the shadow to live in."""
    measure = wx.MemoryDC()
    measure.SelectObject(wx.EmptyBitmap(1, 1))
    measure.SetFont(_font(11, bold=True))
    titleWidth = measure.GetTextExtent(title or u" ")[0]
    measure.SetFont(_font(9))
    artistWidth = measure.GetTextExtent(artist or u" ")[0]
    measure.SetFont(_font(8))
    appWidth = measure.GetTextExtent(app or u" ")[0] + 14
    measure.SelectObject(wx.NullBitmap)

    textWidth = max(MIN_TEXT_WIDTH,
                    min(MAX_TEXT_WIDTH, max(titleWidth, artistWidth, appWidth)))

    artSpan = (ART_SIZE + GUTTER) if artwork else 0
    innerWidth = PADDING * 2 + artSpan + textWidth
    innerHeight = PADDING * 2 + (ART_SIZE if artwork else 62)
    width = innerWidth + SHADOW * 2
    height = innerHeight + SHADOW * 2

    bitmap = wx.EmptyBitmap(width, height)
    dc = wx.MemoryDC()
    dc.SelectObject(bitmap)

    # The shadow band is masked out on the fallback path and given a falloff
    # alpha on the layered path, so its colour only has to be distinctive.
    dc.SetBackground(wx.Brush(MASK_COLOUR, wx.SOLID))
    dc.Clear()

    panel = wx.Rect(SHADOW, SHADOW, innerWidth, innerHeight)
    dc.GradientFillLinear(panel, wx.Colour(*TOP_COLOUR),
                          wx.Colour(*BOTTOM_COLOUR), wx.SOUTH)

    dc.SetPen(wx.Pen(wx.Colour(*HIGHLIGHT_COLOUR), 1))
    dc.DrawLine(panel.x + CORNER, panel.y,
                panel.x + panel.width - CORNER, panel.y)

    # The panel rect is filled corner to corner and the rounding comes from
    # the alpha pass. Clipping it here instead would leave the mask colour in
    # the partially covered corner pixels, which would then blend magenta.
    if artwork:
        dc.DrawBitmap(artwork, panel.x + PADDING, panel.y + PADDING, True)
        # A hairline under the artwork edge lifts it off the panel.
        dc.SetPen(wx.Pen(wx.Colour(0, 0, 0), 1))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRectangle(panel.x + PADDING, panel.y + PADDING,
                         ART_SIZE, ART_SIZE)

    textLeft = panel.x + PADDING + artSpan
    top = panel.y + PADDING

    # App name with a status mark, small and dim, as the flyout had.
    _status_marks(dc, textLeft, top + 2, status, ACCENT_COLOUR)
    dc.SetFont(_font(8))
    dc.SetTextForeground(wx.Colour(*APP_COLOUR))
    dc.DrawText(_elide(dc, app, textWidth - 14), textLeft + 14, top)
    cursor = top + 16

    dc.SetFont(_font(11, bold=True))
    dc.SetTextForeground(wx.Colour(*TITLE_COLOUR))
    shownTitle = _elide(dc, title, textWidth)
    dc.DrawText(shownTitle, textLeft, cursor)
    cursor += dc.GetTextExtent(shownTitle or u" ")[1] + 3

    if artist:
        dc.SetFont(_font(9))
        dc.SetTextForeground(wx.Colour(*ARTIST_COLOUR))
        dc.DrawText(_elide(dc, artist, textWidth), textLeft, cursor)

    dc.SelectObject(wx.NullBitmap)
    return Panel(bitmap, width, height)


def _coverage(x, y, width, height, radius):
    """Antialiased coverage of the rounded panel at a pixel, 0.0 to 1.0.

    Only the corners need real work; everything else is inside or outside by
    inspection, which keeps this loop cheap enough to run per press.
    """
    if radius <= 0:
        return 1.0
    cx = radius if x < radius else (width - 1 - radius if x > width - 1 - radius else x)
    cy = radius if y < radius else (height - 1 - radius if y > height - 1 - radius else y)
    if cx == x and cy == y:
        return 1.0
    dx = x - cx
    dy = y - cy
    distance = (dx * dx + dy * dy) ** 0.5
    return max(0.0, min(1.0, radius + 0.5 - distance))


def _argb_buffer(panel):
    """Premultiplied BGRA bytes for UpdateLayeredWindow.

    Alpha is computed here rather than baked into the bitmap: the panel is
    opaque with antialiased corners, and outside it a quadratic falloff gives
    the shadow.
    """
    image = wx.ImageFromBitmap(panel.bitmap)
    rgb = bytearray(image.GetData())
    out = bytearray(panel.width * panel.height * 4)

    innerWidth = panel.width - SHADOW * 2
    innerHeight = panel.height - SHADOW * 2

    for y in range(panel.height):
        py = y - SHADOW
        rowBase = y * panel.width
        for x in range(panel.width):
            px = x - SHADOW
            index = (rowBase + x) * 3
            target = (rowBase + x) * 4

            if 0 <= px < innerWidth and 0 <= py < innerHeight:
                alpha = _coverage(px, py, innerWidth, innerHeight, CORNER)
                if alpha <= 0.0:
                    continue
                red = rgb[index]
                green = rgb[index + 1]
                blue = rgb[index + 2]
            else:
                # Distance outside the panel, for the shadow falloff.
                ox = 0 if 0 <= px < innerWidth else (
                    -px if px < 0 else px - innerWidth + 1)
                oy = 0 if 0 <= py < innerHeight else (
                    -py if py < 0 else py - innerHeight + 1)
                distance = (ox * ox + oy * oy) ** 0.5
                if distance >= SHADOW:
                    continue
                fade = 1.0 - (distance / float(SHADOW))
                alpha = fade * fade * (SHADOW_ALPHA / 255.0)
                red = green = blue = 0

            a = int(alpha * 255)
            if a <= 0:
                continue
            out[target] = (blue * a) // 255
            out[target + 1] = (green * a) // 255
            out[target + 2] = (red * a) // 255
            out[target + 3] = a
    return out


class OsdFrame(wx.Frame):
    """A layered overlay that hides itself on a timer.

    Created once and reused. Destroying and recreating the frame per press is
    both slower and a reliable way to leak GDI objects.
    """

    def __init__(self):
        wx.Frame.__init__(
            self,
            None,
            -1,
            "SMTC OSD",
            size=(1, 1),
            style=(wx.FRAME_SHAPED | wx.NO_BORDER | wx.FRAME_NO_TASKBAR |
                   wx.STAY_ON_TOP),
        )
        self.bitmap = wx.EmptyBitmap(1, 1)
        self.layered = False
        self.timer = threading.Timer(0.0, lambda: None)
        self.Bind(wx.EVT_PAINT, self.OnPaint)
        # Swallow the close, so EventGhost shutting down cannot leave a
        # half-destroyed frame behind a pending timer.
        self.Bind(wx.EVT_CLOSE, lambda event: None)

    def OnPaint(self, event=None):
        if not self.layered:
            wx.BufferedPaintDC(self, self.bitmap)

    # Deliberately not called Show/Hide: those are wx.Window methods, and
    # overriding them means self.Show(True) recurses into this instead of
    # showing the window.
    def Display(self, title, artist, app, status, artworkPath, timeout,
                displayNumber):
        """Must run on the wx main thread."""
        self.timer.cancel()

        artwork = _load_artwork(artworkPath)
        panel = _render(title, artist, app, status, artwork)
        self.bitmap = panel.bitmap

        display = wx.Display(
            displayNumber if displayNumber < wx.Display.GetCount() else 0)
        area = display.GetClientArea()
        position = (area.x + 12, area.y + 12)

        self.SetSize((panel.width, panel.height))
        self.SetPosition(position)

        self.layered = False
        try:
            self._paint_layered(panel, position)
            self.layered = True
        except Exception:
            # Square corners and no shadow, since a region is all-or-nothing
            # per pixel, but visible. The mask covers the shadow band, so the
            # window shrinks to the panel itself.
            panel.bitmap.SetMask(wx.Mask(panel.bitmap, wx.Colour(*MASK_COLOUR)))
            self.SetShape(wx.RegionFromBitmap(panel.bitmap))

        if self.IsShown():
            self.Raise()
        else:
            self.Show(True)
        if not self.layered:
            self.Refresh()

        if timeout > 0:
            self.timer = threading.Timer(timeout, self.Retire)
            self.timer.daemon = True
            self.timer.start()

    def _paint_layered(self, panel, position):
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        hwnd = self.GetHandle()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)

        buffer = _argb_buffer(panel)

        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = panel.width
        header.biHeight = -panel.height  # top-down
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB

        screenDC = user32.GetDC(0)
        memoryDC = gdi32.CreateCompatibleDC(screenDC)
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(
            memoryDC, ctypes.byref(header), DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0)
        if not dib:
            gdi32.DeleteDC(memoryDC)
            user32.ReleaseDC(0, screenDC)
            raise RuntimeError("CreateDIBSection failed")

        old = gdi32.SelectObject(memoryDC, dib)
        try:
            ctypes.memmove(bits, bytes(buffer), len(buffer))

            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            source = POINT(0, 0)
            destination = POINT(int(position[0]), int(position[1]))
            size = SIZE(panel.width, panel.height)

            ok = user32.UpdateLayeredWindow(
                hwnd, screenDC, ctypes.byref(destination), ctypes.byref(size),
                memoryDC, ctypes.byref(source), 0, ctypes.byref(blend),
                ULW_ALPHA)
            if not ok:
                raise RuntimeError("UpdateLayeredWindow failed")
        finally:
            gdi32.SelectObject(memoryDC, old)
            gdi32.DeleteObject(dib)
            gdi32.DeleteDC(memoryDC)
            user32.ReleaseDC(0, screenDC)

    def Retire(self):
        # Runs on a timer thread, so it has to hop to the wx thread before
        # touching the window.
        wx.CallAfter(self.RetireNow)

    def RetireNow(self):
        self.Show(False)
