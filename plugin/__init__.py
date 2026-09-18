# -*- coding: utf-8 -*-

eg.RegisterPlugin(
    name="System Media Transport Controls",
    author="ntzb",
    version="1.0.0",
    kind="other",
    guid="{6F3A2B1E-4C8D-4E5A-9B72-0D1E5C7A3F44}",
    description=(
        "Reads and controls the active media session: browsers, Spotify, "
        "Media Player and anything else that registers with the System Media "
        "Transport Controls. Prefers whatever is actually playing over a "
        "paused background player."
    ),
)

import ctypes
import json
import os
import tempfile
import threading

import wx

import osd

# Return codes from egsmtc.dll. Zero and positive values are outcomes,
# negative values are failures.
OK = 0
NO_SESSION = 1
NO_THUMBNAIL = 2
BUFFER_TOO_SMALL = -3

# These spellings are a persistence contract: a saved configuration stores the
# generated action class name, so renaming one silently breaks existing trees.
COMMANDS = ("toggle", "next", "previous", "play", "pause", "stop")

BUFFER_CHARS = 4096


class Text:
    noSession = "No media session is currently active."
    noThumbnail = "The active media session publishes no artwork."
    osdTimeout = "Seconds to show it:"
    osdDisplay = "Show on display:"


class SmtcDllError(Exception):
    pass


class Library(object):
    """Lazy holder for egsmtc.dll.

    Loading is deferred so that a missing or wrong-architecture DLL surfaces
    as a plugin error the user can read, rather than an import-time failure
    that stops EventGhost from starting.
    """

    def __init__(self):
        self._dll = None

    def __call__(self):
        if self._dll is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "egsmtc.dll")
            if not os.path.exists(path):
                raise SmtcDllError("egsmtc.dll is missing from %s" %
                                   os.path.dirname(path))
            try:
                dll = ctypes.WinDLL(path)
            except WindowsError, exc:
                raise SmtcDllError(
                    "could not load egsmtc.dll (is it the 32-bit build?): %s"
                    % (exc,))
            dll.smtc_now_playing.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
            dll.smtc_now_playing.restype = ctypes.c_int
            dll.smtc_sessions.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
            dll.smtc_sessions.restype = ctypes.c_int
            dll.smtc_control.argtypes = [ctypes.c_wchar_p]
            dll.smtc_control.restype = ctypes.c_int
            dll.smtc_thumbnail.argtypes = [ctypes.c_wchar_p]
            dll.smtc_thumbnail.restype = ctypes.c_int
            dll.smtc_last_error.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
            dll.smtc_last_error.restype = ctypes.c_int
            self._dll = dll
        return self._dll


library = Library()


def _describe_error(dll, code):
    buf = ctypes.create_unicode_buffer(BUFFER_CHARS)
    detail = u""
    if dll.smtc_last_error(buf, BUFFER_CHARS) == OK:
        detail = buf.value
    return "egsmtc returned %d%s" % (code, detail and (": " + detail) or "")


def NowPlaying():
    """Return a dict describing the current session, or None if there is none.

    Keys: app, title, artist, album, status. The app value is the source
    application's user model id, which is how Windows itself identifies the
    owner of the session.
    """
    dll = library()
    buf = ctypes.create_unicode_buffer(BUFFER_CHARS)
    code = dll.smtc_now_playing(buf, BUFFER_CHARS)
    if code < 0:
        raise SmtcDllError(_describe_error(dll, code))
    if code == NO_SESSION:
        return None
    return json.loads(buf.value)


def Sessions():
    """Return a list of the sessions Windows knows about.

    Each entry is {app, status, current, picked}. "current" is Windows' own
    arbitration; "picked" is the one this plugin would act on. They disagree
    exactly when the selection rule is earning its keep, which is what makes
    this worth logging: a paused background player keeps its session for as
    long as the app runs, and Windows reports it as current whenever the
    playing app's session is momentarily absent.

    A final {"truncated": True} entry means there were more sessions than the
    DLL lists.
    """
    dll = library()
    for chars in (BUFFER_CHARS, BUFFER_CHARS * 4):
        buf = ctypes.create_unicode_buffer(chars)
        code = dll.smtc_sessions(buf, chars)
        if code == BUFFER_TOO_SMALL:
            continue
        if code < 0:
            raise SmtcDllError(_describe_error(dll, code))
        return json.loads(buf.value)
    raise SmtcDllError(_describe_error(dll, BUFFER_TOO_SMALL))


def Thumbnail(path=None):
    """Write the active session's artwork to disk and return the path.

    Returns None when there is no session, or when the session publishes no
    artwork, which is common for sources that only report a title. When path
    is omitted a temporary file is created and the caller owns it; nothing is
    left behind if there was no artwork to write. Use ThumbnailWithCode when
    the two None cases need telling apart.
    """
    return ThumbnailWithCode(path)[1]


def ThumbnailWithCode(path=None):
    """As Thumbnail, but returns (code, path) so the caller can distinguish
    "nothing is playing" from "this session has no artwork"."""
    dll = library()
    # A unicode directory keeps mkstemp's result unicode. ctypes would
    # otherwise decode a byte path with mbcs and the 'ignore' handler, which
    # drops unconvertible characters silently and yields a path that does not
    # exist.
    generated = path is None
    if generated:
        handle, path = tempfile.mkstemp(
            prefix=u"eg-smtc-", suffix=u".img",
            dir=unicode(tempfile.gettempdir()),
        )
        os.close(handle)
    elif isinstance(path, str):
        path = path.decode("mbcs")

    try:
        code = dll.smtc_thumbnail(path)
    except Exception:
        if generated:
            _discard(path)
        raise

    if code < 0:
        if generated:
            _discard(path)
        raise SmtcDllError(_describe_error(dll, code))
    if code in (NO_SESSION, NO_THUMBNAIL):
        if generated:
            _discard(path)
        return code, None
    return code, path


def _discard(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def Control(command):
    """Send a transport command. Returns None if there is no session."""
    if command not in COMMANDS:
        raise ValueError("unknown command: %r" % (command,))
    dll = library()
    code = dll.smtc_control(unicode(command))
    if code < 0:
        raise SmtcDllError(_describe_error(dll, code))
    if code == NO_SESSION:
        return None
    return True


class SMTC(eg.PluginClass):
    text = Text

    def ShowOverlay(self, title, detail, artwork, timeout, displayNumber):
        """Draw the overlay, waiting until it has been rendered.

        Everything wx touches has to happen on the main thread, and the
        artwork file is deleted as soon as this returns, so the caller cannot
        be allowed to race the rendering. eg.actionThread.WaitOnEvent is how
        EventGhost's own OSD action does this.
        """
        done = threading.Event()

        def draw():
            try:
                if self.osdFrame is None:
                    self.osdFrame = osd.OsdFrame()
                self.osdFrame.Display(title, detail, artwork, timeout,
                                      displayNumber)
            finally:
                done.set()

        wx.CallAfter(draw)
        done.wait(5.0)

    def __init__(self):
        self.osdFrame = None
        self.AddAction(GetNowPlaying)
        self.AddAction(GetThumbnail)
        self.AddAction(GetSessions)
        self.AddAction(ShowNowPlaying)
        group = self.AddGroup("Control")
        for command in COMMANDS:
            group.AddAction(
                type(
                    "Control" + command.capitalize(),
                    (ControlActionBase,),
                    {
                        "name": command.capitalize(),
                        "description": (
                            "Sends %s to the active media session." % command
                        ),
                        "command": command,
                    },
                )
            )


class SmtcActionBase(eg.ActionBase):
    """Turns a DLL failure into EventGhost's one-line error.

    EventGhost prints a full traceback for any exception that is not an
    eg.Exception. A session going away mid-call is an expected runtime
    failure, so one logged line is the right outcome.

    Note that eg.result is left holding the previous action's value either
    way: EventGhost assigns it from inside the try, so any exception skips
    the assignment. Raising here buys a clean log line, not a defined result.
    """

    def Run(self):
        raise NotImplementedError

    def __call__(self):
        try:
            return self.Run()
        except SmtcDllError, exc:
            # unicode(), not str(): the detail comes from FormatMessage in the
            # system language, and str() on a non-ASCII message raises
            # UnicodeEncodeError, which would escape this handler and produce
            # the very traceback it exists to avoid.
            raise self.Exception(unicode(exc))


class GetNowPlaying(SmtcActionBase):
    name = "Get Now Playing"
    description = (
        "Puts a dict describing the active media session into eg.result, "
        "or None when nothing is playing."
    )

    def Run(self):
        info = NowPlaying()
        if info is None:
            eg.PrintNotice(Text.noSession)
        return info


class GetSessions(SmtcActionBase):
    name = "List Sessions"
    description = (
        "Puts a list of every media session into eg.result, each as "
        "{app, status, current}. For diagnosing which session a press will "
        "act on."
    )

    def Run(self):
        return Sessions()


class GetThumbnail(SmtcActionBase):
    name = "Get Artwork"
    description = (
        "Writes the active session's artwork to a temporary file and puts "
        "the path into eg.result, or None when there is no artwork. The "
        "caller owns the file and should delete it when done."
    )

    def Run(self):
        code, path = ThumbnailWithCode()
        if path is None:
            eg.PrintNotice(
                Text.noSession if code == NO_SESSION else Text.noThumbnail)
        return path


class ShowNowPlaying(SmtcActionBase):
    name = "Show Now Playing"
    description = (
        "Shows a now-playing overlay with artwork, in the spirit of the "
        "Windows 10 media flyout that Windows 11 removed. Does nothing when "
        "there is no session."
    )

    def Configure(self, timeout=3.0, displayNumber=0):
        panel = eg.ConfigPanel()
        # eg.DisplayChoice, not panel.DisplayChoice: the ConfigPanel mixin
        # provides SpinNumCtrl and friends but not this one, which is how
        # EventGhost's own ShowOSD action builds the same pair.
        timeoutCtrl = panel.SpinNumCtrl(timeout)
        displayChoice = eg.DisplayChoice(panel, displayNumber)
        panel.AddLine(Text.osdTimeout, timeoutCtrl)
        panel.AddLine(Text.osdDisplay, displayChoice)
        while panel.Affirmed():
            panel.SetResult(timeoutCtrl.GetValue(), displayChoice.GetValue())

    def Run(self, timeout=3.0, displayNumber=0):
        info = NowPlaying()
        if info is None:
            eg.PrintNotice(Text.noSession)
            return None

        # Artwork is optional: a session that publishes none still gets a
        # text-only panel rather than nothing at all.
        artwork = None
        try:
            artwork = ThumbnailWithCode()[1]
        except SmtcDllError, exc:
            eg.PrintNotice("Artwork unavailable: %s" % (unicode(exc),))

        title = info.get("title") or info.get("app") or u""
        detail = info.get("artist") or info.get("album") or u""

        try:
            self.plugin.ShowOverlay(title, detail, artwork, timeout,
                                    displayNumber)
        finally:
            # The overlay has the pixels by the time it returns, so the file
            # has done its job. Leaving it would fill %TEMP% one press at a
            # time.
            if artwork:
                _discard(artwork)
        return info


class ControlActionBase(SmtcActionBase):
    command = None

    def Run(self):
        accepted = Control(self.command)
        if accepted is None:
            eg.PrintNotice(Text.noSession)
        return accepted
