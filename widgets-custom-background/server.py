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
  let state = null;      // latest settings from state.json
  let stillUrl = null;   // blob: URL of a flat copy of the background (for the widgets' glass)
  let themeBrand = null; // accent colour picked from the background, "H S% L%"
  let token = 0;

  function mediaUrl(name) { return BASE + "/media/" + encodeURIComponent(name); }

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

  // Widgets refract a hidden copy of the built-in wallpaper
  // (<img data-wallpaper-static-source>), not what is actually on screen.
  // Point that copy at our background (darkness baked in) so they match.
  function applyStatic() {
    const active = state && state.current;
    if (active && !stillUrl) return; // still being built; keep whatever is there
    document.querySelectorAll("img[data-wallpaper-static-source]").forEach((img) => {
      if (active) {
        if (img.dataset.cbOrig === undefined) img.dataset.cbOrig = img.getAttribute("src") || "";
        if (img.getAttribute("src") !== stillUrl) {
          img.removeAttribute("srcset");
          img.setAttribute("src", stillUrl);
        }
        img.style.objectPosition = state.position || "center";
      } else if (img.dataset.cbOrig !== undefined) {
        if ((img.getAttribute("src") || "").startsWith("blob:")) img.setAttribute("src", img.dataset.cbOrig);
        img.style.objectPosition = "";
        delete img.dataset.cbOrig;
      }
    });
  }

  // ---- accent colour theme -------------------------------------------------
  // umbrelOS derives its theme from CSS variables on <html>, written from the
  // built-in wallpaper's brand colour. We overwrite them with a colour taken
  // from our background, and put the originals back when we're switched off.
  const ours = {};      // var name -> value we last wrote (as read back)
  const origVals = {};  // var name -> value that was there before us
  let appliedBrand = null;
  let metaOrig;

  function themeVars(brand) {
    const [h, sat, l] = brand.split(" ");
    const L = parseFloat(l);
    const withL = (x) => h + " " + sat + " " + x + "%";
    const v = {
      "--color-brand": brand,
      "--color-brand-lighter": withL(Math.min(100, L + 8)),
      "--color-brand-lightest": withL(Math.min(100, L + 16)),
      "--wallpaper-theme-color": withL(12),
      "--settings-tone-cold": withL(90),
      "--settings-tone-temperature-border": withL(10),
      "--settings-tone-hot": withL(10),
    };
    [62, 48, 34, 20, 10, 2].forEach((x, i) => { v["--settings-tone-" + (i + 1)] = withL(x); });
    return v;
  }

  function revertTheme() {
    const el = document.documentElement;
    for (const k of Object.keys(ours)) {
      if (origVals[k]) el.style.setProperty(k, origVals[k]);
      else el.style.removeProperty(k);
      delete ours[k];
      delete origVals[k];
    }
    appliedBrand = null;
    if (metaOrig !== undefined) {
      const m = document.querySelector('meta[name="theme-color"]');
      if (m) m.setAttribute("content", metaOrig);
      metaOrig = undefined;
    }
  }

  function applyTheme() {
    const on = state && state.current && state.theme !== false && themeBrand;
    if (!on) { if (Object.keys(ours).length || metaOrig !== undefined) revertTheme(); return; }
    const el = document.documentElement;
    const vars = themeVars(themeBrand);
    for (const [k, v] of Object.entries(vars)) {
      const cur = el.style.getPropertyValue(k);
      if (appliedBrand === themeBrand && cur === ours[k]) continue; // nothing changed
      // If the value isn't one we wrote, the dashboard just set it: that's the original.
      if (ours[k] === undefined || cur !== ours[k]) origVals[k] = cur;
      el.style.setProperty(k, v);
      ours[k] = el.style.getPropertyValue(k);
    }
    appliedBrand = themeBrand;
    const m = document.querySelector('meta[name="theme-color"]');
    if (m) {
      if (metaOrig === undefined) metaOrig = m.getAttribute("content") || "";
      m.setAttribute("content", "hsl(" + vars["--wallpaper-theme-color"] + ")");
    }
  }

  // Most common vivid hue in the image -> "H S% L%" (null if it is basically greyscale)
  function dominantBrand(canvas) {
    const sw = 64, sh = Math.max(1, Math.round(64 * canvas.height / canvas.width));
    const t = document.createElement("canvas");
    t.width = sw; t.height = sh;
    const tc = t.getContext("2d");
    tc.drawImage(canvas, 0, 0, sw, sh);
    const d = tc.getImageData(0, 0, sw, sh).data;
    const bins = Array.from({length: 36}, () => ({w: 0, x: 0, y: 0, s: 0}));
    for (let i = 0; i < d.length; i += 4) {
      const r = d[i] / 255, g = d[i + 1] / 255, b = d[i + 2] / 255;
      const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
      const l = (mx + mn) / 2, dl = mx - mn;
      if (dl < 0.08 || l < 0.1 || l > 0.92) continue;
      const sat = dl / (1 - Math.abs(2 * l - 1));
      let h;
      if (mx === r) h = ((g - b) / dl) % 6;
      else if (mx === g) h = (b - r) / dl + 2;
      else h = (r - g) / dl + 4;
      h *= 60; if (h < 0) h += 360;
      const w = sat * (1 - Math.abs(2 * l - 1)); // favour vivid, mid-tone pixels
      const bin = bins[Math.floor(h / 10) % 36];
      const rad = h * Math.PI / 180;
      bin.w += w; bin.x += Math.cos(rad) * w; bin.y += Math.sin(rad) * w; bin.s += sat * w;
    }
    let best = null;
    for (let i = 0; i < 36; i++) {
      // score a bin together with its neighbours so a smooth gradient beats a noisy spike
      const a = bins[i], b = bins[(i + 35) % 36], c = bins[(i + 1) % 36];
      const score = a.w + 0.5 * (b.w + c.w);
      if (!best || score > best.score) best = {score, a, b, c};
    }
    if (!best || best.score < 1e-6) return null;
    const x = best.a.x + best.b.x + best.c.x, y = best.a.y + best.b.y + best.c.y;
    const w = best.a.w + best.b.w + best.c.w;
    let hue = Math.atan2(y, x) * 180 / Math.PI; if (hue < 0) hue += 360;
    const sat = Math.max(35, Math.min(85, ((best.a.s + best.b.s + best.c.s) / w) * 100));
    return Math.round(hue) + " " + Math.round(sat) + "% 55%";
  }

  async function buildStill(s) {
    const url = mediaUrl(s.current);
    let src, w, h, cleanup = () => {};
    if (/\.mp4$/i.test(s.current)) {
      const v = document.createElement("video");
      v.muted = true; v.preload = "auto"; v.playsInline = true;
      await Promise.race([
        new Promise((res, rej) => {
          v.onerror = () => rej(new Error("video failed to load"));
          v.onloadeddata = () => {
            v.onseeked = res;
            v.currentTime = Math.max(0.1, Math.min(1, (v.duration || 2) / 2));
          };
          v.src = url;
        }),
        new Promise((_, rej) => setTimeout(() => rej(new Error("video timeout")), 20000)),
      ]);
      src = v; w = v.videoWidth; h = v.videoHeight;
      cleanup = () => { v.removeAttribute("src"); v.load(); };
    } else {
      const im = new Image();
      await new Promise((res, rej) => {
        im.onload = res;
        im.onerror = () => rej(new Error("image failed to load"));
        im.src = url;
      });
      src = im; w = im.naturalWidth; h = im.naturalHeight;
    }
    const scale = Math.min(1, 1920 / w);
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(w * scale));
    c.height = Math.max(1, Math.round(h * scale));
    const ctx = c.getContext("2d");
    ctx.drawImage(src, 0, 0, c.width, c.height);
    cleanup();
    let brand = null;
    try { brand = dominantBrand(c); } catch (e) { console.warn("[custom-background] colour pick failed:", e); }
    ctx.fillStyle = "rgba(0,0,0," + ((s.darkness || 0) / 100) + ")";
    ctx.fillRect(0, 0, c.width, c.height);
    const blob = await new Promise((res) => c.toBlob(res, "image/jpeg", 0.85));
    if (!blob) throw new Error("could not encode still");
    return {url: URL.createObjectURL(blob), brand};
  }

  function render(s) {
    const signature = JSON.stringify(s);
    if (signature === last) return;
    last = signature;
    state = s;
    const my = ++token;

    let root = document.getElementById(rootId);
    if (!s.current) {
      if (root) root.remove();
      hideNative(false);
      const old = stillUrl;
      stillUrl = null;
      themeBrand = null;
      applyStatic();
      applyTheme();
      if (old) setTimeout(() => URL.revokeObjectURL(old), 2000);
      return;
    }
    applyTheme(); // picks up a theme on/off change straight away
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
    media.src = mediaUrl(s.current);
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

    buildStill(s).then((r) => {
      if (my !== token) { URL.revokeObjectURL(r.url); return; }
      const old = stillUrl;
      stillUrl = r.url;
      themeBrand = r.brand || "0 0% 55%";
      applyStatic();
      applyTheme();
      if (old) setTimeout(() => URL.revokeObjectURL(old), 2000);
    }).catch((e) => console.warn("[custom-background] widget still failed:", e));
  }

  async function update() {
    try {
      const r = await fetch(BASE + "/state.json?t=" + Date.now(), {cache:"no-store"});
      if (r.ok) render(await r.json());
    } catch (_) {}
  }

  // The dashboard re-creates the hidden wallpaper <img> when screens change;
  // re-apply whenever new elements show up.
  let pending = false;
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; applyStatic(); });
  }).observe(document.documentElement, {childList:true, subtree:true});

  // The dashboard rewrites its theme variables when the built-in wallpaper changes
  // (or after login); put ours back.
  new MutationObserver(() => applyTheme())
    .observe(document.documentElement, {attributes:true, attributeFilter:["style"]});

  update();
  setInterval(update, 5000);
})();"""

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Custom Background</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNTYiIGhlaWdodD0iMjU2IiB2aWV3Qm94PSIwIDAgMjU2IDI1NiI+CiAgPGRlZnM+CiAgICA8bGluZWFyR3JhZGllbnQgaWQ9ImJnIiB4MT0iMCIgeTE9IjAiIHgyPSIxIiB5Mj0iMSI+CiAgICAgIDxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iIzNhMWQ5MyIvPgogICAgICA8c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiMwYThmOTMiLz4KICAgIDwvbGluZWFyR3JhZGllbnQ+CiAgICA8bGluZWFyR3JhZGllbnQgaWQ9InNreSIgeDE9IjAiIHkxPSIwIiB4Mj0iMCIgeTI9IjEiPgogICAgICA8c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiNmZmQ5YWUiLz4KICAgICAgPHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjZmY3ZmFlIi8+CiAgICA8L2xpbmVhckdyYWRpZW50PgogICAgPGNsaXBQYXRoIGlkPSJjYXJkIj4KICAgICAgPHJlY3QgeD0iNTAiIHk9Ijc2IiB3aWR0aD0iMTUyIiBoZWlnaHQ9IjExMiIgcng9IjE2Ii8+CiAgICA8L2NsaXBQYXRoPgogICAgPGZpbHRlciBpZD0ic2hhZG93IiB4PSItMjAlIiB5PSItMjAlIiB3aWR0aD0iMTQwJSIgaGVpZ2h0PSIxNTAlIj4KICAgICAgPGZlRHJvcFNoYWRvdyBkeD0iMCIgZHk9IjgiIHN0ZERldmlhdGlvbj0iOSIgZmxvb2QtY29sb3I9IiMwYjA2MzAiIGZsb29kLW9wYWNpdHk9IjAuMzUiLz4KICAgIDwvZmlsdGVyPgogIDwvZGVmcz4KCiAgPHJlY3Qgd2lkdGg9IjI1NiIgaGVpZ2h0PSIyNTYiIGZpbGw9InVybCgjYmcpIi8+CgogIDwhLS0gc3RhY2tlZCBiYWNrZ3JvdW5kcyBiZWhpbmQgLS0+CiAgPHJlY3QgeD0iNzIiIHk9IjUyIiB3aWR0aD0iMTUyIiBoZWlnaHQ9IjExMiIgcng9IjE2IiBmaWxsPSIjZmZmIiBmaWxsLW9wYWNpdHk9IjAuMTYiLz4KICA8cmVjdCB4PSI2MSIgeT0iNjQiIHdpZHRoPSIxNTIiIGhlaWdodD0iMTEyIiByeD0iMTYiIGZpbGw9IiNmZmYiIGZpbGwtb3BhY2l0eT0iMC4yOCIvPgoKICA8IS0tIGZyb250IGNhcmQ6IGEgd2FsbHBhcGVyIC0tPgogIDxnIGZpbHRlcj0idXJsKCNzaGFkb3cpIj4KICAgIDxyZWN0IHg9IjUwIiB5PSI3NiIgd2lkdGg9IjE1MiIgaGVpZ2h0PSIxMTIiIHJ4PSIxNiIgZmlsbD0idXJsKCNza3kpIi8+CiAgPC9nPgogIDxnIGNsaXAtcGF0aD0idXJsKCNjYXJkKSI+CiAgICA8cmVjdCB4PSI1MCIgeT0iNzYiIHdpZHRoPSIxNTIiIGhlaWdodD0iMTEyIiBmaWxsPSJ1cmwoI3NreSkiLz4KICAgIDxjaXJjbGUgY3g9IjE1OCIgY3k9IjExMiIgcj0iMTUiIGZpbGw9IiNmZmY2ZDgiLz4KICAgIDxwYXRoIGQ9Ik00MCAxODggTDg4IDEyNCBMMTE2IDE2MCBMMTQ2IDEzMCBMMjEyIDE4OCBaIiBmaWxsPSIjN2E1MmRjIi8+CiAgICA8cGF0aCBkPSJNNDAgMTg4IEw3NCAxNTAgTDEwNCAxODggWiIgZmlsbD0iIzMzMTc3ZiIvPgogICAgPHBhdGggZD0iTTk4IDE4OCBMMTUwIDE0NiBMMjEyIDE4OCBaIiBmaWxsPSIjMzMxNzdmIi8+CiAgPC9nPgogIDxyZWN0IHg9IjUwIiB5PSI3NiIgd2lkdGg9IjE1MiIgaGVpZ2h0PSIxMTIiIHJ4PSIxNiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZmZmIiBzdHJva2Utb3BhY2l0eT0iMC41NSIgc3Ryb2tlLXdpZHRoPSIyIi8+CgogIDwhLS0gdmlkZW8gYmFkZ2UgLS0+CiAgPGNpcmNsZSBjeD0iMTk2IiBjeT0iMTg2IiByPSIzMCIgZmlsbD0iI2ZmZiIvPgogIDxjaXJjbGUgY3g9IjE5NiIgY3k9IjE4NiIgcj0iMzAiIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzNhMWQ5MyIgc3Ryb2tlLW9wYWNpdHk9IjAuMTIiIHN0cm9rZS13aWR0aD0iMiIvPgogIDxwYXRoIGQ9Ik0xODcgMTcxIEwxODcgMjAxIEwyMTIgMTg2IFoiIGZpbGw9IiMzYTFkOTMiIHN0cm9rZT0iIzNhMWQ5MyIgc3Ryb2tlLXdpZHRoPSI1IiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+Cjwvc3ZnPgo=">
<style>
:root{
  color-scheme:dark;
  --bg:#080912;
  --panel:rgba(255,255,255,.045);
  --panel-2:rgba(255,255,255,.07);
  --border:rgba(255,255,255,.10);
  --text:#eef0fb;
  --muted:#9aa1c0;
  --accent:#7c5cff;
  --accent-2:#19c3b4;
  --ok:#3ddc97;
  --warn:#ffb454;
  --danger:#ff6474;
  --radius:20px;
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
*{box-sizing:border-box}
html{background:var(--bg)}
body{
  margin:0;color:var(--text);min-height:100vh;
  background:
    radial-gradient(900px 520px at 8% -8%,rgba(124,92,255,.28),transparent 60%),
    radial-gradient(800px 520px at 100% 108%,rgba(25,195,180,.20),transparent 60%),
    var(--bg);
  background-attachment:fixed;
  line-height:1.45;-webkit-font-smoothing:antialiased;
}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer}
:focus-visible{outline:2px solid var(--accent-2);outline-offset:2px}
.app{max-width:1060px;margin:0 auto;padding:32px 20px 72px}

/* header */
.top{display:flex;align-items:center;gap:16px;margin-bottom:24px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:14px;flex:1;min-width:260px}
.brand img{width:52px;height:52px;border-radius:14px;box-shadow:0 8px 24px rgba(0,0,0,.4)}
h1{margin:0;font-size:24px;letter-spacing:-.01em}
.sub{margin:2px 0 0;color:var(--muted);font-size:14px}
.chip{
  display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;
  background:var(--panel);border:1px solid var(--border);font-size:13px;font-weight:600;
}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
.chip.ok .dot{background:var(--ok);box-shadow:0 0 0 4px rgba(61,220,151,.18)}
.chip.bad .dot{background:var(--danger);box-shadow:0 0 0 4px rgba(255,100,116,.18)}

/* cards */
.card{
  background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px;margin-bottom:18px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
}
.card h2{margin:0 0 4px;font-size:17px;letter-spacing:-.005em}
.card .hint{margin:0 0 14px;color:var(--muted);font-size:13.5px}
.banner{
  display:none;gap:14px;align-items:flex-start;border-color:rgba(255,100,116,.45);
  background:linear-gradient(0deg,rgba(255,100,116,.08),rgba(255,100,116,.08)),var(--panel);
}
.banner.show{display:flex}
.banner svg{flex:none;color:var(--danger);margin-top:2px}
.banner .msg{flex:1;min-width:0}
.banner .msg b{display:block;margin-bottom:2px}
.banner .msg span{color:var(--muted);font-size:13.5px;word-break:break-word}

/* buttons */
.btn{
  border:1px solid var(--border);background:var(--panel-2);border-radius:12px;padding:10px 16px;
  font-weight:600;font-size:14px;transition:background .15s,transform .05s,border-color .15s;
}
.btn:hover{background:rgba(255,255,255,.11)}
.btn:active{transform:translateY(1px)}
.btn.primary{border:0;color:#fff;background:linear-gradient(135deg,var(--accent),#5b6cff 55%,var(--accent-2))}
.btn.primary:hover{filter:brightness(1.1);background:linear-gradient(135deg,var(--accent),#5b6cff 55%,var(--accent-2))}
.btn.danger{color:#ffd5da;border-color:rgba(255,100,116,.45);background:rgba(255,100,116,.12)}
.btn.danger:hover{background:rgba(255,100,116,.22)}
.btn.sm{padding:7px 12px;font-size:13px;border-radius:10px}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.icon-btn{
  width:34px;height:34px;display:grid;place-items:center;border-radius:10px;
  border:1px solid var(--border);background:var(--panel-2);padding:0;
}
.icon-btn:hover{background:rgba(255,100,116,.2);border-color:rgba(255,100,116,.45)}

/* hero */
.hero{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);gap:22px}
@media (max-width:880px){.hero{grid-template-columns:1fr}}
.stage{
  position:relative;aspect-ratio:16/9;border-radius:14px;overflow:hidden;background:#05060b;
  border:1px solid var(--border);
}
.stage .media,.stage .shade{position:absolute;inset:0;width:100%;height:100%}
.stage .media{object-fit:cover}
.stage .shade{background:#000;pointer-events:none}
.stage .empty{
  position:absolute;inset:0;display:grid;place-content:center;text-align:center;gap:6px;
  color:var(--muted);padding:20px;
  background:linear-gradient(135deg,#171a2e,#0b0d18);
}
.stage .empty b{color:var(--text);font-size:15px}
.stage .label{
  position:absolute;left:12px;bottom:12px;max-width:calc(100% - 24px);padding:6px 12px;border-radius:999px;
  background:rgba(8,9,18,.62);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  border:1px solid rgba(255,255,255,.14);font-size:12.5px;font-weight:600;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;
}
.controls{display:flex;flex-direction:column;gap:20px}
.field .row{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.field label,.field .lbl{font-weight:600;font-size:14px}
.field .val{color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}
input[type=range]{
  -webkit-appearance:none;appearance:none;width:100%;height:6px;border-radius:999px;outline-offset:6px;
  background:linear-gradient(90deg,var(--accent) var(--fill,40%),rgba(255,255,255,.14) var(--fill,40%));
}
input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none;appearance:none;width:20px;height:20px;border-radius:50%;background:#fff;
  box-shadow:0 2px 8px rgba(0,0,0,.5);border:0;
}
input[type=range]::-moz-range-thumb{width:20px;height:20px;border-radius:50%;background:#fff;border:0;box-shadow:0 2px 8px rgba(0,0,0,.5)}
.seg{display:grid;grid-template-columns:repeat(5,1fr);gap:4px;padding:4px;border-radius:12px;background:rgba(0,0,0,.28);border:1px solid var(--border)}
.seg button{border:0;background:transparent;border-radius:9px;padding:8px 0;font-size:13px;font-weight:600;color:var(--muted);text-transform:capitalize}
.seg button[aria-checked=true]{background:var(--panel-2);color:var(--text);box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
.seg button:hover{color:var(--text)}
.switch-row{display:flex;gap:14px;align-items:flex-start;justify-content:space-between}
.switch-row p{margin:2px 0 0;color:var(--muted);font-size:13px}
.switch{position:relative;flex:none;width:46px;height:28px;margin-top:2px}
.switch input{position:absolute;inset:0;opacity:0;margin:0;cursor:pointer;z-index:1}
.switch .track{position:absolute;inset:0;border-radius:999px;background:rgba(255,255,255,.16);transition:background .2s}
.switch .track::after{
  content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;
  transition:transform .2s;box-shadow:0 2px 6px rgba(0,0,0,.4);
}
.switch input:checked+.track{background:linear-gradient(135deg,var(--accent),var(--accent-2))}
.switch input:checked+.track::after{transform:translateX(18px)}
.switch input:focus-visible+.track{outline:2px solid var(--accent-2);outline-offset:3px}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:auto}

/* upload */
.drop{
  display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center;cursor:pointer;
  padding:30px 18px;border-radius:16px;border:2px dashed rgba(255,255,255,.2);
  background:rgba(255,255,255,.025);transition:border-color .15s,background .15s;position:relative;
}
.drop:hover,.drop.over,.drop:focus-within{border-color:var(--accent-2);background:rgba(25,195,180,.07)}
.drop input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.drop svg{color:var(--accent-2)}
.drop b{font-size:15px}
.drop span{color:var(--muted);font-size:13px}
.uploads{display:flex;flex-direction:column;gap:10px;margin-top:12px}
.up{display:grid;grid-template-columns:1fr auto;gap:6px 12px;align-items:center;padding:12px 14px;border-radius:12px;background:var(--panel-2);border:1px solid var(--border)}
.up .n{font-size:13.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.up .p{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.bar{grid-column:1/-1;height:6px;border-radius:999px;background:rgba(255,255,255,.12);overflow:hidden}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent-2));transition:width .15s}

/* library */
.libhead{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.count{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin-top:6px}
.tile{
  position:relative;border-radius:16px;overflow:hidden;background:var(--panel-2);border:1px solid var(--border);
  display:flex;flex-direction:column;transition:transform .15s,border-color .15s,box-shadow .15s;
}
.tile:hover{transform:translateY(-2px);border-color:rgba(255,255,255,.22)}
.tile.current{border-color:var(--accent-2);box-shadow:0 0 0 1px var(--accent-2),0 10px 30px rgba(25,195,180,.18)}
.thumb{position:relative;aspect-ratio:16/9;background:#05060b;cursor:pointer;border:0;padding:0;display:block;width:100%;overflow:hidden}
.thumb img,.thumb video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.thumb .ph{position:absolute;inset:0;display:none;place-content:center;justify-items:center;gap:4px;color:var(--muted);font-size:12px;background:linear-gradient(135deg,#1a1d33,#0c0e1b)}
.thumb.failed .ph{display:grid}
.badge{
  position:absolute;top:10px;left:10px;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.04em;
  background:rgba(8,9,18,.66);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.16);
}
.badge.use{left:auto;right:10px;background:linear-gradient(135deg,var(--accent),var(--accent-2));border:0;color:#fff}
.meta{padding:12px 12px 12px 14px;display:flex;flex-direction:column;gap:10px}
.meta .name{font-size:13.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta .foot2{display:flex;align-items:center;gap:8px}
.meta .size{flex:1;font-size:12px;color:var(--muted)}
.empty-lib{text-align:center;color:var(--muted);padding:26px 10px;font-size:14px}
.skeleton{height:170px;border-radius:16px;background:linear-gradient(90deg,rgba(255,255,255,.04),rgba(255,255,255,.09),rgba(255,255,255,.04));background-size:200% 100%;animation:sh 1.3s infinite linear}
@keyframes sh{to{background-position:-200% 0}}

details.how{color:var(--muted);font-size:13.5px}
details.how summary{cursor:pointer;font-weight:600;color:var(--text);font-size:14px}
details.how p{margin:10px 0 0}
.foot{text-align:center;color:var(--muted);font-size:12.5px;margin-top:22px}

/* toasts */
.toasts{position:fixed;right:18px;bottom:18px;z-index:2000;display:flex;flex-direction:column;gap:10px;width:min(390px,calc(100vw - 36px));pointer-events:none}
.toast{
  pointer-events:auto;position:relative;overflow:hidden;display:grid;grid-template-columns:auto 1fr auto;gap:12px;align-items:start;
  padding:14px 14px 16px 14px;border-radius:16px;background:rgba(22,24,40,.92);
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border:1px solid rgba(255,255,255,.14);
  box-shadow:0 18px 50px rgba(0,0,0,.5);animation:tin .28s cubic-bezier(.2,.9,.3,1.2);
}
.toast.out{animation:tout .2s ease-in forwards}
@keyframes tin{from{opacity:0;transform:translateY(14px) scale(.96)}to{opacity:1;transform:none}}
@keyframes tout{to{opacity:0;transform:translateX(24px)}}
.toast .ic{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;flex:none;margin-top:1px}
.toast.success .ic{background:rgba(61,220,151,.18);color:var(--ok)}
.toast.error .ic{background:rgba(255,100,116,.18);color:var(--danger)}
.toast.warn .ic{background:rgba(255,180,84,.18);color:var(--warn)}
.toast.info .ic{background:rgba(124,92,255,.22);color:#b9a8ff}
.toast .body{min-width:0}
.toast .tt{font-weight:700;font-size:14px}
.toast .tm{color:var(--muted);font-size:13.5px;word-break:break-word}
.toast .tt+.tm{margin-top:2px}
.toast .act{margin-top:10px}
.toast .x{border:0;background:transparent;color:var(--muted);width:26px;height:26px;border-radius:8px;display:grid;place-items:center;padding:0}
.toast .x:hover{background:rgba(255,255,255,.1);color:var(--text)}
.toast .life{position:absolute;left:0;bottom:0;height:3px;width:100%;transform-origin:left;animation:life linear forwards}
.toast.success .life{background:var(--ok)}.toast.error .life{background:var(--danger)}
.toast.warn .life{background:var(--warn)}.toast.info .life{background:var(--accent)}
.toast:hover .life{animation-play-state:paused}
@keyframes life{from{transform:scaleX(1)}to{transform:scaleX(0)}}
@media (max-width:560px){.toasts{right:50%;transform:translateX(50%);bottom:12px}}

/* confirm dialog */
dialog{
  border:1px solid rgba(255,255,255,.16);border-radius:20px;padding:22px;width:min(420px,calc(100vw - 32px));
  background:rgba(22,24,40,.97);color:var(--text);box-shadow:0 30px 80px rgba(0,0,0,.6);
}
dialog::backdrop{background:rgba(3,4,10,.62);backdrop-filter:blur(4px)}
dialog[open]{animation:tin .22s ease-out}
dialog h3{margin:0 0 6px;font-size:18px}
dialog p{margin:0 0 18px;color:var(--muted);font-size:14px;word-break:break-word}
dialog .btns{display:flex;gap:10px;justify-content:flex-end}

.dropveil{position:fixed;inset:0;z-index:1500;display:none;place-items:center;background:rgba(8,9,18,.78);backdrop-filter:blur(6px);pointer-events:none}
.dropveil.show{display:grid}
.dropveil div{padding:34px 46px;border-radius:24px;border:2px dashed var(--accent-2);font-size:20px;font-weight:700}

@media (prefers-reduced-motion:reduce){*{animation-duration:.01ms!important;transition-duration:.01ms!important}}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
</style>
</head>
<body>
<div class="app">

  <header class="top">
    <div class="brand">
      <img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNTYiIGhlaWdodD0iMjU2IiB2aWV3Qm94PSIwIDAgMjU2IDI1NiI+CiAgPGRlZnM+CiAgICA8bGluZWFyR3JhZGllbnQgaWQ9ImJnIiB4MT0iMCIgeTE9IjAiIHgyPSIxIiB5Mj0iMSI+CiAgICAgIDxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iIzNhMWQ5MyIvPgogICAgICA8c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiMwYThmOTMiLz4KICAgIDwvbGluZWFyR3JhZGllbnQ+CiAgICA8bGluZWFyR3JhZGllbnQgaWQ9InNreSIgeDE9IjAiIHkxPSIwIiB4Mj0iMCIgeTI9IjEiPgogICAgICA8c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiNmZmQ5YWUiLz4KICAgICAgPHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjZmY3ZmFlIi8+CiAgICA8L2xpbmVhckdyYWRpZW50PgogICAgPGNsaXBQYXRoIGlkPSJjYXJkIj4KICAgICAgPHJlY3QgeD0iNTAiIHk9Ijc2IiB3aWR0aD0iMTUyIiBoZWlnaHQ9IjExMiIgcng9IjE2Ii8+CiAgICA8L2NsaXBQYXRoPgogICAgPGZpbHRlciBpZD0ic2hhZG93IiB4PSItMjAlIiB5PSItMjAlIiB3aWR0aD0iMTQwJSIgaGVpZ2h0PSIxNTAlIj4KICAgICAgPGZlRHJvcFNoYWRvdyBkeD0iMCIgZHk9IjgiIHN0ZERldmlhdGlvbj0iOSIgZmxvb2QtY29sb3I9IiMwYjA2MzAiIGZsb29kLW9wYWNpdHk9IjAuMzUiLz4KICAgIDwvZmlsdGVyPgogIDwvZGVmcz4KCiAgPHJlY3Qgd2lkdGg9IjI1NiIgaGVpZ2h0PSIyNTYiIGZpbGw9InVybCgjYmcpIi8+CgogIDwhLS0gc3RhY2tlZCBiYWNrZ3JvdW5kcyBiZWhpbmQgLS0+CiAgPHJlY3QgeD0iNzIiIHk9IjUyIiB3aWR0aD0iMTUyIiBoZWlnaHQ9IjExMiIgcng9IjE2IiBmaWxsPSIjZmZmIiBmaWxsLW9wYWNpdHk9IjAuMTYiLz4KICA8cmVjdCB4PSI2MSIgeT0iNjQiIHdpZHRoPSIxNTIiIGhlaWdodD0iMTEyIiByeD0iMTYiIGZpbGw9IiNmZmYiIGZpbGwtb3BhY2l0eT0iMC4yOCIvPgoKICA8IS0tIGZyb250IGNhcmQ6IGEgd2FsbHBhcGVyIC0tPgogIDxnIGZpbHRlcj0idXJsKCNzaGFkb3cpIj4KICAgIDxyZWN0IHg9IjUwIiB5PSI3NiIgd2lkdGg9IjE1MiIgaGVpZ2h0PSIxMTIiIHJ4PSIxNiIgZmlsbD0idXJsKCNza3kpIi8+CiAgPC9nPgogIDxnIGNsaXAtcGF0aD0idXJsKCNjYXJkKSI+CiAgICA8cmVjdCB4PSI1MCIgeT0iNzYiIHdpZHRoPSIxNTIiIGhlaWdodD0iMTEyIiBmaWxsPSJ1cmwoI3NreSkiLz4KICAgIDxjaXJjbGUgY3g9IjE1OCIgY3k9IjExMiIgcj0iMTUiIGZpbGw9IiNmZmY2ZDgiLz4KICAgIDxwYXRoIGQ9Ik00MCAxODggTDg4IDEyNCBMMTE2IDE2MCBMMTQ2IDEzMCBMMjEyIDE4OCBaIiBmaWxsPSIjN2E1MmRjIi8+CiAgICA8cGF0aCBkPSJNNDAgMTg4IEw3NCAxNTAgTDEwNCAxODggWiIgZmlsbD0iIzMzMTc3ZiIvPgogICAgPHBhdGggZD0iTTk4IDE4OCBMMTUwIDE0NiBMMjEyIDE4OCBaIiBmaWxsPSIjMzMxNzdmIi8+CiAgPC9nPgogIDxyZWN0IHg9IjUwIiB5PSI3NiIgd2lkdGg9IjE1MiIgaGVpZ2h0PSIxMTIiIHJ4PSIxNiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZmZmIiBzdHJva2Utb3BhY2l0eT0iMC41NSIgc3Ryb2tlLXdpZHRoPSIyIi8+CgogIDwhLS0gdmlkZW8gYmFkZ2UgLS0+CiAgPGNpcmNsZSBjeD0iMTk2IiBjeT0iMTg2IiByPSIzMCIgZmlsbD0iI2ZmZiIvPgogIDxjaXJjbGUgY3g9IjE5NiIgY3k9IjE4NiIgcj0iMzAiIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzNhMWQ5MyIgc3Ryb2tlLW9wYWNpdHk9IjAuMTIiIHN0cm9rZS13aWR0aD0iMiIvPgogIDxwYXRoIGQ9Ik0xODcgMTcxIEwxODcgMjAxIEwyMTIgMTg2IFoiIGZpbGw9IiMzYTFkOTMiIHN0cm9rZT0iIzNhMWQ5MyIgc3Ryb2tlLXdpZHRoPSI1IiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+Cjwvc3ZnPgo=" alt="">
      <div>
        <h1>Custom Background</h1>
        <p class="sub">Your own video or image behind the whole umbrelOS dashboard.</p>
      </div>
    </div>
    <div id="chip" class="chip"><span class="dot"></span><span id="chipText">Checking…</span></div>
  </header>

  <section id="banner" class="card banner" role="alert">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>
    <div class="msg"><b>The dashboard isn't connected</b><span id="bannerMsg"></span></div>
    <button id="repair" class="btn sm">Try again</button>
  </section>

  <section class="card">
    <div class="hero">
      <div>
        <div class="stage" id="stage"></div>
      </div>
      <div class="controls">
        <div class="field">
          <div class="row"><label for="darkness">Darkness</label><span class="val" id="darknessVal">35%</span></div>
          <input id="darkness" type="range" min="0" max="80" value="35">
        </div>

        <div class="field">
          <div class="row"><span class="lbl" id="posLbl">Position</span></div>
          <div class="seg" id="seg" role="radiogroup" aria-labelledby="posLbl"></div>
        </div>

        <div class="field switch-row">
          <div>
            <div class="lbl">Match accent colours</div>
            <p>Tint buttons, settings and progress rings across Umbrel to suit your background.</p>
          </div>
          <label class="switch"><input id="theme" type="checkbox" checked aria-label="Match accent colours"><span class="track"></span></label>
        </div>

        <div class="actions">
          <button id="restore" class="btn danger" type="button">Restore Umbrel default</button>
        </div>
      </div>
    </div>
  </section>

  <section class="card">
    <h2>Add a background</h2>
    <p class="hint">MP4, PNG or JPG, up to 1&nbsp;GiB each. You can also drop files anywhere on this page.</p>
    <label class="drop" id="drop">
      <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/></svg>
      <b>Drop files here or click to browse</b>
      <span>Uploads are stored on your Umbrel and survive reboots</span>
      <input id="file" type="file" multiple accept=".mp4,.png,.jpg,.jpeg,video/mp4,image/png,image/jpeg">
    </label>
    <div class="uploads" id="uploads"></div>
  </section>

  <section class="card">
    <div class="libhead"><h2>Your backgrounds</h2><span class="count" id="count"></span></div>
    <div class="grid" id="grid"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>
  </section>

  <section class="card">
    <details class="how">
      <summary>How this works</summary>
      <p>The app adds a small script to the umbrelOS dashboard that shows your background behind everything and keeps the widgets and dock in sync with it. Changes appear on open dashboards within a few seconds, with no reload.</p>
      <p>If an umbrelOS update replaces the dashboard files, the app puts its script back automatically (usually within a minute). Restoring the default removes the custom background and brings Umbrel's own theme back.</p>
    </details>
  </section>

  <div class="foot">Custom Background &middot; for umbrelOS 2.0</div>
</div>

<div class="toasts" id="toasts" aria-live="polite" aria-atomic="false"></div>
<div class="dropveil" id="veil"><div>Drop to upload</div></div>
<dialog id="dlg"><h3 id="dlgTitle"></h3><p id="dlgBody"></p><div class="btns"><button class="btn" id="dlgNo" type="button"></button><button class="btn" id="dlgYes" type="button"></button></div></dialog>

<script>
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const MAX = 1024 * 1024 * 1024;
const EXT = [".mp4", ".png", ".jpg", ".jpeg"];
const POS = ["center", "top", "bottom", "left", "right"];
const ICONS = {
  success: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
  error: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  warn: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8v5M12 17h.01"/></svg>',
  info: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 11v6M12 7h.01"/></svg>',
  close: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  trash: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6"/></svg>',
  play: '<svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
  image: '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>'
};

let state = {current: null, darkness: 35, position: "center", theme: true, integration: null};
let files = [];
let saveTimer = null;
let stageKey = null;
let libKey = null;
let loaded = false;

/* ---------- helpers ---------- */
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid.nodeType ? kid : document.createTextNode(kid));
  return e;
}
const isVideo = n => /\.mp4$/i.test(n);
const mediaUrl = n => "/media/" + encodeURIComponent(n);
function fmtSize(b) {
  if (b >= 1073741824) return (b / 1073741824).toFixed(2) + " GB";
  if (b >= 1048576) return (b / 1048576).toFixed(b >= 10485760 ? 0 : 1) + " MB";
  return Math.max(1, Math.round(b / 1024)) + " KB";
}
async function api(url, opts) {
  const r = await fetch(url, opts);
  const text = await r.text();
  let data = null;
  try { data = JSON.parse(text); } catch (_) {}
  if (!r.ok) {
    const err = new Error((data && data.integration) || text || r.statusText || "Request failed");
    err.status = r.status; err.data = data;
    throw err;
  }
  return data;
}

/* ---------- toasts ---------- */
const toastMap = new Map();
function toast(message, o = {}) {
  const type = o.type || "info";
  const ms = o.timeout ?? (type === "error" || type === "warn" ? 8000 : 4500);
  if (o.key && toastMap.has(o.key)) dismiss(toastMap.get(o.key), true);

  const t = h("div", {class: "toast " + type, role: type === "error" ? "alert" : "status"});
  t.append(h("div", {class: "ic", html: ICONS[type]}));
  const body = h("div", {class: "body"});
  if (o.title) body.append(h("div", {class: "tt"}, o.title));
  if (message) body.append(h("div", {class: "tm"}, message));
  if (o.action) {
    body.append(h("div", {class: "act"}, h("button", {
      class: "btn sm", type: "button",
      onclick: () => { dismiss(t); o.action.run(); }
    }, o.action.label)));
  }
  t.append(body);
  t.append(h("button", {class: "x", type: "button", "aria-label": "Dismiss", html: ICONS.close, onclick: () => dismiss(t)}));
  if (ms > 0) {
    const life = h("div", {class: "life"});
    life.style.animationDuration = ms + "ms";
    life.addEventListener("animationend", () => dismiss(t));
    t.append(life);
  }
  $("#toasts").append(t);
  if (o.key) toastMap.set(o.key, t);
  t._key = o.key;
  while ($("#toasts").children.length > 4) dismiss($("#toasts").firstChild, true);
  return t;
}
function dismiss(t, instant) {
  if (!t || t._gone) return;
  t._gone = true;
  if (t._key && toastMap.get(t._key) === t) toastMap.delete(t._key);
  if (instant) { t.remove(); return; }
  t.classList.add("out");
  setTimeout(() => t.remove(), 200);
}

/* ---------- confirm dialog ---------- */
function confirmDialog({title, body, yes, no = "Cancel", danger = false}) {
  const d = $("#dlg");
  $("#dlgTitle").textContent = title;
  $("#dlgBody").textContent = body;
  const y = $("#dlgYes"), n = $("#dlgNo");
  y.textContent = yes; n.textContent = no;
  y.className = "btn " + (danger ? "danger" : "primary");
  return new Promise(resolve => {
    const done = v => { d.close(); cleanup(); resolve(v); };
    const onY = () => done(true), onN = () => done(false);
    const onClose = () => { cleanup(); resolve(false); };
    function cleanup() { y.removeEventListener("click", onY); n.removeEventListener("click", onN); d.removeEventListener("cancel", onClose); }
    y.addEventListener("click", onY); n.addEventListener("click", onN); d.addEventListener("cancel", onClose);
    d.showModal();
    n.focus();
  });
}

/* ---------- rendering ---------- */
function renderStatus() {
  const i = state.integration;
  const chip = $("#chip");
  chip.className = "chip" + (i ? (i.ok ? " ok" : " bad") : "");
  $("#chipText").textContent = !i ? "Checking…" : i.ok ? "Connected to dashboard" : "Dashboard not connected";
  $("#banner").classList.toggle("show", !!(i && !i.ok));
  if (i && !i.ok) $("#bannerMsg").textContent = i.message || "Unknown problem.";
}

function renderStage() {
  const stage = $("#stage");
  const key = state.current || "";
  if (key !== stageKey) {
    stageKey = key;
    stage.innerHTML = "";
    if (state.current) {
      const m = isVideo(state.current)
        ? h("video", {class: "media", src: mediaUrl(state.current), autoplay: true, loop: true, muted: true, playsinline: true})
        : h("img", {class: "media", src: mediaUrl(state.current), alt: ""});
      if (m.tagName === "VIDEO") m.muted = true;
      stage.append(m, h("div", {class: "shade"}), h("div", {class: "label"}, state.current));
    } else {
      stage.append(h("div", {class: "empty"}, h("b", {}, "Umbrel's default background"), h("span", {}, "Upload a file below and choose Use to set your own.")));
    }
  }
  const m = $(".media", stage), s = $(".shade", stage);
  if (m) m.style.objectPosition = state.position;
  if (s) s.style.opacity = state.darkness / 100;
}

function renderControls() {
  const r = $("#darkness");
  r.value = state.darkness;
  r.style.setProperty("--fill", (state.darkness / 80 * 100) + "%");
  $("#darknessVal").textContent = state.darkness + "%";
  $("#theme").checked = state.theme !== false;
  const seg = $("#seg");
  if (!seg.children.length) {
    for (const p of POS) {
      seg.append(h("button", {type: "button", role: "radio", "data-p": p, onclick: () => { state.position = p; renderControls(); renderStage(); scheduleSave(); }}, p));
    }
  }
  for (const b of seg.children) b.setAttribute("aria-checked", String(b.dataset.p === state.position));
}

function renderLibrary() {
  const key = JSON.stringify([files, state.current]);
  if (key === libKey) return;
  libKey = key;
  const grid = $("#grid");
  grid.innerHTML = "";
  $("#count").textContent = files.length ? files.length + (files.length === 1 ? " file" : " files") : "";
  if (!files.length) {
    grid.append(h("div", {class: "empty-lib", style: "grid-column:1/-1"}, "Nothing here yet. Upload a video or image above to get started."));
    return;
  }
  const ordered = [...files].sort((a, b) => (b.name === state.current) - (a.name === state.current));
  for (const f of ordered) {
    const cur = f.name === state.current;
    const thumb = h("button", {class: "thumb", type: "button", "aria-label": cur ? f.name + " (in use)" : "Use " + f.name, onclick: () => { if (!cur) useBackground(f.name); }});
    const media = isVideo(f.name)
      ? h("video", {src: mediaUrl(f.name) + "#t=0.5", preload: "metadata", muted: true, playsinline: true})
      : h("img", {src: mediaUrl(f.name), loading: "lazy", alt: ""});
    media.addEventListener("error", () => thumb.classList.add("failed"));
    thumb.append(media, h("div", {class: "ph", html: isVideo(f.name) ? ICONS.play : ICONS.image}, ));
    thumb.append(h("span", {class: "badge"}, isVideo(f.name) ? "VIDEO" : "IMAGE"));
    if (cur) thumb.append(h("span", {class: "badge use"}, "IN USE"));
    const del = h("button", {class: "icon-btn", type: "button", title: "Delete", "aria-label": "Delete " + f.name, html: ICONS.trash, onclick: () => deleteFile(f.name)});
    const meta = h("div", {class: "meta"},
      h("div", {class: "name", title: f.name}, f.name),
      h("div", {class: "foot2"},
        h("span", {class: "size"}, fmtSize(f.size)),
        cur ? null : h("button", {class: "btn sm primary", type: "button", onclick: () => useBackground(f.name)}, "Use"),
        del));
    grid.append(h("div", {class: "tile" + (cur ? " current" : "")}, thumb, meta));
  }
}

function renderAll() { renderStatus(); renderStage(); if (!saveTimer) renderControls(); renderLibrary(); }

/* ---------- data ---------- */
async function refresh() {
  try {
    const [s, f] = await Promise.all([api("/api/state"), api("/api/files")]);
    if (!saveTimer) state = {...state, ...s};
    else state.integration = s.integration;
    files = f;
    loaded = true;
    renderAll();
  } catch (e) {
    if (!loaded) toast(e.message, {type: "error", title: "Couldn't load your backgrounds", key: "load"});
  }
}

async function push(name) {
  const body = JSON.stringify({name, darkness: state.darkness, position: state.position, theme: state.theme !== false});
  try {
    await api("/api/apply", {method: "POST", headers: {"Content-Type": "application/json"}, body});
    return {ok: true};
  } catch (e) {
    if (e.status === 503) return {ok: false, saved: true, message: e.message};
    return {ok: false, message: e.message};
  }
}

async function useBackground(name) {
  clearTimeout(saveTimer); saveTimer = null;
  const prev = state.current;
  const r = await push(name);
  await refresh();
  if (r.ok) {
    toast("Open dashboards update within a few seconds.", {
      type: "success", title: "Background applied", key: "apply",
      action: prev && prev !== name ? {label: "Undo", run: () => useBackground(prev)} : null
    });
  } else if (r.saved) {
    toast(r.message, {type: "warn", title: "Saved, but the dashboard wasn't updated", key: "apply"});
  } else {
    toast(r.message, {type: "error", title: "Couldn't apply it", key: "apply"});
  }
}

async function restoreDefault() {
  if (!state.current) { toast("You're already using Umbrel's default background.", {type: "info", key: "apply"}); return; }
  clearTimeout(saveTimer); saveTimer = null;
  const prev = state.current;
  const r = await push(null);
  await refresh();
  if (r.ok || r.saved) {
    toast("Umbrel's own theme is back.", {type: "success", title: "Default background restored", key: "apply", action: {label: "Undo", run: () => useBackground(prev)}});
  } else {
    toast(r.message, {type: "error", title: "Couldn't restore the default", key: "apply"});
  }
}

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    saveTimer = null;
    const r = await push(state.current);
    await refresh();
    if (!state.current) return;
    if (r.ok) toast("", {type: "success", title: "Settings saved", key: "save", timeout: 2200});
    else toast(r.message, {type: r.saved ? "warn" : "error", title: r.saved ? "Saved, but the dashboard wasn't updated" : "Couldn't save settings", key: "save"});
  }, 450);
}

async function deleteFile(name) {
  const inUse = name === state.current;
  const ok = await confirmDialog({
    title: "Delete this background?",
    body: inUse ? name + " is in use. Umbrel's default background will be restored." : name + " will be permanently removed from your Umbrel.",
    yes: "Delete", danger: true
  });
  if (!ok) return;
  try {
    await api("/api/files/" + encodeURIComponent(name), {method: "DELETE"});
    await refresh();
    toast(inUse ? "Umbrel's default background has been restored." : "", {type: "success", title: "Deleted " + name});
  } catch (e) {
    toast(e.message, {type: "error", title: "Couldn't delete it"});
  }
}

/* ---------- uploads ---------- */
function uploadOne(file, row) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const pct = Math.round(e.loaded / e.total * 100);
      row.bar.style.width = pct + "%";
      row.pct.textContent = pct < 100 ? pct + "%  of " + fmtSize(file.size) : "Saving…";
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) { try { resolve(JSON.parse(xhr.responseText)); } catch (_) { resolve({name: file.name}); } }
      else reject(new Error(xhr.responseText || "Upload failed"));
    };
    xhr.onerror = () => reject(new Error("Network error. Check your connection and try again."));
    xhr.onabort = () => reject(Object.assign(new Error("Cancelled"), {cancelled: true}));
    row.cancel.onclick = () => xhr.abort();
    const fd = new FormData();
    fd.append("file", file);
    xhr.send(fd);
  });
}

let uploading = false;
async function handleFiles(list) {
  const picked = [...list];
  if (!picked.length) return;
  const ok = [];
  for (const f of picked) {
    const ext = f.name.slice(f.name.lastIndexOf(".")).toLowerCase();
    if (!EXT.includes(ext)) { toast(f.name + " isn't a supported type. Use MP4, PNG or JPG.", {type: "error", title: "Skipped a file"}); continue; }
    if (f.size > MAX) { toast(f.name + " is " + fmtSize(f.size) + ". The limit is 1 GB.", {type: "error", title: "File too large"}); continue; }
    if (f.size === 0) { toast(f.name + " is empty.", {type: "error", title: "Skipped a file"}); continue; }
    ok.push(f);
  }
  if (!ok.length) return;
  const clash = ok.filter(f => files.some(x => x.name === f.name));
  if (clash.length) {
    const go = await confirmDialog({
      title: "Replace existing file?",
      body: clash.map(f => f.name).join(", ") + (clash.length > 1 ? " already exist." : " already exists.") + " Uploading will replace it.",
      yes: "Replace", danger: true
    });
    if (!go) return;
  }
  if (uploading) { toast("Wait for the current upload to finish first.", {type: "info"}); return; }
  uploading = true;
  const done = [];
  for (const f of ok) {
    const row = {
      bar: h("i"), pct: h("span", {class: "p"}, "Starting…"),
      cancel: h("button", {class: "btn sm", type: "button"}, "Cancel")
    };
    const el = h("div", {class: "up"}, h("div", {class: "n"}, f.name), row.cancel, h("div", {class: "p"}, row.pct), h("div", {class: "bar"}, row.bar));
    $("#uploads").append(el);
    try {
      const res = await uploadOne(f, row);
      done.push(res.name || f.name);
    } catch (e) {
      if (!e.cancelled) toast(e.message, {type: "error", title: "Couldn't upload " + f.name});
      else toast("", {type: "info", title: "Upload cancelled"});
    }
    el.remove();
  }
  uploading = false;
  await refresh();
  if (done.length === 1) {
    toast(done[0] + " is in your library.", {type: "success", title: "Upload complete", action: {label: "Use as background", run: () => useBackground(done[0])}, timeout: 9000});
  } else if (done.length > 1) {
    toast(done.length + " files are in your library.", {type: "success", title: "Uploads complete"});
  }
}

/* ---------- wiring ---------- */
$("#darkness").addEventListener("input", e => { state.darkness = +e.target.value; renderControls(); renderStage(); scheduleSave(); });
$("#theme").addEventListener("change", e => { state.theme = e.target.checked; scheduleSave(); });
$("#restore").addEventListener("click", restoreDefault);
$("#file").addEventListener("change", e => { handleFiles(e.target.files); e.target.value = ""; });
$("#repair").addEventListener("click", async () => {
  const b = $("#repair"); b.disabled = true; b.textContent = "Trying…";
  try {
    await api("/api/repair", {method: "POST"});
    toast("", {type: "success", title: "Dashboard connected"});
  } catch (e) {
    toast(e.message, {type: "error", title: "Still not connected", key: "repair"});
  }
  b.disabled = false; b.textContent = "Try again";
  refresh();
});

const drop = $("#drop"), veil = $("#veil");
let depth = 0;
const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
window.addEventListener("dragenter", e => { if (!hasFiles(e)) return; e.preventDefault(); depth++; veil.classList.add("show"); });
window.addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("dragleave", e => { if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) veil.classList.remove("show"); });
window.addEventListener("drop", e => {
  if (!hasFiles(e)) return;
  e.preventDefault(); depth = 0; veil.classList.remove("show");
  handleFiles(e.dataTransfer.files);
});

renderControls();
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
</script>
</body>
</html>"""

DEFAULT_STATE = {"current": None, "darkness": 35, "position": "center", "theme": True}

def read_state():
    try:
        return {**DEFAULT_STATE, **json.loads(STATE_FILE.read_text())}
    except Exception:
        return dict(DEFAULT_STATE)

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
                json.dumps({k: st.get(k) for k in ("current", "darkness", "position", "theme")}),
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
            # RuntimeError carries this app's own plain-English messages
            msg = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
            INTEGRATION = {"ok": False, "message": msg}
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

        if path == "/api/repair":
            ok, message = install_integration()
            if ok:
                self.send_json({"ok": True})
            else:
                self.send_text(message, 503)
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

                theme = bool(body.get("theme", True))
                s = {"current": name, "darkness": darkness, "position": position, "theme": theme}
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
