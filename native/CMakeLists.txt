cmake_minimum_required(VERSION 3.18)
project(telecalls_engine LANGUAGES C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(
  TELECALLS_BUILD_TGCALLS_BACKEND
  "Build tgcalls-compatible native backend library"
  ON
)

find_package(PkgConfig REQUIRED)
find_package(Threads REQUIRED)
find_package(OpenSSL REQUIRED)

pkg_check_modules(OPUS opus)
if(OPUS_FOUND)
  message(STATUS "Found Opus: ${OPUS_VERSION}")
else()
  message(STATUS "Opus not found, building with codec stubs")
endif()

pkg_check_modules(PORTAUDIO portaudio-2.0)

add_library(
  telecalls_engine
  SHARED
  src/engine.c
  src/engine_debug.c
  src/transport_udp.c
  src/reflector_transport.c
  src/codec_opus.c
  src/media_pipeline.c
  src/proto.c
  src/crypto_mtproto2.c
)
target_include_directories(
    telecalls_engine
    PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

if(OPUS_FOUND)
  target_compile_definitions(telecalls_engine PRIVATE TELECALLS_WITH_OPUS=1)
  target_include_directories(telecalls_engine PRIVATE ${OPUS_INCLUDE_DIRS})
  target_link_directories(telecalls_engine PRIVATE ${OPUS_LIBRARY_DIRS})
  target_link_libraries(telecalls_engine PRIVATE ${OPUS_LIBRARIES})
endif()

if(PORTAUDIO_FOUND)
  target_compile_definitions(telecalls_engine PRIVATE TELECALLS_WITH_PORTAUDIO=1)
  target_include_directories(telecalls_engine PRIVATE ${PORTAUDIO_INCLUDE_DIRS})
  target_link_directories(telecalls_engine PRIVATE ${PORTAUDIO_LIBRARY_DIRS})
  target_link_libraries(telecalls_engine PRIVATE ${PORTAUDIO_LIBRARIES})
endif()

target_link_libraries(telecalls_engine PRIVATE Threads::Threads)
target_link_libraries(telecalls_engine PRIVATE OpenSSL::Crypto)

set_target_properties(
    telecalls_engine
    PROPERTIES
    OUTPUT_NAME telecalls_engine
)

if(TELECALLS_BUILD_TGCALLS_BACKEND)
  set(TELECALLS_TGCALLS_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/third_party/lib_tgcalls")
  set(TELECALLS_TGCALLS_SRC "${TELECALLS_TGCALLS_ROOT}/tgcalls")
  set(TELECALLS_WEBRTC_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/third_party/webrtc")

  if(NOT EXISTS "${TELECALLS_TGCALLS_SRC}/Instance.h")
    message(FATAL_ERROR "tgcalls sources are missing under ${TELECALLS_TGCALLS_SRC}. Run native/tools/fetch_tgcalls_deps.sh")
  endif()
  if(NOT EXISTS "${TELECALLS_WEBRTC_ROOT}/CMakeLists.txt")
    message(FATAL_ERROR "webrtc sources are missing under ${TELECALLS_WEBRTC_ROOT}. Run native/tools/fetch_tgcalls_deps.sh")
  endif()

  # Prefer vendored abseil from tg_owt to avoid mismatch with system package.
  set(CMAKE_DISABLE_FIND_PACKAGE_absl TRUE)
  # Build tg_owt as static to avoid hidden-symbol issues when linking lib_tgcalls.
  set(BUILD_SHARED_LIBS OFF CACHE BOOL "Build static libraries by default" FORCE)
  # Avoid protobuf/Homebrew absl ABI mismatch and ObjC SDK glue we do not use.
  set(TG_OWT_USE_PROTOBUF OFF CACHE BOOL "Disable tg_owt protobuf generation" FORCE)
  set(TG_OWT_BUILD_SDKMACOS OFF CACHE BOOL "Disable tg_owt ObjC SDK wrappers" FORCE)
  if(APPLE)
    add_compile_options(-Wno-narrowing -Wno-c++11-narrowing)
  endif()

  add_subdirectory(
    "${TELECALLS_WEBRTC_ROOT}"
    "${CMAKE_BINARY_DIR}/third_party_webrtc"
    EXCLUDE_FROM_ALL
  )

  if(APPLE)
    target_link_libraries(
      tg_owt
      PRIVATE
      "-framework AppKit"
      "-framework AudioToolbox"
      "-framework AVFoundation"
      "-framework CoreAudio"
      "-framework CoreFoundation"
      "-framework CoreGraphics"
      "-framework CoreMedia"
      "-framework CoreVideo"
      "-framework IOSurface"
      "-framework OpenGL"
      "-framework QuartzCore"
      "-framework VideoToolbox"
    )
  endif()

  set(TELECALLS_LIB_TGCALLS_SOURCES
    "${TELECALLS_TGCALLS_SRC}/Instance.cpp"
    "${TELECALLS_TGCALLS_SRC}/AudioDeviceHelper.cpp"
    "${TELECALLS_TGCALLS_SRC}/CodecSelectHelper.cpp"
    "${TELECALLS_TGCALLS_SRC}/CryptoHelper.cpp"
    "${TELECALLS_TGCALLS_SRC}/EncryptedConnection.cpp"
    "${TELECALLS_TGCALLS_SRC}/InstanceImpl.cpp"
    "${TELECALLS_TGCALLS_SRC}/LogSinkImpl.cpp"
    "${TELECALLS_TGCALLS_SRC}/Manager.cpp"
    "${TELECALLS_TGCALLS_SRC}/MediaManager.cpp"
    "${TELECALLS_TGCALLS_SRC}/Message.cpp"
    "${TELECALLS_TGCALLS_SRC}/NetworkManager.cpp"
    "${TELECALLS_TGCALLS_SRC}/SctpDataChannelProviderInterfaceImpl.cpp"
    "${TELECALLS_TGCALLS_SRC}/StaticThreads.cpp"
    "${TELECALLS_TGCALLS_SRC}/TurnCustomizerImpl.cpp"
    "${TELECALLS_TGCALLS_SRC}/VideoCaptureInterface.cpp"
    "${TELECALLS_TGCALLS_SRC}/VideoCaptureInterfaceImpl.cpp"
    "${TELECALLS_TGCALLS_SRC}/platform/fake/FakeInterface.cpp"
    "${TELECALLS_TGCALLS_SRC}/third-party/json11.cpp"
  )

  add_library(telecalls_lib_tgcalls SHARED ${TELECALLS_LIB_TGCALLS_SOURCES})
  target_include_directories(
      telecalls_lib_tgcalls
      PUBLIC
      "${TELECALLS_TGCALLS_ROOT}"
      "${TELECALLS_TGCALLS_SRC}"
      "${TELECALLS_WEBRTC_ROOT}/src"
  )
  target_compile_definitions(
      telecalls_lib_tgcalls
      PUBLIC
      TGCALLS_USE_STD_OPTIONAL
      WEBRTC_APP_TDESKTOP
      RTC_ENABLE_VP9
  )
  if(APPLE)
    target_compile_definitions(telecalls_lib_tgcalls PUBLIC WEBRTC_MAC)
  elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_compile_definitions(telecalls_lib_tgcalls PUBLIC WEBRTC_LINUX)
  endif()
  target_link_libraries(
      telecalls_lib_tgcalls
      PUBLIC
      tg_owt
      Threads::Threads
      OpenSSL::Crypto
  )
  if(APPLE)
    target_link_libraries(
      telecalls_lib_tgcalls
      PRIVATE
      "-framework AppKit"
      "-framework AudioToolbox"
      "-framework AVFoundation"
      "-framework CoreAudio"
      "-framework CoreFoundation"
      "-framework CoreGraphics"
      "-framework CoreMedia"
      "-framework CoreVideo"
      "-framework IOSurface"
      "-framework OpenGL"
      "-framework QuartzCore"
      "-framework VideoToolbox"
    )
  endif()
  set_target_properties(
      telecalls_lib_tgcalls
      PROPERTIES
      OUTPUT_NAME telecalls_lib_tgcalls
      BUILD_RPATH "@loader_path;${CMAKE_BINARY_DIR}/third_party_webrtc"
      INSTALL_RPATH "@loader_path"
  )

  add_library(
    telecalls_tgcalls_engine
    SHARED
    src/tgcalls_engine.cpp
  )
  target_include_directories(
      telecalls_tgcalls_engine
      PUBLIC
      ${CMAKE_CURRENT_SOURCE_DIR}/include
  )
  target_link_libraries(
      telecalls_tgcalls_engine
      PRIVATE
      telecalls_lib_tgcalls
      Threads::Threads
      OpenSSL::Crypto
  )
  set_target_properties(
      telecalls_tgcalls_engine
      PROPERTIES
      OUTPUT_NAME telecalls_tgcalls_engine
      BUILD_RPATH "@loader_path;${CMAKE_CURRENT_BINARY_DIR}"
      INSTALL_RPATH "@loader_path"
  )
endif()
