# Portability patch for r8brain-free-src (applied via FetchContent's
# PATCH_COMMAND -- see CMakeLists.txt).
#
# CFixedBuffer<T>::alignptr() is a member function template that redeclares
# its own template parameter as "T", shadowing the enclosing class
# template's "T". MSVC has always tolerated this; recent Clang enforces
# [temp.local]p6 and treats it as a hard, non-suppressible error
# ("declaration of 'T' shadows template parameter"), which blocks the macOS
# build entirely. This renames only the inner, unrelated template parameter
# (TAlign) -- alignptr() is called nowhere outside this one class, so the
# rename has no observable effect on behavior.
#
# Invoked as: cmake -DNTC_R8BRAIN_PATCH_FILE=<path/to/r8bbase.h> -P this-file

if(NOT DEFINED NTC_R8BRAIN_PATCH_FILE)
    message(FATAL_ERROR "NTC_R8BRAIN_PATCH_FILE must be set")
endif()

file(READ "${NTC_R8BRAIN_PATCH_FILE}" _ntc_r8brain_contents)

set(_ntc_r8brain_from [[template< class T >
	inline T alignptr( const T ptr, const uintptr_t align )
	{
		return( (T) ( (uintptr_t) ptr + align -
			( (uintptr_t) ptr & ( align - 1 ))) );
	}]])

set(_ntc_r8brain_to [[template< class TAlign >
	inline TAlign alignptr( const TAlign ptr, const uintptr_t align )
	{
		return( (TAlign) ( (uintptr_t) ptr + align -
			( (uintptr_t) ptr & ( align - 1 ))) );
	}]])

string(FIND "${_ntc_r8brain_contents}" "${_ntc_r8brain_from}" _ntc_r8brain_found)
if(_ntc_r8brain_found EQUAL -1)
    # Idempotent: FetchContent can re-run PATCH_COMMAND against an
    # already-populated (and thus already-patched) checkout, e.g. whenever
    # CMakeLists.txt changes and CMake reconfigures. If the patched text is
    # already present, there's nothing to do; only a genuinely unrecognized
    # file is an error.
    string(FIND "${_ntc_r8brain_contents}" "${_ntc_r8brain_to}" _ntc_r8brain_already_patched)
    if(_ntc_r8brain_already_patched EQUAL -1)
        message(FATAL_ERROR
            "patch_r8brain_template_shadow.cmake: expected alignptr() text not "
            "found in ${NTC_R8BRAIN_PATCH_FILE} -- r8brain-free-src may have "
            "changed upstream; update this patch (or drop it if the shadow "
            "error is already fixed there).")
    endif()
    return()
endif()

string(REPLACE "${_ntc_r8brain_from}" "${_ntc_r8brain_to}" _ntc_r8brain_contents "${_ntc_r8brain_contents}")
file(WRITE "${NTC_R8BRAIN_PATCH_FILE}" "${_ntc_r8brain_contents}")
