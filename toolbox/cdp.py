import base64
import hashlib
import json
import os
import re
import select
import socket
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from toolbox import config
from toolbox.paths import DOWNLOADS, new_screenshot

HOST = config.cdp_addr
PORT = config.cdp_port
CLICK_SETTLE = config.click_settle
NAV_PROBE = config.nav_probe
NET_IDLE = config.net_idle
NET_INFLIGHT_MAX = 2
LOAD_TIMEOUT = config.load_timeout
SCROLL_KEEP = config.scroll_keep

MOD = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "super": 4, "shift": 8}
SPECIAL = {
	"return": ("Enter", "Enter", 13),
	"enter": ("Enter", "Enter", 13),
	"escape": ("Escape", "Escape", 27),
	"esc": ("Escape", "Escape", 27),
	"tab": ("Tab", "Tab", 9),
	"space": (" ", "Space", 32),
	"backspace": ("Backspace", "Backspace", 8),
	"delete": ("Delete", "Delete", 46),
	"up": ("ArrowUp", "ArrowUp", 38),
	"down": ("ArrowDown", "ArrowDown", 40),
	"left": ("ArrowLeft", "ArrowLeft", 37),
	"right": ("ArrowRight", "ArrowRight", 39),
	"home": ("Home", "Home", 36),
	"end": ("End", "End", 35),
	"pageup": ("PageUp", "PageUp", 33),
	"pagedown": ("PageDown", "PageDown", 34),
}


class CDPError(Exception):
	pass


def _http_json(path):
	url = f"http://{HOST}:{PORT}{path}"
	try:
		with urllib.request.urlopen(url, timeout=5) as resp:
			return json.loads(resp.read().decode())
	except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
		raise CDPError(str(exc)) from exc


def alive():
	try:
		_http_json("/json/version")
	except CDPError:
		return False
	return True


def list_page_targets():
	pages = [t for t in _http_json("/json/list") if t.get("type") == "page"]
	if not pages:
		raise CDPError("no page targets")
	return pages


def list_tabs():
	return [
		{"id": t["id"], "title": t.get("title", ""), "url": t.get("url", "")}
		for t in list_page_targets()
	]


def _pick(tab=""):
	pages = list_page_targets()
	if not tab:
		return pages[0]
	needle = tab.lower()
	for t in pages:
		blob = f"{t.get('id', '')} {t.get('title', '')} {t.get('url', '')}".lower()
		if needle in blob:
			return t
	raise CDPError(f"no tab matching {tab!r}")


def _ws_connect(ws_url):
	parsed = urlparse(ws_url)
	host = parsed.hostname or HOST
	port = parsed.port or PORT
	path = parsed.path or "/"
	if parsed.query:
		path = f"{path}?{parsed.query}"
	key = base64.b64encode(os.urandom(16)).decode()
	req = (
		f"GET {path} HTTP/1.1\r\n"
		f"Host: {host}:{port}\r\n"
		"Upgrade: websocket\r\n"
		"Connection: Upgrade\r\n"
		f"Sec-WebSocket-Key: {key}\r\n"
		"Sec-WebSocket-Version: 13\r\n"
		f"Origin: http://{host}:{port}\r\n"
		"\r\n"
	)
	sock = socket.create_connection((host, port), timeout=8)
	sock.sendall(req.encode())
	buf = b""
	while b"\r\n\r\n" not in buf:
		chunk = sock.recv(4096)
		if not chunk:
			sock.close()
			raise CDPError("websocket handshake closed")
		buf += chunk
	status = buf.split(b"\r\n", 1)[0]
	if b"101" not in status:
		sock.close()
		raise CDPError(f"websocket handshake failed: {status.decode(errors='replace')}")
	expect = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
	headers = buf.split(b"\r\n\r\n", 1)[0].decode(errors="replace")
	if expect not in headers:
		sock.close()
		raise CDPError("websocket accept mismatch")
	return sock


def _ws_send(sock, payload):
	data = payload.encode()
	header = bytearray([0x81])
	n = len(data)
	mask = os.urandom(4)
	if n < 126:
		header.append(0x80 | n)
	elif n < 65536:
		header.append(0x80 | 126)
		header.extend(struct.pack("!H", n))
	else:
		header.append(0x80 | 127)
		header.extend(struct.pack("!Q", n))
	header.extend(mask)
	masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
	sock.sendall(header + masked)


def _ws_recv(sock):
	def read(n):
		out = b""
		while len(out) < n:
			chunk = sock.recv(n - len(out))
			if not chunk:
				raise CDPError("websocket closed")
			out += chunk
		return out

	hdr = read(2)
	b1, b2 = hdr[0], hdr[1]
	opcode = b1 & 0x0F
	masked = b2 >> 7
	n = b2 & 0x7F
	if n == 126:
		n = struct.unpack("!H", read(2))[0]
	elif n == 127:
		n = struct.unpack("!Q", read(8))[0]
	mask = read(4) if masked else b""
	data = read(n)
	if masked:
		data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
	if opcode == 0x8:
		raise CDPError("websocket close")
	if opcode == 0x9:
		return _ws_recv(sock)
	if opcode != 0x1:
		return _ws_recv(sock)
	return json.loads(data.decode())


class Session:
	def __init__(self, tab="", ws_url=""):
		self.sock = _ws_connect(ws_url or _pick(tab)["webSocketDebuggerUrl"])
		self._n = 0
		self._req = set()

	def close(self):
		try:
			self.sock.close()
		except OSError:
			pass

	def _on_event(self, msg):
		method = msg.get("method")
		params = msg.get("params") or {}
		if method == "Network.requestWillBeSent":
			if params.get("type") in ("WebSocket", "EventSource"):
				return
			rid = params.get("requestId")
			if rid:
				self._req.add(rid)
		elif method in ("Network.loadingFinished", "Network.loadingFailed"):
			self._req.discard(params.get("requestId"))

	def pump(self, timeout=0.1):
		ready, _, _ = select.select([self.sock], [], [], timeout)
		if not ready:
			return None
		self.sock.settimeout(5)
		msg = _ws_recv(self.sock)
		if msg.get("id") is None:
			self._on_event(msg)
		return msg

	def call(self, method, params=None, timeout=15):
		self._n += 1
		msg_id = self._n
		_ws_send(self.sock, json.dumps({"id": msg_id, "method": method, "params": params or {}}))
		self.sock.settimeout(timeout)
		deadline = time.time() + timeout
		while time.time() < deadline:
			msg = _ws_recv(self.sock)
			if msg.get("id") == msg_id:
				if "error" in msg:
					err = msg["error"]
					raise CDPError(err.get("message", str(err)))
				return msg.get("result") or {}
			self._on_event(msg)
		raise CDPError(f"timeout calling {method}")

	def eval(self, expression, user_gesture=False, timeout=15):
		params = {
			"expression": expression,
			"returnByValue": True,
			"awaitPromise": True,
		}
		if user_gesture:
			params["userGesture"] = True
		result = self.call("Runtime.evaluate", params, timeout=timeout)
		inner = result.get("result") or {}
		if inner.get("subtype") == "error" or result.get("exceptionDetails"):
			raise CDPError(inner.get("description") or str(result.get("exceptionDetails")))
		return inner.get("value")

	def arm_load(self):
		self.call("Page.enable")
		try:
			self.call("Page.setLifecycleEventsEnabled", {"enabled": True})
		except CDPError:
			pass
		self.call("Network.enable")

	def location(self):
		value = self.eval("({h: location.href, s: document.readyState, t: document.title})")
		if not isinstance(value, dict):
			raise CDPError("location_info failed")
		return {
			"url": value.get("h", ""),
			"ready": value.get("s", ""),
			"title": value.get("t", ""),
		}

	def wait_load(self, want_url="", avoid_href="", timeout=LOAD_TIMEOUT):
		deadline = time.time() + timeout
		idle_since = None
		last_href = None
		info = {"url": "", "ready": "", "title": ""}
		want = want_url.split("#")[0] if want_url else ""
		while time.time() < deadline:
			self.pump(0.1)
			try:
				info = self.location()
			except CDPError:
				idle_since = None
				continue
			href, state = info["url"], info["ready"]
			if href != last_href:
				last_href = href
				idle_since = None
			ok = state == "complete"
			if want:
				ok = ok and want in href
			if avoid_href:
				ok = ok and avoid_href not in href
			if ok and len(self._req) <= NET_INFLIGHT_MAX:
				if idle_since is None:
					idle_since = time.time()
				elif time.time() - idle_since >= NET_IDLE:
					return info
			else:
				idle_since = None
		if info.get("ready") == "complete":
			return info
		raise CDPError("timeout waiting for page load")

	def wait_after(self, prev, timeout=LOAD_TIMEOUT):
		t0 = time.time()
		navigated = False
		while time.time() < t0 + NAV_PROBE:
			self.pump(0.08)
			try:
				info = self.location()
			except CDPError:
				navigated = True
				break
			if info["url"] and prev.get("url") and _doc_url(info["url"]) != _doc_url(prev.get("url")):
				navigated = True
				break
			if info["ready"] and info["ready"] != "complete" and prev.get("ready") == "complete":
				navigated = True
				break
		if not navigated:
			left = CLICK_SETTLE - (time.time() - t0)
			if left > 0:
				time.sleep(left)
			return False
		self.wait_load(timeout=timeout)
		return True


_VIEWS = {}


def _png_size(path):
	with open(path, "rb") as f:
		if f.read(8) != b"\x89PNG\r\n\x1a\n":
			raise CDPError("not a png")
		f.read(4)
		if f.read(4) != b"IHDR":
			raise CDPError("bad png")
		w, h = struct.unpack(">II", f.read(8))
	return int(w), int(h)


def _write_png(result):
	data = (result or {}).get("data")
	if not data:
		raise CDPError("empty screenshot")
	path = new_screenshot()
	path.write_bytes(base64.b64decode(data))
	return path


def _viewport_metrics(sess):
	value = sess.eval("({w: innerWidth, h: innerHeight, dpr: devicePixelRatio})")
	if not isinstance(value, dict):
		raise CDPError("viewport metrics failed")
	w = float(value.get("w") or 0)
	h = float(value.get("h") or 0)
	dpr = float(value.get("dpr") or 1)
	if w <= 0 or h <= 0:
		raise CDPError("bad viewport size")
	return {"w": w, "h": h, "dpr": dpr}


def _store_view(tid, path, css_x, css_y, css_w, css_h, dpr):
	png_w, png_h = _png_size(path)
	_VIEWS[tid] = {
		"path": str(path),
		"png_w": png_w,
		"png_h": png_h,
		"css_x": float(css_x),
		"css_y": float(css_y),
		"css_w": float(css_w),
		"css_h": float(css_h),
		"dpr": float(dpr),
	}


def _view_for(tab=""):
	try:
		tid = _pick(tab)["id"]
	except CDPError:
		return None
	return _VIEWS.get(tid)


def _shot_to_css(view, x, y):
	pw = view["png_w"]
	ph = view["png_h"]
	if pw <= 0 or ph <= 0:
		raise CDPError("bad screenshot size")
	css_x = view["css_x"] + (float(x) / pw) * view["css_w"]
	css_y = view["css_y"] + (float(y) / ph) * view["css_h"]
	return css_x, css_y


def _clamp_css(x, y, w, h, metrics):
	vw, vh = metrics["w"], metrics["h"]
	x = max(0.0, min(x, vw))
	y = max(0.0, min(y, vh))
	w = max(1.0, min(w, vw - x))
	h = max(1.0, min(h, vh - y))
	return x, y, w, h


def _mouse_click(sess, x, y, clicks=1):
	n = int(clicks)
	if n < 1:
		raise CDPError("clicks must be >= 1")
	sess.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
	for i in range(1, n + 1):
		sess.call(
			"Input.dispatchMouseEvent",
			{"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": i},
		)
		sess.call(
			"Input.dispatchMouseEvent",
			{"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": i},
		)


def _doc_url(url):
	return (url or "").split("#")[0]


def _hash_id(url):
	if not url or "#" not in url:
		return ""
	return url.split("#", 1)[1].split("?")[0]


def _scroll_hash(sess, href=""):
	hid = _hash_id(href)
	if not hid:
		try:
			hid = _hash_id(sess.location().get("url", ""))
		except CDPError:
			hid = ""
	if not hid:
		return
	sess.eval(
		f"""(() => {{
  const id = {json.dumps(hid)};
  const dest = document.getElementById(id) || document.getElementsByName(id)[0];
  if (!dest) return false;
  dest.scrollIntoView({{block: 'center', inline: 'nearest'}});
  return true;
}})()"""
	)
	time.sleep(CLICK_SETTLE)


_DOM = r"""
function docs() {
  const out = [];
  const add = (d) => {
    if (!d || out.indexOf(d) >= 0) return;
    out.push(d);
    let frames;
    try { frames = d.querySelectorAll("iframe,frame"); } catch (e) { return; }
    for (const f of frames) {
      try { add(f.contentDocument); } catch (e) {}
    }
  };
  add(document);
  return out;
}
function deepActive() {
  let el = document.activeElement;
  const seen = [];
  while (el && (el.tagName === "IFRAME" || el.tagName === "FRAME")) {
    if (seen.indexOf(el) >= 0) break;
    seen.push(el);
    try { el = el.contentDocument.activeElement; } catch (e) { break; }
  }
  return el;
}
function vis(el) {
  if (!el || !el.getBoundingClientRect) return false;
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false;
  const s = getComputedStyle(el);
  return s.display !== "none" && s.visibility !== "hidden" && Number(s.opacity) !== 0;
}
function txt(el) {
  const bits = [
    el.getAttribute && el.getAttribute("aria-label"),
    el.getAttribute && el.getAttribute("title"),
    el.alt, el.placeholder, el.value, el.innerText
  ];
  return bits.filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
}
function fieldTxt(el) {
  const bits = [
    el.getAttribute && el.getAttribute("aria-label"),
    el.placeholder, el.name, el.id, el.getAttribute && el.getAttribute("title")
  ];
  if (el.id && el.ownerDocument) {
    try {
      const lab = el.ownerDocument.querySelector("label[for='" + el.id.replace(/'/g, "\\'") + "']");
      if (lab) bits.push(lab.innerText);
    } catch (e) {}
  }
  const wrap = el.closest && el.closest("label");
  if (wrap) bits.push(wrap.innerText);
  return bits.filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
}
function css(d, q) {
  try { return d.querySelector(q); } catch (e) { return null; }
}
function xpath(d, q) {
  try {
    const r = d.evaluate(q, d, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
    return r.singleNodeValue;
  } catch (e) { return null; }
}
function pickText(nodes, q) {
  const needle = String(q).replace(/\s+/g, " ").trim().toLowerCase();
  if (!needle) return null;
  const hits = [];
  for (const n of nodes) {
    if (!vis(n)) continue;
    const t = txt(n).toLowerCase();
    if (!t || t.indexOf(needle) < 0) continue;
    const r = n.getBoundingClientRect();
    hits.push({ n, exact: t === needle, area: r.width * r.height, len: t.length });
  }
  hits.sort((a, b) => (b.exact - a.exact) || (a.area - b.area) || (a.len - b.len));
  return hits.length ? hits[0].n : null;
}
function find(q, want) {
  const kind = want || "click";
  for (const d of docs()) {
    let el = css(d, q) || xpath(d, q);
    if (el) return el;
  }
  const clickSel = "a,button,input,textarea,select,option,label,summary,li,td,th,tr,[role],[onclick],[tabindex],span,div,p";
  const fieldSel = "input:not([type=hidden]),textarea,select,[contenteditable='true'],[role=textbox],[role=searchbox],[role=combobox]";
  for (const d of docs()) {
    const sel = kind === "field" ? fieldSel : clickSel;
    let nodes;
    try { nodes = d.querySelectorAll(sel); } catch (e) { continue; }
    let el;
    if (kind === "field") {
      const needle = String(q).replace(/\s+/g, " ").trim().toLowerCase();
      const hits = [];
      for (const n of nodes) {
        if (!vis(n)) continue;
        const t = fieldTxt(n).toLowerCase();
        if (!t || t.indexOf(needle) < 0) continue;
        const r = n.getBoundingClientRect();
        hits.push({ n, exact: t === needle, area: Math.max(r.width * r.height, 1) });
      }
      hits.sort((a, b) => (b.exact - a.exact) || (a.area - b.area));
      el = hits.length ? hits[0].n : null;
    } else {
      el = pickText(nodes, q);
    }
    if (el) return el;
  }
  return null;
}
function canScroll(el) {
  if (!el) return false;
  let s;
  try { s = getComputedStyle(el); } catch (e) { return false; }
  const oy = s.overflowY;
  if (oy !== "auto" && oy !== "scroll" && oy !== "overlay") return false;
  return el.scrollHeight > el.clientHeight + 4;
}
function pane() {
  const ae = deepActive();
  const d = (ae && ae.ownerDocument) || document;
  let el = ae;
  while (el && el.nodeType === 1 && el !== d.documentElement && el !== d.body) {
    if (canScroll(el)) return el;
    el = el.parentElement;
  }
  const root = d.scrollingElement || d.documentElement;
  if (root && root.scrollHeight > root.clientHeight + 4) return root;
  let best = null, bestA = 0;
  for (const doc of docs()) {
    let nodes;
    try { nodes = doc.querySelectorAll("*"); } catch (e) { continue; }
    for (const n of nodes) {
      if (!canScroll(n)) continue;
      const b = n.getBoundingClientRect();
      const a = Math.max(0, b.width) * Math.max(0, b.height);
      if (a > bestA) { best = n; bestA = a; }
    }
  }
  return best || root || document.documentElement;
}
function setValue(el, value) {
  const v = String(value);
  if (el.tagName === "SELECT") {
    const opts = Array.from(el.options || []);
    let opt = opts.find((o) => o.value === v || o.text.trim() === v);
    if (!opt) {
      const n = v.toLowerCase();
      opt = opts.find((o) => o.text.trim().toLowerCase().indexOf(n) >= 0);
    }
    if (!opt) return { ok: false, err: "no option" };
    el.value = opt.value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, value: el.value };
  }
  if (el.isContentEditable) {
    el.focus();
    el.textContent = v;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    return { ok: true, value: el.textContent };
  }
  el.focus();
  const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, "value");
  if (desc && desc.set) desc.set.call(el, v);
  else el.value = v;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return { ok: true, value: el.value };
}
"""


def _js(body):
	return "(() => {\n" + _DOM + "\n" + body + "\n})()"


_UID_RE = re.compile(r"^e\d+$")
_SNAP = {}
_AX_INTERESTING = {
	"button",
	"link",
	"textbox",
	"searchbox",
	"combobox",
	"checkbox",
	"radio",
	"tab",
	"tabpanel",
	"menuitem",
	"menuitemcheckbox",
	"menuitemradio",
	"option",
	"listbox",
	"slider",
	"spinbutton",
	"switch",
	"heading",
	"cell",
	"gridcell",
	"columnheader",
	"rowheader",
	"row",
	"dialog",
	"alertdialog",
	"alert",
	"status",
	"treeitem",
	"img",
	"image",
	"listitem",
}
_AX_REDACT = ("password", "ssn", "social security", "secret", "cvv", "pin")


def _ax_val(obj):
	if not isinstance(obj, dict):
		return "" if obj is None else str(obj)
	v = obj.get("value")
	if isinstance(v, dict):
		v = v.get("value")
	if v is None:
		return ""
	if isinstance(v, bool):
		return v
	return str(v)


def _ax_role(node):
	return str(_ax_val(node.get("role") or {}) or "").strip()


def _ax_name(node):
	return str(_ax_val(node.get("name") or {}) or "").strip()


def _ax_value(node):
	return str(_ax_val(node.get("value") or {}) or "").strip()


def _ax_props(node):
	out = {}
	for prop in node.get("properties") or []:
		name = prop.get("name")
		if not name:
			continue
		out[name] = _ax_val(prop.get("value") or {})
	return out


def _ax_redact(role, name, value):
	blob = (" ".join([role or "", name or ""])).lower()
	if any(k in blob for k in _AX_REDACT):
		return ""
	return value


def _ax_interesting(node, interactive):
	if node.get("ignored"):
		return False
	role = _ax_role(node).lower()
	name = _ax_name(node)
	props = _ax_props(node)
	if props.get("focused") is True:
		return True
	if role in _AX_INTERESTING:
		if role in ("listitem", "row", "cell", "gridcell", "img", "image", "tabpanel", "status") and not name:
			return False
		return True
	if interactive:
		return False
	return bool(name) and role not in ("generic", "none", "inlineTextBox", "ignored", "LineBreak")


def _ax_enable(sess):
	sess.call("Accessibility.enable")
	try:
		sess.call("DOM.enable")
	except CDPError:
		pass
	try:
		sess.call("Page.enable")
	except CDPError:
		pass


def _ax_frames(sess):
	ids = []

	def walk(node):
		frame = (node or {}).get("frame") or {}
		fid = frame.get("id")
		if fid:
			ids.append(fid)
		for child in (node or {}).get("childFrames") or []:
			walk(child)

	try:
		tree = sess.call("Page.getFrameTree")
	except CDPError:
		return []
	walk(tree.get("frameTree") or tree)
	return ids


def _ax_nodes(sess):
	_ax_enable(sess)
	frames = _ax_frames(sess) or [None]
	nodes = []
	seen = set()
	for fid in frames:
		params = {}
		if fid:
			params["frameId"] = fid
		try:
			result = sess.call("Accessibility.getFullAXTree", params)
		except CDPError:
			if fid:
				continue
			raise
		for node in result.get("nodes") or []:
			nid = node.get("nodeId")
			key = (fid, nid)
			if key in seen:
				continue
			seen.add(key)
			if fid and not node.get("frameId"):
				node = dict(node)
				node["frameId"] = fid
			nodes.append(node)
	return nodes


def _ax_line(uid, node):
	role = _ax_role(node) or "unknown"
	name = _ax_name(node)
	value = _ax_redact(role, name, _ax_value(node))
	props = _ax_props(node)
	bits = [f"[{uid}]", role]
	if name:
		bits.append(json.dumps(name[:80], ensure_ascii=False))
	if value:
		bits.append("value=" + json.dumps(value[:60], ensure_ascii=False))
	for flag in ("focused", "disabled", "checked", "selected", "expanded", "required"):
		if props.get(flag) is True:
			bits.append(flag)
	return " ".join(str(b) for b in bits)


def _snap_store(tab, entries):
	_SNAP[_pick(tab)["id"]] = entries


def _snap_lookup(tab, uid):
	return (_SNAP.get(_pick(tab)["id"]) or {}).get(uid)


def _object_from_backend(sess, backend_id):
	sess.call("DOM.getDocument", {"depth": 0, "pierce": True})
	result = sess.call("DOM.resolveNode", {"backendNodeId": int(backend_id)})
	oid = (result.get("object") or {}).get("objectId")
	if not oid:
		raise CDPError("snapshot node is gone; call snapshot() again")
	return oid


def _call_el(sess, object_id, declaration, args=None):
	params = {
		"functionDeclaration": declaration,
		"objectId": object_id,
		"returnByValue": True,
		"userGesture": True,
	}
	if args:
		params["arguments"] = [{"value": a} for a in args]
	result = sess.call("Runtime.callFunctionOn", params)
	inner = result.get("result") or {}
	if inner.get("subtype") == "error" or result.get("exceptionDetails"):
		raise CDPError(inner.get("description") or str(result.get("exceptionDetails")))
	return inner.get("value")


_CLICK_FN = """function() {
  this.scrollIntoView({block: 'center', inline: 'center'});
  const href = this.href || '';
  const target = (this.getAttribute && this.getAttribute('target')) || '';
  const blank = this.tagName === 'A' && !!href && (target === '_blank' || target === '_new');
  if (blank) this.removeAttribute('target');
  this.click();
  return {ok: true, tag: this.tagName, href, blank};
}"""

_FILL_FN = """function(value) {
  this.scrollIntoView({block: 'center', inline: 'nearest'});
  const v = String(value);
  const tag = this.tagName;
  const editable = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || this.isContentEditable
    || this.getAttribute('contenteditable') === 'true'
    || ['textbox','searchbox','combobox'].indexOf((this.getAttribute('role') || '').toLowerCase()) >= 0;
  if (!editable) return {ok: false, err: 'not a field', tag: tag};
  if (this.tagName === 'SELECT') {
    const opts = Array.from(this.options || []);
    let opt = opts.find((o) => o.value === v || o.text.trim() === v);
    if (!opt) {
      const n = v.toLowerCase();
      opt = opts.find((o) => o.text.trim().toLowerCase().indexOf(n) >= 0);
    }
    if (!opt) return {ok: false, err: 'no option'};
    this.value = opt.value;
    this.dispatchEvent(new Event('input', {bubbles: true}));
    this.dispatchEvent(new Event('change', {bubbles: true}));
    return {ok: true, tag: this.tagName, value: this.value};
  }
  if (this.isContentEditable) {
    this.focus();
    this.textContent = v;
    this.dispatchEvent(new Event('input', {bubbles: true}));
    return {ok: true, tag: this.tagName, value: this.textContent};
  }
  this.focus();
  const proto = this.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  if (desc && desc.set) desc.set.call(this, v);
  else this.value = v;
  this.dispatchEvent(new Event('input', {bubbles: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true, tag: this.tagName, value: this.value};
}"""

_VIEW_FN = """function() {
  this.scrollIntoView({block: 'center', inline: 'nearest'});
  return {ok: true, tag: this.tagName};
}"""


def _uid_backend(tab, uid):
	info = _snap_lookup(tab, uid)
	if not info:
		raise CDPError(f"unknown snapshot id {uid!r}; call snapshot() first")
	backend = info.get("backendDOMNodeId")
	if not backend:
		raise CDPError(f"{uid} has no DOM node")
	return backend


def snapshot(tab="", interactive=True):
	sess = _session(tab)
	try:
		nodes = _ax_nodes(sess)
	finally:
		sess.close()
	entries = {}
	lines = []
	n = 0
	for node in nodes:
		if not _ax_interesting(node, interactive):
			continue
		n += 1
		uid = f"e{n}"
		entries[uid] = {
			"backendDOMNodeId": node.get("backendDOMNodeId"),
			"frameId": node.get("frameId") or "",
			"role": _ax_role(node),
			"name": _ax_name(node),
		}
		lines.append(_ax_line(uid, node))
		if n >= 200:
			lines.append("… truncated at 200 nodes")
			break
	_snap_store(tab, entries)
	return "\n".join(lines) if lines else "(no accessibility nodes)"


def focused(tab=""):
	sess = _session(tab)
	try:
		_ax_enable(sess)
		raw = sess.call(
			"Runtime.evaluate",
			{"expression": _js("""
  const el = deepActive();
  if (!el || el === el.ownerDocument.body || el === el.ownerDocument.documentElement) return null;
  return el;
""")},
		)
		oid = (raw.get("result") or {}).get("objectId")
		subtype = (raw.get("result") or {}).get("subtype")
		if not oid or subtype != "node":
			return {"role": "", "name": "", "value": "", "focused": False}
		tree = sess.call(
			"Accessibility.getPartialAXTree",
			{"objectId": oid, "fetchRelatives": False},
		)
		node = (tree.get("nodes") or [None])[0] or {}
		role = _ax_role(node)
		name = _ax_name(node)
		value = _ax_redact(role, name, _ax_value(node))
		props = _ax_props(node)
		return {
			"role": role,
			"name": name,
			"value": value,
			"focused": True,
			"disabled": bool(props.get("disabled")),
		}
	finally:
		sess.close()


def wait_for(query, tab="", timeout=None):
	q = (query or "").strip()
	if not q:
		raise CDPError("wait_for needs a query")
	limit = LOAD_TIMEOUT if timeout is None else float(timeout)
	deadline = time.time() + limit
	last = None
	while time.time() < deadline:
		try:
			if _UID_RE.match(q):
				info = _snap_lookup(tab, q)
				if info and info.get("backendDOMNodeId"):
					sess = _session(tab)
					try:
						_object_from_backend(sess, info["backendDOMNodeId"])
					finally:
						sess.close()
					return {"ok": True, "uid": q, "role": info.get("role", ""), "name": info.get("name", "")}
			else:
				found = evaluate(
					_js(
						f"return !!(find({json.dumps(q)}, 'click') || find({json.dumps(q)}, 'field'));"
					),
					tab=tab,
				)
				if found:
					return {"ok": True, "query": q}
				text = snapshot(tab=tab, interactive=True)
				if q.lower() in text.lower():
					return {"ok": True, "query": q}
		except CDPError as exc:
			last = exc
		time.sleep(0.25)
	raise CDPError(f"timeout waiting for {q!r}" + (f" ({last})" if last else ""))


def _session(tab=""):
	return Session(tab)


def _browser_session():
	info = _http_json("/json/version")
	ws = info.get("webSocketDebuggerUrl")
	if not ws:
		raise CDPError("no browser websocket")
	return Session(ws_url=ws)


def evaluate(expression, tab=""):
	sess = _session(tab)
	try:
		return sess.eval(expression)
	finally:
		sess.close()


def page_text(tab=""):
	value = evaluate(
		_js(
			"""
  const parts = [];
  for (const d of docs()) {
    const t = d.body ? d.body.innerText : '';
    if (t && t.trim()) parts.push(t);
  }
  return parts.join('\\n');
"""
		),
		tab=tab,
	)
	return value or ""


def links(tab=""):
	value = evaluate(
		_js(
			"""
  const out = [];
  const seen = new Map();
  for (const d of docs()) {
    let nodes;
    try { nodes = d.querySelectorAll('a[href]'); } catch (e) { continue; }
    for (const a of nodes) {
      const href = a.href;
      const text = (a.innerText || '').trim().replace(/\\s+/g, ' ');
      if (!href) continue;
      const prev = seen.get(href);
      if (!prev) {
        const item = {text, href};
        seen.set(href, item);
        out.push(item);
      } else if (!prev.text && text) {
        prev.text = text;
      }
    }
  }
  return out;
"""
		),
		tab=tab,
	)
	return value or []


def location_info(tab=""):
	sess = _session(tab)
	try:
		return sess.location()
	finally:
		sess.close()


def wait_load(tab="", want_url="", avoid_href="", timeout=LOAD_TIMEOUT):
	sess = _session(tab)
	try:
		sess.arm_load()
		return sess.wait_load(want_url=want_url, avoid_href=avoid_href, timeout=timeout)
	finally:
		sess.close()


def goto(url, tab=""):
	sess = _session(tab)
	try:
		sess.arm_load()
		sess.call("Page.navigate", {"url": url})
		avoid = "about:blank" if url and url != "about:blank" else ""
		return sess.wait_load(want_url=url, avoid_href=avoid)
	finally:
		sess.close()


def switch_tab(tab):
	if not tab:
		raise CDPError("switch_tab needs a tab id, title, or url")
	tid = _pick(tab)["id"]
	sess = _browser_session()
	try:
		sess.call("Target.activateTarget", {"targetId": tid})
	finally:
		sess.close()
	page = _session(tid)
	try:
		page.call("Page.bringToFront")
	finally:
		page.close()
	return tid


def click(query, tab=""):
	q = (query or "").strip()
	if _UID_RE.match(q):
		sess = _session(tab)
		try:
			sess.arm_load()
			prev = sess.location()
			oid = _object_from_backend(sess, _uid_backend(tab, q))
			value = _call_el(sess, oid, _CLICK_FN) or {}
			if not value.get("ok"):
				raise CDPError(f"no element matching {q!r}")
			navigated = sess.wait_after(prev)
			href = value.get("href") or ""
			if value.get("blank") and href.startswith("http") and not navigated:
				sess.call("Page.navigate", {"url": href})
				sess.wait_load(want_url=href)
				navigated = True
			elif not navigated:
				_scroll_hash(sess, href)
			value["loaded"] = navigated
			value["uid"] = q
			return value
		finally:
			sess.close()
	script = _js(
		f"""
  const q = {json.dumps(query)};
  const el = find(q, 'click');
  if (!el) return {{ok: false}};
  el.scrollIntoView({{block: 'center', inline: 'center'}});
  const href = el.href || '';
  const target = el.getAttribute('target') || '';
  const blank = el.tagName === 'A' && !!href && (target === '_blank' || target === '_new');
  if (blank) el.removeAttribute('target');
  el.click();
  return {{ok: true, tag: el.tagName, href, blank}};
"""
	)
	sess = _session(tab)
	try:
		sess.arm_load()
		prev = sess.location()
		value = sess.eval(script, user_gesture=True)
		if not value or not value.get("ok"):
			raise CDPError(f"no element matching {query!r}")
		navigated = sess.wait_after(prev)
		href = value.get("href") or ""
		if value.get("blank") and href.startswith("http") and not navigated:
			sess.call("Page.navigate", {"url": href})
			sess.wait_load(want_url=href)
			navigated = True
		elif not navigated:
			_scroll_hash(sess, href)
		value["loaded"] = navigated
	finally:
		sess.close()
	return value


def type_text(text, tab="", replace=False):
	if replace:
		evaluate(
			_js(
				"""
  const el = deepActive();
  if (!el) return false;
  if ('value' in el) { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }
  else if (el.isContentEditable) { el.textContent = ''; }
  return true;
"""
			),
			tab=tab,
		)
	sess = _session(tab)
	try:
		sess.call("Input.insertText", {"text": text})
	finally:
		sess.close()


def key(combo, tab=""):
	parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
	if not parts:
		raise CDPError("empty key combo")
	mods = 0
	key_name = parts[-1]
	for p in parts[:-1]:
		if p not in MOD:
			raise CDPError(f"unknown modifier {p!r}")
		mods |= MOD[p]
	if key_name in SPECIAL:
		key_str, code, vk = SPECIAL[key_name]
	elif len(key_name) == 1:
		key_str, code, vk = key_name, f"Key{key_name.upper()}", ord(key_name.upper())
	else:
		key_str, code, vk = key_name, key_name, 0
	params = {"windowsVirtualKeyCode": vk, "key": key_str, "code": code, "modifiers": mods}
	sess = _session(tab)
	try:
		wait = key_name in ("return", "enter")
		if wait:
			sess.arm_load()
			prev = sess.location()
		sess.call("Input.dispatchKeyEvent", {"type": "keyDown", **params})
		sess.call("Input.dispatchKeyEvent", {"type": "keyUp", **params})
		if wait:
			sess.wait_after(prev)
	finally:
		sess.close()


def scroll(direction, tab=""):
	d = (direction or "").strip().lower()
	if d not in ("up", "down"):
		raise CDPError("scroll direction must be 'up' or 'down'")
	sign = 1 if d == "down" else -1
	evaluate(
		_js(
			f"""
  const box = pane();
  const h = box.clientHeight || window.innerHeight;
  const dy = Math.floor(h * {SCROLL_KEEP - 1} / {SCROLL_KEEP}) * {sign};
  const root = box.ownerDocument && (box.ownerDocument.scrollingElement || box.ownerDocument.documentElement);
  if (box === root || box === document.body) {{
    const win = (box.ownerDocument && box.ownerDocument.defaultView) || window;
    win.scrollTo({{top: win.scrollY + dy, left: 0, behavior: 'instant'}});
  }} else {{
    box.scrollTop += dy;
  }}
  return true;
"""
		),
		tab=tab,
	)
	time.sleep(CLICK_SETTLE)


def blur(tab=""):
	evaluate(
		_js(
			"""
  for (const d of docs()) {
    const ae = d.activeElement;
    if (ae && ae !== d.body && ae.blur) ae.blur();
  }
  return true;
"""
		),
		tab=tab,
	)
	time.sleep(CLICK_SETTLE)


def fill(query, value, tab=""):
	q = (query or "").strip()
	if _UID_RE.match(q):
		sess = _session(tab)
		try:
			oid = _object_from_backend(sess, _uid_backend(tab, q))
			value_out = _call_el(sess, oid, _FILL_FN, [value]) or {}
		finally:
			sess.close()
		if not value_out.get("ok"):
			err = value_out.get("err")
			raise CDPError(f"no field matching {q!r}" + (f" ({err})" if err else ""))
		value_out["uid"] = q
		time.sleep(CLICK_SETTLE)
		return value_out
	script = _js(
		f"""
  const el = find({json.dumps(query)}, 'field');
  if (!el) return {{ok: false}};
  el.scrollIntoView({{block: 'center', inline: 'nearest'}});
  return Object.assign({{tag: el.tagName}}, setValue(el, {json.dumps(value)}));
"""
	)
	value_out = evaluate(script, tab=tab)
	if not value_out or not value_out.get("ok"):
		err = (value_out or {}).get("err")
		raise CDPError(f"no field matching {query!r}" + (f" ({err})" if err else ""))
	time.sleep(CLICK_SETTLE)
	return value_out


def form_fields(tab=""):
	value = evaluate(
		_js(
			"""
  const out = [];
  const sel = "input,textarea,select,[contenteditable='true'],[role=textbox],[role=searchbox],[role=combobox]";
  for (const d of docs()) {
    let nodes;
    try { nodes = d.querySelectorAll(sel); } catch (e) { continue; }
    for (const el of nodes) {
      const type = (el.type || '').toLowerCase();
      if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'image') continue;
      if (!vis(el) && type !== 'file') continue;
      const item = {
        tag: el.tagName.toLowerCase(),
        type: type || (el.isContentEditable ? 'contenteditable' : ''),
        name: el.name || '',
        id: el.id || '',
        label: fieldTxt(el).slice(0, 120),
        placeholder: (el.placeholder || '').slice(0, 80),
        value: type === 'password' ? '' : String(el.value || el.textContent || '').slice(0, 120),
        checked: !!el.checked,
        disabled: !!el.disabled
      };
      out.push(item);
      if (out.length >= 200) return out;
    }
  }
  return out;
"""
		),
		tab=tab,
	)
	return value or []


def scroll_into_view(query, tab=""):
	q = (query or "").strip()
	if _UID_RE.match(q):
		sess = _session(tab)
		try:
			oid = _object_from_backend(sess, _uid_backend(tab, q))
			value = _call_el(sess, oid, _VIEW_FN) or {}
		finally:
			sess.close()
		if not value.get("ok"):
			raise CDPError(f"no element matching {q!r}")
		value["uid"] = q
		time.sleep(CLICK_SETTLE)
		return value
	script = _js(
		f"""
  const el = find({json.dumps(query)}, 'click') || find({json.dumps(query)}, 'field');
  if (!el) return {{ok: false}};
  el.scrollIntoView({{block: 'center', inline: 'nearest'}});
  return {{ok: true, tag: el.tagName}};
"""
	)
	value = evaluate(script, tab=tab)
	if not value or not value.get("ok"):
		raise CDPError(f"no element matching {query!r}")
	time.sleep(CLICK_SETTLE)
	return value


def screenshot(tab=""):
	sess = _session(tab)
	try:
		metrics = _viewport_metrics(sess)
		result = sess.call("Page.captureScreenshot", {"format": "png"})
		tid = _pick(tab)["id"]
	finally:
		sess.close()
	path = _write_png(result)
	_store_view(
		tid,
		path,
		css_x=0,
		css_y=0,
		css_w=metrics["w"],
		css_h=metrics["h"],
		dpr=metrics["dpr"],
	)
	return str(path)


def view_info(tab=""):
	view = _view_for(tab)
	if not view:
		return None
	return dict(view)


def view_line(tab=""):
	view = _view_for(tab)
	if not view:
		return ""
	return "png %dx%d  css %.0fx%.0f  origin %.1f,%.1f  dpr %s" % (
		view["png_w"],
		view["png_h"],
		view["css_w"],
		view["css_h"],
		view["css_x"],
		view["css_y"],
		view["dpr"],
	)


def pixel_click(x, y, tab="", clicks=1):
	view = _view_for(tab)
	if not view:
		raise CDPError("no screenshot view; call screenshot() or zoom() first")
	css_x, css_y = _shot_to_css(view, x, y)
	sess = _session(tab)
	try:
		sess.arm_load()
		prev = sess.location()
		_mouse_click(sess, css_x, css_y, clicks=clicks)
		navigated = sess.wait_after(prev)
	finally:
		sess.close()
	return {"ok": True, "x": css_x, "y": css_y, "loaded": navigated}


def zoom(x, y, width, height, tab=""):
	view = _view_for(tab)
	if not view:
		screenshot(tab=tab)
		view = _view_for(tab)
	if not view:
		raise CDPError("no screenshot view")
	x0, y0 = _shot_to_css(view, x, y)
	x1, y1 = _shot_to_css(view, x + width, y + height)
	css_x = min(x0, x1)
	css_y = min(y0, y1)
	css_w = abs(x1 - x0)
	css_h = abs(y1 - y0)
	metrics = None
	sess = _session(tab)
	try:
		metrics = _viewport_metrics(sess)
		css_x, css_y, css_w, css_h = _clamp_css(css_x, css_y, css_w, css_h, metrics)
		short = min(css_w, css_h)
		scale = 1.0
		if short > 0 and short < 400:
			scale = min(3.0, 400.0 / short)
		result = sess.call(
			"Page.captureScreenshot",
			{
				"format": "png",
				"clip": {
					"x": css_x,
					"y": css_y,
					"width": css_w,
					"height": css_h,
					"scale": scale,
				},
			},
		)
		tid = _pick(tab)["id"]
	finally:
		sess.close()
	path = _write_png(result)
	_store_view(
		tid,
		path,
		css_x=css_x,
		css_y=css_y,
		css_w=css_w,
		css_h=css_h,
		dpr=metrics["dpr"],
	)
	return str(path)


def set_window(state="maximized"):
	if state not in ("maximized", "normal", "minimized", "fullscreen"):
		raise CDPError("bad window state")
	pages = list_page_targets()
	sess = _browser_session()
	try:
		win = sess.call("Browser.getWindowForTarget", {"targetId": pages[0]["id"]})
		wid = win["windowId"]
		if state == "maximized":
			sess.call(
				"Browser.setWindowBounds",
				{"windowId": wid, "bounds": {"windowState": "normal"}},
			)
		sess.call(
			"Browser.setWindowBounds",
			{"windowId": wid, "bounds": {"windowState": state}},
		)
		return sess.call("Browser.getWindowForTarget", {"targetId": pages[0]["id"]})
	finally:
		sess.close()


def allow_downloads(path=""):
	dest = os.path.abspath(path or str(DOWNLOADS))
	os.makedirs(dest, exist_ok=True)
	sess = _browser_session()
	try:
		sess.call(
			"Browser.setDownloadBehavior",
			{"behavior": "allow", "downloadPath": dest, "eventsEnabled": True},
		)
	finally:
		sess.close()
	return dest


def set_file_input(path, query="", tab=""):
	abs_path = os.path.abspath(path)
	if not os.path.isfile(abs_path):
		raise CDPError(f"not a file: {abs_path}")
	sel = query or "input[type=file]"
	sess = _session(tab)
	try:
		obj = sess.call(
			"Runtime.evaluate",
			{"expression": _js(f"return find({json.dumps(sel)}, 'field');")},
		)
		object_id = (obj.get("result") or {}).get("objectId")
		if not object_id:
			raise CDPError(f"no file input matching {sel!r}")
		sess.call("DOM.setFileInputFiles", {"files": [abs_path], "objectId": object_id})
	finally:
		sess.close()
	return abs_path
