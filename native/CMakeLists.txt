cmake_minimum_required(VERSION 3.18)
project(telecalls_engine LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

add_library(telecalls_engine SHARED src/engine.c)
target_include_directories(
    telecalls_engine
    PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

set_target_properties(
    telecalls_engine
    PROPERTIES
    OUTPUT_NAME telecalls_engine
)
