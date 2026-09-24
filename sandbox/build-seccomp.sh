#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SRC="$ROOT/seccomp/build_seccomp_filter.c"
OUT="$ROOT/seccomp.bpf"
BUILD_TMP="$(mktemp -d)"
trap 'rm -rf "$BUILD_TMP"' EXIT

if ! command -v cc >/dev/null 2>&1; then
	echo "swaygentic: cc not found (need a C compiler to build the seccomp filter)" >&2
	exit 1
fi
if [[ ! -e /usr/include/seccomp.h ]]; then
	echo "swaygentic: libseccomp headers missing (/usr/include/seccomp.h)" >&2
	exit 1
fi

cc -O2 -Wall -o "$BUILD_TMP/build_seccomp_filter" "$SRC" -lseccomp
"$BUILD_TMP/build_seccomp_filter" "$OUT"
echo "swaygentic: wrote $OUT"
