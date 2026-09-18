// Flat C surface over Windows.Media.Control, for in-process use by
// EventGhost (32-bit Python 2.7, which has no WinRT projection of its own).
//
// Calls arrive on EventGhost's single ActionThread, which is a COM
// single-threaded apartment. That matters twice over. C++/WinRT forbids
// blocking waits on an async operation from an STA, so the work has to be
// marshalled elsewhere. And because every queued EventGhost action and event
// runs on that one thread, a call that blocks stalls the whole automation
// setup, not just the macro that made it.
//
// So: one long-lived multi-threaded-apartment worker thread serves a queue,
// and each caller waits with a deadline. On timeout the caller marks the
// request abandoned and returns; the worker finishes in its own time, skips
// the side effect and discards the result. Nothing shared with a caller
// outlives the call, and the thread count stays at one however badly a media
// app misbehaves.

#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Media.Control.h>
#include <winrt/Windows.Storage.Streams.h>

#include <windows.h>

#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>

using namespace winrt;
using namespace winrt::Windows::Media::Control;
using namespace winrt::Windows::Storage::Streams;

namespace {

constexpr auto kDefaultTimeout = std::chrono::milliseconds(3000);

// Returned to the caller. Zero and positive values are outcomes, negative
// values are failures.
constexpr int kOk = 0;
constexpr int kNoSession = 1;
constexpr int kNoThumbnail = 2;
constexpr int kErrFailed = -1;
constexpr int kErrTimeout = -2;
constexpr int kErrBuffer = -3;
constexpr int kErrArgument = -4;

// Metadata is truncated per field so the JSON always fits the caller's
// buffer. Failing a call because a title was pathologically long would be a
// worse outcome than shortening it.
constexpr size_t kMaxFieldChars = 512;

std::mutex g_lastErrorMutex;
std::wstring g_lastError;

void PublishError(std::wstring message) {
    std::lock_guard<std::mutex> guard(g_lastErrorMutex);
    g_lastError = std::move(message);
}

// Deliberately not named SetLastError: windows.h declares that, and a future
// call with an integral argument would silently bind to kernel32's version
// and record nothing. The name would also imply GetLastError explains our
// failures, which it does not.
struct Request {
    std::function<int(Request&)> work;

    std::wstring text;   // JSON or other string output
    std::wstring error;  // detail for the caller's log

    std::atomic<bool> abandoned{false};

    std::mutex mutex;
    std::condition_variable ready;
    bool done = false;
    int code = kErrFailed;

    void RecordFailure(std::wstring message) { error = std::move(message); }

    // Checked by the worker before anything observable: a command that is
    // issued, or a file that is written. A caller that has given up must not
    // get a side effect seconds later.
    bool Abandoned() const { return abandoned.load(); }
};

class Worker {
public:
    // Deliberately leaked, and the thread is detached rather than joined.
    // The worker parks in a blocking wait forever, so a destructor could only
    // either call std::terminate on a joinable thread or hang trying to join
    // it. Leaking one object and one idle thread for the process lifetime is
    // the cheaper trade.
    static Worker& Instance() {
        static Worker* worker = new Worker();
        return *worker;
    }

    void Post(std::shared_ptr<Request> request) {
        std::lock_guard<std::mutex> guard(mutex_);
        if (!started_) {
            std::thread([this] { Loop(); }).detach();
            started_ = true;
        }
        queue_.push_back(std::move(request));
        wake_.notify_one();
    }

private:
    void Loop() {
        // The apartment is initialised once, on this thread, and never on a
        // thread EventGhost owns, so RPC_E_CHANGED_MODE is not reachable.
        try {
            init_apartment(apartment_type::multi_threaded);
        } catch (...) {
            PublishError(L"could not initialise a multi-threaded apartment");
            return;
        }

        for (;;) {
            std::shared_ptr<Request> request;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                wake_.wait(lock, [this] { return !queue_.empty(); });
                request = std::move(queue_.front());
                queue_.pop_front();
            }

            int code;
            try {
                code = request->work(*request);
            } catch (hresult_error const& error) {
                request->RecordFailure(std::wstring(error.message().c_str()));
                code = kErrFailed;
            } catch (std::exception const& error) {
                std::string what(error.what());
                request->RecordFailure(std::wstring(what.begin(), what.end()));
                code = kErrFailed;
            } catch (...) {
                request->RecordFailure(L"unknown failure");
                code = kErrFailed;
            }

            {
                std::lock_guard<std::mutex> lock(request->mutex);
                request->code = code;
                request->done = true;
            }
            request->ready.notify_all();
        }
    }

    std::mutex mutex_;
    std::condition_variable wake_;
    std::deque<std::shared_ptr<Request>> queue_;
    bool started_ = false;
};

// Runs work on the worker thread and copies out whatever it produced. The
// request is shared with the worker and outlives this call; the strings the
// caller sees are copies, so an abandoned worker cannot write to anything
// still in scope here.
int Run(std::function<int(Request&)> work, std::wstring& text, std::wstring& error) {
    auto request = std::make_shared<Request>();
    request->work = std::move(work);

    try {
        Worker::Instance().Post(request);
    } catch (...) {
        error = L"could not queue the request";
        return kErrFailed;
    }

    std::unique_lock<std::mutex> lock(request->mutex);
    if (!request->ready.wait_for(lock, kDefaultTimeout,
                                 [&request] { return request->done; })) {
        request->abandoned.store(true);
        error = L"timed out waiting for the media session";
        return kErrTimeout;
    }

    text = request->text;
    error = request->error;
    return request->code;
}

// Cached because it is agile and long-lived, and because each RequestAsync is
// another cross-process call that can hang. The current session still has to
// be fetched every time, since which session is current changes.
// Only ever touched on the worker thread.
GlobalSystemMediaTransportControlsSessionManager const& Manager() {
    static GlobalSystemMediaTransportControlsSessionManager manager =
        GlobalSystemMediaTransportControlsSessionManager::RequestAsync().get();
    return manager;
}

GlobalSystemMediaTransportControlsSession CurrentSession() {
    return Manager().GetCurrentSession();
}

const wchar_t* StatusName(
    GlobalSystemMediaTransportControlsSessionPlaybackStatus status) {
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
    if (value.size() > kMaxFieldChars) value = value.substr(0, kMaxFieldChars);
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

// Every export funnels through here so the published error always belongs to
// the call the caller is about to inspect, rather than to whichever call
// failed most recently.
int Finish(int code, std::wstring const& error) {
    PublishError(code < 0 ? error : std::wstring());
    return code;
}

}  // namespace

extern "C" {

// Writes a JSON object describing the session Windows considers current.
// Returns kNoSession and writes "{}" when nothing is playing.
int __stdcall smtc_now_playing(wchar_t* buffer, int capacity) try {
    if (buffer == nullptr || capacity <= 0) {
        return Finish(kErrArgument, L"invalid buffer");
    }

    std::wstring json;
    std::wstring error;
    int code = Run(
        [](Request& request) -> int {
            auto session = CurrentSession();
            if (session == nullptr) {
                request.text = L"{}";
                return kNoSession;
            }

            auto properties = session.TryGetMediaPropertiesAsync().get();
            auto playback = session.GetPlaybackInfo();

            std::wstring json;
            json.push_back(L'{');
            AppendJsonField(json, L"app", session.SourceAppUserModelId());
            AppendJsonField(json, L"title", properties.Title());
            AppendJsonField(json, L"artist", properties.Artist());
            AppendJsonField(json, L"album", properties.AlbumTitle());
            AppendJsonField(json, L"status", StatusName(playback.PlaybackStatus()),
                            true);
            json.push_back(L'}');

            request.text = std::move(json);
            return kOk;
        },
        json, error);

    if (code < 0) return Finish(code, error);

    int copied = CopyOut(json, buffer, capacity);
    if (copied != kOk) {
        return Finish(copied, L"the caller's buffer is too small for the metadata");
    }
    return Finish(code, error);
} catch (...) {
    return Finish(kErrFailed, L"unhandled failure in smtc_now_playing");
}

// command is one of "toggle", "next", "previous", "play", "pause", "stop".
int __stdcall smtc_control(const wchar_t* command) try {
    if (command == nullptr) return Finish(kErrArgument, L"no command given");
    std::wstring verb(command);

    std::wstring text;
    std::wstring error;
    int code = Run(
        [verb](Request& request) -> int {
            auto session = CurrentSession();
            if (session == nullptr) return kNoSession;

            // The caller may already have given up and retried by now.
            // Issuing this would skip two tracks for one key press.
            if (request.Abandoned()) return kErrTimeout;

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
                request.RecordFailure(L"unknown command");
                return kErrArgument;
            }

            if (!accepted) {
                request.RecordFailure(L"the session refused the command");
                return kErrFailed;
            }
            return kOk;
        },
        text, error);

    return Finish(code, error);
} catch (...) {
    return Finish(kErrFailed, L"unhandled failure in smtc_control");
}

// Writes the current session's artwork to path, as delivered by the app
// (usually JPEG or PNG; the bytes are not transcoded). Returns kNoThumbnail
// when there is a session but it publishes no artwork, which is common.
int __stdcall smtc_thumbnail(const wchar_t* path) try {
    if (path == nullptr) return Finish(kErrArgument, L"no path given");
    std::wstring target(path);

    std::wstring text;
    std::wstring error;
    int code = Run(
        [target](Request& request) -> int {
            auto session = CurrentSession();
            if (session == nullptr) return kNoSession;

            auto properties = session.TryGetMediaPropertiesAsync().get();
            auto reference = properties.Thumbnail();
            if (reference == nullptr) {
                request.RecordFailure(L"this session publishes no artwork");
                return kNoThumbnail;
            }

            auto stream = reference.OpenReadAsync().get();
            auto size = static_cast<uint32_t>(stream.Size());
            if (size == 0) {
                request.RecordFailure(L"the artwork stream was empty");
                return kNoThumbnail;
            }

            Buffer request_buffer(size);
            // Use the buffer ReadAsync hands back rather than the one passed
            // in: IInputStream does not promise they are the same object.
            auto filled = stream.ReadAsync(request_buffer, size,
                                           InputStreamOptions::None).get();
            if (filled.Length() != size) {
                request.RecordFailure(L"short read from the artwork stream");
                return kErrFailed;
            }

            if (request.Abandoned()) return kErrTimeout;

            // Write beside the target and rename over it, so a reader never
            // observes a half-written image and two overlapping calls cannot
            // interleave into one corrupt file.
            std::wstring staging = target + L".part";
            FILE* file = nullptr;
            if (_wfopen_s(&file, staging.c_str(), L"wb") != 0 || file == nullptr) {
                request.RecordFailure(L"could not open the artwork output file");
                return kErrFailed;
            }
            size_t written = fwrite(filled.data(), 1, filled.Length(), file);
            fclose(file);

            if (written != filled.Length()) {
                _wremove(staging.c_str());
                request.RecordFailure(L"short write to the artwork output file");
                return kErrFailed;
            }

            if (!MoveFileExW(staging.c_str(), target.c_str(),
                             MOVEFILE_REPLACE_EXISTING)) {
                _wremove(staging.c_str());
                request.RecordFailure(L"could not replace the artwork file");
                return kErrFailed;
            }
            return kOk;
        },
        text, error);

    return Finish(code, error);
} catch (...) {
    return Finish(kErrFailed, L"unhandled failure in smtc_thumbnail");
}

// Detail for the most recent failure, for logging. Cleared by any call that
// did not fail, so a stale message cannot be attributed to a later call.
int __stdcall smtc_last_error(wchar_t* buffer, int capacity) try {
    std::lock_guard<std::mutex> guard(g_lastErrorMutex);
    return CopyOut(g_lastError, buffer, capacity);
} catch (...) {
    return kErrFailed;
}

}  // extern "C"
