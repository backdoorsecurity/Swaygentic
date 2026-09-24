#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
WRAPPER_BIN="$ROOT/bin"
MARKER="# swaygentic wrapper"

need_cmd() {
	command -v "$1" >/dev/null 2>&1
}

die() {
	echo "swaygentic: $*" >&2
	exit 1
}

as_user() {
	if [[ "$(id -u)" -eq 0 && -n "${REAL_USER:-}" && "$REAL_USER" != root ]]; then
		sudo -u "$REAL_USER" -H -- "$@"
	else
		"$@"
	fi
}

setup_user() {
	if [[ "$(id -u)" -eq 0 ]]; then
		if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == root ]]; then
			die "run as your user (sudo is used only for package installs). do not run as root."
		fi
		REAL_USER="$SUDO_USER"
		REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
		SUDO=()
	else
		REAL_USER="$(id -un)"
		REAL_HOME="$HOME"
		need_cmd sudo || die "sudo not found"
		SUDO=(sudo)
	fi
	[[ -n "$REAL_HOME" && -d "$REAL_HOME" ]] || die "could not resolve home for $REAL_USER"
	REAL_HOME="$(readlink -f "$REAL_HOME")"
}

# path string for rc files: $HOME/... when under the user's home
home_ref() {
	local p="$1"
	if [[ "$p" == "$REAL_HOME"/* ]]; then
		printf '%s' "\$HOME/${p#"$REAL_HOME"/}"
	else
		printf '%s' "$p"
	fi
}

detect_distro() {
	local id="" like=""
	if [[ -r /etc/os-release ]]; then
		id="$(. /etc/os-release && printf '%s' "$ID")"
		like="$(. /etc/os-release && printf '%s' "${ID_LIKE:-}")"
	fi
	case "$id $like" in
	*arch*) DISTRO=arch ;;
	*debian* | *ubuntu*) DISTRO=debian ;;
	*)
		if [[ -f /etc/arch-release ]]; then
			DISTRO=arch
		elif [[ -f /etc/debian_version ]]; then
			DISTRO=debian
		else
			die "Debian or Arch Linux required"
		fi
		;;
	esac
}

check_host() {
	detect_distro
	local arch
	case "$DISTRO" in
	debian)
		need_cmd apt-get || die "apt-get not found"
		arch="$(dpkg --print-architecture 2>/dev/null || uname -m)"
		;;
	arch)
		need_cmd pacman || die "pacman not found"
		arch="$(uname -m)"
		;;
	esac
	[[ "$arch" == amd64 || "$arch" == x86_64 ]] || die "x86_64 required (got $arch); seccomp filter is x86"
}

apt_install() {
	export DEBIAN_FRONTEND=noninteractive
	"${SUDO[@]}" apt-get update
	"${SUDO[@]}" apt-get install -y --no-install-recommends \
		bubblewrap \
		xdg-dbus-proxy \
		python3 \
		gcc \
		libseccomp-dev \
		curl \
		ca-certificates \
		coreutils \
		util-linux \
		procps \
		xdg-desktop-portal \
		at-spi2-core \
		libgl1-mesa-dri \
		libgtk-3-0 \
		libgtk-4-1 \
		qt6-wayland \
		fonts-liberation \
		dbus-user-session \
		apparmor
	local extra=() p
	for p in mesa-libgallium fonts-noto-color-emoji xdg-desktop-portal-gtk; do
		if apt-cache show "$p" >/dev/null 2>&1; then
			extra+=("$p")
		fi
	done
	if ((${#extra[@]})); then
		"${SUDO[@]}" apt-get install -y --no-install-recommends "${extra[@]}"
	fi
}

# Arch has no partial upgrades: -Syu, not -Sy. base-devel + git cover a makepkg
# build of brave-origin-nightly-bin when no AUR helper is installed.
pacman_install() {
	echo "swaygentic: pacman -Syu (Arch does not support partial upgrades)"
	"${SUDO[@]}" pacman -Syu --needed --noconfirm \
		bubblewrap \
		xdg-dbus-proxy \
		python \
		gcc \
		libseccomp \
		curl \
		ca-certificates \
		coreutils \
		util-linux \
		procps-ng \
		xdg-desktop-portal \
		xdg-desktop-portal-gtk \
		at-spi2-core \
		mesa \
		gtk3 \
		gtk4 \
		qt6-wayland \
		ttf-liberation \
		noto-fonts-emoji \
		dbus \
		git \
		base-devel
}

install_packages() {
	case "$DISTRO" in
	debian) apt_install ;;
	arch) pacman_install ;;
	esac
}

install_brave_makepkg() {
	need_cmd git || die "git required to build brave-origin-nightly-bin"
	need_cmd makepkg || die "makepkg required (base-devel)"
	local src
	src="$(as_user mktemp -d "${TMPDIR:-/tmp}/swaygentic-brave.XXXXXX")"
	as_user git clone --depth 1 https://aur.archlinux.org/brave-origin-nightly-bin.git "$src/brave-origin-nightly-bin"
	as_user bash -lc "cd $(printf '%q' "$src/brave-origin-nightly-bin") && makepkg -si --noconfirm --needed"
	as_user rm -rf "$src"
}

install_brave() {
	if need_cmd brave-origin-nightly; then
		echo "swaygentic: brave-origin-nightly already present, skipping"
		return 0
	fi
	if [[ "$DISTRO" == arch ]] && ! { need_cmd paru || need_cmd pikaur || need_cmd yay; }; then
		echo "swaygentic: no AUR helper; building brave-origin-nightly-bin with makepkg"
		install_brave_makepkg
	else
		curl -fsS https://dl.brave.com/install.sh | FLAVOR=origin CHANNEL=nightly sh
	fi
	need_cmd brave-origin-nightly || die "brave-origin-nightly not on PATH after install"
}

prompt_yes() {
	local ans
	if [[ ! -t 0 ]]; then
		echo "swaygentic: no tty, skipping grok prompt" >&2
		return 1
	fi
	printf "%s" "$1"
	read -r ans
	case "$ans" in
	y | Y | yes | YES) return 0 ;;
	*) return 1 ;;
	esac
}

install_grok() {
	if as_user bash -lc 'command -v grok >/dev/null || test -x "$HOME/.grok/bin/grok"'; then
		echo "swaygentic: grok already present, skipping"
		return 0
	fi
	if ! prompt_yes "Install grok CLI from https://x.ai/cli/install.sh ? [y/N] "; then
		echo "swaygentic: skipping grok"
		return 0
	fi
	as_user bash -lc 'curl -fsSL https://x.ai/cli/install.sh | bash'
}

# replace any previous marker block, then write the new PATH lines
write_wrapper_path() {
	local file="$1"
	shift
	mkdir -p "$(dirname "$file")"
	python3 - "$file" "$MARKER" "$@" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
marker = sys.argv[2]
block = sys.argv[3:]
lines = path.read_text().splitlines() if path.is_file() else []
out = []
i = 0

def is_ours(s):
	t = s.lstrip()
	return (
		s.startswith("export PATH=")
		or s.startswith("if not contains ")
		or t.startswith("set -gx PATH ")
		or s == "end"
	)

while i < len(lines):
	if lines[i] == marker:
		i += 1
		while i < len(lines) and is_ours(lines[i]):
			i += 1
		continue
	out.append(lines[i])
	i += 1

while out and out[-1] == "":
	out.pop()
out.append("")
out.append(marker)
out.extend(block)
path.write_text("\n".join(out) + "\n")
PY
}

append_path_sh() {
	local wrapped
	wrapped="$(home_ref "$WRAPPER_BIN")"
	write_wrapper_path "$1" "export PATH=\"$wrapped:\$PATH\""
}

append_path_fish() {
	local wrapped
	wrapped="$(home_ref "$WRAPPER_BIN")"
	write_wrapper_path "$1" \
		"if not contains \"$wrapped\" \$PATH" \
		"	set -gx PATH \"$wrapped\" \$PATH" \
		"end"
}

add_wrapper_path() {
	append_path_sh "$REAL_HOME/.bashrc"
	append_path_sh "$REAL_HOME/.zshrc"
	append_path_fish "$REAL_HOME/.config/fish/config.fish"
	if [[ "$(id -u)" -eq 0 ]]; then
		"${SUDO[@]}" chown "$REAL_USER:" "$REAL_HOME/.bashrc" "$REAL_HOME/.zshrc"
		"${SUDO[@]}" chown -R "$REAL_USER:" "$REAL_HOME/.config/fish"
	fi
}

chmod_scripts() {
	chmod +x "$ROOT/bin/swaygentic" "$ROOT/sandbox/run.sh" "$ROOT/sandbox/build-seccomp.sh" "$ROOT/install.sh"
}

build_seccomp() {
	"$ROOT/sandbox/build-seccomp.sh"
}

bwrap_ok() {
	as_user bwrap --unshare-all --die-with-parent \
		--ro-bind /usr /usr --symlink usr/bin /bin \
		--proc /proc --dev /dev --tmpfs /tmp \
		-- /bin/true
}

probe_bwrap() {
	if bwrap_ok; then
		return 0
	fi
	echo "swaygentic: bwrap userns probe failed; refreshing bubblewrap" >&2
	case "$DISTRO" in
	debian)
		"${SUDO[@]}" apt-get install -y --reinstall bubblewrap apparmor || true
		if need_cmd aa-status; then
			"${SUDO[@]}" systemctl reload apparmor 2>/dev/null || "${SUDO[@]}" apparmor_parser -r /etc/apparmor.d/*bwrap* 2>/dev/null || true
		fi
		;;
	arch)
		"${SUDO[@]}" pacman -S --noconfirm bubblewrap || true
		;;
	esac
	if bwrap_ok; then
		return 0
	fi
	echo "swaygentic: bwrap still cannot create a user namespace." >&2
	if [[ -r /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]]; then
		echo "  if /proc/sys/kernel/apparmor_restrict_unprivileged_userns is 1, either" >&2
		echo "  load a bwrap AppArmor profile that allows userns, or (last resort):" >&2
		echo "  echo kernel.apparmor_restrict_unprivileged_userns=0 | sudo tee /etc/sysctl.d/60-swaygentic-userns.conf" >&2
		echo "  sudo sysctl --system" >&2
	fi
	if [[ -r /proc/sys/kernel/unprivileged_userns_clone ]] && [[ "$(cat /proc/sys/kernel/unprivileged_userns_clone)" == 0 ]]; then
		echo "  kernel.unprivileged_userns_clone is 0. enable unprivileged user namespaces:" >&2
		echo "  echo kernel.unprivileged_userns_clone=1 | sudo tee /etc/sysctl.d/60-swaygentic-userns.conf" >&2
		echo "  sudo sysctl --system" >&2
	fi
	if [[ -r /proc/sys/user/max_user_namespaces ]] && [[ "$(cat /proc/sys/user/max_user_namespaces)" == 0 ]]; then
		echo "  user.max_user_namespaces is 0. raise it:" >&2
		echo "  echo user.max_user_namespaces=10240 | sudo tee /etc/sysctl.d/60-swaygentic-userns.conf" >&2
		echo "  sudo sysctl --system" >&2
	fi
	return 1
}

verify() {
	local c
	for c in bwrap xdg-dbus-proxy python3 cc curl sha256sum setsid brave-origin-nightly; do
		need_cmd "$c" || die "missing $c after install"
	done
	[[ -x "$WRAPPER_BIN/swaygentic" ]] || die "wrapper not executable"
	python3 -c "import sys; sys.path.insert(0, '$ROOT'); from toolbox import config; assert config.browser == 'brave-origin-nightly'"
	echo "brave: $(brave-origin-nightly --version)"
	echo "python: $(python3 --version)"
	echo "bwrap: $(bwrap --version | head -n1)"
	if as_user bash -lc 'command -v grok >/dev/null || test -x "$HOME/.grok/bin/grok"'; then
		echo "grok: $(as_user bash -lc 'command -v grok || echo "$HOME/.grok/bin/grok"')"
	else
		echo "grok: not installed"
	fi
	if [[ -z "${WAYLAND_DISPLAY:-}" && ! -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wayland-0" && ! -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wayland-1" ]]; then
		echo "swaygentic: this session is not Wayland. log into a Wayland desktop before launch()." >&2
	fi
	echo "PATH: added $(home_ref "$WRAPPER_BIN") to ~/.bashrc ~/.zshrc ~/.config/fish/config.fish (new shells)"
}

mcp_reg() {
grok mcp add swaygentic -- ~/Swaygentic/bin/swaygentic-mcp
}

mkdir -p $ROOT/screenshots $ROOT/downloads

main() {
	setup_user
	check_host
	install_packages
	install_brave
	install_grok
	chmod_scripts
	add_wrapper_path
	build_seccomp
	probe_bwrap
	verify
	mcp_reg
	echo "swaygentic: install finished. open a new shell, then run: swaygentic"
}

main "$@"
