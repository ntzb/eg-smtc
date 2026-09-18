// Loads egsmtc.dll the way the plugin does and calls it.
//
// The export-name check in CI proves the names are present and undecorated.
// It cannot prove the DLL loads (an unresolvable import), that the calling
// convention matches the declaration, or that a call returns at all. This
// does, and it is written to pass whether or not the machine running it has a
// media session, because a CI runner has none.

#include <windows.h>

#include <chrono>
#include <cstdio>
#include <cwchar>

namespace {

using NowPlayingFn = int(__stdcall*)(wchar_t*, int);
using SessionsFn = int(__stdcall*)(wchar_t*, int);
using ControlFn = int(__stdcall*)(const wchar_t*);
using LastErrorFn = int(__stdcall*)(wchar_t*, int);

constexpr int kOk = 0;
constexpr int kNoSession = 1;
constexpr int kErrFailed = -1;
constexpr int kErrArgument = -4;

// Per call, not for the whole run: the DLL's own deadline is three seconds,
// so anything near that on an idle machine means a call is not being served.
constexpr auto kPerCallBudget = std::chrono::seconds(2);

int failures = 0;

void Fail(const char* message) {
    std::printf("FAIL: %s\n", message);
    ++failures;
}

}  // namespace

int main() {
    HMODULE module = LoadLibraryW(L"egsmtc.dll");
    if (module == nullptr) {
        std::printf("FAIL: LoadLibrary failed with %lu\n", GetLastError());
        return 1;
    }

    auto now_playing =
        reinterpret_cast<NowPlayingFn>(GetProcAddress(module, "smtc_now_playing"));
    auto sessions =
        reinterpret_cast<SessionsFn>(GetProcAddress(module, "smtc_sessions"));
    auto control =
        reinterpret_cast<ControlFn>(GetProcAddress(module, "smtc_control"));
    auto last_error =
        reinterpret_cast<LastErrorFn>(GetProcAddress(module, "smtc_last_error"));

    if (now_playing == nullptr || sessions == nullptr || control == nullptr ||
        last_error == nullptr) {
        Fail("an export could not be resolved by name");
        return 1;
    }

    wchar_t buffer[4096] = {};
    auto started = std::chrono::steady_clock::now();
    int code = now_playing(buffer, 4096);
    auto took = std::chrono::steady_clock::now() - started;
    std::printf("smtc_now_playing -> %d, json=%ls\n", code, buffer);

    // kErrTimeout is deliberately NOT accepted. A deadline expiring on an
    // idle machine is the signature of a worker that never ran, which is the
    // regression this test exists to catch.
    if (code != kOk && code != kNoSession && code != kErrFailed) {
        Fail("smtc_now_playing returned an unexpected code");
    }
    if (took > kPerCallBudget) {
        Fail("smtc_now_playing took suspiciously long on an idle machine");
    }
    if (code == kOk && buffer[0] != L'{') {
        Fail("smtc_now_playing reported success without writing JSON");
    }
    if (code == kNoSession && std::wcscmp(buffer, L"{}") != 0) {
        Fail("smtc_now_playing reported no session without writing {}");
    }

    wchar_t list[4096] = {};
    int listed = sessions(list, 4096);
    std::printf("smtc_sessions -> %d, json=%ls\n", listed, list);
    if (listed != kOk && listed != kNoSession) {
        Fail("smtc_sessions returned an unexpected code");
    }
    if (list[0] != L'[') {
        Fail("smtc_sessions did not write a JSON array");
    }

    // An unknown verb exercises the control path and its error reporting
    // without touching whatever might be playing on the machine. The
    // assertion is unconditional because the verb is validated before the
    // session lookup; accepting kNoSession here, as an earlier version did,
    // made it unfalsifiable on a runner with nothing playing.
    int bogus = control(L"definitely-not-a-command");
    std::printf("smtc_control(bogus) -> %d\n", bogus);
    if (bogus != kErrArgument) {
        Fail("smtc_control did not reject an unknown command");
    }

    // Reads the message the rejection above just published. A successful call
    // clears it, so this has to follow a known failure or it proves nothing.
    wchar_t detail[1024] = {};
    int described = last_error(detail, 1024);
    std::printf("smtc_last_error -> %d, detail=%ls\n", described, detail);
    if (described != kOk) {
        Fail("smtc_last_error did not return a string");
    }
    if (detail[0] == L'\0') {
        Fail("smtc_last_error returned nothing after a failed call");
    }

    // A buffer of zero capacity must be rejected, not written to.
    if (now_playing(buffer, 0) != kErrArgument) {
        Fail("smtc_now_playing accepted a zero-capacity buffer");
    }
    if (sessions(buffer, 0) != kErrArgument) {
        Fail("smtc_sessions accepted a zero-capacity buffer");
    }
    if (control(nullptr) != kErrArgument) {
        Fail("smtc_control accepted a null command");
    }

    // Long enough for the worker to drain and leave the apartment. The second
    // burst then runs in a *new* apartment, which is the case that breaks if
    // the process-wide activation factory cache were reused across one, and
    // the case a single-burst test structurally cannot see.
    Sleep(1500);

    started = std::chrono::steady_clock::now();
    int again = now_playing(buffer, 4096);
    took = std::chrono::steady_clock::now() - started;
    std::printf("smtc_now_playing (second burst) -> %d, json=%ls\n", again, buffer);
    if (again != code) {
        Fail("the second burst disagreed with the first: the apartment or the "
             "factory cache did not survive the worker going idle");
    }
    if (took > kPerCallBudget) {
        Fail("the second burst took suspiciously long: the worker may not have "
             "restarted cleanly");
    }

    // Not unloaded on purpose: pulling the code out from under a worker that
    // might still be running would fault. The plugin never unloads it either.
    (void)module;
    std::printf(failures ? "SMOKE FAILED\n" : "SMOKE OK\n");
    std::fflush(stdout);
    return failures ? 1 : 0;
}
