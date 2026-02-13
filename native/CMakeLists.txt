cmake_minimum_required(VERSION 3.18)
project(telecalls_engine LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

find_package(PkgConfig REQUIRED)
find_package(Threads REQUIRED)

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
  src/transport_udp.c
  src/codec_opus.c
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

set_target_properties(
    telecalls_engine
    PROPERTIES
    OUTPUT_NAME telecalls_engine
)
