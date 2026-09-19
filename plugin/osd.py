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
the rounded corners smooth and lets the shadow wrap them. A shaped frame with
a region mask, the approach EventGhost's own ShowOSD uses, can only produce
hard-edged corners because a region is all-or-nothing per pixel. The layered
path needs a little ctypes, so there is a fallback to the shaped frame: a
jagged overlay beats none.

The window never takes focus. It is WS_EX_NOACTIVATE and is shown and hidden
with SetWindowPos and SWP_NOACTIVATE, never Show or Raise, both of which
activate on wxMSW and would pull focus out of whatever you were typing in.
ShowOSD does the same thing for the same reason.

wxPython here is 3.0.2 classic, as bundled with EventGhost, so the old
spellings (wx.EmptyBitmap, wx.BitmapFromImage, wx.RegionFromBitmap) are the
correct ones rather than legacy aliases.
"""

import ctypes
import os
import threading
from ctypes import wintypes

import wx

# Layout, echoing the flyout it replaces: square artwork on the left, app
# name then title then artist on the right.
ART_SIZE = 72
PADDING = 16
GUTTER = 14
CORNER = 10
SHADOW = 14
SHADOW_DROP = 4          # shifts the shadow down, so it reads as cast light
MIN_TEXT_WIDTH = 210
MAX_TEXT_WIDTH = 380

APP_POINTS = 9
TITLE_POINTS = 12
ARTIST_POINTS = 10

TOP_COLOUR = (48, 48, 50)
BOTTOM_COLOUR = (28, 28, 30)
# The DC has no alpha, so this is a solid colour chosen to read as a lit edge
# against TOP_COLOUR rather than as a border.
HIGHLIGHT_COLOUR = (74, 74, 78)
ART_EDGE_COLOUR = (20, 20, 22)
TITLE_COLOUR = (255, 255, 255)
ARTIST_COLOUR = (176, 176, 180)
APP_COLOUR = (130, 130, 136)
ACCENT_COLOUR = (120, 190, 255)

SHADOW_ALPHA = 0.42

FACE = "Segoe UI"

# A thumbnail larger than this is not a thumbnail. wx.Image decodes fully
# before anything scales it, and this is a 32-bit process shared with
# wxPython, Python and every other plugin.
MAX_ARTWORK_BYTES = 8 * 1024 * 1024

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
SWP_NOOWNERZORDER = 0x0200

HWND_FLAGS = SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_FRAMECHANGED


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


_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

# Declared rather than left to ctypes' default int marshalling. Correct on
# x86 either way, which this plugin is by construction, but an undeclared
# HANDLE is the standard way this code breaks the day it is built for x64.
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
_user32.SetWindowLongW.restype = ctypes.c_long
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_uint]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int
_user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(POINT), wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
_user32.UpdateLayeredWindow.restype = wintypes.BOOL
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.DeleteDC.restype = wintypes.BOOL


def _font(size, bold=False):
    return wx.Font(
        size,
        wx.FONTFAMILY_SWISS,
        wx.FONTSTYLE_NORMAL,
        wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL,
        face=FACE,
    )


def _trim_surrogate(text):
    """Drop a trailing lone high surrogate.

    Python 2 on Windows is a narrow build, so slicing is by UTF-16 code unit
    and can cut a pair in half, which renders as a tofu box. Emoji in titles
    is routine from YouTube.
    """
    if text and u"\ud800" <= text[-1] <= u"\udbff":
        return text[:-1]
    return text


def _elide(dc, text, limit):
    """Shorten text with an ellipsis until it fits limit pixels.

    Binary search rather than one GetTextExtent per character removed: a long
    title costs eight measurements instead of a few hundred.
    """
    if not text:
        return u""
    if dc.GetTextExtent(text)[0] <= limit:
        return text

    ellipsis = u"…"
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = _trim_surrogate(text[:middle]) + ellipsis
        if dc.GetTextExtent(candidate)[0] <= limit:
            low = middle
        else:
            high = middle - 1
    return _trim_surrogate(text[:low]) + ellipsis if low else ellipsis


def _load_artwork(path):
    """Return a square bitmap for path, or None if it is unusable.

    The bytes come straight from whichever app published them, so a decode
    failure is an expected outcome rather than a bug: fall back to a
    text-only panel instead of failing the whole overlay.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        if os.path.getsize(path) > MAX_ARTWORK_BYTES:
            return None
    except OSError:
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
        # The clamps are what keep GetSubImage in bounds after rounding.
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
    """A rendered overlay: the RGB bitmap and its geometry."""

    def __init__(self, bitmap, width, height, innerWidth, innerHeight):
        self.bitmap = bitmap
        self.width = width
        self.height = height
        self.innerWidth = innerWidth
        self.innerHeight = innerHeight


def _render(title, artist, app, status, artwork):
    """Draw the panel, inset by SHADOW on every side for the shadow."""
    measure = wx.MemoryDC()
    measure.SelectObject(wx.EmptyBitmap(1, 1))
    measure.SetFont(_font(APP_POINTS))
    appHeight = measure.GetTextExtent(app or u" ")[1]
    appWidth = measure.GetTextExtent(app or u" ")[0] + 14
    measure.SetFont(_font(TITLE_POINTS, bold=True))
    titleWidth, titleHeight = measure.GetTextExtent(title or u" ")
    measure.SetFont(_font(ARTIST_POINTS))
    artistWidth, artistHeight = measure.GetTextExtent(artist or u" ")
    measure.SelectObject(wx.NullBitmap)

    textWidth = max(MIN_TEXT_WIDTH,
                    min(MAX_TEXT_WIDTH, max(titleWidth, artistWidth, appWidth)))
    # Rounded to a step so the alpha cache has a handful of possible keys
    # rather than one per title width. A radio stream retitles every song,
    # and each distinct width would otherwise cost another ~120 KB forever.
    # It also stops the card twitching in width between tracks.
    textWidth = min(MAX_TEXT_WIDTH, ((textWidth + 15) // 16) * 16)

    # Measured rather than assumed, so the block can be centred against the
    # artwork instead of pinned to the top of the card.
    textHeight = appHeight + 5 + titleHeight
    if artist:
        textHeight += 3 + artistHeight

    artSpan = (ART_SIZE + GUTTER) if artwork else 0
    innerWidth = PADDING * 2 + artSpan + textWidth
    innerHeight = PADDING * 2 + max(ART_SIZE if artwork else 0, textHeight)
    width = innerWidth + SHADOW * 2
    # The extra drop is room for the shadow's offset, without which the
    # bottom row terminates part-way down the falloff and can band.
    height = innerHeight + SHADOW * 2 + SHADOW_DROP

    bitmap = wx.EmptyBitmap(width, height)
    dc = wx.MemoryDC()
    dc.SelectObject(bitmap)

    # Black, not a key colour: the shadow is black, so anything sampled from
    # this band by mistake is invisible rather than magenta. Deriving the
    # fallback region from a colour key was the only reason for a key, and
    # that region is just the panel rect.
    dc.SetBackground(wx.Brush(wx.Colour(0, 0, 0), wx.SOLID))
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
        artLeft = panel.x + PADDING
        artTop = panel.y + (innerHeight - ART_SIZE) // 2
        dc.DrawBitmap(artwork, artLeft, artTop, True)
        dc.SetPen(wx.Pen(wx.Colour(*ART_EDGE_COLOUR), 1))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRectangle(artLeft, artTop, ART_SIZE, ART_SIZE)

    textLeft = panel.x + PADDING + artSpan
    cursor = panel.y + (innerHeight - textHeight) // 2

    _status_marks(dc, textLeft, cursor + 1, status, ACCENT_COLOUR)
    dc.SetFont(_font(APP_POINTS))
    dc.SetTextForeground(wx.Colour(*APP_COLOUR))
    dc.DrawText(_elide(dc, app, textWidth - 14), textLeft + 14, cursor)
    cursor += appHeight + 5

    dc.SetFont(_font(TITLE_POINTS, bold=True))
    dc.SetTextForeground(wx.Colour(*TITLE_COLOUR))
    dc.DrawText(_elide(dc, title, textWidth), textLeft, cursor)
    cursor += titleHeight + 3

    if artist:
        dc.SetFont(_font(ARTIST_POINTS))
        dc.SetTextForeground(wx.Colour(*ARTIST_COLOUR))
        dc.DrawText(_elide(dc, artist, textWidth), textLeft, cursor)

    dc.SelectObject(wx.NullBitmap)
    return Panel(bitmap, width, height, innerWidth, innerHeight)


_ALPHA_CACHE = {}


def _alpha_mask(width, height, innerWidth, innerHeight):
    """Per-pixel panel coverage and total alpha, as two bytearrays.

    Both are needed, and conflating them was visible as a purple halo: the
    shadow band of the rendered bitmap is filled with the mask colour, so
    scaling the panel's colour by the *combined* alpha tinted the shadow
    magenta. Colour belongs to the panel and is scaled by coverage; the
    shadow is black and contributes alpha alone.

    Cached by geometry: this depends on nothing else, the text width is
    clamped to a narrow range, so after the first press of a given size it
    costs nothing.

    The panel and the shadow are composited rather than treated as exclusive
    regions. Computing the shadow only outside the panel's bounding rect left
    a transparent wedge in each corner, between the rounded arc and the
    square corner of the rect, with a hard step where the shadow began.
    """
    key = (width, height, innerWidth, innerHeight)
    cached = _ALPHA_CACHE.get(key)
    if cached is not None:
        return cached

    coverageMask = bytearray(width * height)
    alphaMask = bytearray(width * height)

    # Signed distance to the rounded panel, and to the same shape dropped by
    # SHADOW_DROP for the shadow.
    left = SHADOW
    top = SHADOW
    right = SHADOW + innerWidth - 1
    bottom = SHADOW + innerHeight - 1

    innerLeft = left + CORNER
    innerRight = right - CORNER
    innerTop = top + CORNER
    innerBottom = bottom - CORNER

    shadowTop = innerTop + SHADOW_DROP
    shadowBottom = innerBottom + SHADOW_DROP

    for y in range(height):
        rowBase = y * width

        dyPanel = 0
        if y < innerTop:
            dyPanel = innerTop - y
        elif y > innerBottom:
            dyPanel = y - innerBottom

        dyShadow = 0
        if y < shadowTop:
            dyShadow = shadowTop - y
        elif y > shadowBottom:
            dyShadow = y - shadowBottom

        for x in range(width):
            dxPanel = 0
            if x < innerLeft:
                dxPanel = innerLeft - x
            elif x > innerRight:
                dxPanel = x - innerRight

            if dxPanel == 0 and dyPanel == 0:
                coverageMask[rowBase + x] = 255
                alphaMask[rowBase + x] = 255
                continue

            # 1.0, not 0.5: the distance puts the shape's boundary through
            # the centres of the outermost pixels, while _render fills the
            # rect to their outer edges. Half a pixel of disagreement shows
            # up as a uniform translucent rim on all four sides, which lands
            # squarely on the highlight line and dims it.
            distance = (dxPanel * dxPanel + dyPanel * dyPanel) ** 0.5 - CORNER
            coverage = 1.0 - distance
            if coverage >= 1.0:
                coverageMask[rowBase + x] = 255
                alphaMask[rowBase + x] = 255
                continue
            if coverage < 0.0:
                coverage = 0.0

            shadowDistance = (
                (dxPanel * dxPanel + dyShadow * dyShadow) ** 0.5 - CORNER)
            if shadowDistance < SHADOW:
                fade = 1.0 - (max(0.0, shadowDistance) / float(SHADOW))
                shadow = fade * fade * SHADOW_ALPHA
            else:
                shadow = 0.0

            alpha = coverage + shadow * (1.0 - coverage)
            coverageMask[rowBase + x] = int(coverage * 255.0)
            alphaMask[rowBase + x] = int(alpha * 255.0)

    _ALPHA_CACHE[key] = (coverageMask, alphaMask)
    return _ALPHA_CACHE[key]


def _argb_buffer(panel):
    """Premultiplied BGRA bytes for UpdateLayeredWindow.

    Colour is scaled by panel coverage, not by total alpha. The shadow is
    black, so it contributes alpha and no colour; scaling by alpha instead
    would drag the bitmap's shadow-band fill into the result, which read as a
    purple halo around the card.
    """
    image = wx.ImageFromBitmap(panel.bitmap)
    rgb = bytearray(image.GetData())
    coverageMask, alphaMask = _alpha_mask(
        panel.width, panel.height, panel.innerWidth, panel.innerHeight)

    out = bytearray(panel.width * panel.height * 4)
    for index in range(panel.width * panel.height):
        alpha = alphaMask[index]
        if not alpha:
            continue
        target = index * 4
        out[target + 3] = alpha

        coverage = coverageMask[index]
        if not coverage:
            continue  # shadow only: premultiplied black
        source = index * 3
        if coverage == 255:
            out[target] = rgb[source + 2]
            out[target + 1] = rgb[source + 1]
            out[target + 2] = rgb[source]
        else:
            out[target] = (rgb[source + 2] * coverage) // 255
            out[target + 1] = (rgb[source + 1] * coverage) // 255
            out[target + 2] = (rgb[source] * coverage) // 255
    return out


class OsdFrame(wx.Frame):
    """A layered overlay that hides itself on a timer.

    Created once and reused. Destroying and recreating the frame per press is
    both slower and a reliable way to leak GDI objects. That reuse means Win32
    state is sticky, so both the layered style and the window region have to
    be cleared when switching between the layered and shaped paths.
    """

    def __init__(self):
        wx.Frame.__init__(
            self,
            None,
            -1,
            "SMTC OSD",
            size=(1, 1),
            style=(wx.FRAME_SHAPED | wx.NO_BORDER | wx.FRAME_NO_TASKBAR |
                   wx.FRAME_TOOL_WINDOW | wx.STAY_ON_TOP),
        )
        self.bitmap = wx.EmptyBitmap(1, 1)
        self.layered = False
        self.shaped = False
        self.timer = threading.Timer(0.0, lambda: None)
        self.Bind(wx.EVT_PAINT, self.OnPaint)
        # Swallow the close, so EventGhost shutting down cannot leave a
        # half-destroyed frame behind a pending timer.
        self.Bind(wx.EVT_CLOSE, lambda event: None)

        # Never take focus. Show/Raise would, and a media keypress must not
        # pull the caret out of whatever the user is typing in.
        handle = self.GetHandle()
        style = _user32.GetWindowLongW(handle, GWL_EXSTYLE)
        _user32.SetWindowLongW(handle, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)

    def OnPaint(self, event=None):
        # A paint DC is created even when unused: without one the update
        # region is never validated and Windows re-sends WM_PAINT forever,
        # which would be 100% of the wx main thread.
        if self.layered:
            wx.PaintDC(self)
            return
        wx.BufferedPaintDC(self, self.bitmap)

    # Deliberately not called Show/Hide: those are wx.Window methods, and
    # overriding them means self.Show(True) recurses into this instead of
    # showing the window.
    def Display(self, title, artist, app, status, artworkPath, timeout,
                displayNumber, onError=None):
        """Must run on the wx main thread."""
        self.timer.cancel()

        artwork = _load_artwork(artworkPath)
        panel = _render(title, artist, app, status, artwork)
        self.bitmap = panel.bitmap

        display = wx.Display(
            displayNumber if displayNumber < wx.Display.GetCount() else 0)
        area = display.GetClientArea()
        position = (area.x + 12, area.y + 12)

        try:
            self._paint_layered(panel, position)
        except Exception, exc:
            if onError is not None:
                onError(exc)
            self._paint_shaped(panel, position)

        self._show()

        if timeout > 0:
            self.timer = threading.Timer(timeout, self.Retire)
            self.timer.daemon = True
            self.timer.start()

    def _paint_layered(self, panel, position):
        handle = self.GetHandle()

        buffer = _argb_buffer(panel)
        expected = panel.width * panel.height * 4
        if len(buffer) != expected:
            raise RuntimeError("alpha buffer is %d bytes, expected %d"
                               % (len(buffer), expected))

        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = panel.width
        header.biHeight = -panel.height  # top-down
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB

        screenDC = _user32.GetDC(None)
        memoryDC = _gdi32.CreateCompatibleDC(screenDC)
        bits = ctypes.c_void_p()
        dib = _gdi32.CreateDIBSection(
            memoryDC, ctypes.byref(header), DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0)
        if not dib:
            _gdi32.DeleteDC(memoryDC)
            _user32.ReleaseDC(None, screenDC)
            raise RuntimeError("CreateDIBSection failed")

        old = _gdi32.SelectObject(memoryDC, dib)
        try:
            ctypes.memmove(bits, bytes(buffer), len(buffer))

            # A window region clips the layered composite, so a region left
            # over from an earlier fallback would crop this one.
            if self.shaped:
                self.SetShape(wx.Region())
                self.shaped = False

            # The layered bit goes on only now that there is content for it.
            # Set earlier, any failure above would leave the window layered
            # with nothing composited, which is completely invisible.
            style = _user32.GetWindowLongW(handle, GWL_EXSTYLE)
            _user32.SetWindowLongW(handle, GWL_EXSTYLE, style | WS_EX_LAYERED)

            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            source = POINT(0, 0)
            destination = POINT(int(position[0]), int(position[1]))
            size = SIZE(panel.width, panel.height)

            if not _user32.UpdateLayeredWindow(
                    handle, screenDC, ctypes.byref(destination),
                    ctypes.byref(size), memoryDC, ctypes.byref(source), 0,
                    ctypes.byref(blend), ULW_ALPHA):
                raise ctypes.WinError()
            self.layered = True
        finally:
            _gdi32.SelectObject(memoryDC, old)
            _gdi32.DeleteObject(dib)
            _gdi32.DeleteDC(memoryDC)
            _user32.ReleaseDC(None, screenDC)

    def _paint_shaped(self, panel, position):
        """Square corners and no shadow, but visible."""
        handle = self.GetHandle()
        style = _user32.GetWindowLongW(handle, GWL_EXSTYLE)
        if style & WS_EX_LAYERED:
            # Clearing the bit needs a frame change to take effect, and until
            # it does the window composites nothing and shows nothing.
            _user32.SetWindowLongW(handle, GWL_EXSTYLE,
                                   style & ~WS_EX_LAYERED)
            _user32.SetWindowPos(handle, None, 0, 0, 0, 0,
                                 HWND_FLAGS | SWP_NOMOVE | SWP_NOSIZE |
                                 SWP_NOZORDER)
        self.layered = False

        self.SetSize((panel.width, panel.height))
        self.SetPosition(position)
        # The panel is a known rectangle inside the shadow band, so build the
        # region directly instead of scanning 60k pixels to rediscover it.
        self.SetShape(wx.Region(SHADOW, SHADOW, panel.innerWidth,
                                panel.innerHeight))
        self.shaped = True
        self.Refresh()

    def _show(self):
        # SetWindowPos with SWP_NOACTIVATE, not Show/Raise: on wxMSW those
        # activate the window and steal focus. UpdateLayeredWindow has
        # already placed and sized the window on the layered path.
        _user32.SetWindowPos(self.GetHandle(), None, 0, 0, 0, 0,
                             HWND_FLAGS | SWP_SHOWWINDOW | SWP_NOMOVE |
                             SWP_NOSIZE | SWP_NOZORDER)

    def Retire(self):
        # Runs on a timer thread, so it has to hop to the wx thread before
        # touching the window. During shutdown there may be no app left to
        # hop to.
        if wx.GetApp() is None:
            return
        try:
            wx.CallAfter(self.RetireNow)
        except Exception:
            pass

    def RetireNow(self):
        # The frame may already be destroyed by the time this runs; a dead
        # wx classic object is falsy.
        if not self:
            return
        try:
            _user32.SetWindowPos(self.GetHandle(), None, 0, 0, 0, 0,
                                 HWND_FLAGS | SWP_HIDEWINDOW | SWP_NOMOVE |
                                 SWP_NOSIZE | SWP_NOZORDER)
        except Exception:
            pass

    def Dispose(self):
        """Cancel the timer and destroy the frame. Called from __close__.

        Not named Close: wx.Window.Close takes a force argument, and this
        file already argues that shadowing wx.Window methods is how you get
        a surprise later.
        """
        self.timer.cancel()
        if self:
            self.Destroy()
