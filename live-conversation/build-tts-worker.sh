#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
runtime_dir="${SHERPA_ONNX_RUNTIME_DIR:-/home/john/.openclaw/tools/sherpa-onnx-tts/runtime}"

g++ -std=c++17 -O3 -DNDEBUG -D_GLIBCXX_USE_CXX11_ABI=0 \
  -I"$runtime_dir/include" \
  "$script_dir/tts-worker.cc" \
  -L"$runtime_dir/lib" -lsherpa-onnx-cxx-api \
  -Wl,-rpath,"$runtime_dir/lib" \
  -o "$script_dir/openclaw-kokoro-tts-worker"
