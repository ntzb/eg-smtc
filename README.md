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
  `eg.result`, or `None` when there is no session at all.
- **Get Artwork** - writes the session's artwork to a temp file and returns
  the path, or `None` when there is no session or it publishes no artwork.
- **List Sessions** - puts every session into `eg.result` as
  `{app, status, current, picked}`, for diagnosing which one a press acts on.
- **Control / Toggle, Next, Previous, Play, Pause, Stop** - sends a transport
  command to the selected session.

The plugin module also exposes `NowPlaying()`, `Sessions()`, `Thumbnail()`,
`ThumbnailWithCode()` and `Control()` for use from a Python Script action.

## Why a DLL and not a second process

The session lives behind a WinRT API that has to be called from a
multi-threaded apartment, and the result is wanted synchronously while
handling a button press.
A DLL in EventGhost's own process costs about a megabyte of address space and
answers in single-digit milliseconds.
A helper process would mean a cold start on every keypress and another thing
to supervise.

Calls arrive on EventGhost's single ActionThread, which is an STA, and which
runs every queued action and event. A call that blocked there would stall the
whole setup, so each export marshals its work onto a worker thread in a
multi-threaded apartment and waits with a deadline. On timeout the caller
marks the request abandoned and returns; the worker finishes in its own time,
skips the side effect and discards the result.

The worker is started on demand and retires after a couple of idle seconds. A
thread sitting in the MTA blocks combase's process-detach handler, so a worker
that lived for the process lifetime would hang EventGhost's shutdown.

## Architecture notes

### Which session gets acted on

`GetCurrentSession()` is documented as "the session the system believes the
user would most likely want to control", and the obvious design is to honour
it so that dispatch matches a media key press.
That turned out to be wrong in practice, so this plugin **deliberately
diverges** from it.

An app keeps its SMTC session for as long as it runs.
Spotify's desktop client has no stop at all, only pause, so its session sits
in `Paused` indefinitely while the app is open.
Chromium registers a session per media element and drops it on every
navigation or video change.
Put those together and Windows hands back the paused background player as
"current" during the window where the playing app has no session, which was
observed on the target machine: a video playing in Brave, and the API
reporting Spotify, paused, untouched for hours.
Acting on that would resume Spotify instead of pausing the video.

So every session is ranked and the best one wins, in this order:

1. supports the requested command, when one was given, since acting on a
   session that cannot skip tracks only produces a refusal
2. playback state, `Playing` > `Changing` > `Opened` > `Paused` > `Stopped` >
   `Closed`, with `Changing` above `Paused` because that is what Chromium
   reports mid-navigation
3. whatever Windows considers current, which is the right tiebreak when two
   things are genuinely playing
4. the session's own `LastUpdatedTime`, which is what distinguishes the video
   you just paused from the player that has been idle for an hour

`smtc_sessions` reports both `current` and `picked` so the two can be
compared when the choice looks wrong.

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
| `smtc_now_playing(wchar_t*, int)` | JSON describing the selected session; 4096 chars is enough |
| `smtc_sessions(wchar_t*, int)` | JSON array of sessions with `current`/`picked`; at most 12, then `{"truncated":true}` |
| `smtc_control(const wchar_t*)` | `toggle`/`next`/`previous`/`play`/`pause`/`stop` |
| `smtc_thumbnail(const wchar_t*)` | writes artwork bytes to a path |
| `smtc_last_error(wchar_t*, int)` | detail for the last failure |

Return values:

| Code | Meaning |
| --- | --- |
| `0` | success |
| `1` | no session at all (`smtc_sessions` still writes `[]`) |
| `2` | `smtc_thumbnail` only: a session, but it publishes no artwork |
| `-1` | failure; see `smtc_last_error` |
| `-2` | the media session did not answer within the deadline |
| `-3` | the caller's buffer was too small |
| `-4` | a bad argument, such as an unknown command |
| `-5` | too many requests already queued |
| `-6` | no session can perform that command right now |

`smtc_last_error` describes the most recent failure and is cleared by any call
that did not fail, so a stale message cannot be attributed to a later call.
