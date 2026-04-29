#!/bin/bash
# UI 래퍼: libthorvg.so 로딩 보장
DIR="$(cd "$(dirname "$0")" && pwd)"
export LD_LIBRARY_PATH="${DIR}:${LD_LIBRARY_PATH}"
exec "${DIR}/ui.bin" "$@"
