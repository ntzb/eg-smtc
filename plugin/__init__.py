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
import time
import urllib2

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

# Matches the overlay's own cap, so a URL that is not a thumbnail is
# rejected before it is written to disk rather than after.
MAX_ARTWORK_BYTES = 8 * 1024 * 1024


class Text:
    noSession = "No media session is currently active."
    noThumbnail = "The active media session publishes no artwork."
    osdTimeout = "Seconds to show it:"
    osdDisplay = "Show on display:"
    osdTitle = "Title:"
    osdArtist = "Second line:"
    osdApp = "Source:"
    osdArtwork = "Artwork (file path or URL):"
    osdStatus = "Status (Playing or Paused):"


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


# Total wall-clock budget for fetching artwork over the network. urllib2's
# timeout argument is the *socket* timeout: it applies per recv, so a server
# dribbling bytes never trips it, and it does not cover DNS at all, which is
# the slow part when the host is simply off. Only a deadline bounds this.
FETCH_BUDGET_SECONDS = 4.0
FETCH_CHUNK = 64 * 1024


def _fetch_artwork(source):
    """Return (path, owned) for source, fetching it if it is a URL.

    Accepts a path or an http(s) URL, because the obvious thing to show for
    Kodi is the art from its own /image/ endpoint and the overlay needs bytes
    on disk. "owned" says whether the caller should delete the file.

    Must not be called on EventGhost's ActionThread: it does network I/O.
    """
    if not source:
        return None, False
    if not source.lower().startswith((u"http://", u"https://")):
        if os.path.exists(source):
            return source, False
        eg.PrintNotice("SMTC overlay: no artwork at %s" % (source,))
        return None, False

    deadline = time.time() + FETCH_BUDGET_SECONDS
    handle = None
    path = None
    descriptor = None
    try:
        handle = urllib2.urlopen(source, timeout=FETCH_BUDGET_SECONDS)
        chunks = []
        total = 0
        while total <= MAX_ARTWORK_BYTES:
            if time.time() > deadline:
                eg.PrintNotice("SMTC overlay: artwork fetch timed out: %s"
                               % (source,))
                return None, False
            chunk = handle.read(FETCH_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)

        if not total or total > MAX_ARTWORK_BYTES:
            eg.PrintNotice("SMTC overlay: artwork was empty or too large: %s"
                           % (source,))
            return None, False

        descriptor, path = tempfile.mkstemp(
            prefix=u"eg-smtc-", suffix=u".img",
            dir=unicode(tempfile.gettempdir()))
        stream = os.fdopen(descriptor, "wb")
        descriptor = None  # the file object owns it now
        try:
            stream.write("".join(chunks))
        finally:
            stream.close()
        return path, True
    except Exception, exc:
        eg.PrintNotice("SMTC overlay: could not fetch artwork from %s: %s"
                       % (source, exc))
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if path:
            _discard(path)
        return None, False
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def _friendly_app(appId):
    """Turn a source app user model id into something worth showing.

    These arrive as "Spotify.exe" or a long packaged-app identity, neither of
    which belongs in an overlay.
    """
    name = appId.rsplit(u"!", 1)[-1]
    if name.lower().endswith(u".exe"):
        name = name[:-4]
    return name.replace(u"_", u" ").strip() or appId


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

    def DrawLater(self, title, artist, app, status, source, timeout,
                  displayNumber):
        """Fetch the artwork off-thread, then draw.

        Returns at once. A generation counter means a slow fetch cannot paint
        over an overlay that a later press has already drawn.
        """
        self.generation += 1
        generation = self.generation

        def fetch():
            path, owned = None, False
            try:
                path, owned = _fetch_artwork(source)
                if generation != self.generation:
                    # Superseded while we were waiting on the network.
                    if path and owned:
                        _discard(path)
                    return
                self.DrawOverlay(title, artist, app, status, path, timeout,
                                 displayNumber, ownsArtwork=owned)
            except Exception:
                if path and owned:
                    _discard(path)
                eg.PrintTraceback("SMTC overlay failed to prepare")

        worker = threading.Thread(target=fetch, name="SMTC overlay")
        worker.daemon = True
        worker.start()

    def DrawOverlay(self, title, artist, app, status, artwork, timeout,
                    displayNumber, ownsArtwork=False):
        """Queue the overlay onto the wx thread and return immediately.

        Deliberately does not wait. Blocking the ActionThread on the wx
        thread can deadlock: the wx thread regularly waits on the
        ActionThread (tree edits, plugin changes), and a plain
        threading.Event does not pump messages, so the two would sit on each
        other until a timeout. It would also stall the keypress for as long
        as rendering takes.

        When ownsArtwork is set, the wx side deletes the artwork file once
        it has been decoded. It defaults to off: this method is reachable
        from a Python Script action, and a caller who passes a path to their
        own cover art and omits the flag should not have it deleted.
        """
        def draw():
            try:
                if self.osdFrame is None:
                    self.osdFrame = osd.OsdFrame()
                self.osdFrame.Display(
                    title, artist, app, status, artwork, timeout,
                    displayNumber,
                    onError=lambda exc: eg.PrintError(
                        "SMTC overlay fell back to a plain window: %s"
                        % (exc,)),
                )
            except Exception:
                # Nothing above this catches, and an exception escaping into
                # wx's CallAfter dispatcher would leave the action reporting
                # success with nothing drawn.
                eg.PrintTraceback("SMTC overlay failed to draw")
            finally:
                # Only a file this plugin created is ours to remove: the
                # Show Overlay action can be pointed at one the user owns.
                if artwork and ownsArtwork:
                    _discard(artwork)

        try:
            wx.CallAfter(draw)
        except Exception:
            # No app to hand it to, at shutdown. draw() will never run, so
            # nothing else would clean up after it.
            if artwork and ownsArtwork:
                _discard(artwork)
            raise

    def __close__(self):
        # Deleting the plugin from the tree would otherwise leave a live
        # hidden top-level window with a pending timer behind it.
        frame, self.osdFrame = self.osdFrame, None
        if frame is not None:
            wx.CallAfter(frame.Dispose)

    def __init__(self):
        self.osdFrame = None
        self.generation = 0
        self.AddAction(GetNowPlaying)
        self.AddAction(GetThumbnail)
        self.AddAction(GetSessions)
        self.AddAction(ShowNowPlaying)
        self.AddAction(ShowOverlay)
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

    def Run(self, *args):
        raise NotImplementedError

    def __call__(self, *args):
        # *args is required, not tidiness: EventGhost compiles an action's
        # saved arguments into a CallWrapper that calls self(*args), so a
        # parameterised action raises TypeError here the moment it is
        # configured, and a TypeError is not caught below.
        try:
            return self.Run(*args)
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
        # On a first configure, ConfigPanel disables OK until a control fires
        # SetIsDirty, so an action whose defaults are already valid can never
        # be accepted: the user has to nudge a spinner to enable the button.
        # Marking it dirty up front says the panel does have a usable result.
        panel.SetIsDirty()
        while panel.Affirmed():
            panel.SetResult(timeoutCtrl.GetValue(), displayChoice.GetValue())

    def GetLabel(self, timeout=3.0, displayNumber=0):
        # The default would render the tree entry as "Show Now Playing: 3.0".
        return self.name

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
        artist = info.get("artist") or info.get("album") or u""
        app = _friendly_app(info.get("app") or u"")

        # ShowOverlay takes ownership of the artwork file and deletes it
        # once decoded, since it renders asynchronously.
        self.plugin.DrawOverlay(title, artist, app,
                                info.get("status") or u"", artwork, timeout,
                                displayNumber, ownsArtwork=True)
        return info


class ShowOverlay(SmtcActionBase):
    name = "Show Overlay"
    description = (
        "Shows the same overlay with content you supply, for a player the "
        "System Media Transport Controls cannot see. Kodi is the reason this "
        "exists: it registers no session, so a macro has to feed it from "
        "Kodi's own JSON-RPC. The artwork field takes a file path or an "
        "http URL, so Kodi's /image/ endpoint can be used directly. All the "
        "text fields accept EventGhost's {...} substitutions."
    )

    def Configure(self, title=u"", artist=u"", app=u"", artwork=u"",
                  status=u"Playing", timeout=3.0, displayNumber=0):
        panel = eg.ConfigPanel()
        titleCtrl = panel.TextCtrl(title)
        artistCtrl = panel.TextCtrl(artist)
        appCtrl = panel.TextCtrl(app)
        artworkCtrl = panel.TextCtrl(artwork)
        statusCtrl = panel.TextCtrl(status)
        timeoutCtrl = panel.SpinNumCtrl(timeout)
        displayChoice = eg.DisplayChoice(panel, displayNumber)
        panel.AddLine(Text.osdTitle, titleCtrl)
        panel.AddLine(Text.osdArtist, artistCtrl)
        panel.AddLine(Text.osdApp, appCtrl)
        panel.AddLine(Text.osdArtwork, artworkCtrl)
        panel.AddLine(Text.osdStatus, statusCtrl)
        panel.AddLine(Text.osdTimeout, timeoutCtrl)
        panel.AddLine(Text.osdDisplay, displayChoice)
        panel.SetIsDirty()
        while panel.Affirmed():
            panel.SetResult(titleCtrl.GetValue(), artistCtrl.GetValue(),
                            appCtrl.GetValue(), artworkCtrl.GetValue(),
                            statusCtrl.GetValue(), timeoutCtrl.GetValue(),
                            displayChoice.GetValue())

    def GetLabel(self, title=u"", *args):
        return "%s: %s" % (self.name, title) if title else self.name

    def Run(self, title=u"", artist=u"", app=u"", artwork=u"",
            status=u"Playing", timeout=3.0, displayNumber=0):
        title = self._parse("Title", title)
        artist = self._parse("Second line", artist)
        app = self._parse("Source", app)
        source = self._parse("Artwork", artwork)
        status = self._parse("Status", status) or u"Playing"

        # Fetched on a throwaway thread, never here. urllib2 can block for as
        # long as a DNS lookup takes, and this is EventGhost's single
        # ActionThread: stalling it stalls every queued action and event, not
        # just this macro. The wx thread would be no better, since that
        # freezes the UI.
        self.plugin.DrawLater(title, artist, app, status, source, timeout,
                              displayNumber)
        return None

    def _parse(self, field, value):
        """eg.ParseString, with a readable error naming the offending box.

        A stray brace in a title, "Live at {The Venue", raises SyntaxError,
        and an expression inside braces can raise anything at all. Without
        this the user gets a full traceback on every press and no clue which
        field caused it.
        """
        try:
            return eg.ParseString(value)
        except Exception, exc:
            raise self.Exception("%s: %s" % (field, exc))


class ControlActionBase(SmtcActionBase):
    command = None

    def Run(self):
        accepted = Control(self.command)
        if accepted is None:
            eg.PrintNotice(Text.noSession)
        return accepted
