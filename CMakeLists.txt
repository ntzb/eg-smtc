cmake_minimum_required(VERSION 3.20)
project(eg_smtc CXX)

# EventGhost is a 32-bit process, so the DLL it loads has to be x86. The
# workflow configures the generator for Win32; this guard exists so a local
# x64 build fails loudly instead of producing something that cannot load.
if(NOT CMAKE_SIZEOF_VOID_P EQUAL 4)
  message(FATAL_ERROR
    "eg_smtc must be built for x86: EventGhost is a 32-bit process. "
    "Configure with -A Win32.")
endif()

# C++20, not 17: under C++17 C++/WinRT falls back to <experimental/coroutine>,
# which current MSVC refuses to compile at all.
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(smtc SHARED src/smtc.cpp src/egsmtc.def)

set_target_properties(smtc PROPERTIES
  OUTPUT_NAME "egsmtc"
  PREFIX ""
)

# /WX: this code has already shipped a use-after-free that a warning could
# plausibly have flagged, so new warnings should stop the build.
target_compile_options(smtc PRIVATE /W4 /WX /permissive- /EHsc)

# WindowsApp.lib carries the WinRT activation entry points used by C++/WinRT.
target_link_libraries(smtc PRIVATE WindowsApp.lib)

# Link the CRT statically so the DLL drops next to EventGhost.exe without
# needing a redistributable installed on the target machine.
set_property(TARGET smtc PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded")

# Loads the built DLL and calls it, so CI catches an unresolvable import, a
# calling-convention mismatch or a hang, none of which a name check can see.
add_executable(smoke src/smoke.cpp)
target_compile_options(smoke PRIVATE /W4 /WX /permissive- /EHsc)
set_property(TARGET smoke PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded")
add_dependencies(smoke smtc)
