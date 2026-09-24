#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"

if [[ $# -lt 1 ]]; then
	echo "Usage: $0 command [args...]" >&2
	exit 2
fi

if [[ "${container:-}" == "bwrap" ]]; then
	exec "$@"
fi

if ! command -v bwrap >/dev/null 2>&1; then
	echo "swaygentic: bwrap not found" >&2
	exit 1
fi

SECCOMP_BPF="$ROOT/sandbox/seccomp.bpf"
if [[ ! -r "$SECCOMP_BPF" ]]; then
	"$ROOT/sandbox/build-seccomp.sh"
fi
if [[ ! -r "$SECCOMP_BPF" ]]; then
	echo "swaygentic: seccomp filter missing: $SECCOMP_BPF" >&2
	exit 1
fi

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${WAYLAND_DISPLAY:-}" ]]; then
	if [[ -S "${XDG_RUNTIME_DIR}/wayland-0" ]]; then
		export WAYLAND_DISPLAY=wayland-0
	elif [[ -S "${XDG_RUNTIME_DIR}/wayland-1" ]]; then
		export WAYLAND_DISPLAY=wayland-1
	else
		export WAYLAND_DISPLAY=wayland-0
	fi
fi
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "${XDG_RUNTIME_DIR}/bus" ]]; then
	export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
fi

RUNTIME_DIR="$XDG_RUNTIME_DIR"
WAYLAND_SOCKET="$RUNTIME_DIR/${WAYLAND_DISPLAY}"
BUS_ADDR="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$RUNTIME_DIR/bus}"
PROXY_SOCK="$RUNTIME_DIR/swaygentic-dbus"
PROXY_PIDFILE="$RUNTIME_DIR/swaygentic-dbus.pid"
PROXY_SIGFILE="$RUNTIME_DIR/swaygentic-dbus.sig"

DBUS_POLICY=(
	--talk=org.a11y.Bus
	--talk=org.a11y.*
	--talk=org.freedesktop.Notifications
	--talk=org.freedesktop.portal.Desktop
	--talk=org.freedesktop.portal.Documents
)

args=()

bind_into_home() {
	local flag="$1" src="$2" dest="${3:-$2}"
	[[ -e "$src" ]] || return 0
	local rel="${dest#"$HOME"/}"
	if [[ "$rel" == "$dest" ]]; then
		echo "swaygentic: $dest is not under \$HOME" >&2
		return 1
	fi
	local acc="$HOME"
	local IFS=/
	local -a parts
	read -ra parts <<<"$rel"
	local last=$((${#parts[@]} - 1)) i
	for ((i = 0; i < last; i++)); do
		acc="$acc/${parts[i]}"
		args+=(--dir "$acc")
	done
	if [[ -d "$src" ]]; then
		args+=(--dir "$dest")
	fi
	args+=("$flag" "$src" "$dest")
}

args+=(
	--unshare-all --share-net
	--cap-drop ALL
	--die-with-parent
	--clearenv
	--proc /proc
	--dev /dev
	--tmpfs /tmp
	--tmpfs "$HOME"
	--dir "$RUNTIME_DIR"
	--ro-bind /usr /usr
	--symlink usr/bin /bin
	--symlink usr/sbin /sbin
	--symlink usr/lib /lib
	--symlink usr/lib64 /lib64
	--ro-bind /etc /etc
	--ro-bind-try /opt/brave.com/brave-origin-nightly /opt/brave.com/brave-origin-nightly
	--ro-bind-try /var/cache/fontconfig /var/cache/fontconfig
	--ro-bind-try /run/systemd/resolve /run/systemd/resolve
)

if [[ -d /dev/dri ]]; then
	shopt -s nullglob
	local_nodes=(/dev/dri/renderD* /dev/dri/card*)
	shopt -u nullglob
	if ((${#local_nodes[@]} > 0)); then
		args+=(--dir /dev/dri)
		for n in "${local_nodes[@]}"; do
			args+=(--dev-bind "$n" "$n")
		done
		args+=(--ro-bind-try /sys/devices /sys/devices)
		args+=(--ro-bind-try /sys/dev/char /sys/dev/char)
		args+=(--ro-bind-try /sys/class/drm /sys/class/drm)
	fi
fi

args+=(--ro-bind-try "$WAYLAND_SOCKET" "$WAYLAND_SOCKET")
if [[ -d "$RUNTIME_DIR/at-spi" ]]; then
	args+=(--bind "$RUNTIME_DIR/at-spi" "$RUNTIME_DIR/at-spi")
fi
args+=(--bind "$PROXY_SOCK" "$RUNTIME_DIR/bus")

for p in \
	"$HOME/.grok" \
	"$HOME/.config/BraveSoftware/Brave-Origin-Nightly" \
	"$HOME/.cache/BraveSoftware/Brave-Origin-Nightly" \
	"$HOME/.config/gtk-3.0" \
	"$HOME/.config/gtk-4.0" \
	"$HOME/.config/fontconfig" \
	"$HOME/.config/kdeglobals" \
	"$HOME/.config/brave-origin-nightly-flags.conf"
do
	bind_into_home --bind "$p"
done

bind_into_home --ro-bind "$HOME/.gitconfig"
bind_into_home --bind "$ROOT"

args+=(
	--setenv HOME "$HOME"
	--setenv USER "$(id -un)"
	--setenv LOGNAME "$(id -un)"
	--setenv PATH "$HOME/.grok/bin:$HOME/.local/bin:/usr/bin:/usr/sbin:/bin:/sbin"
	--setenv XDG_RUNTIME_DIR "$RUNTIME_DIR"
	--setenv XDG_SESSION_TYPE wayland
	--setenv DBUS_SESSION_BUS_ADDRESS "unix:path=$RUNTIME_DIR/bus"
	--setenv container bwrap
	--setenv SWAYGENTIC_JAIL 1
	--setenv QT_QPA_PLATFORM wayland
	--setenv GDK_BACKEND wayland
	--chdir "$ROOT"
)

ENV_PASSTHROUGH=(
	LANG LANGUAGE TZ
	LC_ALL LC_CTYPE LC_NUMERIC LC_TIME LC_COLLATE LC_MONETARY LC_MESSAGES
	LC_PAPER LC_NAME LC_ADDRESS LC_TELEPHONE LC_MEASUREMENT LC_IDENTIFICATION
	XDG_CURRENT_DESKTOP XDG_SESSION_DESKTOP XDG_SESSION_ID
	XDG_DATA_DIRS XDG_CONFIG_DIRS
	DESKTOP_SESSION KDE_FULL_SESSION KDE_SESSION_VERSION
	QT_QPA_PLATFORMTHEME QT_STYLE_OVERRIDE
	TERM COLORTERM
	XAI_API_KEY GROK_MODEL
)

for var in "${ENV_PASSTHROUGH[@]}"; do
	if [[ -n "${!var:-}" ]]; then
		args+=(--setenv "$var" "${!var}")
	fi
done

if [[ -S "$WAYLAND_SOCKET" ]]; then
	args+=(--setenv WAYLAND_DISPLAY "$WAYLAND_DISPLAY")
fi

args+=("$@")

start_proxy() {
	if ! command -v xdg-dbus-proxy >/dev/null 2>&1; then
		echo "swaygentic: xdg-dbus-proxy not found" >&2
		return 1
	fi
	local sig
	sig="$(printf '%s\n' "${DBUS_POLICY[@]}" | sha256sum | cut -d' ' -f1)"
	if [[ -f "$PROXY_PIDFILE" && -f "$PROXY_SIGFILE" ]] \
		&& [[ "$(cat "$PROXY_SIGFILE" 2>/dev/null)" == "$sig" ]] \
		&& kill -0 "$(cat "$PROXY_PIDFILE" 2>/dev/null)" 2>/dev/null \
		&& [[ -S "$PROXY_SOCK" ]]; then
		return 0
	fi
	if [[ -f "$PROXY_PIDFILE" ]]; then
		kill "$(cat "$PROXY_PIDFILE" 2>/dev/null)" 2>/dev/null || true
	fi
	rm -f "$PROXY_SOCK"
	setsid xdg-dbus-proxy "$BUS_ADDR" "$PROXY_SOCK" --filter "${DBUS_POLICY[@]}" \
		>/dev/null 2>&1 &
	echo $! >"$PROXY_PIDFILE"
	echo "$sig" >"$PROXY_SIGFILE"
	local i
	for i in $(seq 1 50); do
		[[ -S "$PROXY_SOCK" ]] && return 0
		sleep 0.1
	done
	echo "swaygentic: xdg-dbus-proxy did not create $PROXY_SOCK" >&2
	return 1
}

start_proxy
exec bwrap --seccomp 10 "${args[@]}" 10<"$SECCOMP_BPF"
