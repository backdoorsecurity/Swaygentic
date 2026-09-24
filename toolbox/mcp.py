#!/usr/bin/env python3
import base64
import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

from toolbox.paths import SCREENSHOTS
import main as bravectl

PROTO = "2024-11-05"
_lsp = False


def _read():
	global _lsp
	raw = sys.stdin.buffer
	head = raw.readline()
	if not head:
		return None
	if head.lower().startswith(b"content-length:"):
		_lsp = True
		n = int(head.split(b":", 1)[1])
		while True:
			line = raw.readline()
			if not line or line in (b"\r\n", b"\n"):
				break
		body = raw.read(n)
		return json.loads(body.decode())
	line = head.strip()
	if not line:
		return _read()
	return json.loads(line.decode())


def _write(msg):
	data = json.dumps(msg, separators=(",", ":")).encode()
	out = sys.stdout.buffer
	if _lsp:
		out.write(("Content-Length: %d\r\n\r\n" % len(data)).encode())
		out.write(data)
	else:
		out.write(data + b"\n")
	out.flush()


def _ok(id, result):
	_write({"jsonrpc": "2.0", "id": id, "result": result})


def _err(id, code, message):
	_write({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})


def _str(name, default=None):
	return {"type": "string", "description": name if default is None else "%s (default %s)" % (name, default)}


def _bool(name, default):
	return {"type": "boolean", "description": "%s (default %s)" % (name, default)}


TAB = _str("tab id, title, or url substring", "current tab")

TOOLS = [
	{
		"name": "launch",
		"description": "Start Brave at url, or open a tab if it is already running.",
		"inputSchema": {
			"type": "object",
			"properties": {"url": _str("url")},
			"required": ["url"],
		},
	},
	{
		"name": "relaunch",
		"description": "Quit Brave and launch again at url.",
		"inputSchema": {
			"type": "object",
			"properties": {"url": _str("url")},
			"required": ["url"],
		},
	},
	{
		"name": "stop",
		"description": "Quit the browser.",
		"inputSchema": {"type": "object", "properties": {}},
	},
	{
		"name": "new_tab",
		"description": "Open a new tab at url.",
		"inputSchema": {
			"type": "object",
			"properties": {"url": _str("url")},
			"required": ["url"],
		},
	},
	{
		"name": "list_tabs",
		"description": "List page tabs as id, title, url.",
		"inputSchema": {"type": "object", "properties": {}},
	},
	{
		"name": "switch_tab",
		"description": "Activate a tab by id, title, or url substring.",
		"inputSchema": {
			"type": "object",
			"properties": {"tab": _str("tab id, title, or url substring")},
			"required": ["tab"],
		},
	},
	{
		"name": "goto",
		"description": "Navigate the current tab (or tab=) and screenshot.",
		"inputSchema": {
			"type": "object",
			"properties": {"url": _str("url"), "tab": TAB},
			"required": ["url"],
		},
	},
	{
		"name": "screenshot",
		"description": "Capture the page viewport PNG. Caption includes png/css size for pixel mapping.",
		"inputSchema": {"type": "object", "properties": {"tab": TAB}},
	},
	{
		"name": "zoom",
		"description": "Crop the current screenshot to a box in screenshot pixels and recapture that region. Later pixel_click coords are in this crop.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"x": {"type": "number", "description": "left in current screenshot pixels"},
				"y": {"type": "number", "description": "top in current screenshot pixels"},
				"width": {"type": "number", "description": "box width in screenshot pixels"},
				"height": {"type": "number", "description": "box height in screenshot pixels"},
				"tab": TAB,
			},
			"required": ["x", "y", "width", "height"],
		},
	},
	{
		"name": "pixel_click",
		"description": "Click at x,y in the current screenshot (full viewport or last zoom crop). Prefer snapshot/click(query) when a DOM node exists.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"x": {"type": "number", "description": "x in current screenshot pixels"},
				"y": {"type": "number", "description": "y in current screenshot pixels"},
				"tab": TAB,
				"clicks": {"type": "number", "description": "click count (default 1)"},
				"screenshot": _bool("force a screenshot", False),
			},
			"required": ["x", "y"],
		},
	},
	{
		"name": "wait_load",
		"description": "Wait until the page is loaded, then screenshot.",
		"inputSchema": {"type": "object", "properties": {"tab": TAB}},
	},
	{
		"name": "click",
		"description": "Click by CSS, XPath, visible text, or snapshot id (e12). Searches same-origin iframes. Screenshots only if the page navigated or screenshot=true.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"query": _str("CSS, XPath, visible text, or snapshot id e12"),
				"tab": TAB,
				"screenshot": _bool("force a screenshot", False),
			},
			"required": ["query"],
		},
	},
	{
		"name": "type_text",
		"description": "Type into the focused field. Screenshots after typing.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"text": _str("text to type"),
				"tab": TAB,
				"replace": _bool("clear the field first", False),
			},
			"required": ["text"],
		},
	},
	{
		"name": "key",
		"description": "Press a key or combo (enter, tab, escape, ctrl+a, ...). Screenshots after.",
		"inputSchema": {
			"type": "object",
			"properties": {"combo": _str("key or combo"), "tab": TAB},
			"required": ["combo"],
		},
	},
	{
		"name": "scroll",
		"description": "Scroll up or down by 7/8 of the nearest overflow pane (or the window). Screenshots after.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"direction": {"type": "string", "enum": ["up", "down"], "description": "up or down"},
				"tab": TAB,
			},
			"required": ["direction"],
		},
	},
	{
		"name": "blur",
		"description": "Unfocus the active field (including inside same-origin iframes). Screenshots after.",
		"inputSchema": {"type": "object", "properties": {"tab": TAB}},
	},
	{
		"name": "fill",
		"description": "Set an input, textarea, or select by CSS, XPath, label text, or snapshot id (e12). Searches same-origin iframes. Screenshots after.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"query": _str("CSS, XPath, field label, or snapshot id e12"),
				"value": _str("value to set (select: option text or value)"),
				"tab": TAB,
			},
			"required": ["query", "value"],
		},
	},
	{
		"name": "form_fields",
		"description": "Visible form fields (label, type, name, id, value). Includes same-origin iframes. No screenshot.",
		"inputSchema": {"type": "object", "properties": {"tab": TAB}},
	},
	{
		"name": "scroll_into_view",
		"description": "Scroll the matching element into the center of its pane. CSS, XPath, visible text, or snapshot id. Screenshots after.",
		"inputSchema": {
			"type": "object",
			"properties": {"query": _str("CSS, XPath, visible text, or snapshot id e12"), "tab": TAB},
			"required": ["query"],
		},
	},
	{
		"name": "snapshot",
		"description": "Accessibility tree of the page as text with snapshot ids ([e12] button \"Save\"). Prefer this over a screenshot to choose what to click or fill. No screenshot.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"tab": TAB,
				"interactive": _bool("only buttons, fields, tabs, dialogs, and similar", True),
			},
		},
	},
	{
		"name": "focused",
		"description": "Role, name, and value of the focused element (iframes included). No screenshot.",
		"inputSchema": {"type": "object", "properties": {"tab": TAB}},
	},
	{
		"name": "wait_for",
		"description": "Poll until a CSS, XPath, visible text, or snapshot id is present. No screenshot.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"query": _str("CSS, XPath, visible text, or snapshot id e12"),
				"tab": TAB,
				"timeout": {"type": "number", "description": "seconds to wait (default 20)"},
			},
			"required": ["query"],
		},
	},
	{
		"name": "evaluate",
		"description": "Run JavaScript in the page and return the value. No screenshot.",
		"inputSchema": {
			"type": "object",
			"properties": {"expression": _str("JavaScript"), "tab": TAB},
			"required": ["expression"],
		},
	},
	{
		"name": "page_text",
		"description": "Visible innerText of the page, including same-origin iframes. No screenshot.",
		"inputSchema": {"type": "object", "properties": {"tab": TAB}},
	},
	{
		"name": "links",
		"description": "Visible links as text and href, including same-origin iframes. No screenshot.",
		"inputSchema": {"type": "object", "properties": {"tab": TAB}},
	},
	{
		"name": "download",
		"description": "Fetch a URL into downloads/.",
		"inputSchema": {
			"type": "object",
			"properties": {"url": _str("file url"), "dest": _str("optional dest path or name", "")},
			"required": ["url"],
		},
	},
	{
		"name": "upload",
		"description": "Attach a local file to a file input.",
		"inputSchema": {
			"type": "object",
			"properties": {
				"path": _str("local file path"),
				"query": _str("file input selector", "input[type=file]"),
				"tab": TAB,
			},
			"required": ["path"],
		},
	},
]


def _png(value):
	if not isinstance(value, str) or not value.endswith(".png"):
		return None
	path = Path(value)
	if not path.is_file():
		return None
	try:
		path.resolve().relative_to(SCREENSHOTS.resolve())
	except ValueError:
		return None
	return path


def _text_payload(value):
	if value is None:
		return ""
	if isinstance(value, str):
		return value
	return json.dumps(value, default=str)


def _call(name, args):
	args = {k: v for k, v in (args or {}).items() if v is not None}
	fn = getattr(bravectl, name, None)
	if fn is None:
		raise AttributeError("no function %s" % name)
	return fn(**args)


def _location():
	try:
		info = bravectl.evaluate("({h: location.href, t: document.title})")
		if isinstance(info, dict):
			return info.get("h") or "", info.get("t") or ""
	except Exception:
		pass
	return "", ""


def _result(value):
	content = []
	shot = _png(value)
	url, title = ("", "")
	if shot is not None:
		url, title = _location()
		lines = []
		if title:
			lines.append(title)
		if url:
			lines.append(url)
		lines.append(str(shot))
		try:
			from toolbox import cdp as _cdp

			line = _cdp.view_line()
			if line:
				lines.append(line)
		except Exception:
			pass
		content.append({"type": "text", "text": "\n".join(lines)})
		content.append(
			{
				"type": "image",
				"mimeType": "image/png",
				"data": base64.b64encode(shot.read_bytes()).decode("ascii"),
			}
		)
		return {"content": content}
	text = _text_payload(value)
	content.append({"type": "text", "text": text if text else "(ok)"})
	return {"content": content}


def _init(params):
	want = (params or {}).get("protocolVersion") or PROTO
	return {
		"protocolVersion": want if want.startswith("2024") or want.startswith("2025") else PROTO,
		"capabilities": {"tools": {"listChanged": False}},
		"serverInfo": {"name": "swaygentic", "version": "0.1.0"},
	}


def _handle(msg):
	if not isinstance(msg, dict):
		return
	mid = msg.get("id")
	method = msg.get("method")
	params = msg.get("params") or {}
	if method is None:
		return
	if mid is None:
		return
	if method == "initialize":
		_ok(mid, _init(params))
		return
	if method == "ping":
		_ok(mid, {})
		return
	if method == "tools/list":
		_ok(mid, {"tools": TOOLS})
		return
	if method == "tools/call":
		name = params.get("name")
		args = params.get("arguments") or {}
		try:
			value = _call(name, args)
			_ok(mid, _result(value))
		except Exception as exc:
			sys.stderr.write("%s\n%s\n" % (exc, traceback.format_exc()))
			_ok(
				mid,
				{
					"content": [{"type": "text", "text": str(exc)}],
					"isError": True,
				},
			)
		return
	_err(mid, -32601, "method not found: %s" % method)


def main():
	while True:
		try:
			msg = _read()
		except Exception as exc:
			sys.stderr.write("bad message: %s\n" % exc)
			continue
		if msg is None:
			break
		_handle(msg)


if __name__ == "__main__":
	main()
