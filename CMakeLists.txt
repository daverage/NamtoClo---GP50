cmake_minimum_required(VERSION 3.24)

if(WIN32)
    project(NamToClo VERSION 2.8.0 LANGUAGES C CXX RC)
else()
    project(NamToClo VERSION 2.8.0 LANGUAGES C CXX)
endif()

if(NOT WIN32 AND NOT APPLE)
    message(FATAL_ERROR "NAM to CLO supports Windows x64 and macOS (Apple Silicon first). See CLAUDE.md.")
endif()
if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "A 64-bit build is required.")
endif()

set(NAM_CORE_SOURCE_DIR "" CACHE PATH "Optional local NeuralAmpModelerCore source tree (avoids network FetchContent)")
set(R8BRAIN_SOURCE_DIR "" CACHE PATH "Optional local r8brain-free-src source tree (avoids network FetchContent)")
set(NTC_OOURA_SOURCE_DIR "" CACHE PATH "Optional source tree containing src/fft4g.c (SoXR/Ooura FFT4G)")
set(NTC_R8BRAIN_REF "version-3.7" CACHE STRING "Pinned r8brain revision used by the converter")

# --------------------------------------------------------------------------
# Third-party dependencies (shared by every target below; unchanged from the
# original Windows-only build -- see CLAUDE.md "Dependencies fetched at
# configure time").
# --------------------------------------------------------------------------

if(NAM_CORE_SOURCE_DIR)
    set(_ntc_namcore_dir "${NAM_CORE_SOURCE_DIR}")
else()
    include(FetchContent)
    if(POLICY CMP0169)
        cmake_policy(SET CMP0169 OLD)
    endif()
    FetchContent_Declare(
        ntc_namcore_src
        GIT_REPOSITORY https://github.com/sdatkinson/NeuralAmpModelerCore.git
        GIT_TAG v0.5.4
        GIT_SHALLOW TRUE
        GIT_SUBMODULES_RECURSE TRUE
    )
    FetchContent_GetProperties(ntc_namcore_src)
    if(NOT ntc_namcore_src_POPULATED)
        FetchContent_Populate(ntc_namcore_src)
    endif()
    set(_ntc_namcore_dir "${ntc_namcore_src_SOURCE_DIR}")
endif()

if(NOT EXISTS "${_ntc_namcore_dir}/NAM/get_dsp.h")
    message(FATAL_ERROR
        "NeuralAmpModelerCore was not found. "
        "Provide -DNAM_CORE_SOURCE_DIR=<path> or allow FetchContent network access.")
endif()

# The official converter uses r8b::CDSPResampler24. r8brain-free-src is
# header-only and is compiled into this executable; there is no runtime DLL.
if(R8BRAIN_SOURCE_DIR)
    set(_ntc_r8brain_dir "${R8BRAIN_SOURCE_DIR}")
else()
    include(FetchContent)
    FetchContent_Declare(
        ntc_r8brain_src
        GIT_REPOSITORY https://github.com/avaneev/r8brain-free-src.git
        GIT_TAG ${NTC_R8BRAIN_REF}
        GIT_SHALLOW TRUE
        PATCH_COMMAND ${CMAKE_COMMAND}
            "-DNTC_R8BRAIN_PATCH_FILE=<SOURCE_DIR>/r8bbase.h"
            -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/patch_r8brain_template_shadow.cmake"
    )
    FetchContent_GetProperties(ntc_r8brain_src)
    if(NOT ntc_r8brain_src_POPULATED)
        FetchContent_Populate(ntc_r8brain_src)
    endif()
    set(_ntc_r8brain_dir "${ntc_r8brain_src_SOURCE_DIR}")
endif()
if(NOT EXISTS "${_ntc_r8brain_dir}/CDSPResampler.h")
    message(FATAL_ERROR
        "r8brain-free-src was not found. "
        "Provide -DR8BRAIN_SOURCE_DIR=<path> or allow FetchContent network access.")
endif()

# HTUSBTools/GP-200 spectral estimator uses Takuya Ooura's FFT4G RDFT.
# Compile the C implementation shipped by SoXR 0.1.3 directly into the
# executable; this is a build-time source dependency, not a runtime DLL.
if(NTC_OOURA_SOURCE_DIR)
    set(_ntc_ooura_dir "${NTC_OOURA_SOURCE_DIR}")
else()
    include(FetchContent)
    FetchContent_Declare(
        ntc_ooura_src
        GIT_REPOSITORY https://github.com/chirlu/soxr.git
        GIT_TAG 0.1.3
        GIT_SHALLOW TRUE
    )
    FetchContent_GetProperties(ntc_ooura_src)
    if(NOT ntc_ooura_src_POPULATED)
        FetchContent_Populate(ntc_ooura_src)
    endif()
    set(_ntc_ooura_dir "${ntc_ooura_src_SOURCE_DIR}")
endif()
if(NOT EXISTS "${_ntc_ooura_dir}/src/fft4g.c")
    message(FATAL_ERROR
        "Ooura FFT4G source was not found. "
        "Provide -DNTC_OOURA_SOURCE_DIR=<soxr-source-path> or allow FetchContent network access.")
endif()

file(GLOB_RECURSE NTC_NAMCORE_SOURCES CONFIGURE_DEPENDS
    "${_ntc_namcore_dir}/NAM/*.cpp"
)
# NAMCore v0.5.x registers architecture/config parsers (WaveNet, LSTM,
# ConvNet, SlimmableContainer, ...) from translation-unit static
# initializers. When the sources are packed into a normal STATIC library,
# MSVC only extracts objects that satisfy referenced symbols, so parser-only
# objects can be discarded and get_dsp() then reports e.g.
# "No config parser registered for architecture: WaveNet" at runtime. Clang/
# libtool on macOS can exhibit the same dead-object-stripping behavior for a
# STATIC archive, so keep this as an OBJECT library on every platform rather
# than special-casing Windows.
add_library(ntc_namcore OBJECT ${NTC_NAMCORE_SOURCES})
target_compile_features(ntc_namcore PUBLIC cxx_std_20)
target_include_directories(ntc_namcore PUBLIC
    "${_ntc_namcore_dir}"
    "${_ntc_namcore_dir}/Dependencies/eigen"
    "${_ntc_namcore_dir}/Dependencies/nlohmann"
    "${_ntc_namcore_dir}/Dependencies/AudioDSPTools"
)
target_compile_definitions(ntc_namcore PRIVATE NAM_ENABLE_A2_FAST NAM_SAMPLE_FLOAT)
if(WIN32)
    target_compile_definitions(ntc_namcore PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
endif()

# r8brain releases before v7 are not fully header-only; the historical
# template-era branch used by the official converter ships r8bbase.cpp.
# Build that third-party translation unit separately so this project's
# warning policy does not turn third-party optimizer diagnostics into a
# build failure.
if(EXISTS "${_ntc_r8brain_dir}/r8bbase.cpp")
    add_library(ntc_r8brain_base OBJECT "${_ntc_r8brain_dir}/r8bbase.cpp")
    target_include_directories(ntc_r8brain_base PRIVATE "${_ntc_r8brain_dir}")
endif()

# --------------------------------------------------------------------------
# namtoclo_core: the portable conversion/protocol engine. Everything under
# src/core is platform-neutral C++20 (see CLAUDE.md / README "Cross-platform
# architecture") except for two narrow seams -- platform.hpp and
# midi_transport.hpp -- whose implementations are supplied per OS below.
# --------------------------------------------------------------------------

# NOTE: this must stay an OBJECT library, not STATIC. NAMCore's architecture
# parsers register themselves via static initializers in ntc_namcore (see
# that target's comment above); if namtoclo_core were a STATIC archive, the
# same dead-object-stripping problem would simply move one level out --
# ntc_namcore's .o files would become archive members of namtoclo_core.a,
# and both MSVC's and Apple ld's archive linking only pulls in members that
# resolve a symbol reference, discarding "unreferenced" parser-registration
# objects again. Keeping this OBJECT (and linking $<TARGET_OBJECTS:...>
# transitively into whatever final executable consumes it, never through an
# intermediate archive) is what actually fixes get_dsp() at runtime -- see
# the "No config parser registered for architecture" failure this exact
# setup hit during the initial macOS bring-up.
add_library(namtoclo_core OBJECT
    src/core/native_converter.cpp
    src/core/native_converter.hpp
    src/core/native_converter_internal.hpp
    src/core/stimulus.cpp
    src/core/stimulus.hpp
    src/core/corrective_ir.cpp
    src/core/corrective_ir.hpp
    src/core/clo_refiner.cpp
    src/core/clo_refiner.hpp
    src/core/common.cpp
    src/core/common.hpp
    src/core/platform.hpp
    src/core/midi_transport.hpp
    src/core/gp5_optimizer.cpp
    src/core/gp5_optimizer.hpp
    src/core/gp200_clo_upload.cpp
    src/core/gp200_clo_upload.hpp
    src/core/gp200_midi.cpp
    src/core/gp200_midi.hpp
    src/core/gp5_clo_upload.cpp
    src/core/gp5_clo_upload.hpp
    src/core/gp5_midi.cpp
    src/core/gp5_midi.hpp
    "${_ntc_ooura_dir}/src/fft4g.c"
)

target_compile_features(namtoclo_core PUBLIC cxx_std_20)
target_include_directories(namtoclo_core PUBLIC src/core)
target_include_directories(namtoclo_core PRIVATE "${_ntc_ooura_dir}/src" "${_ntc_r8brain_dir}")
# Link (not $<TARGET_OBJECTS:...>-as-sources) so CMake propagates these
# OBJECT libraries' object files transitively into whatever final
# executable links namtoclo_core -- nesting OBJECT libraries via sources+
# TARGET_OBJECTS instead of target_link_libraries does not reliably
# propagate through a second OBJECT library.
target_link_libraries(namtoclo_core PUBLIC ntc_namcore)
if(TARGET ntc_r8brain_base)
    target_link_libraries(namtoclo_core PUBLIC ntc_r8brain_base)
endif()
target_compile_definitions(namtoclo_core PRIVATE NAM_SAMPLE_FLOAT)

if(WIN32)
    target_sources(namtoclo_core PRIVATE
        src/platform/windows/platform_win.cpp
        src/platform/windows/midi_transport_winmm.cpp
    )
    target_compile_definitions(namtoclo_core PUBLIC WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
    target_link_libraries(namtoclo_core PUBLIC winmm)
elseif(APPLE)
    target_sources(namtoclo_core PRIVATE
        src/platform/macos/platform_mac.cpp
        src/platform/macos/midi_transport_coremidi.cpp
        src/platform/macos/net_client_mac.mm
        # Tone3000 client + offline preview renderer: portable C++20, but
        # tone3000_client.cpp calls into net_client.hpp, which currently has
        # only this macOS implementation (see net_client.hpp and the
        # tone3000 integration plan), so these stay APPLE-only rather than
        # in the unconditional namtoclo_core source list above -- adding
        # them there would leave tone3000_client.cpp's net:: calls
        # unresolved at Windows link time even though nothing on Windows
        # calls into them (OBJECT library .obj files are linked wholesale).
        src/core/net_client.hpp
        src/core/tone3000_client.cpp
        src/core/tone3000_client.hpp
        src/core/tone3000_preview.cpp
        src/core/tone3000_preview.hpp
    )
    find_library(NTC_COREMIDI_FRAMEWORK CoreMIDI REQUIRED)
    find_library(NTC_COREFOUNDATION_FRAMEWORK CoreFoundation REQUIRED)
    find_library(NTC_FOUNDATION_FRAMEWORK Foundation REQUIRED)
    find_library(NTC_APPKIT_FRAMEWORK AppKit REQUIRED)
    find_library(NTC_SECURITY_FRAMEWORK Security REQUIRED)
    target_link_libraries(namtoclo_core PUBLIC
        "${NTC_COREMIDI_FRAMEWORK}" "${NTC_COREFOUNDATION_FRAMEWORK}"
        "${NTC_FOUNDATION_FRAMEWORK}" "${NTC_APPKIT_FRAMEWORK}" "${NTC_SECURITY_FRAMEWORK}"
    )
    # net_client_mac.mm is Objective-C++ (uses ARC-managed Foundation/
    # AppKit/Security types) -- enable ARC for it specifically rather than
    # for the whole target, which is otherwise plain C++20.
    set_source_files_properties(src/platform/macos/net_client_mac.mm PROPERTIES
        COMPILE_FLAGS "-fobjc-arc"
    )
endif()

if(MSVC)
    target_compile_options(namtoclo_core PRIVATE /W4 /WX- /permissive- /EHsc /utf-8 /fp:precise)
    set_property(TARGET namtoclo_core PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    target_compile_options(ntc_namcore PRIVATE /W3 /WX- /permissive- /EHsc /utf-8 /fp:precise)
    set_property(TARGET ntc_namcore PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    if(TARGET ntc_r8brain_base)
        target_compile_options(ntc_r8brain_base PRIVATE /W0 /WX- /permissive- /EHsc /utf-8 /fp:precise)
        set_property(TARGET ntc_r8brain_base PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    endif()
else()
    target_compile_options(namtoclo_core PRIVATE -Wall -Wextra -Wno-unused-parameter)
endif()

# CMake does not propagate an OBJECT library's compiled objects through a
# second OBJECT library (namtoclo_core here) to whatever finally links it --
# target_link_libraries between OBJECT libraries only propagates usage
# requirements (include dirs, etc), not the object files themselves. Every
# executable that links namtoclo_core must therefore also link ntc_namcore
# (and ntc_r8brain_base, when it exists) directly, so this helper is the
# one place that link set is defined.
function(ntc_link_core target)
    target_link_libraries(${target} PRIVATE namtoclo_core ntc_namcore)
    if(TARGET ntc_r8brain_base)
        target_link_libraries(${target} PRIVATE ntc_r8brain_base)
    endif()
endfunction()

# --------------------------------------------------------------------------
# namtoclo: cross-platform headless CLI (Phase 3 engineering/test interface;
# also the macOS user-facing entry point until the SwiftUI shell lands).
# --------------------------------------------------------------------------

add_executable(namtoclo src/cli/main.cpp)
ntc_link_core(namtoclo)
target_compile_features(namtoclo PRIVATE cxx_std_20)
install(TARGETS namtoclo RUNTIME DESTINATION .)

# Both the CLI and the Windows GUI resolve nam_input_wav.wav and
# resources/reference_clips relative to their own executable path (see
# resolveOriginalStimulusPath()/resolveReferenceClipsDir() in
# native_converter.cpp), so every target needs its own copy alongside it.
function(ntc_copy_runtime_resources target)
    add_custom_command(TARGET ${target} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E make_directory "$<TARGET_FILE_DIR:${target}>/reference_clips"
        COMMAND ${CMAKE_COMMAND} -E copy_directory
            "${CMAKE_SOURCE_DIR}/resources/reference_clips"
            "$<TARGET_FILE_DIR:${target}>/reference_clips"
        COMMENT "Copying Tone Match reference clips next to ${target}")
    if(EXISTS "${CMAKE_SOURCE_DIR}/nam_input_wav.wav")
        add_custom_command(TARGET ${target} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy
                "${CMAKE_SOURCE_DIR}/nam_input_wav.wav"
                "$<TARGET_FILE_DIR:${target}>/nam_input_wav.wav"
            COMMENT "Copying nam_input_wav.wav next to ${target}")
    endif()
endfunction()

ntc_copy_runtime_resources(namtoclo)
install(DIRECTORY resources/reference_clips/ DESTINATION reference_clips)

# --------------------------------------------------------------------------
# Hardware-free protocol tests (see tests/gp5_protocol_test.cpp): exercise
# GP-5/GP-50 payload construction (chunk framing, CRC-8, nibble encoding,
# slot placement) without needing a MIDI device attached. Real-hardware
# verification stays a separate, manual step -- see CLAUDE.md.
# --------------------------------------------------------------------------

enable_testing()
add_executable(ntc_gp5_protocol_test tests/gp5_protocol_test.cpp)
ntc_link_core(ntc_gp5_protocol_test)
target_compile_features(ntc_gp5_protocol_test PRIVATE cxx_std_20)
if(MSVC)
    set_property(TARGET ntc_gp5_protocol_test PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()
add_test(NAME gp5_protocol_test COMMAND ntc_gp5_protocol_test)

# --------------------------------------------------------------------------
# NamToClo.exe: the existing Win32 GUI. Windows-only; unchanged behavior.
# --------------------------------------------------------------------------

if(WIN32)
    add_executable(NamToClo WIN32
        src/gui/gui.cpp
        src/gui/resources.rc
        src/gui/resource.h
    )
    target_include_directories(NamToClo PRIVATE src/gui)
    target_compile_features(NamToClo PRIVATE cxx_std_20)
    target_compile_definitions(NamToClo PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
    ntc_link_core(NamToClo)
    target_link_libraries(NamToClo PRIVATE comdlg32 shell32 ole32 dbghelp advapi32 comctl32)

    if(MSVC)
        target_compile_options(NamToClo PRIVATE /W4 /WX- /permissive- /EHsc /utf-8 /fp:precise)
        set_property(TARGET NamToClo PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    endif()

    install(TARGETS NamToClo RUNTIME DESTINATION .)
    ntc_copy_runtime_resources(NamToClo)
endif()

install(FILES README.md LICENSE THIRD_PARTY.md DESTINATION .)

# Preserve license notices when distributing a statically linked build.
if(EXISTS "${_ntc_namcore_dir}/LICENSE")
    install(FILES "${_ntc_namcore_dir}/LICENSE" DESTINATION . RENAME NEURALAMPMODELERCORE_LICENSE.txt)
endif()
if(EXISTS "${_ntc_namcore_dir}/Dependencies/nlohmann/LICENSE.MIT")
    install(FILES "${_ntc_namcore_dir}/Dependencies/nlohmann/LICENSE.MIT" DESTINATION . RENAME NLOHMANN_JSON_LICENSE.txt)
endif()
if(EXISTS "${_ntc_namcore_dir}/Dependencies/eigen/COPYING.MPL2")
    install(FILES "${_ntc_namcore_dir}/Dependencies/eigen/COPYING.MPL2" DESTINATION . RENAME EIGEN_LICENSE.txt)
endif()
if(EXISTS "${_ntc_namcore_dir}/Dependencies/AudioDSPTools/LICENSE")
    install(FILES "${_ntc_namcore_dir}/Dependencies/AudioDSPTools/LICENSE" DESTINATION . RENAME AUDIODSPTOOLS_LICENSE.txt)
endif()
if(EXISTS "${_ntc_r8brain_dir}/LICENSE")
    install(FILES "${_ntc_r8brain_dir}/LICENSE" DESTINATION . RENAME R8BRAIN_LICENSE.txt)
elseif(EXISTS "${_ntc_r8brain_dir}/License.txt")
    install(FILES "${_ntc_r8brain_dir}/License.txt" DESTINATION . RENAME R8BRAIN_LICENSE.txt)
endif()
