#!/usr/bin/env python3
import cgi
import json
import mimetypes
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

PORT = int(os.environ.get("PORT", "38888"))
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/app/data/media"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/app/data/state"))
STATE_FILE = STATE_DIR / "settings.json"

# The dashboard's own UI folder on the host (mounted read-write), and the
# host-side path of our media folder (used as a symlink target, so umbreld
# can serve the media from the dashboard's own origin).
UI_DIR = Path(os.environ.get("UI_DIR", "/host-ui"))
HOST_MEDIA_DIR = os.environ.get("HOST_MEDIA_DIR", "")
CB_DIR = UI_DIR / "custom-background"
INDEX = UI_DIR / "index.html"
BACKUP = STATE_DIR / "index.html.orig"
MARKER = "custom-background/hook.js"
TAG = '<script defer src="/custom-background/hook.js"></script>'
INTEGRATION = {"ok": False, "message": "Not installed yet"}
LOCK = threading.Lock()

MEDIA_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED = {".mp4", ".png", ".jpg", ".jpeg"}
MAX_UPLOAD = 1024 * 1024 * 1024

HOOK_JS = r"""(() => {
  const BASE = "/custom-background";
  const rootId = "__umbrel_custom_background_v1";
  const styleId = "__umbrel_custom_background_style_v1";
  let last = null;

  function hideNative(on) {
    let st = document.getElementById(styleId);
    if (!on) { if (st) st.remove(); return; }
    if (st) return;
    st = document.createElement("style");
    st.id = styleId;
    // umbrelOS draws its wallpaper as fixed full-screen <img>/<video> elements.
    st.textContent = "#root img.fixed.inset-0,#root video.fixed.inset-0{opacity:0!important}";
    document.head.appendChild(st);
  }

  function render(s) {
    const signature = JSON.stringify(s);
    if (signature === last) return;
    last = signature;

    let root = document.getElementById(rootId);
    if (!s.current) {
      if (root) root.remove();
      hideNative(false);
      return;
    }
    if (!root) {
      root = document.createElement("div");
      root.id = rootId;
      Object.assign(root.style, {
        position:"fixed", inset:"0", zIndex:"-1",
        overflow:"hidden", pointerEvents:"none", background:"#111"
      });
      document.body.prepend(root);
    }
    root.innerHTML = "";
    hideNative(true);

    const isVideo = /\.mp4$/i.test(s.current);
    const media = document.createElement(isVideo ? "video" : "img");
    media.src = BASE + "/media/" + encodeURIComponent(s.current);
    if (isVideo) {
      media.autoplay = true;
      media.loop = true;
      media.muted = true;
      media.playsInline = true;
      media.setAttribute("playsinline", "");
    }
    Object.assign(media.style, {
      position:"absolute", inset:"0", width:"100%", height:"100%",
      objectFit:"cover", objectPosition:s.position || "center"
    });
    root.appendChild(media);

    const overlay = document.createElement("div");
    Object.assign(overlay.style, {
      position:"absolute", inset:"0",
      background:"rgba(0,0,0," + ((s.darkness || 0) / 100) + ")"
    });
    root.appendChild(overlay);
  }

  async function update() {
    try {
      const r = await fetch(BASE + "/state.json?t=" + Date.now(), {cache:"no-store"});
      if (r.ok) render(await r.json());
    } catch (_) {}
  }

  update();
  setInterval(update, 5000);
})();"""

HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Custom Background</title>
<style>
:root{color-scheme:dark;font-family:Inter,system-ui,sans-serif}
body{margin:0;background:#111;color:#eee;padding:28px}
.wrap{max-width:900px;margin:auto}
.card{background:#1b1b1b;border:1px solid #303030;border-radius:16px;padding:20px;margin-bottom:16px}
h1{margin:0 0 6px;font-size:28px}
.muted{color:#999}
button,.file{background:#ff7518;color:#fff;border:0;border-radius:10px;padding:11px 16px;font-weight:700;cursor:pointer}
button.secondary{background:#333}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.preview{height:320px;border-radius:12px;overflow:hidden;background:#090909;display:flex;align-items:center;justify-content:center;margin-top:15px}
.preview img,.preview video{width:100%;height:100%;object-fit:cover}
input[type=file]{display:none}
.item{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-top:1px solid #2b2b2b}
label{display:block;margin:12px 0 6px}
.range{width:100%}
select{background:#292929;color:#fff;border:1px solid #444;border-radius:8px;padding:8px}
.status{padding:10px;border-radius:9px;background:#252525;margin-top:12px}
</style>
</head>
<body>
<div class="wrap">
<div class="card">
<h1>Custom Background</h1>
<div class="muted">MP4, PNG, JPG and JPEG backgrounds for umbrelOS 2.0.</div>
<div id="status" class="status">Loading…</div>
</div>

<div class="card">
<h2>Current background</h2>
<div id="current" class="muted">Umbrel default</div>
<div class="preview" id="preview"></div>

<label>Darkness <span id="darknessValue">35%</span></label>
<input id="darkness" class="range" type="range" min="0" max="80" value="35">

<label>Position</label>
<select id="position">
<option>center</option><option>top</option><option>bottom</option>
<option>left</option><option>right</option>
</select>

<br><br>
<div class="row">
<label class="file">Upload background
<input id="upload" type="file" accept=".mp4,.png,.jpg,.jpeg,video/mp4,image/png,image/jpeg">
</label>
<button class="secondary" onclick="restore()">Restore default</button>
</div>
</div>

<div class="card">
<h2>Backgrounds</h2>
<div id="list">Loading…</div>
</div>

<div class="card">
<div class="muted">
The app stores your media persistently and keeps the dashboard hook installed.
Umbrel updates may temporarily remove the hook; this app automatically reinstalls it.
</div>
</div>
</div>

<script>
const $ = id => document.getElementById(id);
let settings = {darkness:35, position:"center"};

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw Error(await r.text());
  return r.json();
}

function esc(s) {
  return s.replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderPreview(s) {
  $("current").textContent = s.current || "Umbrel default";
  $("preview").innerHTML = "";
  if (!s.current) return;

  const u = "/media/" + encodeURIComponent(s.current);
  const el = /\.mp4$/i.test(s.current)
    ? document.createElement("video")
    : document.createElement("img");

  el.src = u;
  el.style.objectFit = "cover";
  el.style.objectPosition = s.position || "center";

  if (el.tagName === "VIDEO") {
    el.autoplay = true; el.loop = true; el.muted = true; el.playsInline = true;
  }
  $("preview").appendChild(el);
}

async function load() {
  try {
    const s = await api("/api/state");
    settings = {...settings, ...s};
    $("darkness").value = settings.darkness;
    $("darknessValue").textContent = settings.darkness + "%";
    $("position").value = settings.position;
    renderPreview(s);

    const files = await api("/api/files");
    $("list").innerHTML = "";

    if (!files.length) {
      $("list").innerHTML = '<div class="muted">No backgrounds uploaded yet.</div>';
    }

    for (const f of files) {
      const d = document.createElement("div");
      d.className = "item";
      d.innerHTML =
        "<span>" + esc(f.name) + "</span>" +
        '<span class="row">' +
        '<button class="apply">Apply</button>' +
        '<button class="secondary del">Delete</button>' +
        "</span>";
      d.querySelector(".apply").addEventListener("click", () => applyBg(f.name));
      d.querySelector(".del").addEventListener("click", () => delFile(f.name));
      $("list").appendChild(d);
    }

    const i = s.integration || {};
    $("status").textContent = i.ok
      ? "Dashboard integration: active"
      : "Dashboard integration problem: " + (i.message || "unknown");
  } catch (e) {
    $("status").textContent = "Error: " + e.message;
  }
}

async function applyBg(name) {
  const r = await fetch("/api/apply", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      name,
      darkness:+$("darkness").value,
      position:$("position").value
    })
  });
  const text = await r.text();
  await load();
  let msg = text;
  try { msg = JSON.parse(text).integration || text; } catch (_) {}
  if (r.ok) alert("Background applied. Reload the Umbrel dashboard.");
  else alert("Saved, but couldn't patch the dashboard: " + msg);
}

async function restore() {
  await api("/api/apply", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      name:null,
      darkness:+$("darkness").value,
      position:$("position").value
    })
  });
  await load();
  alert("Umbrel's default background has been restored.");
}

async function delFile(name) {
  if (!confirm("Delete " + name + "?")) return;
  await api("/api/files/" + encodeURIComponent(name), {method:"DELETE"});
  await load();
}

$("upload").onchange = async e => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  const r = await fetch("/api/upload", {method:"POST", body:fd});
  if (!r.ok) alert(await r.text());
  await load();
  e.target.value = "";
};

$("darkness").onchange = async () => {
  if (settings.current) await applyBg(settings.current);
};

$("position").onchange = async () => {
  if (settings.current) await applyBg(settings.current);
};

load();
</script>
</body>
</html>"""

def read_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"current": None, "darkness": 35, "position": "center"}

def write_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))

def safe_name(name):
    name = Path(name).name
    if not name or Path(name).suffix.lower() not in ALLOWED:
        raise ValueError("Only MP4, PNG, JPG and JPEG files are supported.")
    if name.startswith("."):
        raise ValueError("Hidden filenames are not allowed.")
    return name

def write_if_changed(path, text):
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except FileNotFoundError:
        pass
    path.write_text(text, encoding="utf-8")

def install_integration():
    """Make sure the dashboard serves our hook + state + media. Idempotent."""
    global INTEGRATION
    with LOCK:
        try:
            if not INDEX.is_file():
                raise RuntimeError(f"Dashboard folder isn't mounted (no index.html in {UI_DIR}).")
            if not HOST_MEDIA_DIR:
                raise RuntimeError("HOST_MEDIA_DIR is not set.")

            CB_DIR.mkdir(exist_ok=True)
            write_if_changed(CB_DIR / "hook.js", HOOK_JS)

            st = read_state()
            write_if_changed(
                CB_DIR / "state.json",
                json.dumps({k: st.get(k) for k in ("current", "darkness", "position")}),
            )

            link = CB_DIR / "media"
            if not (link.is_symlink() and os.readlink(link) == HOST_MEDIA_DIR):
                if link.is_symlink() or link.is_file():
                    link.unlink()
                elif link.is_dir():
                    shutil.rmtree(link)
                os.symlink(HOST_MEDIA_DIR, link)

            html = INDEX.read_text(encoding="utf-8")
            if MARKER not in html:
                if "</head>" not in html:
                    raise RuntimeError("index.html has no </head>; can't insert the hook.")
                BACKUP.write_text(html, encoding="utf-8")  # last clean copy
                INDEX.write_text(html.replace("</head>", TAG + "</head>", 1), encoding="utf-8")

            INTEGRATION = {"ok": True, "message": "Dashboard patched."}
        except OSError as e:
            if e.errno == 30:
                msg = "The dashboard folder is read-only on this system, so it can't be patched."
            else:
                msg = f"{type(e).__name__}: {e}"
            INTEGRATION = {"ok": False, "message": msg}
        except Exception as e:
            INTEGRATION = {"ok": False, "message": f"{type(e).__name__}: {e}"}
        return INTEGRATION["ok"], INTEGRATION["message"]

def watchdog():
    # Re-applies the patch if an umbrelOS update replaced the dashboard files.
    while True:
        time.sleep(30)
        install_integration()

class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, code=200):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_text(self, text, code=200):
        raw = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])

        if path in ("/", "/index.html"):
            raw = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if path == "/api/state":
            s = read_state()
            s["integration"] = INTEGRATION
            self.send_json(s)
            return

        if path == "/api/files":
            files = []
            for p in sorted(MEDIA_DIR.iterdir()):
                if p.is_file() and p.suffix.lower() in ALLOWED:
                    files.append({"name": p.name, "size": p.stat().st_size})
            self.send_json(files)
            return

        if path.startswith("/media/"):
            name = safe_name(path[len("/media/"):])
            p = MEDIA_DIR / name
            if not p.is_file():
                self.send_error(404)
                return

            size = p.stat().st_size
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            start = 0
            end = size - 1
            range_header = self.headers.get("Range")

            if range_header:
                m = re.match(r"bytes=(\d+)-(\d*)", range_header)
                if m:
                    start = int(m.group(1))
                    if m.group(2):
                        end = int(m.group(2))
                    end = min(end, size - 1)
                    if start > end:
                        self.send_error(416)
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
            else:
                self.send_response(200)

            length = end - start + 1
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.end_headers()

            with p.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk: break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return

        self.send_error(404)

    def do_DELETE(self):
        path = unquote(self.path.split("?", 1)[0])
        if path.startswith("/api/files/"):
            try:
                name = safe_name(path[len("/api/files/"):])
                p = MEDIA_DIR / name
                p.unlink(missing_ok=True)
                s = read_state()
                if s.get("current") == name:
                    s["current"] = None
                    write_state(s)
                    install_integration()
                self.send_json({"ok": True})
            except Exception as e:
                self.send_text(str(e), 400)
            return
        self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_UPLOAD + 1024 * 1024:
                    self.send_text("Upload is too large or empty.", 413)
                    return

                ctype = self.headers.get("Content-Type", "")
                if not ctype.startswith("multipart/form-data"):
                    self.send_text("Expected multipart/form-data.", 400)
                    return

                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD":"POST",
                        "CONTENT_TYPE":ctype,
                        "CONTENT_LENGTH":str(length)
                    }
                )
                item = form["file"] if "file" in form else None
                if item is None or not getattr(item, "filename", None):
                    self.send_text("No file supplied.", 400)
                    return

                name = safe_name(item.filename)
                tmp = MEDIA_DIR / (name + ".uploading")
                with tmp.open("wb") as out:
                    shutil.copyfileobj(item.file, out)
                if tmp.stat().st_size > MAX_UPLOAD:
                    tmp.unlink()
                    self.send_text("File exceeds 1 GiB.", 413)
                    return
                tmp.replace(MEDIA_DIR / name)

                self.send_json({"ok": True, "name": name})
            except Exception as e:
                self.send_text(str(e), 400)
            return

        if path == "/api/apply":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n))
                name = body.get("name")
                if name:
                    name = safe_name(name)
                    if not (MEDIA_DIR / name).is_file():
                        raise ValueError("Background file does not exist.")

                darkness = max(0, min(80, int(body.get("darkness", 35))))
                position = str(body.get("position", "center"))
                if position not in {"center","top","bottom","left","right"}:
                    position = "center"

                s = {"current": name, "darkness": darkness, "position": position}
                write_state(s)
                ok, message = install_integration()
                s["integration"] = message
                self.send_json(s, 200 if ok else 503)
            except Exception as e:
                self.send_text(str(e), 400)
            return

        self.send_error(404)

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

install_integration()
threading.Thread(target=watchdog, daemon=True).start()
print("Integration:", INTEGRATION, flush=True)
ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
