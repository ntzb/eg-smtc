// Flat C surface over Windows.Media.Control, for in-process use by
// EventGhost (32-bit Python 2.7, which has no WinRT projection of its own).
//
// Every export marshals its work onto a dedicated multi-threaded-apartment
// thread and waits with a deadline. Two reasons: C++/WinRT forbids blocking
// waits on an async operation from a single-threaded apartment, and
// EventGhost's caller is wx's STA main thread, which must never be parked on
// a media session that has stopped responding.

#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Media.Control.h>
#include <winrt/Windows.Storage.Streams.h>

#include <cstdio>
#include <cstring>
#include <future>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>

using namespace winrt;
using namespace winrt::Windows::Media::Control;
using namespace winrt::Windows::Storage::Streams;

namespace {

constexpr auto kDefaultTimeout = std::chrono::seconds(3);

// Error codes returned to the caller. Negative values are failures.
constexpr int kOk = 0;
constexpr int kNoSession = 1;
constexpr int kErrFailed = -1;
constexpr int kErrTimeout = -2;
constexpr int kErrBuffer = -3;
constexpr int kErrArgument = -4;

std::wstring g_lastErrorShared;
std::mutex g_lastErrorMutex;

void SetLastError(std::wstring message) {
    std::lock_guard<std::mutex> guard(g_lastErrorMutex);
    g_lastErrorShared = std::move(message);
}

// Runs work on an MTA thread, bounded by a deadline. A timed-out thread is
// deliberately abandoned rather than joined: it is blocked inside WinRT and
// cannot be cancelled, and detaching keeps the caller responsive.
template <typename Fn>
int RunBounded(Fn work, std::chrono::milliseconds timeout) {
    auto task = std::make_shared<std::packaged_task<int()>>([work]() -> int {
        init_apartment(apartment_type::multi_threaded);
        try {
            return work();
        } catch (hresult_error const& error) {
            SetLastError(std::wstring(error.message().c_str()));
            return kErrFailed;
        } catch (std::exception const& error) {
            std::string what(error.what());
            SetLastError(std::wstring(what.begin(), what.end()));
            return kErrFailed;
        } catch (...) {
            SetLastError(L"unknown failure");
            return kErrFailed;
        }
    });

    auto result = task->get_future();
    std::thread([task]() { (*task)(); }).detach();

    if (result.wait_for(timeout) != std::future_status::ready) {
        SetLastError(L"timed out waiting for the media session");
        return kErrTimeout;
    }
    return result.get();
}

GlobalSystemMediaTransportControlsSession CurrentSession() {
    auto manager =
        GlobalSystemMediaTransportControlsSessionManager::RequestAsync().get();
    return manager.GetCurrentSession();
}

const wchar_t* StatusName(GlobalSystemMediaTransportControlsSessionPlaybackStatus status) {
    using Status = GlobalSystemMediaTransportControlsSessionPlaybackStatus;
    switch (status) {
        case Status::Closed: return L"Closed";
        case Status::Opened: return L"Opened";
        case Status::Changing: return L"Changing";
        case Status::Stopped: return L"Stopped";
        case Status::Playing: return L"Playing";
        case Status::Paused: return L"Paused";
    }
    return L"Unknown";
}

void AppendJsonString(std::wstring& out, std::wstring_view value) {
    out.push_back(L'"');
    for (wchar_t ch : value) {
        switch (ch) {
            case L'"': out.append(L"\\\""); break;
            case L'\\': out.append(L"\\\\"); break;
            case L'\n': out.append(L"\\n"); break;
            case L'\r': out.append(L"\\r"); break;
            case L'\t': out.append(L"\\t"); break;
            default:
                if (ch < 0x20) {
                    wchar_t escape[7];
                    swprintf_s(escape, L"\\u%04x", static_cast<unsigned>(ch));
                    out.append(escape);
                } else {
                    out.push_back(ch);
                }
        }
    }
    out.push_back(L'"');
}

void AppendJsonField(std::wstring& out, const wchar_t* key,
                     std::wstring_view value, bool last = false) {
    AppendJsonString(out, key);
    out.push_back(L':');
    AppendJsonString(out, value);
    if (!last) out.push_back(L',');
}

int CopyOut(std::wstring const& text, wchar_t* buffer, int capacity) {
    if (buffer == nullptr || capacity <= 0) return kErrArgument;
    if (static_cast<size_t>(capacity) <= text.size()) return kErrBuffer;
    memcpy(buffer, text.c_str(), (text.size() + 1) * sizeof(wchar_t));
    return kOk;
}

}  // namespace

extern "C" {

// Writes a JSON object describing the session Windows considers current.
// Returns kNoSession and writes "{}" when nothing is playing.
int __stdcall smtc_now_playing(wchar_t* buffer, int capacity) {
    if (buffer == nullptr || capacity <= 0) return kErrArgument;

    std::wstring json;
    int code = RunBounded(
        [&json]() -> int {
            auto session = CurrentSession();
            if (session == nullptr) {
                json = L"{}";
                return kNoSession;
            }

            auto properties = session.TryGetMediaPropertiesAsync().get();
            auto playback = session.GetPlaybackInfo();

            json.push_back(L'{');
            AppendJsonField(json, L"app", session.SourceAppUserModelId());
            AppendJsonField(json, L"title", properties.Title());
            AppendJsonField(json, L"artist", properties.Artist());
            AppendJsonField(json, L"album", properties.AlbumTitle());
            AppendJsonField(json, L"status", StatusName(playback.PlaybackStatus()),
                            true);
            json.push_back(L'}');
            return kOk;
        },
        kDefaultTimeout);

    if (code < 0) return code;
    int copied = CopyOut(json, buffer, capacity);
    return copied == kOk ? code : copied;
}

// command is one of "toggle", "next", "previous", "play", "pause", "stop".
int __stdcall smtc_control(const wchar_t* command) {
    if (command == nullptr) return kErrArgument;
    std::wstring verb(command);

    return RunBounded(
        [verb]() -> int {
            auto session = CurrentSession();
            if (session == nullptr) return kNoSession;

            bool accepted = false;
            if (verb == L"toggle") {
                accepted = session.TryTogglePlayPauseAsync().get();
            } else if (verb == L"next") {
                accepted = session.TrySkipNextAsync().get();
            } else if (verb == L"previous") {
                accepted = session.TrySkipPreviousAsync().get();
            } else if (verb == L"play") {
                accepted = session.TryPlayAsync().get();
            } else if (verb == L"pause") {
                accepted = session.TryPauseAsync().get();
            } else if (verb == L"stop") {
                accepted = session.TryStopAsync().get();
            } else {
                SetLastError(L"unknown command");
                return kErrArgument;
            }

            if (!accepted) {
                SetLastError(L"the session refused the command");
                return kErrFailed;
            }
            return kOk;
        },
        kDefaultTimeout);
}

// Writes the current session's thumbnail to path, as delivered by the app
// (usually JPEG or PNG; the bytes are not transcoded).
int __stdcall smtc_thumbnail(const wchar_t* path) {
    if (path == nullptr) return kErrArgument;
    std::wstring target(path);

    return RunBounded(
        [target]() -> int {
            auto session = CurrentSession();
            if (session == nullptr) return kNoSession;

            auto properties = session.TryGetMediaPropertiesAsync().get();
            auto reference = properties.Thumbnail();
            if (reference == nullptr) {
                SetLastError(L"this session publishes no thumbnail");
                return kNoSession;
            }

            auto stream = reference.OpenReadAsync().get();
            auto size = static_cast<uint32_t>(stream.Size());
            if (size == 0) {
                SetLastError(L"thumbnail stream was empty");
                return kNoSession;
            }

            Buffer buffer(size);
            stream.ReadAsync(buffer, size, InputStreamOptions::None).get();

            FILE* file = nullptr;
            if (_wfopen_s(&file, target.c_str(), L"wb") != 0 || file == nullptr) {
                SetLastError(L"could not open the thumbnail output file");
                return kErrFailed;
            }
            auto bytes = buffer.data();
            size_t written = fwrite(bytes, 1, buffer.Length(), file);
            fclose(file);

            if (written != buffer.Length()) {
                SetLastError(L"short write to the thumbnail output file");
                return kErrFailed;
            }
            return kOk;
        },
        kDefaultTimeout);
}

// Human-readable detail for the most recent failure, for logging.
int __stdcall smtc_last_error(wchar_t* buffer, int capacity) {
    std::lock_guard<std::mutex> guard(g_lastErrorMutex);
    return CopyOut(g_lastErrorShared, buffer, capacity);
}

}  // extern "C"
