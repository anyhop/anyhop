#!/bin/sh
# One-command, user-level installer for anyhop on macOS and systemd Linux.
set -eu

ANYHOP_VERSION="0.1.18"
UV_VERSION="0.11.29"
UV_INSTALLER_SHA256="504a79fd2ed0dcd47e7f04f0792cfd0871f62e24a7fe40fa8ae0f563a369f2bd"
UV_INSTALLER_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-installer.sh"

mode=install
linger=false
case "${1:-}" in
"") ;;
--linger) linger=true ;;
--uninstall) mode=uninstall ;;
-h | --help)
	echo "usage: install.sh [--linger | --uninstall]"
	echo "  --linger  Linux only: keep the daemon running after logout"
	echo "  --uninstall  remove the service, uv-owned anyhop tool, and all anyhop state"
	exit 0
	;;
*)
	echo "anyhop installer: unknown option: $1" >&2
	exit 2
	;;
esac
if [ "$#" -gt 1 ]; then
	echo "anyhop installer: expected at most one option" >&2
	exit 2
fi

say() { printf '%s\n' "anyhop installer: $*"; }
die() {
	printf '%s\n' "anyhop installer: $*" >&2
	exit 1
}
have() { command -v "$1" >/dev/null 2>&1; }
absolute_command() {
	command_path=$(command -v "$1") || return 1
	absolute_file "$command_path"
}
absolute_file() {
	command_path=$1
	command_dir=$(dirname "$command_path")
	command_base=$(basename "$command_path")
	(CDPATH='' cd -P "$command_dir" 2>/dev/null && printf '%s/%s\n' "$(pwd)" "$command_base")
}
root_path() { printf '%s%s\n' "${_ANYHOP_INSTALL_TEST_ROOT:-}" "$1"; }

valid_path_text() {
	case "$1" in
	"" | *"
"* | *""*) return 1 ;;
	/*) return 0 ;;
	*) return 1 ;;
	esac
}

canonical_dir() { (CDPATH='' cd -P "$1" 2>/dev/null && pwd); }

uv_runnable() {
	[ -x "$1" ] || return 1
	"$1" --version >/dev/null 2>&1 || return 1
}

uv_compatible() {
	uv_runnable "$1" || return 1
	uv_install_help=$("$1" tool install --help 2>/dev/null) || return 1
	for uv_flag in --no-config --force --no-sources --default-index; do
		printf '%s\n' "$uv_install_help" | grep -q -- "$uv_flag" || return 1
	done
	"$1" tool update-shell --help >/dev/null 2>&1 || return 1
}

uv_tool_list() {
	UV_TOOL_DIR=$uv_tools_dir UV_TOOL_BIN_DIR=$uv_bin_dir "$uv" tool list 2>/dev/null
}

receipt_value() {
	receipt_key=$1
	[ "$(grep -c "^${receipt_key}=" "$receipt" || true)" = 1 ] ||
		die "refusing malformed bootstrap receipt; remove anyhop manually: $receipt"
	sed -n "s/^${receipt_key}=//p" "$receipt"
}

validate_state_path() {
	valid_path_text "$state_dir" || die "refusing invalid recorded state path; remove anyhop state manually."
	case "$state_dir" in
	/ | // | /tmp | /private | /private/tmp | /var | /home | /Users | "$home_dir") die "refusing unsafe recorded state path '$state_dir'; remove anyhop state manually." ;;
	/*/*) ;;
	*) die "refusing shallow recorded state path '$state_dir'; remove anyhop state manually." ;;
	esac
	case "$receipt/" in
	"$state_dir"/*) die "refusing state path '$state_dir' because it contains the bootstrap receipt." ;;
	esac
	if [ -e "$state_dir" ] || [ -L "$state_dir" ]; then
		if ! { [ -d "$state_dir" ] && [ ! -L "$state_dir" ]; }; then
			die "refusing replaced recorded state path '$state_dir'."
		fi
		canonical_state=$(canonical_dir "$state_dir") || die "cannot validate recorded state path '$state_dir'."
		[ "$canonical_state" = "$state_dir" ] || die "refusing non-canonical recorded state path '$state_dir'."
		state_marker=$state_dir/.anyhop-bootstrap-receipt
		if [ -e "$state_marker" ] || [ -L "$state_marker" ]; then
			if ! { [ -f "$state_marker" ] && [ ! -L "$state_marker" ]; }; then
				die "refusing replaced state ownership marker: $state_marker"
			fi
			if ! { [ "$(wc -l <"$state_marker" | tr -d ' ')" = 3 ] &&
				grep -Fqx 'receipt_version=1' "$state_marker" &&
				grep -Fqx "receipt_path=$receipt" "$state_marker" &&
				grep -Fqx "state_dir=$state_dir" "$state_marker"; }; then
				die "refusing state path with an invalid bootstrap marker: $state_dir"
			fi
		elif [ "$uninstall_phase" != tool_removing ]; then
			die "refusing to purge unmarked state path '$state_dir'."
		fi
	fi
}

load_receipt() {
	if ! { [ -f "$receipt" ] && [ ! -L "$receipt" ] && [ -r "$receipt" ]; }; then
		die "refusing unreadable or linked bootstrap receipt: $receipt"
	fi
	[ "$(wc -l <"$receipt" | tr -d ' ')" = 6 ] || die "refusing malformed bootstrap receipt: $receipt"
	if grep -Ev '^(receipt_version|state_dir|uv_path|uv_tools_dir|uv_bin_dir|linger_changed)=' "$receipt" >/dev/null; then
		die "refusing malformed bootstrap receipt: $receipt"
	fi
	[ "$(receipt_value receipt_version)" = 1 ] || die "unsupported bootstrap receipt version: $receipt"
	state_dir=$(receipt_value state_dir)
	uv=$(receipt_value uv_path)
	uv_tools_dir=$(receipt_value uv_tools_dir)
	uv_bin_dir=$(receipt_value uv_bin_dir)
	linger_changed=$(receipt_value linger_changed)
	if ! valid_path_text "$uv" || ! valid_path_text "$uv_tools_dir" || ! valid_path_text "$uv_bin_dir"; then
		die "refusing malformed uv paths in bootstrap receipt: $receipt"
	fi
	case "$linger_changed" in 0 | 1) ;; *) die "refusing malformed linger ownership in bootstrap receipt: $receipt" ;; esac
}

phase_value() {
	phase_key=$1
	[ "$(grep -c "^${phase_key}=" "$phase_file" || true)" = 1 ] ||
		die "refusing malformed uninstall phase; remove anyhop manually: $phase_file"
	sed -n "s/^${phase_key}=//p" "$phase_file"
}

read_uninstall_phase() {
	if ! { [ -f "$phase_file" ] && [ ! -L "$phase_file" ] && [ -r "$phase_file" ]; }; then
		die "refusing unreadable or linked uninstall phase: $phase_file"
	fi
	[ "$(wc -l <"$phase_file" | tr -d ' ')" = 4 ] || die "refusing malformed uninstall phase: $phase_file"
	if grep -Ev '^(phase_version|receipt_path|state_dir|phase)=' "$phase_file" >/dev/null; then
		die "refusing malformed uninstall phase: $phase_file"
	fi
	[ "$(phase_value phase_version)" = 1 ] || die "unsupported uninstall phase version: $phase_file"
	phase_receipt=$(phase_value receipt_path)
	phase_state=$(phase_value state_dir)
	phase_name=$(phase_value phase)
	[ "$phase_receipt" = "$receipt" ] || die "uninstall phase does not belong to this bootstrap receipt: $phase_file"
	valid_path_text "$phase_state" || die "refusing invalid state path in uninstall phase: $phase_file"
	[ "$phase_name" = tool_removing ] || die "refusing unknown uninstall phase in $phase_file"
}

load_uninstall_phase() {
	uninstall_phase=installed
	if [ -e "$phase_file" ] || [ -L "$phase_file" ]; then
		read_uninstall_phase
		[ "$phase_state" = "$state_dir" ] || die "uninstall phase state does not match the bootstrap receipt."
		uninstall_phase=tool_removing
	fi
}

write_uninstall_phase() {
	phase_tmp=$( (umask 077 && mktemp "$receipt_dir/uninstall-phase.XXXXXX") ) || die "cannot stage the uninstall phase."
	if ! printf 'phase_version=1\nreceipt_path=%s\nstate_dir=%s\nphase=tool_removing\n' \
		"$receipt" "$state_dir" >"$phase_tmp"; then
		rm -f -- "$phase_tmp"
		phase_tmp=""
		die "cannot write the uninstall phase."
	fi
	if ! mv "$phase_tmp" "$phase_file"; then
		rm -f -- "$phase_tmp"
		phase_tmp=""
		die "cannot install the uninstall phase."
	fi
	phase_tmp=""
	uninstall_phase=tool_removing
}

write_receipt() {
	write_linger=$1
	state_marker=$state_dir/.anyhop-bootstrap-receipt
	marker_tmp=$(mktemp "$state_dir/.anyhop-bootstrap-receipt.XXXXXX") || die "cannot stage the state ownership marker."
	receipt_tmp=$(mktemp "$receipt_dir/bootstrap-receipt.XXXXXX") || die "cannot stage the bootstrap receipt."
	printf 'receipt_version=1\nreceipt_path=%s\nstate_dir=%s\n' "$receipt" "$state_dir" >"$marker_tmp"
	printf 'receipt_version=1\nstate_dir=%s\nuv_path=%s\nuv_tools_dir=%s\nuv_bin_dir=%s\nlinger_changed=%s\n' \
		"$state_dir" "$uv" "$uv_tools_dir" "$uv_bin_dir" "$write_linger" >"$receipt_tmp"
	mv "$marker_tmp" "$state_marker" || die "cannot record state ownership."
	mv "$receipt_tmp" "$receipt" || die "cannot install bootstrap receipt."
}

probe_writable_dir() {
	probe_file=$(mktemp "$1/.anyhop-write-test.XXXXXX") || die "$2 is not writable: $1"
	rm -f "$probe_file" || die "could not clean the writability probe in $1"
}

select_compatible_uv() {
	uv=""
	if have uv; then
		candidate=$(absolute_command uv)
		if uv_compatible "$candidate"; then uv=$candidate; fi
	fi
	if [ -z "$uv" ]; then
		for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
			if [ -x "$candidate" ]; then
				candidate=$(absolute_file "$candidate")
				if uv_compatible "$candidate"; then
					uv=$candidate
					break
				fi
			fi
		done
	fi
}

escape_double_quoted_path() {
	# These sed expressions escape literal shell metacharacters; expansion here
	# would corrupt the profile line we are constructing.
	# shellcheck disable=SC2016
	printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/`/\\`/g; s/\$/\\$/g'
}

profile_path_fallback() {
	profile=$HOME/.profile
	escaped_bin=$(escape_double_quoted_path "$uv_bin_dir")
	profile_line="export PATH=\"$escaped_bin:\$PATH\" # anyhop bootstrap"
	if [ -f "$profile" ] && grep -Fqx "$profile_line" "$profile"; then return; fi
	printf '\n%s\n' "$profile_line" >>"$profile" || die "could not update $profile; anyhop remains reversible with --uninstall."
}

ensure_future_path() {
	case ":$PATH:" in *":$uv_bin_dir:"*) return ;; esac
	escaped_bin=$(escape_double_quoted_path "$uv_bin_dir")
	shell_value=${SHELL:-sh}
	shell_name=${shell_value##*/}
	case "$shell_name" in
	bash | zsh)
		if ! UV_TOOL_DIR=$uv_tools_dir UV_TOOL_BIN_DIR=$uv_bin_dir "$uv" tool update-shell >/dev/null 2>&1; then
			profile_path_fallback
		fi
		;;
	*) profile_path_fallback ;;
	esac
	path_guidance="export PATH=\"$escaped_bin:\$PATH\""
	say "PATH was updated for future shells; restart your shell or run now: $path_guidance"
}

purge_state_and_receipt() {
	if [ -e "$state_dir" ] || [ -L "$state_dir" ]; then
		rm -rf -- "$state_dir" || die "could not finish removing anyhop state at $state_dir; rerun this uninstaller."
		say "removed anyhop state at $state_dir"
	fi
	rm -f -- "$receipt" || die "anyhop state was removed, but the bootstrap receipt could not be removed: $receipt"
	rm -f -- "$phase_file" || die "anyhop state and receipt were removed, but the uninstall phase could not be removed: $phase_file"
	rmdir "$receipt_dir" 2>/dev/null || true
}

finish_tool_removal() {
	uv_runnable "$uv" || die "recorded uv is missing or unusable at $uv; repair it before resuming uninstall."
	uv_list=$(uv_tool_list) || die "recorded uv could not inspect its tool directory; no cleanup changes were made."
	existing_anyhop=""
	if have anyhop; then existing_anyhop=$(absolute_command anyhop); fi
	anyhop=$uv_bin_dir/anyhop
	if printf '%s\n' "$uv_list" | grep -Eq '^anyhop[[:space:]]'; then
		if [ -n "$existing_anyhop" ] && [ "$existing_anyhop" != "$anyhop" ]; then
			die "PATH resolves anyhop to $existing_anyhop, not the recorded uv bootstrap at $anyhop; remove the conflict before resuming uninstall."
		fi
		say "removing uv-owned anyhop tool"
		UV_TOOL_DIR=$uv_tools_dir UV_TOOL_BIN_DIR=$uv_bin_dir "$uv" tool uninstall anyhop ||
			die "uv tool removal failed; anyhop state and the resumable receipt were left intact. Rerun this uninstaller."
		uv_list=$(uv_tool_list) || die "uv reported tool removal, but ownership could not be verified; anyhop state and the resumable receipt were retained."
		if printf '%s\n' "$uv_list" | grep -Eq '^anyhop[[:space:]]'; then
			die "uv reported tool removal, but still lists anyhop; anyhop state and the resumable receipt were retained."
		fi
	else
		if [ -e "$anyhop" ] || [ -L "$anyhop" ]; then
			die "uv no longer owns anyhop, but an unowned shim remains at $anyhop; remove its owner before resuming bootstrap cleanup."
		fi
		if [ -n "$existing_anyhop" ]; then
			die "uv no longer owns anyhop, but anyhop now resolves to $existing_anyhop; remove the foreign installation before resuming bootstrap cleanup."
		fi
		say "uv-owned anyhop tool was already removed; resuming bootstrap cleanup"
	fi
	if [ -e "$anyhop" ] || [ -L "$anyhop" ]; then
		die "the recorded anyhop shim remains after uv tool removal at $anyhop; anyhop state and the resumable receipt were retained."
	fi
	purge_state_and_receipt
	say "anyhop was fully removed; uv was retained because it may be used independently"
}

finish_orphaned_phase() {
	read_uninstall_phase
	if [ -e "$phase_state" ] || [ -L "$phase_state" ]; then
		die "the bootstrap receipt is missing while recorded state still exists at $phase_state; refusing automatic cleanup."
	fi
	rm -f -- "$phase_file" || die "could not finish removing the interrupted uninstall phase: $phase_file"
	rmdir "$receipt_dir" 2>/dev/null || true
	say "finished cleanup from an interrupted bootstrap uninstall"
}

uninstall_from_receipt() {
	if [ ! -e "$receipt" ] && [ ! -L "$receipt" ]; then
		if [ -e "$phase_file" ] || [ -L "$phase_file" ]; then finish_orphaned_phase; fi
		rmdir "$receipt_dir" 2>/dev/null || true
		if have anyhop; then
			existing_anyhop=$(absolute_command anyhop)
			die "anyhop at $existing_anyhop is not owned by this uv bootstrap (no receipt); uninstall it with its package manager."
		fi
		say "no uv bootstrap receipt was found; nothing to remove"
		return
	fi
	load_receipt
	load_uninstall_phase
	validate_state_path
	if [ "$uninstall_phase" = tool_removing ]; then
		say "resuming an interrupted bootstrap uninstall"
		finish_tool_removal
		return
	fi
	uv_runnable "$uv" || die "recorded uv is missing or unusable at $uv; repair it before uninstalling."
	uv_list=$(uv_tool_list) || die "recorded uv could not inspect its tool directory; no uninstall changes were made."
	printf '%s\n' "$uv_list" | grep -Eq '^anyhop[[:space:]]' || die "recorded uv no longer owns anyhop; remove the stale service/state manually."
	existing_anyhop=""
	if have anyhop; then existing_anyhop=$(absolute_command anyhop); fi
	anyhop=$uv_bin_dir/anyhop
	if [ -n "$existing_anyhop" ] && [ "$existing_anyhop" != "$anyhop" ]; then
		die "PATH resolves anyhop to $existing_anyhop, not the recorded uv bootstrap at $anyhop; remove the conflict before uninstalling."
	fi
	[ -x "$anyhop" ] || die "uv owns anyhop, but its anyhop shim is missing at $anyhop; repair the uv tool before uninstalling."
	if [ "$(uname -s)" = Darwin ]; then
		helper_status=$(ANYHOP_HOME=$state_dir "$anyhop" helper status --json 2>/dev/null) || die "could not verify the privileged helper; no uninstall changes were made."
		if printf '%s\n' "$helper_status" | grep -Eq '"installed"[[:space:]]*:[[:space:]]*true'; then
			die "the macOS privileged helper is installed; run 'sudo $anyhop helper uninstall', then rerun this uninstaller."
		fi
		printf '%s\n' "$helper_status" | grep -Eq '"installed"[[:space:]]*:[[:space:]]*false' ||
			die "could not determine whether the privileged helper is installed; no uninstall changes were made."
	fi
	say "stopping anyhop and managed proxy processes"
	ANYHOP_HOME=$state_dir PATH="$uv_bin_dir:$PATH" "$anyhop" stop ||
		die "anyhop could not stop its managed processes; the login service, uv tool, state, and receipt were left intact. Retry: $anyhop stop"
	say "removing anyhop login service"
	ANYHOP_HOME=$state_dir PATH="$uv_bin_dir:$PATH" "$anyhop" daemon uninstall || die "service removal failed; the uv tool and state were left intact. Retry: $anyhop daemon uninstall"
	# KeepAlive/Restart may briefly relaunch the applier between the first stop
	# and removal of its supervisor. A second idempotent stop closes that race
	# before the executable and its state are deleted.
	ANYHOP_HOME=$state_dir PATH="$uv_bin_dir:$PATH" "$anyhop" stop ||
		die "the login service was removed, but anyhop could not finish stopping managed processes. The uv tool, state, and receipt were retained. Retry: $anyhop stop"
	if [ "$linger_changed" = 1 ]; then
		have loginctl || die "service was removed, but login lingering could not be restored: loginctl is missing. The tool, state, and receipt were retained."
		loginctl disable-linger || die "service was removed, but login lingering could not be disabled. The tool, state, and receipt were retained."
		say "restored login lingering to its pre-install state"
	fi
	# Persist the monotonic phase before asking uv to mutate. If this shell exits
	# after uv removes the tool, the next --uninstall can distinguish that state
	# from a foreign/missing owner and safely finish receipt-backed cleanup.
	write_uninstall_phase
	finish_tool_removal
}

uninstall_bootstrap() {
	uninstall_from_receipt
}

[ "$(id -u)" != 0 ] || die "do not run this installer as root or with sudo; rerun it as your normal user."
[ -n "${HOME:-}" ] || die "HOME is not set; rerun from a normal login session."
valid_path_text "$HOME" || die "HOME must be an absolute path without newlines."
home_dir=$(canonical_dir "$HOME") || die "HOME is not an accessible directory."
valid_path_text "$home_dir" || die "HOME canonicalized to an unsafe path."
receipt_dir=$home_dir/.local/state/anyhop
receipt=$receipt_dir/bootstrap-receipt
phase_file=$receipt_dir/uninstall-phase
phase_tmp=""
uninstall_phase=installed

cleanup_phase_tmp() {
	if [ -n "$phase_tmp" ]; then rm -f -- "$phase_tmp"; fi
}

if [ "$mode" = uninstall ]; then
	trap cleanup_phase_tmp EXIT
	trap 'exit 130' INT
	trap 'exit 143' HUP TERM
	uninstall_bootstrap
	exit 0
fi
if { [ -e "$phase_file" ] || [ -L "$phase_file" ]; } && [ ! -e "$receipt" ] && [ ! -L "$receipt" ]; then
	die "an interrupted uninstall is awaiting cleanup; rerun this installer with --uninstall before installing."
fi

system=$(uname -s)
machine=$(uname -m)
case "$system" in
Darwin | Linux) ;;
*) die "unsupported operating system '$system' (supported: macOS and Linux)." ;;
esac
case "$machine" in
arm64 | aarch64 | x86_64) ;;
*) die "unsupported architecture '$machine' (supported: arm64/aarch64 and x86_64)." ;;
esac
if [ "$system" = Darwin ] && [ "$linger" = true ]; then
	die "--linger is Linux-only; macOS LaunchAgents run for the login session."
fi

if [ "$system" = Linux ]; then
	os_release=$(root_path /etc/os-release)
	[ -r "$os_release" ] || die "cannot read /etc/os-release; this Linux distribution is unsupported."
	distro=$(sed -n 's/^PRETTY_NAME=//p' "$os_release" | head -n 1)
	distro=$(printf '%s\n' "$distro" | sed 's/^"//;s/"$//')
	[ -n "$distro" ] || distro=$(sed -n 's/^ID=//p' "$os_release" | head -n 1)

	kernel_release=$(uname -r)
	proc_version=$(root_path /proc/version)
	case "$kernel_release" in *[Mm]icrosoft*) die "WSL is not supported by this installer; use a native Linux host." ;; esac
	if [ -r "$proc_version" ] && grep -qi microsoft "$proc_version"; then
		die "WSL is not supported by this installer; use a native Linux host."
	fi
	if [ -e "$(root_path /.dockerenv)" ] || [ -e "$(root_path /run/.containerenv)" ]; then
		die "containers are not supported; use the anyhop Docker image and let the container runtime own lifecycle."
	fi
	proc_cgroup=$(root_path /proc/1/cgroup)
	if [ -r "$proc_cgroup" ] && grep -Eq '(docker|containerd|kubepods|podman|lxc)' "$proc_cgroup"; then
		die "containers are not supported; use the anyhop Docker image and let the container runtime own lifecycle."
	fi
	have systemctl || die "$distro is unsupported here: systemctl was not found; install manually with uv on a systemd host."
	systemctl --user show-environment >/dev/null 2>&1 ||
		die "$distro has no usable systemd --user session; log in normally and retry (a stock CI/container shell is insufficient)."
fi

# Read an existing receipt without mutating anything; its recorded uv dirs are
# part of owner detection when this shell's UV_TOOL_* variables have changed.
requested_state=${ANYHOP_HOME:-$home_dir/.anyhop}
valid_path_text "$requested_state" || die "ANYHOP_HOME must be an absolute path without newlines."
receipt_present=false
recorded_linger=0
if [ -e "$receipt" ] || [ -L "$receipt" ]; then
	load_receipt
	load_uninstall_phase
	validate_state_path
	[ "$uninstall_phase" = installed ] || die "an uninstall is in progress; rerun this installer with --uninstall before installing."
	recorded_state=$state_dir
	recorded_uv=$uv
	recorded_uv_tools_dir=$uv_tools_dir
	recorded_uv_bin_dir=$uv_bin_dir
	recorded_linger=$linger_changed
	receipt_present=true
	# A receipt owns its recorded state location across fresh login shells. Only
	# an explicit ANYHOP_HOME is a request to compare or change that location.
	if [ "${ANYHOP_HOME+x}" != x ]; then requested_state=$recorded_state; fi
fi

# Establish whether an existing anyhop belongs to uv before downloading or
# installing anything. Every other owner is a hard handoff, never overwritten.
existing_anyhop=""
if have anyhop; then existing_anyhop=$(absolute_command anyhop); fi
if have brew; then
	brew=$(absolute_command brew)
	if "$brew" list --versions anyhop >/dev/null 2>&1; then
		die "anyhop is already owned by Homebrew; use 'brew upgrade anyhop' (or 'brew uninstall anyhop') instead."
	fi
fi
if have pipx && pipx list --short 2>/dev/null | grep -Eq '^anyhop([[:space:]]|$)'; then
	die "anyhop is already owned by pipx; use 'pipx upgrade anyhop' (or uninstall it) instead."
fi
uv_owns_anyhop=false
if [ "$receipt_present" = true ]; then
	uv=$recorded_uv
	uv_tools_dir=$recorded_uv_tools_dir
	uv_bin_dir=$recorded_uv_bin_dir
	uv_compatible "$uv" || die "the recorded uv is missing or incompatible at $uv."
	if uv_tool_list | grep -Eq '^anyhop[[:space:]]'; then uv_owns_anyhop=true; fi
else
	select_compatible_uv
fi
if [ -n "$uv" ] && [ "$receipt_present" != true ]; then
	uv_bin_dir=$("$uv" tool dir --bin)
	uv_tools_dir=$("$uv" tool dir)
	if UV_TOOL_DIR=$uv_tools_dir UV_TOOL_BIN_DIR=$uv_bin_dir "$uv" tool list 2>/dev/null | grep -Eq '^anyhop[[:space:]]'; then
		uv_owns_anyhop=true
	fi
fi

if [ -n "$existing_anyhop" ] && [ "$uv_owns_anyhop" != true ]; then
	case "$existing_anyhop" in
	*/Cellar/anyhop/* | */homebrew/*/anyhop/* | */linuxbrew/*/anyhop/*)
		die "anyhop is already owned by Homebrew at $existing_anyhop; use 'brew upgrade anyhop' (or 'brew uninstall anyhop') instead."
		;;
	esac
	case "$existing_anyhop" in
	*/.venv/bin/anyhop | */venv/bin/anyhop)
		die "anyhop resolves to a checkout or virtual environment at $existing_anyhop; deactivate it/remove it from PATH, then retry."
		;;
	esac
	die "anyhop already exists at $existing_anyhop and is not uv-owned; uninstall it with its Python/pip owner, then retry."
fi
if [ "$uv_owns_anyhop" = true ] && [ -n "$existing_anyhop" ]; then
	case "$existing_anyhop" in
	"$uv_bin_dir"/anyhop) ;;
	*) die "uv owns an anyhop tool, but PATH resolves anyhop to $existing_anyhop; remove the conflicting executable, then retry." ;;
	esac
fi
if [ -n "$uv" ] && [ "$uv_owns_anyhop" != true ] && { [ -e "$uv_bin_dir/anyhop" ] || [ -L "$uv_bin_dir/anyhop" ]; }; then
	die "the uv tool bin already contains a foreign anyhop shim at $uv_bin_dir/anyhop; remove its owner before installing."
fi

# All read-only owner refusals passed. Reserve a fixed, writable receipt and a
# canonical state path before downloading or mutating uv/anyhop/service state.
umask 077
tmp=""
created_state_dir=false
created_receipt_dir=false
cleanup() {
	if [ -n "$tmp" ]; then rm -rf "$tmp"; fi
	if [ ! -e "$receipt" ] && [ ! -L "$receipt" ]; then
		if [ "$created_state_dir" = true ]; then rmdir "$requested_state" 2>/dev/null || true; fi
		if [ "$created_receipt_dir" = true ]; then rmdir "$receipt_dir" 2>/dev/null || true; fi
	fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
if [ "$receipt_present" = true ] && [ ! -e "$requested_state" ] && [ "$requested_state" != "$recorded_state" ]; then
	die "this bootstrap already records state at $recorded_state; uninstall it before changing ANYHOP_HOME."
fi
[ -d "$requested_state" ] || created_state_dir=true
mkdir -p "$requested_state" || die "cannot create anyhop state directory: $requested_state"
state_dir=$(canonical_dir "$requested_state") || die "cannot canonicalize anyhop state directory: $requested_state"
case "$state_dir" in
/ | // | /tmp | /private | /private/tmp | /var | /home | /Users | "$home_dir") die "refusing unsafe ANYHOP_HOME '$state_dir'." ;;
/*/*) ;;
*) die "refusing shallow ANYHOP_HOME '$state_dir'; use a dedicated state directory." ;;
esac
case "$receipt/" in
"$state_dir"/*) die "refusing ANYHOP_HOME '$state_dir' because it contains the bootstrap receipt." ;;
esac
if [ "$receipt_present" = true ]; then
	[ "$state_dir" = "$recorded_state" ] || die "this bootstrap already records state at $recorded_state; uninstall it before changing ANYHOP_HOME."
else
	state_marker=$state_dir/.anyhop-bootstrap-receipt
	if [ -e "$state_marker" ] || [ -L "$state_marker" ]; then
		die "state contains a bootstrap marker without its receipt; repair or remove it manually: $state_marker"
	fi
fi
[ -d "$receipt_dir" ] || created_receipt_dir=true
mkdir -p "$receipt_dir" || die "cannot create bootstrap receipt directory: $receipt_dir"
probe_writable_dir "$receipt_dir" "bootstrap receipt directory"
probe_writable_dir "$state_dir" "anyhop state directory"

if [ -z "$uv" ]; then
	have curl || die "curl is required to download the pinned uv installer."
	tmp=$(mktemp -d "${TMPDIR:-/tmp}/anyhop-install.XXXXXX") || die "could not create a temporary directory."
	installer="$tmp/uv-installer.sh"
	say "downloading uv $UV_VERSION installer"
	curl --fail --location --silent --show-error \
		"$UV_INSTALLER_URL" --output "$installer" || die "could not download $UV_INSTALLER_URL"
	if have sha256sum; then
		actual_sha=$(sha256sum "$installer" | awk '{print $1}')
	elif have shasum; then
		actual_sha=$(shasum -a 256 "$installer" | awk '{print $1}')
	else
		die "no SHA-256 tool found (need sha256sum or shasum); uv was not installed."
	fi
	[ "$actual_sha" = "$UV_INSTALLER_SHA256" ] ||
		die "uv installer SHA-256 mismatch (expected $UV_INSTALLER_SHA256, got $actual_sha); nothing was executed."
	sh -n "$installer" || die "the verified uv installer failed shell syntax inspection; nothing was executed."
	say "verified uv installer SHA-256; installing uv in your user account"
	env -u UV_DOWNLOAD_URL -u INSTALLER_DOWNLOAD_URL \
		-u UV_INSTALLER_GHE_BASE_URL -u UV_INSTALLER_GITHUB_BASE_URL \
		-u UV_INSTALL_DIR -u CARGO_DIST_FORCE_INSTALL_DIR \
		-u UV_UNMANAGED_INSTALL -u XDG_BIN_HOME -u XDG_DATA_HOME \
		UV_NO_MODIFY_PATH=1 sh "$installer" || die "uv installation failed; anyhop was not installed."
	for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
		if [ -x "$candidate" ]; then
			uv=$(absolute_file "$candidate")
			break
		fi
	done
	[ -n "$uv" ] || die "uv installed but its absolute shim was not found in ~/.local/bin or ~/.cargo/bin."
	installed_uv_version=$("$uv" --version | awk '{print $2}')
	[ "$installed_uv_version" = "$UV_VERSION" ] ||
		die "expected uv $UV_VERSION after install, found $installed_uv_version at $uv."
else
	say "reusing compatible uv at $uv"
fi

if [ "$receipt_present" != true ]; then
	uv_bin_dir=$("$uv" tool dir --bin)
	uv_tools_dir=$("$uv" tool dir)
fi
anyhop="$uv_bin_dir/anyhop"
current_version=""
if [ -x "$anyhop" ]; then current_version=$("$anyhop" version 2>/dev/null || true); fi
if [ "$current_version" = "$ANYHOP_VERSION" ]; then
	say "anyhop $ANYHOP_VERSION is already installed by uv; leaving the tool unchanged"
else
	say "installing anyhop $ANYHOP_VERSION from PyPI with uv"
	env -u UV_INDEX -u UV_EXTRA_INDEX_URL -u UV_INDEX_URL -u UV_DEFAULT_INDEX \
		-u UV_INDEX_STRATEGY -u UV_FIND_LINKS -u UV_NO_INDEX -u UV_OFFLINE \
		-u UV_CONFIG_FILE -u UV_INSECURE_HOST -u UV_TORCH_BACKEND \
		-u UV_OVERRIDE -u UV_CONSTRAINT -u UV_BUILD_CONSTRAINT -u UV_EXCLUDE \
		-u UV_EXCLUDE_NEWER -u UV_EXCLUDE_NEWER_PACKAGE -u UV_PRERELEASE \
		-u UV_RESOLUTION -u UV_FORK_STRATEGY -u UV_NO_BINARY \
		-u UV_NO_BINARY_PACKAGE -u UV_NO_BUILD -u UV_NO_BUILD_PACKAGE \
		-u UV_NO_BUILD_ISOLATION \
		"UV_TOOL_DIR=$uv_tools_dir" "UV_TOOL_BIN_DIR=$uv_bin_dir" \
		"$uv" tool install --no-config --force --no-sources \
		--default-index https://pypi.org/simple "anyhop==$ANYHOP_VERSION" ||
		die "package installation failed; no service was registered. Retry with: $uv tool install --no-config --force --no-sources --default-index https://pypi.org/simple anyhop==$ANYHOP_VERSION"
fi
[ -x "$anyhop" ] || die "uv completed but the anyhop shim was not found at $anyhop."
[ "$("$anyhop" version)" = "$ANYHOP_VERSION" ] || die "installed anyhop did not report version $ANYHOP_VERSION."

# Persist physical dirs so a later shell can reverse custom uv locations even
# when its UV_TOOL_* environment differs.
uv_bin_dir=$(canonical_dir "$uv_bin_dir") || die "cannot canonicalize uv tool bin: $uv_bin_dir"
uv_tools_dir=$(canonical_dir "$uv_tools_dir") || die "cannot canonicalize uv tools dir: $uv_tools_dir"
if ! valid_path_text "$uv" || ! valid_path_text "$uv_bin_dir" || ! valid_path_text "$uv_tools_dir"; then
	die "uv executable and tool directories must be absolute paths without newlines."
fi
anyhop=$uv_bin_dir/anyhop

# From this point onward every failure is reversible through --uninstall,
# including rejection of an unexpectedly desktop-enabled package artifact.
linger_changed=$recorded_linger
linger_preexisting=true
write_receipt "$recorded_linger"

# Verify the installed artifact, not merely the requested extras: no launcher
# and neither future desktop module may cross this headless channel boundary.
[ ! -e "$uv_bin_dir/anyhop-tray" ] || die "headless verification failed: $uv_bin_dir/anyhop-tray exists."
tool_python="$uv_tools_dir/anyhop/bin/python"
[ -x "$tool_python" ] || die "cannot inspect the installed anyhop environment at $uv_tools_dir/anyhop."
"$tool_python" -c 'import importlib.metadata as m; files={str(p) for p in (m.files("anyhop") or ())}; raise SystemExit(1 if {"anyhop/tray.py", "anyhop/companion.py"} & files else 0)' ||
	die "headless verification failed: a tray/companion module is installed. Run: $uv tool uninstall anyhop"

if [ "$linger" = true ]; then
	have loginctl || die "--linger requires loginctl; anyhop is installed and reversible with --uninstall."
	linger_value=$(loginctl show-user "$(id -u)" -p Linger --value 2>/dev/null || true)
	case "$linger_value" in
	yes | true) ;;
	no | false)
		linger_preexisting=false
		linger_changed=1
		;;
	*) die "could not determine the current login lingering state; no service change was made." ;;
	esac
	write_receipt "$linger_changed"
fi
ensure_future_path

say "registering anyhop as a user login service"
if [ "$linger" = true ]; then
	if ! ANYHOP_HOME=$state_dir PATH="$uv_bin_dir:$PATH" "$anyhop" daemon install --linger; then
		if [ "$linger_preexisting" = false ]; then
			linger_after=$(loginctl show-user "$(id -u)" -p Linger --value 2>/dev/null || true)
			case "$linger_after" in
			yes | true) write_receipt 1 ;;
			no | false) write_receipt "$recorded_linger" ;;
			*) write_receipt 1 ;;
			esac
		fi
		die "anyhop is installed, but service registration failed. Retry it or use this installer with --uninstall."
	fi
else
	ANYHOP_HOME=$state_dir PATH="$uv_bin_dir:$PATH" "$anyhop" daemon install ||
		die "anyhop is installed, but service registration failed. Retry it or use this installer with --uninstall (manual fallback: $uv tool uninstall anyhop)."
fi

status=$(ANYHOP_HOME=$state_dir "$anyhop" daemon status --json) || die "service status verification failed; retry: $anyhop daemon status"
printf '%s\n' "$status" | grep -Eq '"installed"[[:space:]]*:[[:space:]]*true' || die "the anyhop login service is not installed."
printf '%s\n' "$status" | grep -Eq '"active"[[:space:]]*:[[:space:]]*true' || die "the anyhop login service is not active."

ready=false
attempt=0
while [ "$attempt" -lt 60 ]; do
	if ANYHOP_HOME=$state_dir "$anyhop" health >/dev/null 2>&1; then
		ready=true
		break
	fi
	attempt=$((attempt + 1))
	sleep 1
done
[ "$ready" = true ] || die "the service was registered but did not become healthy; inspect: $anyhop logs && $anyhop daemon status"
say "installed anyhop $ANYHOP_VERSION and verified its healthy login service"
say "open the Web UI with: $anyhop ui"
say "uninstall everything later with: curl -LsSf https://github.com/anyhop/anyhop/releases/latest/download/install.sh | sh -s -- --uninstall"
