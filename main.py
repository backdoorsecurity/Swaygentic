from toolbox import cdp
from toolbox import launch as launch_mod
from toolbox.launch import launch, relaunch, stop
from toolbox.transfer import download, upload


def screenshot(tab=""):
	return cdp.screenshot(tab=tab)


def zoom(x, y, width, height, tab=""):
	return cdp.zoom(x, y, width, height, tab=tab)


def pixel_click(x, y, tab="", clicks=1, *, screenshot=False):
	info = cdp.pixel_click(x, y, tab=tab, clicks=clicks)
	if screenshot or (info or {}).get("loaded"):
		return cdp.screenshot(tab=tab)
	return None


def wait_load(tab=""):
	cdp.wait_load(tab=tab)
	return cdp.screenshot(tab=tab)


def click(query, tab="", *, screenshot=False):
	info = cdp.click(query, tab=tab)
	if screenshot or (info or {}).get("loaded"):
		return cdp.screenshot(tab=tab)
	return None


def type_text(text, tab="", replace=False):
	cdp.type_text(text, tab=tab, replace=replace)
	return cdp.screenshot(tab=tab)


def key(combo, tab=""):
	cdp.key(combo, tab=tab)
	return cdp.screenshot(tab=tab)


def scroll(direction, tab=""):
	cdp.scroll(direction, tab=tab)
	return cdp.screenshot(tab=tab)


def blur(tab=""):
	cdp.blur(tab=tab)
	return cdp.screenshot(tab=tab)


def fill(query, value, tab=""):
	cdp.fill(query, value, tab=tab)
	return cdp.screenshot(tab=tab)


def form_fields(tab=""):
	return cdp.form_fields(tab=tab)


def snapshot(tab="", interactive=True):
	return cdp.snapshot(tab=tab, interactive=interactive)


def focused(tab=""):
	return cdp.focused(tab=tab)


def wait_for(query, tab="", timeout=None):
	return cdp.wait_for(query, tab=tab, timeout=timeout)


def scroll_into_view(query, tab=""):
	cdp.scroll_into_view(query, tab=tab)
	return cdp.screenshot(tab=tab)


def goto(url, tab=""):
	cdp.goto(url, tab=tab)
	return cdp.screenshot(tab=tab)


def new_tab(url):
	tab = launch_mod.new_tab(url)
	return cdp.screenshot(tab=tab)


def list_tabs():
	return cdp.list_tabs()


def switch_tab(tab):
	cdp.switch_tab(tab)
	return cdp.screenshot(tab=tab)


def evaluate(expression, tab=""):
	return cdp.evaluate(expression, tab=tab)


def page_text(tab=""):
	return cdp.page_text(tab=tab)


def links(tab=""):
	return cdp.links(tab=tab)
