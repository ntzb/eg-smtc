# eg-smtc

An EventGhost plugin that reads and controls the Windows **System Media
Transport Controls** (SMTC): the media session Windows itself targets when a
media key is pressed, whether that is a browser tab, Spotify, Media Player or
anything else that registers with it.

It exists because EventGhost runs on Python 2.7, which has no WinRT
projection.
PyWinRT and `winsdk` are both Python 3.9+, and `Windows.Media.Control` has no
classic COM surface.
So the WinRT work happens in a small native DLL that the plugin loads
in-process with `ctypes`.

## What it provides

Actions:

- **Get Now Playing** - puts `{app, title, artist, album, status}` into
  `eg.result`, or `None` when nothing is playing.
- **Get Artwork** - writes the session's artwork to a temp file and returns
  the path, or `None` when the session publishes none.
- **Control / Toggle, Next, Previous, Play, Pause, Stop** - sends a transport
  command to the current session.

The plugin module also exposes `NowPlaying()`, `Thumbnail()` and
`Control()` for use from a Python Script action.

## Why a DLL and not a second process

The session lives behind a WinRT API that has to be called from a
multi-threaded apartment, and the result is wanted synchronously while
handling a button press.
A DLL in EventGhost's own process costs about a megabyte of address space and
answers in single-digit milliseconds.
A helper process would mean a cold start on every keypress and another thing
to supervise.

Each export marshals its work onto a dedicated MTA thread and waits with a
deadline, so a media session that stops responding cannot park EventGhost's wx
main thread.

## Architecture notes

`GlobalSystemMediaTransportControlsSessionManager.GetCurrentSession()` returns
"the session the system believes the user would most likely want to control",
which is the same arbitration Windows applies to a media key press.
Honouring it keeps dispatch consistent with the rest of the system instead of
guessing from the foreground window.

Kodi does **not** register with SMTC and so never appears here.
That is a gap in Kodi, not in this plugin.
Anything driving Kodi should ask Kodi directly over JSON-RPC and treat SMTC as
the source for everything else.

Reading SMTC does not require package identity.
`Windows.Media.Control` is absent from Microsoft's list of WinRT APIs that do,
and the `globalMediaControl` capability shown in the class reference is an
appx manifest declaration that is enforced for packaged apps only.

## Building

The DLL must be **x86**, because EventGhost is a 32-bit process.
CMake fails the configure step otherwise rather than producing a DLL that
cannot load.

```
cmake -S . -B build -A Win32
cmake --build build --config Release
```

Needs MSVC with the Windows SDK, for the C++/WinRT headers and
`WindowsApp.lib`.
CI builds it on `windows-latest` and publishes a ready-to-drop plugin folder
as an artifact, so a local toolchain is not required.

## Installing

Take the `SMTC-plugin` artifact from a CI run (or a release zip) and copy the
`SMTC` folder into EventGhost's `plugins` directory, so that you end up with:

```
EventGhost\plugins\SMTC\__init__.py
EventGhost\plugins\SMTC\egsmtc.dll
```

Then add the plugin from EventGhost's Add Plugin dialog, under Other.

## Exports

| Export | Meaning |
| --- | --- |
| `smtc_now_playing(wchar_t*, int)` | JSON describing the current session |
| `smtc_control(const wchar_t*)` | `toggle`/`next`/`previous`/`play`/`pause`/`stop` |
| `smtc_thumbnail(const wchar_t*)` | writes artwork bytes to a path |
| `smtc_last_error(wchar_t*, int)` | detail for the last failure |

All return `0` on success, `1` when there is no session, and a negative value
on failure.
