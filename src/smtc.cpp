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
// So: one persistent worker thread serves a queue, and each caller waits with
// a deadline. On timeout the caller marks the request abandoned and returns;
// the worker then discards it without running it, or, if it is already
// running, finishes without performing the side effect. Nothing shared with a
// caller outlives the call.
//
// The worker enters the multi-threaded apartment only while it has work and
// leaves it once the queue drains. A thread parked inside the MTA blocks
// combase's process-detach handler, which hangs the host on shutdown, and
// EventGhost offers no shutdown hook that could release it: __close__ runs
// only when the user deletes the plugin. Parked outside the apartment the
// thread holds nothing that process exit cannot reclaim.

#define NOMINMAX
#define WIN32_LEAN_AND_MEAN

#include <winrt/Windows.Foundation.Collections.h>
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
#include <vector>

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
constexpr int kErrBusy = -5;
constexpr int kErrUnsupported = -6;

// Budget for the *encoded* size of each metadata field, which is what has to
// fit. Bounding the raw length would bound nothing, since a control character
// escapes to six characters and 512 raw could emit 3072.
constexpr size_t kMaxFieldChars = 512;

// smtc_sessions writes one object per session into a fixed caller buffer, so
// the count has to be bounded or a machine with many media tabs fails the
// whole call. A truncated marker is appended when this bites.
constexpr size_t kMaxListedSessions = 12;

// A backlog this deep means the session has been unresponsive for far longer
// than any caller is still waiting. Refusing is a better answer than a queue
// that grows without bound and a caller that waits its whole deadline behind
// work nobody wants any more.
constexpr size_t kMaxPending = 64;

std::mutex g_lastErrorMutex;
std::wstring g_lastError;

void PublishError(std::wstring message) {
    std::lock_guard<std::mutex> guard(g_lastErrorMutex);
    g_lastError = std::move(message);
}

struct Request {
    std::function<int(Request&)> work;

    std::wstring text;   // JSON or other string output
    std::wstring error;  // detail for the caller's log

    std::atomic<bool> abandoned{false};

    std::mutex mutex;
    std::condition_variable ready;
    bool done = false;
    int code = kErrFailed;

    // Not named SetLastError: windows.h declares that, and a call with an
    // integral argument would silently bind to kernel32's version and record
    // nothing. That name would also imply GetLastError explains our failures.
    void RecordFailure(std::wstring message) { error = std::move(message); }

    // Checked before the worker starts a request at all, and again before
    // anything observable: a command issued, or a file written. A caller that
    // has given up must not get a side effect seconds later.
    bool Abandoned() const { return abandoned.load(); }
};

// Leaves the apartment however the burst ends. Falling through to an explicit
// uninit would be skipped by an exception, and the next burst's init would
// then take the reference count to two: one uninit later the thread parks in
// the MTA forever, which is exactly the state that hangs process detach.
struct ApartmentScope {
    ~ApartmentScope() {
        clear_factory_cache();
        uninit_apartment();
    }
};

class Worker {
public:
    // Deliberately leaked: the object must outlive any in-flight request, and
    // a destructor could only race with the worker thread.
    static Worker& Instance() {
        static Worker* worker = new Worker();
        return *worker;
    }

    // False when the backlog is too deep to accept more.
    bool Post(std::shared_ptr<Request> request) {
        std::lock_guard<std::mutex> guard(mutex_);
        if (queue_.size() >= kMaxPending) return false;
        queue_.push_back(std::move(request));
        if (!running_) {
            // A plain bool, not std::call_once: a once_flag is consumed even
            // when the thread later dies, which would make a single failed
            // burst permanent. This way the next Post always starts a worker.
            std::thread([this] { Loop(); }).detach();
            running_ = true;
        }
        wake_.notify_one();
        return true;
    }

private:
    void Loop() {
        // Nothing may escape a thread entry point: that is std::terminate.
        // Clearing running_ on the way out means even a thread death is
        // recoverable, since the next Post starts a replacement.
        try {
            Serve();
        } catch (...) {
            PublishError(L"the media worker stopped unexpectedly");
        }
        std::lock_guard<std::mutex> guard(mutex_);
        running_ = false;
    }

    void Serve() {
        for (;;) {
            {
                std::unique_lock<std::mutex> lock(mutex_);
                wake_.wait(lock, [this] { return !queue_.empty(); });
            }

            // Per burst, so one failed burst does not end the thread and
            // leave the DLL with no worker for the rest of the process.
            try {
                bool entered = true;
                try {
                    init_apartment(apartment_type::multi_threaded);
                } catch (...) {
                    entered = false;
                }
                if (!entered) {
                    // Tell the callers the truth rather than leaving each of
                    // them to discover it as a deadline expiring with a
                    // misleading message about a slow media session.
                    FailAll(L"could not initialise a multi-threaded apartment");
                    continue;
                }

                ApartmentScope scope;
                DrainQueue();
            } catch (...) {
                FailAll(L"the media worker failed to serve the queue");
            }
        }
    }

    void DrainQueue() {
        for (;;) {
            std::shared_ptr<Request> request = Take();
            if (!request) return;

            // Skipped entirely, not merely prevented from having an effect.
            // Running abandoned work would spend seconds of WinRT round trips
            // on a result nobody will read, while fresh calls queue behind it
            // and time out in turn.
            if (request->Abandoned()) {
                Complete(request, kErrTimeout);
                continue;
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
            Complete(request, code);
        }
    }

    void FailAll(std::wstring const& reason) {
        for (;;) {
            std::shared_ptr<Request> request = Take();
            if (!request) return;
            request->RecordFailure(reason);
            Complete(request, kErrFailed);
        }
    }

    std::shared_ptr<Request> Take() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (queue_.empty()) return nullptr;
        std::shared_ptr<Request> request = std::move(queue_.front());
        queue_.pop_front();
        return request;
    }

    // The local shared_ptr must outlive the notify: dropping the last
    // reference before notifying would destroy the condition variable while
    // this thread is still inside it.
    static void Complete(std::shared_ptr<Request> const& request, int code) {
        {
            std::lock_guard<std::mutex> lock(request->mutex);
            request->code = code;
            request->done = true;
        }
        request->ready.notify_all();
    }

    std::mutex mutex_;
    std::condition_variable wake_;
    std::deque<std::shared_ptr<Request>> queue_;
    bool running_ = false;
};

// Runs work on the worker thread and copies out whatever it produced. The
// request is shared with the worker and outlives this call; the strings the
// caller sees are copies, so an abandoned worker cannot write to anything
// still in scope here.
int Run(std::function<int(Request&)> work, std::wstring& text, std::wstring& error) {
    auto request = std::make_shared<Request>();
    request->work = std::move(work);

    try {
        if (!Worker::Instance().Post(request)) {
            error = L"too many media requests are already queued";
            return kErrBusy;
        }
    } catch (...) {
        // The request may already be queued, so it has to be disowned rather
        // than just dropped, or the worker would run it later and produce a
        // side effect for a call that has already reported failure.
        request->abandoned.store(true);
        error = L"could not start the media worker";
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

// Requested per call rather than cached in a static. A static would hold a
// WinRT reference past uninit_apartment and be destroyed in a dead apartment,
// which is a worse problem than the extra round trip.
GlobalSystemMediaTransportControlsSessionManager RequestManager() {
    return GlobalSystemMediaTransportControlsSessionManager::RequestAsync().get();
}

using Session = GlobalSystemMediaTransportControlsSession;
using Manager = GlobalSystemMediaTransportControlsSessionManager;
using Status = GlobalSystemMediaTransportControlsSessionPlaybackStatus;
using Controls = GlobalSystemMediaTransportControlsSessionPlaybackControls;

// Compared by SourceAppUserModelId, having measured that COM identity does
// not work here: GetSessions and GetCurrentSession hand back distinct proxies
// for the same underlying session, so comparing IUnknown pointers reported
// "not the current session" for every entry, including the one
// GetCurrentSession had just named. COM only promises IUnknown identity for
// the same object within an apartment, not across two activations.
//
// The cost is that an app registering several sessions collides, so both of
// two Brave tabs match the current one and the first evaluated wins. That
// picks the right application, which is what matters here, and there is no
// session id in the API to do better.
bool SameSession(Session const& left, Session const& right) {
    if (left == nullptr || right == nullptr) return false;
    try {
        std::wstring a{left.SourceAppUserModelId()};
        std::wstring b{right.SourceAppUserModelId()};
        return !a.empty() && a == b;
    } catch (hresult_error const&) {
        return false;
    }
}

// Ranked rather than a bare "is it Playing" test. Chromium reports Changing
// transiently while it swaps media elements, and during that window a naive
// test finds nothing playing and falls back to whatever Windows considers
// current, which is the paused background player this whole function exists
// to avoid.
int StatusRank(Status status) {
    switch (status) {
        case Status::Playing: return 5;
        case Status::Changing: return 4;
        case Status::Opened: return 3;
        case Status::Paused: return 2;
        case Status::Stopped: return 1;
        case Status::Closed: return 0;
    }
    return 0;
}

bool Supports(Controls const& controls, std::wstring const& verb) {
    if (verb == L"toggle") {
        return controls.IsPlayPauseToggleEnabled() || controls.IsPlayEnabled() ||
               controls.IsPauseEnabled();
    }
    if (verb == L"next") return controls.IsNextEnabled();
    if (verb == L"previous") return controls.IsPreviousEnabled();
    if (verb == L"play") return controls.IsPlayEnabled();
    if (verb == L"pause") return controls.IsPauseEnabled();
    if (verb == L"stop") return controls.IsStopEnabled();
    return true;
}

struct Candidate {
    Session session{nullptr};
    bool supports = true;
    int rank = -1;
    bool current = false;

    // Ordered by what the user most likely meant. Supporting the requested
    // verb comes first, because acting on a session that cannot perform it
    // just produces a refusal. Then playback state, so a playing video wins
    // over a paused background player. Then Windows' own arbitration, which
    // tracks the most recent interaction and is what a media key would have
    // followed; that is what makes "pause the video, press again" resume the
    // video rather than something else.
    //
    // There is deliberately no tiebreak on the session's own
    // LastUpdatedTime. It reads as an obvious recency signal and is not one:
    // measured on the target machine, Spotify refreshes its timeline
    // continuously while paused, so its value is always newer than a paused
    // video's static one, and ranking by it chose the wrong session every
    // time.
    bool Beats(Candidate const& other) const {
        if (session == nullptr) return false;
        if (other.session == nullptr) return true;
        if (supports != other.supports) return supports;
        if (rank != other.rank) return rank > other.rank;
        return current && !other.current;
    }
};

// Builds a candidate, or returns an empty one if the session has gone away.
//
// Every session in the list is touched now, where once only the current one
// was, so a stale entry has to be survivable: an app that just died, or a
// Chromium media element that vanished, throws RPC_E_DISCONNECTED here. One
// such entry must not fail a call that a perfectly good session later in the
// list would have answered.
Candidate Evaluate(Session const& session, Session const& current,
                   std::wstring const& verb) {
    Candidate candidate;
    try {
        if (session == nullptr) return candidate;
        auto playback = session.GetPlaybackInfo();
        candidate.session = session;
        candidate.rank = StatusRank(playback.PlaybackStatus());
        candidate.current = SameSession(session, current);
        if (!verb.empty()) {
            candidate.supports = Supports(playback.Controls(), verb);
        }
    } catch (hresult_error const&) {
        return Candidate{};
    }
    return candidate;
}

// Picks the session to report on and act upon.
//
// GetCurrentSession() alone is not enough, which took a while to establish.
// An app keeps its SMTC session for as long as it runs, and Spotify's desktop
// client has no stop at all, only pause, so its session sits in Paused
// indefinitely while the app is open. Windows reports that paused session as
// current whenever the playing app's session is momentarily absent, which
// Chromium causes on every media element change. Observed in practice: a
// video playing in Brave, and the API handing back paused Spotify.
//
// So this ranks every session and takes the best. Playback state settles the
// original case, and Windows' current session settles the one a keypress
// later: after pausing the video nothing is playing, both candidates are
// Paused, and the current session is still the video.
//
// verb may be empty; when given, a session that cannot perform it loses.
Session PickSession(Manager const& manager, std::wstring const& verb) {
    Session current{nullptr};
    try {
        current = manager.GetCurrentSession();
    } catch (hresult_error const&) {
    }

    Candidate best;
    try {
        auto sessions = manager.GetSessions();
        for (uint32_t i = 0; i < sessions.Size(); ++i) {
            Session session{nullptr};
            try {
                session = sessions.GetAt(i);
            } catch (hresult_error const&) {
                // E_CHANGED_STATE if the list moved under us, or a stale
                // entry; either way the remaining entries are still worth
                // trying.
                continue;
            }
            Candidate candidate = Evaluate(session, current, verb);
            if (candidate.Beats(best)) best = candidate;
        }
    } catch (hresult_error const&) {
        // Enumeration itself failed; the current session is still usable.
    }

    if (best.session != nullptr) return best.session;
    return current;
}

const wchar_t* StatusName(Status status) {
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

bool IsHighSurrogate(wchar_t ch) { return ch >= 0xD800 && ch <= 0xDBFF; }

// Appends a JSON string literal, spending at most kMaxFieldChars of encoded
// output. Budgeting the encoded length is the only way to bound the result,
// since one input character can expand to six.
void AppendJsonString(std::wstring& out, std::wstring_view value) {
    out.push_back(L'"');
    size_t spent = 0;
    for (size_t i = 0; i < value.size(); ++i) {
        wchar_t ch = value[i];

        // A surrogate pair is emitted as a unit or not at all: a lone high
        // surrogate would leave the caller with undecodable JSON.
        if (IsHighSurrogate(ch)) {
            if (i + 1 >= value.size() || spent + 2 > kMaxFieldChars) break;
            out.push_back(ch);
            out.push_back(value[i + 1]);
            spent += 2;
            ++i;
            continue;
        }

        wchar_t escape[7];
        const wchar_t* piece;
        size_t length;
        switch (ch) {
            case L'"': piece = L"\\\""; length = 2; break;
            case L'\\': piece = L"\\\\"; length = 2; break;
            case L'\n': piece = L"\\n"; length = 2; break;
            case L'\r': piece = L"\\r"; length = 2; break;
            case L'\t': piece = L"\\t"; length = 2; break;
            default:
                if (ch < 0x20) {
                    swprintf_s(escape, L"\\u%04x", static_cast<unsigned>(ch));
                    piece = escape;
                    length = 6;
                } else {
                    escape[0] = ch;
                    escape[1] = L'\0';
                    piece = escape;
                    length = 1;
                }
        }

        if (spent + length > kMaxFieldChars) break;
        out.append(piece, length);
        spent += length;
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

void AppendJsonNumber(std::wstring& out, const wchar_t* key, int64_t value,
                      bool last = false) {
    AppendJsonString(out, key);
    out.push_back(L':');
    wchar_t digits[32];
    swprintf_s(digits, L"%lld", static_cast<long long>(value));
    out.append(digits);
    if (!last) out.push_back(L',');
}

void AppendJsonBool(std::wstring& out, const wchar_t* key, bool value,
                    bool last = false) {
    AppendJsonString(out, key);
    out.push_back(L':');
    out.append(value ? L"true" : L"false");
    if (!last) out.push_back(L',');
}

int CopyOut(std::wstring const& text, wchar_t* buffer, int capacity) {
    if (buffer == nullptr || capacity <= 0) return kErrArgument;
    if (static_cast<size_t>(capacity) <= text.size()) return kErrBuffer;
    memcpy(buffer, text.c_str(), (text.size() + 1) * sizeof(wchar_t));
    return kOk;
}

bool IsKnownVerb(std::wstring const& verb) {
    return verb == L"toggle" || verb == L"next" || verb == L"previous" ||
           verb == L"play" || verb == L"pause" || verb == L"stop";
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

// Writes a JSON object describing the session this plugin would act on, which
// is not always the one Windows calls current: see PickSession. Returns
// kNoSession and writes "{}" when there is no session at all. A buffer of
// 4096 characters covers the worst case the field budget allows.
int __stdcall smtc_now_playing(wchar_t* buffer, int capacity) try {
    if (buffer == nullptr || capacity <= 0) {
        return Finish(kErrArgument, L"invalid buffer");
    }

    std::wstring json;
    std::wstring error;
    int code = Run(
        [](Request& request) -> int {
            auto session = PickSession(RequestManager(), std::wstring());
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

// Writes a JSON array of the sessions Windows knows about, each as
// {"app":...,"status":...,"current":bool,"picked":bool}.
//
// "current" is Windows' own arbitration; "picked" is the one this plugin
// would act on. They disagree exactly when PickSession is earning its keep,
// which is the point of the export. Metadata is left out because it would
// cost an async round trip per session.
//
// At most kMaxListedSessions entries, followed by {"truncated":true} when
// there were more, so the output cannot outgrow the caller's buffer.
int __stdcall smtc_sessions(wchar_t* buffer, int capacity) try {
    if (buffer == nullptr || capacity <= 0) {
        return Finish(kErrArgument, L"invalid buffer");
    }

    std::wstring json;
    std::wstring error;
    int code = Run(
        [](Request& request) -> int {
            auto manager = RequestManager();

            Session current{nullptr};
            try {
                current = manager.GetCurrentSession();
            } catch (hresult_error const&) {
            }
            Session picked = PickSession(manager, std::wstring());

            std::wstring currentApp;
            if (current != nullptr) {
                try {
                    currentApp = current.SourceAppUserModelId();
                } catch (hresult_error const&) {
                }
            }

            std::vector<Session> all;
            try {
                auto sessions = manager.GetSessions();
                for (uint32_t i = 0; i < sessions.Size(); ++i) {
                    try {
                        all.push_back(sessions.GetAt(i));
                    } catch (hresult_error const&) {
                        continue;
                    }
                }
            } catch (hresult_error const&) {
            }

            std::wstring json;
            json.push_back(L'[');
            size_t emitted = 0;
            for (auto const& session : all) {
                if (emitted >= kMaxListedSessions) break;
                if (session == nullptr) continue;

                std::wstring appId;
                const wchar_t* status = L"Unknown";
                int64_t updated = 0;
                try {
                    appId = session.SourceAppUserModelId();
                    status = StatusName(session.GetPlaybackInfo().PlaybackStatus());
                    try {
                        updated = session.GetTimelineProperties()
                                      .LastUpdatedTime()
                                      .time_since_epoch()
                                      .count();
                    } catch (hresult_error const&) {
                    }
                } catch (hresult_error const&) {
                    // A session that died between enumeration and inspection
                    // is simply not listed.
                    continue;
                }

                if (emitted) json.push_back(L',');
                ++emitted;

                json.push_back(L'{');
                AppendJsonField(json, L"app", appId);
                AppendJsonField(json, L"status", status);
                AppendJsonBool(json, L"current", SameSession(session, current));
                AppendJsonBool(json, L"picked", SameSession(session, picked));
                // Reported but deliberately not used for selection: this is
                // how it was established that Spotify keeps refreshing its
                // timeline while paused. Kept because it explains a choice
                // that looks wrong.
                AppendJsonNumber(json, L"updated", updated, true);
                json.push_back(L'}');
            }
            if (all.size() > emitted) {
                if (emitted) json.push_back(L',');
                json.append(L"{\"truncated\":true}");
            }
            // Trailing entry rather than a wrapper object, so the shape stays
            // a plain array for existing callers.
            if (emitted || !currentApp.empty()) {
                if (emitted) json.push_back(L',');
                json.push_back(L'{');
                AppendJsonField(json, L"currentApp", currentApp, true);
                json.push_back(L'}');
            }
            json.push_back(L']');

            request.text = std::move(json);
            return all.empty() ? kNoSession : kOk;
        },
        json, error);

    if (code < 0) return Finish(code, error);

    int copied = CopyOut(json, buffer, capacity);
    if (copied != kOk) {
        return Finish(copied,
                      L"the caller's buffer is too small for the session list");
    }
    return Finish(code, error);
} catch (...) {
    return Finish(kErrFailed, L"unhandled failure in smtc_sessions");
}

// command is one of "toggle", "next", "previous", "play", "pause", "stop".
int __stdcall smtc_control(const wchar_t* command) try {
    if (command == nullptr) return Finish(kErrArgument, L"no command given");
    std::wstring verb(command);
    // Validated before the session lookup on purpose: whether a command is
    // spelled correctly does not depend on what is playing, and rejecting a
    // typo should not cost two round trips or the whole deadline.
    if (!IsKnownVerb(verb)) return Finish(kErrArgument, L"unknown command");

    std::wstring text;
    std::wstring error;
    int code = Run(
        [verb](Request& request) -> int {
            // The verb steers selection: a session that cannot skip tracks
            // should not be chosen for Next just because it is playing.
            auto session = PickSession(RequestManager(), verb);
            if (session == nullptr) return kNoSession;

            // The caller may already have given up and retried by now.
            // Issuing this would skip two tracks for one key press.
            if (request.Abandoned()) return kErrTimeout;

            if (!Supports(session.GetPlaybackInfo().Controls(), verb)) {
                request.RecordFailure(
                    L"no media session supports that command right now");
                return kErrUnsupported;
            }

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
            } else {
                accepted = session.TryStopAsync().get();
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

// Writes the picked session's artwork to path, as delivered by the app
// (usually JPEG or PNG; the bytes are not transcoded). Returns kNoThumbnail
// when there is a session but it publishes no artwork, which is common.
int __stdcall smtc_thumbnail(const wchar_t* path) try {
    if (path == nullptr) return Finish(kErrArgument, L"no path given");
    std::wstring target(path);

    std::wstring text;
    std::wstring error;
    int code = Run(
        [target](Request& request) -> int {
            auto session = PickSession(RequestManager(), std::wstring());
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

            Buffer destination(size);
            // Use the buffer ReadAsync hands back rather than the one passed
            // in: IInputStream does not promise they are the same object.
            auto filled =
                stream.ReadAsync(destination, size, InputStreamOptions::None).get();
            if (filled.Length() != size) {
                request.RecordFailure(L"short read from the artwork stream");
                return kErrFailed;
            }

            if (request.Abandoned()) return kErrTimeout;

            // Write beside the target and rename over it, so a reader never
            // observes a half-written image.
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
    // Copied under the lock and written outside it. A caller that passes an
    // invalid pointer then faults without holding a mutex that every other
    // export needs, which would otherwise wedge the whole DLL.
    std::wstring message;
    {
        std::lock_guard<std::mutex> guard(g_lastErrorMutex);
        message = g_lastError;
    }
    return CopyOut(message, buffer, capacity);
} catch (...) {
    return kErrFailed;
}

}  // extern "C"
