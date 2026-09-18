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
using ControlFn = int(__stdcall*)(const wchar_t*);
using LastErrorFn = int(__stdcall*)(wchar_t*, int);

constexpr int kOk = 0;
constexpr int kNoSession = 1;
constexpr int kErrFailed = -1;
constexpr int kErrTimeout = -2;
constexpr int kErrArgument = -4;

constexpr auto kBudget = std::chrono::seconds(10);

int failures = 0;

void Fail(const char* message) {
    std::printf("FAIL: %s\n", message);
    ++failures;
}

}  // namespace

int main() {
    auto started = std::chrono::steady_clock::now();

    HMODULE module = LoadLibraryW(L"egsmtc.dll");
    if (module == nullptr) {
        std::printf("FAIL: LoadLibrary failed with %lu\n", GetLastError());
        return 1;
    }

    auto now_playing =
        reinterpret_cast<NowPlayingFn>(GetProcAddress(module, "smtc_now_playing"));
    auto control =
        reinterpret_cast<ControlFn>(GetProcAddress(module, "smtc_control"));
    auto last_error =
        reinterpret_cast<LastErrorFn>(GetProcAddress(module, "smtc_last_error"));

    if (now_playing == nullptr || control == nullptr || last_error == nullptr) {
        Fail("an export could not be resolved by name");
        return 1;
    }

    wchar_t buffer[4096] = {};
    int code = now_playing(buffer, 4096);
    std::printf("smtc_now_playing -> %d, json=%ls\n", code, buffer);
    if (code != kOk && code != kNoSession && code != kErrFailed &&
        code != kErrTimeout) {
        Fail("smtc_now_playing returned an unexpected code");
    }
    if (code == kOk && buffer[0] != L'{') {
        Fail("smtc_now_playing reported success without writing JSON");
    }
    if (code == kNoSession && std::wcscmp(buffer, L"{}") != 0) {
        Fail("smtc_now_playing reported no session without writing {}");
    }

    // An unknown verb exercises the control path and its error reporting
    // without touching whatever might be playing on the machine.
    int bogus = control(L"definitely-not-a-command");
    std::printf("smtc_control(bogus) -> %d\n", bogus);
    if (bogus != kErrArgument && bogus != kNoSession) {
        Fail("smtc_control did not reject an unknown command");
    }

    wchar_t detail[1024] = {};
    int described = last_error(detail, 1024);
    std::printf("smtc_last_error -> %d, detail=%ls\n", described, detail);
    if (described != kOk) {
        Fail("smtc_last_error did not return a string");
    }

    // A buffer of zero capacity must be rejected, not written to.
    if (now_playing(buffer, 0) != -4) {
        Fail("smtc_now_playing accepted a zero-capacity buffer");
    }
    if (control(nullptr) != -4) {
        Fail("smtc_control accepted a null command");
    }

    auto elapsed = std::chrono::steady_clock::now() - started;
    if (elapsed > kBudget) {
        Fail("the calls took longer than the budget");
    }
    std::printf("elapsed: %lld ms\n",
                static_cast<long long>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(elapsed)
                        .count()));

    FreeLibrary(module);
    std::printf(failures ? "SMOKE FAILED\n" : "SMOKE OK\n");
    return failures ? 1 : 0;
}
