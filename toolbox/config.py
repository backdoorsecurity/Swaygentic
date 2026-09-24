from toolbox.paths import DOWNLOADS, PROJECT


def _load():
	ns = {}
	exec((PROJECT / "variables").read_text(), ns)
	return ns


_NS = _load()


def _get(name, default=None):
	if name in _NS:
		return _NS[name]
	return default


browser = _get("browser", "brave-origin-nightly")
url = _get("url", "about:blank")
browser_flags = list(_get("browser_flags") or [])
cdp_addr = _get("cdp_addr", "127.0.0.1")
cdp_port = int(_get("cdp_port", 9222))
launch_settle = float(_get("launch_settle", 1.0))
settle = float(_get("settle", 0.3))
scroll_keep = int(_get("scroll_keep", 8))
scroll_settle = float(_get("scroll_settle", 0.45))
click_settle = float(_get("click_settle", 0.45))
nav_probe = float(_get("nav_probe", 0.45))
net_idle = float(_get("net_idle", 0.5))
load_timeout = float(_get("load_timeout", 20))


def browser_args():
	DOWNLOADS.mkdir(parents=True, exist_ok=True)
	flags = [str(f) for f in browser_flags]
	flags.append(f"--remote-debugging-address={cdp_addr}")
	flags.append(f"--remote-debugging-port={cdp_port}")
	flags.append("--remote-allow-origins=*")
	flags.append("--download-default-directory=%s" % DOWNLOADS)
	return flags
