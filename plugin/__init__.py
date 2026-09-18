# -*- coding: utf-8 -*-

eg.RegisterPlugin(
    name="System Media Transport Controls",
    author="ntzb",
    version="1.0.0",
    kind="other",
    guid="{6F3A2B1E-4C8D-4E5A-9B72-0D1E5C7A3F44}",
    description=(
        "Reads and controls whatever Windows currently considers the active "
        "media session: browsers, Spotify, Media Player and anything else "
        "that registers with the System Media Transport Controls."
    ),
)

import ctypes
import json
import os
import tempfile

# Return codes from egsmtc.dll. Zero and positive values are outcomes,
# negative values are failures.
OK = 0
NO_SESSION = 1
NO_THUMBNAIL = 2

# These spellings are a persistence contract: a saved configuration stores the
# generated action class name, so renaming one silently breaks existing trees.
COMMANDS = ("toggle", "next", "previous", "play", "pause", "stop")

BUFFER_CHARS = 4096


class Text:
    noSession = "No media session is currently active."
    noThumbnail = "The current media session publishes no artwork."


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


def Thumbnail(path=None):
    """Write the current session's artwork to disk and return the path.

    Returns None when there is no session, or when the session publishes no
    artwork, which is common for sources that only report a title. When path
    is omitted a temporary file is created and the caller owns it; nothing is
    left behind if there was no artwork to write.
    """
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
        return None
    return path


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

    def __init__(self):
        self.AddAction(GetNowPlaying)
        self.AddAction(GetThumbnail)
        group = self.AddGroup("Control")
        for command in COMMANDS:
            group.AddAction(
                type(
                    "Control" + command.capitalize(),
                    (ControlActionBase,),
                    {
                        "name": command.capitalize(),
                        "description": (
                            "Sends %s to the current media session." % command
                        ),
                        "command": command,
                    },
                )
            )


class SmtcActionBase(eg.ActionBase):
    """Turns a DLL failure into EventGhost's one-line error.

    EventGhost prints a full traceback for any exception that is not an
    eg.Exception, and leaves eg.result holding the previous action's value.
    For an expected runtime failure, such as the session going away mid-call,
    a single logged line is the right outcome.
    """

    def Run(self):
        raise NotImplementedError

    def __call__(self):
        try:
            return self.Run()
        except SmtcDllError, exc:
            raise self.Exception(str(exc))


class GetNowPlaying(SmtcActionBase):
    name = "Get Now Playing"
    description = (
        "Puts a dict describing the current media session into eg.result, "
        "or None when nothing is playing."
    )

    def Run(self):
        info = NowPlaying()
        if info is None:
            eg.PrintNotice(Text.noSession)
        return info


class GetThumbnail(SmtcActionBase):
    name = "Get Artwork"
    description = (
        "Writes the current session's artwork to a temporary file and puts "
        "the path into eg.result, or None when there is no artwork. The "
        "caller owns the file and should delete it when done."
    )

    def Run(self):
        path = Thumbnail()
        if path is None:
            eg.PrintNotice(Text.noThumbnail)
        return path


class ControlActionBase(SmtcActionBase):
    command = None

    def Run(self):
        accepted = Control(self.command)
        if accepted is None:
            eg.PrintNotice(Text.noSession)
        return accepted
