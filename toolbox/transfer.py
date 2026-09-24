import urllib.request
from pathlib import Path

from toolbox import cdp
from toolbox.paths import DOWNLOADS


def download(url, dest=""):
	DOWNLOADS.mkdir(parents=True, exist_ok=True)
	if dest:
		out = Path(dest).expanduser()
		if not out.is_absolute():
			out = DOWNLOADS / out
	else:
		name = url.rstrip("/").split("/")[-1] or "download"
		out = DOWNLOADS / name
	out.parent.mkdir(parents=True, exist_ok=True)
	urllib.request.urlretrieve(url, out)
	return str(out)


def upload(path, query="", tab=""):
	src = Path(path).expanduser().resolve()
	if not src.is_file():
		raise FileNotFoundError(str(src))
	return cdp.set_file_input(str(src), query=query, tab=tab)
