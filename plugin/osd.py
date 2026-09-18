# -*- coding: utf-8 -*-
"""A now-playing overlay, in the spirit of the Windows 10 media flyout.

Windows 11 removed that flyout: the volume OSD became a standalone indicator
and media controls moved into Quick Settings, so pressing a media key no
longer shows anything. Nothing in the OS will draw this any more, and the
third-party replacements cost 50-200 MB of resident memory for a panel that
appears for two seconds.

This draws it in EventGhost's own process instead, out of the metadata and
artwork the plugin already has, for the cost of one wx frame.

wxPython here is 3.0.2 classic, as bundled with EventGhost, so the old
spellings (wx.EmptyBitmap, wx.BitmapFromImage, wx.RegionFromBitmap) are the
correct ones rather than legacy aliases.
"""

import os
import threading

import wx

# Laid out to echo the Windows 10 flyout: square artwork on the left, two
# lines of text on the right, dark panel, top-left of the display.
ART_SIZE = 72
PADDING = 14
GUTTER = 12
CORNER = 8
MIN_TEXT_WIDTH = 190
MAX_TEXT_WIDTH = 330

BACKGROUND = (32, 32, 32)
TITLE_COLOUR = (255, 255, 255)
DETAIL_COLOUR = (170, 170, 170)

# Must not occur in the artwork or the panel, since it becomes the
# transparency mask for the window region.
MASK_COLOUR = (255, 0, 255)


def _font(size, bold=False):
    return wx.Font(
        size,
        wx.FONTFAMILY_SWISS,
        wx.FONTSTYLE_NORMAL,
        wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL,
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
            max(1, int(round(width * scale))),
            max(1, int(round(height * scale))),
            wx.IMAGE_QUALITY_HIGH,
        )
        left = max(0, (image.GetWidth() - ART_SIZE) // 2)
        top = max(0, (image.GetHeight() - ART_SIZE) // 2)
        image = image.GetSubImage(
            wx.Rect(left, top,
                    min(ART_SIZE, image.GetWidth()),
                    min(ART_SIZE, image.GetHeight()))
        )
        return wx.BitmapFromImage(image)
    except Exception:
        return None
    finally:
        del log


class OsdFrame(wx.Frame):
    """A shaped, click-through-free overlay that hides itself on a timer.

    Created once and reused. Destroying and recreating a shaped frame per
    press is both slower and a reliable way to leak GDI regions.
    """

    def __init__(self):
        wx.Frame.__init__(
            self,
            None,
            -1,
            "SMTC OSD",
            size=(0, 0),
            style=(wx.FRAME_SHAPED | wx.NO_BORDER | wx.FRAME_NO_TASKBAR |
                   wx.STAY_ON_TOP),
        )
        self.bitmap = wx.EmptyBitmap(1, 1)
        self.timer = threading.Timer(0.0, lambda: None)
        self.Bind(wx.EVT_PAINT, self.OnPaint)
        # Swallow the close so EventGhost shutting down cannot leave a
        # half-destroyed frame behind a pending timer.
        self.Bind(wx.EVT_CLOSE, lambda event: None)

    def OnPaint(self, event=None):
        wx.BufferedPaintDC(self, self.bitmap)

    def Compose(self, title, detail, artwork):
        """Render the panel to a bitmap and return it with its mask."""
        measure = wx.MemoryDC()
        measure.SelectObject(wx.EmptyBitmap(1, 1))
        measure.SetFont(_font(11, bold=True))
        titleWidth = measure.GetTextExtent(title or u" ")[0]
        measure.SetFont(_font(9))
        detailWidth = measure.GetTextExtent(detail or u" ")[0]
        measure.SelectObject(wx.NullBitmap)

        textWidth = max(MIN_TEXT_WIDTH,
                        min(MAX_TEXT_WIDTH, max(titleWidth, detailWidth)))

        artWidth = (ART_SIZE + GUTTER) if artwork else 0
        width = PADDING * 2 + artWidth + textWidth
        height = PADDING * 2 + (ART_SIZE if artwork else 52)

        bitmap = wx.EmptyBitmap(width, height)
        dc = wx.MemoryDC()
        dc.SelectObject(bitmap)

        # Everything outside the rounded panel is masked away.
        dc.SetBackground(wx.Brush(MASK_COLOUR, wx.SOLID))
        dc.Clear()
        dc.SetBrush(wx.Brush(BACKGROUND, wx.SOLID))
        dc.SetPen(wx.Pen(BACKGROUND, 1))
        dc.DrawRoundedRectangle(0, 0, width, height, CORNER)

        if artwork:
            dc.DrawBitmap(artwork, PADDING, PADDING, True)

        textLeft = PADDING + artWidth
        dc.SetFont(_font(11, bold=True))
        dc.SetTextForeground(TITLE_COLOUR)
        shownTitle = _elide(dc, title, textWidth)
        titleHeight = dc.GetTextExtent(shownTitle or u" ")[1]

        dc.SetFont(_font(9))
        shownDetail = _elide(dc, detail, textWidth)
        detailHeight = dc.GetTextExtent(shownDetail or u" ")[1]

        block = titleHeight + (4 + detailHeight if shownDetail else 0)
        top = (height - block) // 2

        dc.SetFont(_font(11, bold=True))
        dc.SetTextForeground(TITLE_COLOUR)
        dc.DrawText(shownTitle, textLeft, top)
        if shownDetail:
            dc.SetFont(_font(9))
            dc.SetTextForeground(DETAIL_COLOUR)
            dc.DrawText(shownDetail, textLeft, top + titleHeight + 4)

        dc.SelectObject(wx.NullBitmap)
        bitmap.SetMask(wx.Mask(bitmap, MASK_COLOUR))
        return bitmap

    # Deliberately not called Show/Hide: those are wx.Window methods, and
    # overriding them means self.Show(True) recurses into this instead of
    # showing the window.
    def Display(self, title, detail, artworkPath, timeout, displayNumber):
        """Must run on the wx main thread."""
        self.timer.cancel()

        artwork = _load_artwork(artworkPath)
        self.bitmap = self.Compose(title, detail, artwork)
        width, height = self.bitmap.GetSize()

        self.SetSize((width, height))
        self.SetShape(wx.RegionFromBitmap(self.bitmap))

        display = wx.Display(
            displayNumber if displayNumber < wx.Display.GetCount() else 0)
        area = display.GetClientArea()
        self.SetPosition((area.x + 16, area.y + 16))

        self.Refresh()
        if self.IsShown():
            self.Raise()
        else:
            self.Show(True)

        if timeout > 0:
            self.timer = threading.Timer(timeout, self.Retire)
            self.timer.daemon = True
            self.timer.start()

    def Retire(self):
        # Runs on a timer thread, so it has to hop to the wx thread before
        # touching the window.
        wx.CallAfter(self.RetireNow)

    def RetireNow(self):
        self.Show(False)
