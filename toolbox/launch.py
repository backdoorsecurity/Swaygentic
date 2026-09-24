import os
import subprocess
import time
from pathlib import Path

from toolbox import cdp, config


def _comm(pid):
	try:
		return Path("/proc/%d/comm" % pid).read_text().strip()
	except OSError:
		return ""


def _pid_running(pid):
	try:
		os.kill(pid, 0)
	except OSError:
		return False
	return True


def _cmdline(pid):
	try:
		return open("/proc/%d/cmdline" % pid, "rb").read().replace(b"\x00", b" ").decode(errors="replace")
	except OSError:
		return ""


def _default_profile():
	name = Path(config.browser).name.lower()
	if "origin" in name:
		if "nightly" in name:
			return Path.home() / ".config" / "BraveSoftware" / "Brave-Origin-Nightly"
		if "beta" in name:
			return Path.home() / ".config" / "BraveSoftware" / "Brave-Origin-Beta"
		return Path.home() / ".config" / "BraveSoftware" / "Brave-Origin"
	if "nightly" in name:
		return Path.home() / ".config" / "BraveSoftware" / "Brave-Browser-Nightly"
	if "beta" in name:
		return Path.home() / ".config" / "BraveSoftware" / "Brave-Browser-Beta"
	if "brave" in name:
		return Path.home() / ".config" / "BraveSoftware" / "Brave-Browser"
	return Path.home() / ".config" / "chromium"


def _browser_comms():
	name = Path(config.browser).name.lower()
	if "brave" in name or "origin" in name:
		return ("brave", "brave-origin", "brave-origin-ni", "brave-browser", "brave-browser-n")
	return ("chrome", "chromium", "chromium-browser")


def _user_data_dir(cmd):
	key = "--user-data-dir="
	for part in cmd.split():
		if part.startswith(key):
			return part[len(key):].rstrip("/")
	return ""


def _is_ours(pid):
	if _comm(pid) not in _browser_comms():
		return False
	cmd = _cmdline(pid)
	name = Path(config.browser).name.lower()
	if "origin" in name:
		return "brave-origin" in cmd or "Brave-Origin" in cmd
	if "nightly" in name:
		return "brave-nightly" in cmd or "Brave-Browser-Nightly" in cmd
	udd = _user_data_dir(cmd)
	default = str(_default_profile())
	if udd and udd != default:
		return False
	if "brave" in name and ("brave-nightly" in cmd or "brave-origin" in cmd):
		return False
	return True


def _browser_pids():
	pids = []
	for name in os.listdir("/proc"):
		if not name.isdigit():
			continue
		pid = int(name)
		if _is_ours(pid):
			pids.append(pid)
	return pids


def _is_browser_main(pid):
	cmd = _cmdline(pid)
	if "--type=" in cmd or "crashpad" in cmd:
		return False
	return _comm(pid) in _browser_comms()


def _wait_gone(pids, timeout):
	deadline = time.time() + timeout
	while time.time() < deadline:
		live = [p for p in pids if _pid_running(p)]
		if not live:
			return True
		time.sleep(0.1)
	return not any(_pid_running(p) for p in pids)


def _kill(pids, sig):
	for pid in pids:
		try:
			os.kill(pid, sig)
		except OSError:
			pass


def _clear_singleton():
	profile = _default_profile()
	for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
		path = profile / name
		try:
			path.unlink()
		except OSError:
			pass


def _stop_browser():
	pids = _browser_pids()
	if pids:
		mains = [p for p in pids if _is_browser_main(p)]
		_kill(mains or pids, 15)
		if not _wait_gone(pids, 8):
			left = _browser_pids()
			_kill(left, 15)
			_wait_gone(left, 2)
		left = _browser_pids()
		if left:
			_kill(left, 9)
			_wait_gone(left, 2)
	if not _browser_pids():
		_clear_singleton()


def _wait_cdp():
	n = 0
	while n < 50:
		if cdp.alive():
			return
		n += 1
		time.sleep(0.2)
	raise RuntimeError("timeout waiting for CDP")


def _spawn(cmd, env):
	return subprocess.Popen(
		cmd,
		env=env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		start_new_session=True,
	)


def _cmd(binary, url):
	cmd = [
		binary,
		*config.browser_args(),
	]
	if url:
		cmd.append(url)
	return cmd


def _wait_tab(before, url, timeout=10):
	deadline = time.time() + timeout
	dest = url or ""
	last = None
	while time.time() < deadline:
		try:
			pages = cdp.list_page_targets()
		except cdp.CDPError:
			time.sleep(0.15)
			continue
		fresh = [p for p in pages if p.get("id") not in before]
		if fresh:
			last = fresh[-1]
			if dest and dest != "about:blank":
				for page in fresh:
					if dest in (page.get("url") or ""):
						cdp.switch_tab(page["id"])
						return page["id"]
			else:
				tid = last["id"]
				cdp.switch_tab(tid)
				return tid
		time.sleep(0.15)
	if last:
		cdp.switch_tab(last["id"])
		return last["id"]
	raise RuntimeError("timeout waiting for tab")


def _wait_page(tab, url):
	dest = url or ""
	if dest and dest != "about:blank":
		try:
			cdp.wait_load(tab=tab, want_url=dest, avoid_href="about:blank")
			return
		except cdp.CDPError:
			pass
	time.sleep(config.launch_settle)


def _prepare():
	cdp.allow_downloads()
	cdp.set_window("maximized")


def _start(binary, url):
	if _browser_pids():
		_stop_browser()
	_spawn(_cmd(binary, url), os.environ.copy())
	_wait_cdp()
	_prepare()
	return _wait_tab(set(), url)


def _handoff(binary, url):
	before = {t["id"] for t in cdp.list_page_targets()}
	proc = _spawn(_cmd(binary, url), os.environ.copy())
	try:
		proc.wait(timeout=15)
	except subprocess.TimeoutExpired:
		pass
	return _wait_tab(before, url)


def launch(url):
	binary = config.browser
	was_up = cdp.alive()
	if not was_up:
		tab = _start(binary, url)
		_wait_page(tab, url)
		return cdp.screenshot(tab=tab)
	tab = _handoff(binary, url)
	_wait_page(tab, url)
	return cdp.screenshot(tab=tab)


def new_tab(url):
	binary = config.browser
	if not cdp.alive():
		tab = _start(binary, url)
	else:
		tab = _handoff(binary, url)
	_wait_page(tab, url)
	return tab


def relaunch(url):
	_stop_browser()
	return launch(url)


def stop():
	_stop_browser()
