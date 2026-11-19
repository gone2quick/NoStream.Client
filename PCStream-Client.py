#!/usr/bin/env python3
"""
Stream PC video + PC audio (and optionally your microphone) to your phone
through Tailscale Funnel.

  ffmpeg captures screen/webcam (+ audio) -> HLS segments in a temp dir
  -> this script serves them on localhost:PORT -> `tailscale funnel PORT`
  exposes it -> open the printed URL on your phone.

Requirements
  * Python 3.8+ and ffmpeg on PATH
  * Windows audio:  pip install pyaudiowpatch numpy
    (captures what your speakers play via WASAPI loopback, no Stereo Mix needed)

Examples
  python stream_to_phone.py                    # screen + PC audio
  python stream_to_phone.py --audio both       # screen + PC audio + microphone
  python stream_to_phone.py --audio mic        # screen + microphone only
  python stream_to_phone.py --audio none       # video only
  python stream_to_phone.py --source webcam --video-dev "Integrated Camera" --audio both
  python stream_to_phone.py --list-devices

Then in another terminal:
  tailscale funnel --bg 8080
  tailscale funnel status

Pages (https://<your-machine>.<tailnet>.ts.net/ ...):
  /             browse page: a grid of everyone who is live right now (thumbnail, name, uptime); click one to watch
  /<username>/  that user's player: live stream (if any) plus all their past recordings
<username> is what you type when the script starts (or pass --user NAME).

Sending to a shared server (so other people can stream to it):
  host:    python PCStream.py --server --port 8080      # website + accounts + live chat; then `tailscale funnel --bg 8080`
  streamer: sign up on the server's website (that gives you a stream key), then run the client:
            python PCStream.py --push [https://other.host]    # asks for your stream key, then the stream's name
            (Ctrl+C ends the stream)
Nothing is captured-and-served locally in that mode: ffmpeg runs on the streamer's PC, the HLS segments are
uploaded to the server, and the server shows them on its browse page and records them. PCStream_client.py is
this same script with --push turned on by default (pointing at server.tail1be5a7.ts.net).

Server mode (no capturing, just the website):
  python stream_to_phone.py --server
serves the stream browser from --out-dir and shows whatever streams are live
there. Run capture instances on the same machine (or on a PC that writes into
the same --out-dir, e.g. a shared folder) with --no-serve so they don't fight
over the port:
  python stream_to_phone.py --no-serve --user alice

Titles: give a stream a name with --title "Speedrun night" (asked for when you start interactively; Enter skips it).
It shows on the browse page and in the player.

When a stream ends the recording is compiled into one recording.mp4 and the HLS pieces (segments, playlists,
live files) are deleted, so only the finished video is kept (--keep-segments keeps them; if the MP4 can't be
made, the HLS recording is kept so nothing is lost).

Recordings: every run is saved to  <out-dir>/<username>/<date_time>/  as an HLS
VOD (vod.m3u8) and, when you stop with Ctrl+C, also as recording.mp4. Default
out-dir is a "streams" folder next to this script (--out-dir to change,
--no-record to disable, --no-mp4 to skip the MP4).

NOTE: Funnel is PUBLIC internet and there is no password: the username in the
URL is the only thing protecting the stream, so pick something long and hard to
guess, and stop the funnel when you're done.
"""
import argparse
import collections
import getpass
import hashlib
import hmac
import http.client
import http.server
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

OS = platform.system()  # Windows / Linux / Darwin

# ---- console text color (everything this script prints, plus prompts and ffmpeg's own output) ----
TEXT_COLOR = "\033[92m"  # bright green. Other options: 32 = darker green, 94 = bright blue, 34 = darker blue


def _enable_text_color(code):
    """Make the terminal show all text in one color until the script exits. Skipped when output is redirected."""
    if not (sys.stdout.isatty() and sys.stderr.isatty()):
        return
    if OS == "Windows":  # turn on ANSI escape handling in the Windows console
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            for h in (-11, -12):  # stdout, stderr
                handle = k32.GetStdHandle(h)
                mode = ctypes.c_ulong()
                if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                    k32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            return
    sys.stdout.write(code)
    sys.stdout.flush()
    import atexit
    atexit.register(lambda: (sys.stdout.write("\033[0m"), sys.stdout.flush()))


_enable_text_color(TEXT_COLOR)

DEFAULT_PUSH_URL = "https://server.tail1be5a7.ts.net"  # what a bare  --push  sends to
PUSH_DEFAULT = DEFAULT_PUSH_URL  # pushing is on by default in this client build

PLAYER_HTML = r"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Streams</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  html,body{margin:0;background:#0e0e10;color:#efeff1;font-family:system-ui,sans-serif}
  #stage{position:relative;background:#000}
  video{display:block;width:100%;max-height:60vh;background:#000}
  #unmute{position:absolute;top:10px;right:10px;padding:8px 14px;border:0;border-radius:8px;
          background:#ffffffdd;color:#000;font-size:15px}
  #back{display:block;padding:10px 14px;color:#7fb0ff;text-decoration:none;font-size:14px}
  #now{padding:10px 14px;font-size:14px;opacity:.85}
  #status{padding:0 14px 8px;font-size:12px;opacity:.7;word-break:break-word;min-height:16px}
  h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;opacity:.6;margin:16px 14px 8px}
  .item{display:flex;align-items:center;gap:10px;padding:12px 14px;border-top:1px solid #26262c;cursor:pointer}
  .item:hover,.item.active{background:#1b1b21}
  .item .t{flex:1;min-width:0}
  .item .t b{display:block;font-weight:600;font-size:15px}
  .item .t span{font-size:12px;opacity:.65}
  .badge{background:#e91916;color:#fff;border-radius:4px;padding:2px 6px;font-size:11px;font-weight:700}
  .badge.rec{background:#6b6b76}
  a.dl{color:#7fb0ff;font-size:13px;text-decoration:none;padding:6px}
  .empty{padding:6px 14px;opacity:.55;font-size:14px}
/*AUTHCSS*//*CHATCSS*/
</style>
</head><body>
<div id="top"><a id="back" href="../">&#8249; All streams</a><span id="authbar"></span></div>
<div id="layout"><div id="main">
<div id="stage">
  <video id="v" autoplay muted playsinline controls></video>
  <button id="unmute">Tap for sound</button>
</div>
<div id="now">Loading...</div>
<div id="status"></div>
<div id="lists"><h2>Live</h2><div id="live"></div>
<h2 id="recsHead">Recordings</h2><div id="recs"></div></div>
</div>
<aside id="chat" hidden>
  <div id="chatHead">Live chat</div>
  <div id="chatLog"></div>
  <div id="chatErr"></div>
  <form id="chatForm"><input id="chatIn" maxlength="300" autocomplete="off" placeholder="Log in to chat"><button>Chat</button></form>
</aside>
</div>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"></script>
<script>
const v = document.getElementById('v'), st = document.getElementById('status'), nowEl = document.getElementById('now');
let hls = null, currentKey = null, firstLoad = true, playingLiveId = null, liveCfg = {delay: 0, buffer: 0};

function report(msg) {
  st.textContent = msg;
  try { fetch('clientlog', {method: 'POST', headers: {'Content-Type': 'text/plain'}, body: msg}); } catch (e) {}
}
document.getElementById('unmute').onclick = e => { v.muted = false; v.play(); e.target.remove(); };
v.addEventListener('error', () => report('video element error ' + (v.error && v.error.code) + ' ' + (v.error && v.error.message)));
v.addEventListener('playing', () => { st.textContent = ''; });

function hlsConfig(key) {
  const c = {manifestLoadingMaxRetry: 30, manifestLoadingRetryDelay: 1000};
  if (key !== 'live') return c;
  if (liveCfg.delay > 0) {  // the streamer wants viewers this many seconds behind live
    c.liveSyncDuration = liveCfg.delay;
    c.liveMaxLatencyDuration = liveCfg.delay + 30;
    c.backBufferLength = Infinity;  // keep what was already watched, so people can rewind
  }
  if (liveCfg.buffer > 0) { c.maxBufferLength = liveCfg.buffer; c.maxMaxBufferLength = liveCfg.buffer + 30; }
  return c;
}
function play(key, url, label) {
  currentKey = key; nowEl.textContent = label; st.textContent = 'loading...';
  if (hls) { hls.destroy(); hls = null; }
  if (/\.mp4$/.test(url)) {  // finished recording: plain MP4
    v.src = url; v.play().catch(() => {});
  } else if (window.Hls && Hls.isSupported()) {
    const h = hls = new Hls(hlsConfig(key));
    h.loadSource(url); h.attachMedia(v);
    h.on(Hls.Events.MANIFEST_PARSED, () => { v.play().catch(() => {}); });
    h.on(Hls.Events.ERROR, (_, d) => {
      report((d.fatal ? 'FATAL ' : '') + d.type + ' / ' + d.details + (d.reason ? ': ' + d.reason : '') +
             (d.error && d.error.message ? ' (' + d.error.message + ')' : ''));
      if (d.fatal) { d.type === Hls.ErrorTypes.NETWORK_ERROR ? h.startLoad() : h.recoverMediaError(); }
    });
  } else {  // iOS Safari plays HLS natively
    v.src = url; v.play().catch(() => {});
  }
  highlight();
}

function fmtDur(s) {
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? h + 'h ' : '') + (h || m ? m + 'm ' : '') + x + 's';
}
function when(id) {
  const m = id.match(/^(\d+)-(\d+)-(\d+)_(\d+)-(\d+)-(\d+)$/);
  if (!m) return id;
  return new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'});
}
function row(key, title, sub, badge, badgeClass, onclick, dlUrl, linkText) {
  const d = document.createElement('div');
  d.className = 'item'; d.dataset.key = key; d.onclick = onclick;
  const t = document.createElement('div'); t.className = 't';
  const b = document.createElement('b'); b.textContent = title;
  const s = document.createElement('span'); s.textContent = sub;
  t.append(b, s); d.append(t);
  if (badge) { const g = document.createElement('span'); g.className = 'badge ' + badgeClass; g.textContent = badge; d.append(g); }
  if (dlUrl) {
    const a = document.createElement('a'); a.className = 'dl'; a.href = dlUrl; a.textContent = linkText || 'MP4'; if (!linkText) a.setAttribute('download', '');
    a.onclick = e => e.stopPropagation(); d.append(a);
  }
  return d;
}
function empty(text) { const d = document.createElement('div'); d.className = 'empty'; d.textContent = text; return d; }
function highlight() {
  document.querySelectorAll('.item').forEach(el => el.classList.toggle('active', el.dataset.key === currentKey));
}

function startLive(id, title) { playingLiveId = id; play('live', 'live/stream.m3u8', title ? 'Live: ' + title : 'Live'); }

function render(data) {
  const live = document.getElementById('live'), recs = document.getElementById('recs');
  live.replaceChildren(); recs.replaceChildren();
  const liveId = data.live ? data.live.id : null, liveTitle = data.live ? data.live.title : '';
  if (data.live) liveCfg = {delay: data.live.delay || 0, buffer: data.live.buffer || 0};
  if (liveId) {
    live.append(row('live', liveTitle || 'Live now', (liveTitle ? 'Live now \u00b7 ' : '') + 'Started ' + when(liveId), 'LIVE', '',
                    () => startLive(liveId, liveTitle)));
  } else {
    live.append(empty('Nothing live right now.'));
  }
  const shown = data.recordings.filter(r => r.id !== liveId);  // the running stream is the Live row, not a recording
  if (!shown.length) recs.append(empty('No recordings yet.'));
  for (const r of shown) {
    const url = 'vod/' + r.id + '/' + (r.mp4 && !r.in_progress ? 'recording.mp4' : 'vod.m3u8');
    const name = r.title || when(r.id);
    recs.append(row('vod:' + r.id, name, (r.title ? when(r.id) + ' \u00b7 ' : '') + fmtDur(r.duration) + (r.in_progress ? ' so far' : ''),
                    r.in_progress ? 'REC' : '', 'rec',
                    () => play('vod:' + r.id, url, 'Recording: ' + name),
                    r.mp4 ? 'vod/' + r.id + '/recording.mp4' : null));
  }
  highlight();
  // follow the live stream: when it starts, or when a NEW stream replaces the one being watched
  // (otherwise hls.js keeps the old stream's playlist and segments)
  if (liveId && liveId !== playingLiveId && (currentKey === null || currentKey === 'live')) startLive(liveId, liveTitle);
  if (firstLoad) {
    firstLoad = false;
    if (!liveId) nowEl.textContent = 'Nothing is live. Pick a recording below.';
  }
}
async function refresh() {
  try {
    render(await (await fetch('api/streams.json', {cache: 'no-store'})).json());
  } catch (e) { st.textContent = 'cannot reach server'; }
}
refresh(); setInterval(refresh, 10000);
</script>
<!--AUTH-->
<script>/*CHATJS*/</script>
</body></html>
"""


ROOT_HTML = r"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live streams</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  html,body{margin:0;background:#0e0e10;color:#efeff1;font-family:system-ui,sans-serif}
  header{display:flex;align-items:baseline;gap:12px;padding:18px 20px 6px}
  h1{margin:0;font-size:22px}
  #count{font-size:14px;opacity:.65}
  #grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px;padding:14px 20px 30px}
  a.card{display:block;color:inherit;text-decoration:none}
  .thumb{position:relative;aspect-ratio:16/9;background:linear-gradient(135deg,#2a2a35,#17171c);border-radius:8px;
         overflow:hidden;outline:2px solid transparent;transition:outline-color .12s, transform .12s}
  a.card:hover .thumb{outline-color:#9147ff;transform:translateY(-2px)}
  .thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
  .thumb .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
             font-size:44px;font-weight:700;opacity:.35}
  .live{position:absolute;top:8px;left:8px;background:#e91916;color:#fff;border-radius:4px;
        padding:2px 6px;font-size:12px;font-weight:700;letter-spacing:.04em}
  .up{position:absolute;bottom:8px;left:8px;background:#000b;border-radius:4px;padding:2px 6px;font-size:12px}
  .meta{display:flex;gap:10px;padding:10px 2px 0;align-items:center}
  .av{flex:none;width:36px;height:36px;border-radius:50%;background:#9147ff;display:flex;
      align-items:center;justify-content:center;font-weight:700;text-transform:uppercase}
  .meta b{display:block;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .meta span{font-size:12px;opacity:.65}
  .meta div{min-width:0}
  #empty{display:none;text-align:center;padding:90px 20px;opacity:.6}
  #empty h2{margin:0 0 6px;font-size:20px;font-weight:600}
/*AUTHCSS*/
</style>
</head><body>
<header><h1>Live now</h1><span id="count"></span><span id="authbar" style="margin-left:auto"></span></header>
<div id="grid"></div>
<div id="empty"><h2>Nobody is live right now</h2>This page updates by itself.</div>
<script>
const grid = document.getElementById('grid'), emptyEl = document.getElementById('empty'), countEl = document.getElementById('count');
const cards = {};   // user -> {el, img, ph, up, thumb, started}

function startedMs(id) {
  const m = id.match(/^(\d+)-(\d+)-(\d+)_(\d+)-(\d+)-(\d+)$/);
  return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime() : Date.now();
}
function fmt(ms) {
  const s = Math.max(0, Math.floor(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? h + ':' + String(m).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0') : m + ':' + String(s % 60).padStart(2, '0');
}
function make(user) {
  const a = document.createElement('a'); a.className = 'card'; a.href = encodeURIComponent(user) + '/';
  const t = document.createElement('div'); t.className = 'thumb';
  const ph = document.createElement('div'); ph.className = 'ph'; ph.textContent = user.slice(0, 1).toUpperCase();
  const img = document.createElement('img'); img.alt = ''; img.style.display = 'none';
  img.onload = () => { img.style.display = ''; ph.style.display = 'none'; };
  img.onerror = () => { img.style.display = 'none'; ph.style.display = ''; };
  const live = document.createElement('span'); live.className = 'live'; live.textContent = 'LIVE';
  const up = document.createElement('span'); up.className = 'up';
  t.append(ph, img, live, up);
  const meta = document.createElement('div'); meta.className = 'meta';
  const av = document.createElement('div'); av.className = 'av'; av.textContent = user.slice(0, 1);
  const txt = document.createElement('div');
  const b = document.createElement('b'); b.textContent = user;
  const sp = document.createElement('span'); sp.textContent = 'Streaming live';
  txt.append(b, sp); meta.append(av, txt); a.append(t, meta);
  return {el: a, img, up, b, sp, thumb: 0, started: 0};
}
function tick() { for (const c of Object.values(cards)) c.up.textContent = fmt(Date.now() - c.started); }

function render(list) {
  const seen = new Set(list.map(s => s.user));
  for (const u of Object.keys(cards)) if (!seen.has(u)) { cards[u].el.remove(); delete cards[u]; }
  for (const s of list) {
    let c = cards[s.user];
    if (!c) { c = cards[s.user] = make(s.user); grid.append(c.el); }
    c.started = startedMs(s.id);
    c.b.textContent = s.title || s.user;
    c.sp.textContent = s.title ? s.user : 'Streaming live';
    if (s.thumb && s.thumb !== c.thumb) {
      c.thumb = s.thumb;
      c.img.src = encodeURIComponent(s.user) + '/live/thumb.jpg?t=' + s.thumb;
    }
  }
  countEl.textContent = list.length ? list.length + (list.length === 1 ? ' stream' : ' streams') : '';
  emptyEl.style.display = list.length ? 'none' : 'block';
  tick();
}
async function refresh() {
  try { render((await (await fetch('api/live.json', {cache: 'no-store'})).json()).live); } catch (e) {}
}
refresh(); setInterval(refresh, 5000); setInterval(tick, 1000);
</script>
<!--AUTH-->
</body></html>
"""


AUTH_CSS = r"""#authbar{display:flex;gap:8px;align-items:center}
#authbar button,.dlg button{font:inherit;font-size:13px;padding:6px 12px;border-radius:6px;border:1px solid #3a3a44;background:#1f1f26;color:#efeff1;cursor:pointer}
#authbar button.primary,.dlg button.primary{background:#9147ff;border-color:#9147ff}
.dlg button:disabled{opacity:.5}
.dlg-back{position:fixed;inset:0;background:#000b;display:none;align-items:center;justify-content:center;z-index:50}
.dlg{position:relative;background:#18181b;border:1px solid #2f2f35;border-radius:10px;padding:20px;width:min(380px,92vw);display:flex;flex-direction:column;gap:10px}
.dlg h3{margin:0 0 4px;font-size:18px}
.dlg p{margin:0;font-size:13px;opacity:.8;line-height:1.4}
.dlg input{font:inherit;font-size:15px;padding:9px 10px;border-radius:6px;border:1px solid #3a3a44;background:#0e0e10;color:#efeff1;width:100%}
.dlg input.mono{font-family:ui-monospace,Consolas,monospace;font-size:13px}
.dlg .row{display:flex;gap:8px}
.dlg .x{position:absolute;top:8px;right:8px;padding:2px 9px;font-size:18px}
.dlg .err{color:#ff7a7a;font-size:13px}
.dlg a.sw{color:#7fb0ff;font-size:13px;word-break:break-all}
.dlg .bn-name{flex:1;align-self:center;font-size:14px}
"""
CHAT_CSS = r"""#top{display:flex;align-items:center;justify-content:space-between;padding-right:14px}
#layout{display:flex;align-items:flex-start}
#main{flex:1;min-width:0}
#chat{display:flex;flex-direction:column;width:340px;flex:none;height:100vh;position:sticky;top:0;border-left:1px solid #26262c;background:#121215}
#chat[hidden]{display:none}
#chatHead{padding:12px 14px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;opacity:.7;border-bottom:1px solid #26262c}
#chatLog{flex:1;overflow-y:auto;padding:8px 12px;font-size:14px;line-height:1.35}
.msg{padding:3px 0;word-break:break-word}
.msg .tm{opacity:.45;font-size:11px;margin-right:6px}
.msg .un{font-weight:700;margin-right:6px;color:#b99cff}
.msg .un.host{color:#ff8a65}
.msg .bn{display:none;margin-left:8px;font-size:11px;color:#ff7a7a;cursor:pointer;opacity:.7}
.msg .bn:hover{opacity:1}
#chatLog.host .msg .bn{display:inline}
#chatErr{padding:0 12px 4px;font-size:12px;color:#ff7a7a}
#chatForm{display:flex;gap:6px;padding:10px;border-top:1px solid #26262c}
#chatForm input{flex:1;min-width:0;font:inherit;font-size:14px;padding:8px 10px;border-radius:6px;border:1px solid #3a3a44;background:#0e0e10;color:#efeff1}
#chatForm button{font:inherit;font-size:13px;padding:6px 14px;border-radius:6px;border:0;background:#9147ff;color:#fff;cursor:pointer}
@media (max-width:820px){
  #layout{display:flex;flex-direction:column;align-items:stretch}
  #main{display:contents}
  #chat{order:1;width:auto;height:55vh;position:static;border-left:0;border-top:1px solid #26262c}
  #lists{order:2}
}
"""
AUTH_JS = r"""(function(){
  const bar = document.getElementById('authbar'), back = document.getElementById('dlgBack'), dlg = document.getElementById('dlg');
  let me = {enabled: false, user: null}, loaded = false;
  function el(tag, props, ...kids) {
    const e = document.createElement(tag);
    for (const k in (props || {})) { if (k === 'text') e.textContent = props[k]; else if (k in e) e[k] = props[k]; else e.setAttribute(k, props[k]); }
    e.append(...kids); return e;
  }
  async function api(path, body) {
    const r = await fetch(path, body === undefined ? {cache: 'no-store'} :
      {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    let d = {}; try { d = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(d.error || ('Error ' + r.status));
    return d;
  }
  function emit() { loaded = true; window.dispatchEvent(new CustomEvent('pcauth', {detail: me})); }
  function paintBar() {
    bar.replaceChildren();
    if (!me.enabled) return;
    if (me.user) bar.append(el('button', {text: me.user, onclick: () => open('account')}));
    else bar.append(el('button', {text: 'Log in', onclick: () => open('login')}),
                    el('button', {text: 'Sign up', className: 'primary', onclick: () => open('register')}));
  }
  function close() { back.style.display = 'none'; dlg.replaceChildren(); }
  back.addEventListener('mousedown', e => { if (e.target === back) close(); });
  function open(mode, extra) {
    dlg.replaceChildren();
    const err = el('div', {className: 'err'});
    const x = el('button', {text: '\u00d7', className: 'x', title: 'Close', onclick: close});
    if (mode === 'login' || mode === 'register') {
      const reg = mode === 'register';
      const u = el('input', {placeholder: 'Username', autocomplete: 'username', maxLength: 32});
      const p = el('input', {type: 'password', placeholder: reg ? 'Password (8+ characters)' : 'Password',
                             autocomplete: reg ? 'new-password' : 'current-password', maxLength: 128});
      const go = el('button', {text: reg ? 'Create account' : 'Log in', className: 'primary'});
      const submit = async () => {
        err.textContent = ''; go.disabled = true;
        try {
          const d = await api(reg ? '/api/register' : '/api/login', {username: u.value.trim(), password: p.value});
          me = {enabled: true, user: d.user}; paintBar(); emit();
          if (d.key) open('key', d.key); else close();
          return;
        } catch (e) { err.textContent = e.message; }
        go.disabled = false;
      };
      go.onclick = submit;
      u.addEventListener('keydown', e => { if (e.key === 'Enter') p.focus(); });
      p.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
      dlg.append(x, el('h3', {text: reg ? 'Create an account' : 'Log in'}), u, p, err, go,
        el('a', {href: '#', className: 'sw', text: reg ? 'I already have an account' : 'Create an account',
                 onclick: ev => { ev.preventDefault(); open(reg ? 'login' : 'register'); }}));
      back.style.display = 'flex'; u.focus();
      return;
    }
    if (mode === 'reveal') {
      const p = el('input', {type: 'password', placeholder: 'Your password', autocomplete: 'current-password', maxLength: 128});
      const go = el('button', {text: 'Show stream key', className: 'primary'});
      const submit = async () => {
        err.textContent = ''; go.disabled = true;
        try { open('key', (await api('/api/show-key', {password: p.value})).key); return; }
        catch (e) { err.textContent = e.message; }
        go.disabled = false;
      };
      go.onclick = submit;
      p.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
      dlg.append(x, el('h3', {text: 'Show your stream key'}), el('p', {text: 'Enter your password to see it.'}), p, err, go,
                 el('button', {text: 'Back', onclick: () => open('account')}));
      back.style.display = 'flex'; p.focus();
      return;
    }
    if (mode === 'bans') {
      const list = el('div', {className: 'bans'});
      const u = el('input', {placeholder: 'Username to ban', maxLength: 32});
      const draw = names => {
        list.replaceChildren();
        if (!names.length) list.append(el('p', {text: 'Nobody is banned from your channel.'}));
        for (const n of names) list.append(el('div', {className: 'row'}, el('span', {text: n, className: 'bn-name'}),
          el('button', {text: 'Unban', onclick: async () => {
            try { draw((await api('/api/unban', {username: n})).banned); } catch (e) { err.textContent = e.message; } }})));
      };
      const ban = async () => {
        err.textContent = '';
        try { draw((await api('/api/ban', {username: u.value.trim()})).banned); u.value = ''; } catch (e) { err.textContent = e.message; }
      };
      u.addEventListener('keydown', e => { if (e.key === 'Enter') ban(); });
      dlg.append(x, el('h3', {text: 'Banned viewers'}),
        el('p', {text: 'Banned accounts can no longer see your streams, recordings or chat. The server owner cannot be banned.'}),
        list, el('div', {className: 'row'}, u, el('button', {text: 'Ban', className: 'primary', onclick: ban})), err,
        el('button', {text: 'Back', onclick: () => open('account')}));
      api('/api/bans', {}).then(d => draw(d.banned)).catch(e => { err.textContent = e.message; });
      back.style.display = 'flex';
      return;
    }
    if (mode === 'key') {
      const k = el('input', {value: extra, readOnly: true, className: 'mono'});
      k.onfocus = () => k.select();
      const copy = el('button', {text: 'Copy', onclick: async () => {
        try { await navigator.clipboard.writeText(extra); copy.textContent = 'Copied'; } catch (e) { k.select(); } }});
      dlg.append(x, el('h3', {text: 'Your stream key'}),
        el('p', {text: 'Paste this into the streaming client when it asks for your stream key. You can see it again any time from your account menu (you will need your password). Anyone who has it can stream as you, so keep it private.'}),
        el('div', {className: 'row'}, k, copy),
        el('button', {text: 'Done', className: 'primary', onclick: close}));
    } else {
      const link = location.origin + '/' + encodeURIComponent(me.user) + '/';
      const bans = el('button', {text: 'Banned viewers', onclick: () => open('bans')});
      const show = el('button', {text: 'Show my stream key', onclick: () => open('reveal')});
      const rot = el('button', {text: 'Generate a new stream key', onclick: async () => {
        if (!confirm('This replaces your current key: the streaming client will need the new one. Continue?')) return;
        try { open('key', (await api('/api/rotate-key', {})).key); } catch (e) { err.textContent = e.message; } }});
      const out = el('button', {text: 'Log out', onclick: async () => {
        try { await api('/api/logout', {}); } catch (e) {}
        me = {enabled: true, user: null}; paintBar(); emit(); close(); }});
      dlg.append(x, el('h3', {text: me.user}), el('p', {text: 'Your channel page:'}),
        el('a', {href: link, className: 'sw', text: link}), show, rot, bans, err, out);
    }
    back.style.display = 'flex';
  }
  window.pcAuth = {me: () => me, open, isLoaded: () => loaded};
  api('/api/me').then(d => { me = d; }).catch(() => { me = {enabled: false, user: null}; }).then(() => { paintBar(); emit(); });
})();
"""
CHAT_JS = r"""(function(){
  const box = document.getElementById('chat'), log = document.getElementById('chatLog'), form = document.getElementById('chatForm'),
        input = document.getElementById('chatIn'), errEl = document.getElementById('chatErr');
  const channel = decodeURIComponent(location.pathname.split('/').filter(Boolean)[0] || '');
  let last = 0, started = false, me = {enabled: false, user: null};
  function add(m) {
    if (m.id <= last) return;
    last = m.id;
    const near = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    const d = document.createElement('div'); d.className = 'msg';
    const t = document.createElement('span'); t.className = 'tm';
    t.textContent = new Date(m.ts * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    const u = document.createElement('span'); u.className = 'un' + (m.user === channel ? ' host' : ''); u.textContent = m.user;
    const x = document.createElement('span'); x.textContent = m.text;
    d.dataset.user = m.user;
    d.append(t, u, x);
    if (m.user !== channel) {  // only the channel's streamer sees this button (CSS: #chatLog.host)
      const b = document.createElement('span'); b.className = 'bn'; b.textContent = 'ban'; b.title = 'Ban this user from your channel';
      b.onclick = async () => {
        if (!confirm('Ban ' + m.user + ' from your channel? They will no longer be able to see your streams or chat.')) return;
        try {
          const r = await fetch('/api/ban', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: m.user})});
          const j = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(j.error || ('Error ' + r.status));
          log.querySelectorAll('.msg').forEach(e => { if (e.dataset.user === m.user) e.remove(); });
        } catch (e) { errEl.textContent = e.message; }
      };
      d.append(b);
    }
    log.append(d);
    while (log.children.length > 300) log.firstChild.remove();
    if (near) log.scrollTop = log.scrollHeight;
  }
  async function poll() {
    try {
      const r = await fetch('api/chat?since=' + last, {cache: 'no-store'});
      ((await r.json()).messages || []).forEach(add);
    } catch (e) {}
    setTimeout(poll, document.hidden ? 6000 : 1500);
  }
  function setMe(m) {
    me = m;
    if (!me.enabled) return;
    box.hidden = false;
    log.classList.toggle('host', me.user === channel);
    input.readOnly = !me.user;
    input.placeholder = me.user ? 'Chat as ' + me.user : 'Log in to chat';
    if (!started) { started = true; poll(); }
  }
  input.addEventListener('focus', () => { if (!me.user && window.pcAuth) window.pcAuth.open('login'); });
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    if (!me.user) { window.pcAuth.open('login'); return; }
    errEl.textContent = '';
    try {
      const r = await fetch('api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})});
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || ('Error ' + r.status));
      input.value = ''; add(d.message);
    } catch (e) { errEl.textContent = e.message; }
  });
  window.addEventListener('pcauth', e => setMe(e.detail));
  if (window.pcAuth && window.pcAuth.isLoaded()) setMe(window.pcAuth.me());
})();
"""
AUTH_BLOCK = '<div class="dlg-back" id="dlgBack"><div class="dlg" id="dlg"></div></div>\n<script>' + AUTH_JS + '</script>'
PLAYER_HTML = (PLAYER_HTML.replace("/*AUTHCSS*/", AUTH_CSS).replace("/*CHATCSS*/", CHAT_CSS)
               .replace("<!--AUTH-->", AUTH_BLOCK).replace("/*CHATJS*/", CHAT_JS))
ROOT_HTML = ROOT_HTML.replace("/*AUTHCSS*/", AUTH_CSS).replace("<!--AUTH-->", AUTH_BLOCK)


# --------------------------------------------------------------------------
# Windows audio: WASAPI loopback (PC sound) + optional mic, mixed in Python
# and piped to ffmpeg as raw PCM. Silence is generated when nothing plays,
# because loopback capture delivers no data during silence.
# --------------------------------------------------------------------------
class WinAudioMixer:
    def __init__(self, want_pc, want_mic, pc_name=None, mic_name=None, mic_gain=1.0):
        try:
            import numpy as np
            import pyaudiowpatch as pyaudio
        except ImportError:
            sys.exit("Windows audio needs:  pip install pyaudiowpatch numpy")
        self.np, self.pyaudio = np, pyaudio
        self.pa = pyaudio.PyAudio()
        self.lock = threading.Lock()
        self.bufs = {}
        self.gains = {}
        self.streams = []
        self.stop = False

        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        pc_dev = self._find_loopback(wasapi, pc_name) if want_pc else None
        mic_dev = self._find_mic(wasapi, mic_name) if want_mic else None
        self.rate = int((pc_dev or mic_dev)["defaultSampleRate"])

        if pc_dev:
            print(f"PC audio : {pc_dev['name']}")
            self._open("pc", pc_dev, 1.0)
        if mic_dev:
            print(f"Mic      : {mic_dev['name']}")
            self._open("mic", mic_dev, mic_gain)

    def _find_loopback(self, wasapi, name):
        gen = list(self.pa.get_loopback_device_info_generator())
        if name:
            for d in gen:
                if name.lower() in d["name"].lower():
                    return d
            sys.exit(f"No loopback device matching '{name}'. Use --list-devices.")
        default_out = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        for d in gen:
            if default_out["name"] in d["name"]:
                return d
        if gen:
            return gen[0]
        sys.exit("No WASAPI loopback device found.")

    def _find_mic(self, wasapi, name):
        if name:
            for i in range(self.pa.get_device_count()):
                d = self.pa.get_device_info_by_index(i)
                if (d["hostApi"] == wasapi["index"] and d["maxInputChannels"] > 0
                        and not d.get("isLoopbackDevice") and name.lower() in d["name"].lower()):
                    return d
            sys.exit(f"No microphone matching '{name}'. Use --list-devices.")
        return self.pa.get_device_info_by_index(wasapi["defaultInputDevice"])

    def _open(self, key, dev, gain):
        np = self.np
        ch = int(dev["maxInputChannels"])
        src_rate = int(dev["defaultSampleRate"])
        self.bufs[key] = np.zeros((0, 2), np.float32)
        self.gains[key] = gain

        def cb(in_data, frame_count, time_info, status):
            a = np.frombuffer(in_data, np.int16).reshape(-1, ch).astype(np.float32)
            a = np.repeat(a, 2, axis=1) if ch == 1 else a[:, :2]
            if src_rate != self.rate and len(a):
                n = int(round(len(a) * self.rate / src_rate))
                idx = np.arange(n) * (len(a) / n)
                x = np.arange(len(a))
                a = np.stack([np.interp(idx, x, a[:, c]) for c in range(2)], axis=1).astype(np.float32)
            with self.lock:
                b = np.concatenate([self.bufs[key], a])
                limit = self.rate // 2  # cap buffering at 0.5 s
                self.bufs[key] = b[-limit:] if len(b) > limit else b
            return (None, self.pyaudio.paContinue)

        s = self.pa.open(format=self.pyaudio.paInt16, channels=ch, rate=src_rate, input=True,
                         input_device_index=dev["index"], frames_per_buffer=1024, stream_callback=cb)
        self.streams.append(s)

    def pump(self, out):
        """Write wall-clock-paced mixed PCM to `out` (ffmpeg stdin) until stopped."""
        np = self.np
        start, sent = time.monotonic(), 0
        try:
            while not self.stop:
                due = int((time.monotonic() - start) * self.rate) - sent
                if due < 512:
                    time.sleep(0.005)
                    continue
                mix = np.zeros((due, 2), np.float32)
                with self.lock:
                    for k, b in self.bufs.items():
                        take = b[:due]
                        self.bufs[k] = b[due:]
                        mix[:len(take)] += take * self.gains[k]
                out.write(np.clip(mix, -32768, 32767).astype(np.int16).tobytes())
                out.flush()
                sent += due
        except (BrokenPipeError, OSError, ValueError):
            pass

    def close(self):
        self.stop = True
        for s in self.streams:
            try:
                s.stop_stream()
                s.close()
            except Exception:
                pass
        self.pa.terminate()


def list_devices():
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    if OS == "Windows":
        print("=== Video devices (use with --source webcam --video-dev NAME) ===")
        subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
        try:
            import pyaudiowpatch as pyaudio
            pa = pyaudio.PyAudio()
            print("\n=== PC audio loopback devices (--pc-dev NAME) ===")
            for d in pa.get_loopback_device_info_generator():
                print("  ", d["name"])
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            print("\n=== Microphones (--mic-dev NAME) ===")
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if d["hostApi"] == wasapi["index"] and d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice"):
                    print("  ", d["name"])
        except ImportError:
            print("\n(install pyaudiowpatch numpy to list audio devices)")
    elif OS == "Darwin":
        subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""])
    else:
        print("Video devices:")
        subprocess.run("ls /dev/video* 2>/dev/null", shell=True)
        print("\nAudio sources (PulseAudio/PipeWire):")
        subprocess.run("pactl list short sources", shell=True)


# --------------------------------------------------------------------------
def build_ffmpeg_cmd(a, win_rate=None, base_ts=None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info" if a.debug else "warning"]
    use_audio = a.audio != "none"
    extra_maps = []
    if OS == "Windows" and base_ts:
        # Keep the raw wall-clock timestamps from BOTH inputs so audio and video stay aligned;
        # they are rebased to ~0 below (epoch-sized timestamps break MPEG-TS / hls.js).
        cmd += ["-copyts"]

    if OS == "Windows":
        if a.source == "screen":
            region = []
            if a.region:
                m = re.fullmatch(r"(\d+)x(\d+)\+(\d+)\+(\d+)", a.region.strip())
                if not m:
                    sys.exit("--region must look like 1280x720+0+0  (WIDTHxHEIGHT+X+Y)")
                w, h, x, y = m.groups()
                region = ["-offset_x", x, "-offset_y", y, "-video_size", f"{w}x{h}"]
            cmd += ["-f", "gdigrab", "-probesize", "100M", "-framerate", str(a.fps),
                    "-draw_mouse", "1"] + region + ["-i", "desktop"]
        else:
            if not a.video_dev:
                sys.exit("--video-dev required for webcam (see --list-devices)")
            cmd += ["-f", "dshow", "-use_wallclock_as_timestamps", "1", "-framerate", str(a.fps),
                    "-i", f"video={a.video_dev}"]
        if use_audio:
            cmd += ["-f", "s16le", "-ar", str(win_rate), "-ac", "2", "-thread_queue_size", "512",
                    "-use_wallclock_as_timestamps", "1", "-i", "pipe:0"]

    elif OS == "Darwin":
        # macOS cannot capture system audio natively; install BlackHole and
        # pass its index via --audio-dev (or use a Multi-Output device).
        v = a.video_dev or ("1" if a.source == "screen" else "0")
        au = "none" if not use_audio else (a.audio_dev or "0")
        cmd += ["-f", "avfoundation", "-framerate", str(a.fps), "-capture_cursor", "1", "-i", f"{v}:{au}"]

    else:  # Linux
        if a.source == "screen":
            cmd += ["-f", "x11grab", "-framerate", str(a.fps),
                    "-i", a.video_dev or os.environ.get("DISPLAY", ":0.0")]
        else:
            cmd += ["-f", "v4l2", "-framerate", str(a.fps), "-i", a.video_dev or "/dev/video0"]
        if use_audio:
            if a.audio == "pc":
                cmd += ["-f", "pulse", "-i", a.audio_dev or "@DEFAULT_MONITOR@"]
            elif a.audio == "mic":
                cmd += ["-f", "pulse", "-i", a.audio_dev or "default"]
            else:  # both
                cmd += ["-f", "pulse", "-i", "@DEFAULT_MONITOR@", "-f", "pulse", "-i", "default"]
                cmd += ["-filter_complex", "[1:a][2:a]amix=inputs=2:duration=longest:normalize=0[a]"]
                extra_maps = ["-map", "0:v", "-map", "[a]"]

    rebase_v = rebase_a = ""
    if OS == "Windows" and base_ts:
        rebase_v = f"setpts=PTS-{base_ts:.6f}/TB,"
        rebase_a = f"asetpts=PTS-{base_ts:.6f}/TB,"
    gop = a.fps * a.segment
    # the live playlist has to hold more video than the viewers' delay (+30 s of slack), or nobody could be that far behind
    delay = getattr(a, "delay", 0)
    list_size = max(10, -(-(delay + 30) // a.segment)) if delay else 10
    br_num = int("".join(c for c in a.bitrate if c.isdigit()))
    br_unit = a.bitrate[-1]
    cmd += [
        "-vf", f"{rebase_v}scale=-2:{a.height},scale='min(1920,iw)':-2,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-profile:v", "main", "-level", "4.0", "-r", str(a.fps),
        "-b:v", a.bitrate, "-maxrate", a.bitrate, "-bufsize", f"{br_num * 2}{br_unit}",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
    ]
    if not use_audio:
        cmd += ["-an"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", "-af", f"{rebase_a}aresample=async=1000"]

    # Output paths are relative: ffmpeg runs with cwd = the session folder
    # (this also avoids Windows drive-letter colons inside the tee syntax).
    if extra_maps:
        maps = extra_maps
    else:
        maps = ["-map", "0:v"]
        if use_audio:
            maps += ["-map", "0:a" if OS == "Darwin" else "1:a"]
    cmd += maps

    live_opt = (f"f=hls:hls_time={a.segment}:hls_list_size={list_size}:hls_delete_threshold=5:hls_allow_cache=0:"
                "hls_flags=delete_segments+independent_segments+omit_endlist+append_list:"
                "hls_segment_filename=live/seg_%05d.ts")
    rec_opt = (f"f=hls:hls_time={a.segment}:hls_list_size=0:hls_playlist_type=event:"
               "hls_flags=independent_segments:hls_segment_filename=rec_%05d.ts")
    tee = f"[{live_opt}]live/stream.m3u8"
    if a.local_record:
        tee += f"|[{rec_opt}]vod.m3u8"
    cmd += ["-f", "tee", tee]
    return cmd


DEBUG = False

SESSION_ID = r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}"
VOD_FILE_RE = re.compile(rf"vod/({SESSION_ID})/(vod\.m3u8|rec_\d+\.ts|recording\.mp4)")
LIVE_FILE_RE = re.compile(r"live/(stream\.m3u8|seg_\d+\.ts|thumb\.jpg)")
SESSION_RE = re.compile(SESSION_ID)
INGEST_FILE_RE = re.compile(r"/ingest/([A-Za-z0-9._-]{1,64})/(" + SESSION_ID + r")/live/(stream\.m3u8|seg_\d+\.ts|thumb\.jpg)")
INGEST_END_RE = re.compile(r"/ingest/([A-Za-z0-9._-]{1,64})/(" + SESSION_ID + r")/end")
MAX_UPLOAD = 100 * 1024 * 1024
INGEST_LOCK = threading.Lock()
CONTENT_TYPES = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t", ".mp4": "video/mp4",
                 ".jpg": "image/jpeg"}


META_LOCK = threading.Lock()
_FIN_LOCK = threading.Lock()
_FINALIZING = set()
KEEP_SEGMENTS = False
LIVE_STALE_SECS = 30  # a stream counts as live while its playlist was updated this recently
RESERVED_NAMES = {"api", "clientlog", "index.html", "favicon.ico", "ingest", "login", "register", "account", "admin"}
USER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def valid_username(name):
    return bool(USER_RE.fullmatch(name)) and name not in (".", "..") and name.lower() not in RESERVED_NAMES


# --------------------------------------------------------------------------
# Accounts + live chat (server mode)
# --------------------------------------------------------------------------
ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,30}[A-Za-z0-9]")
WIN_DEVICE_NAMES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
SESSION_COOKIE = "pcs_session"
SESSION_TTL = 30 * 24 * 3600
MAX_ACCOUNTS = 5000
CHAT_MAX_LEN = 300
CHAT_PATH_RE = re.compile(r"/([A-Za-z0-9._-]{1,64})/api/chat")
KDF = "scrypt" if hasattr(hashlib, "scrypt") else "pbkdf2"


BANNED_HTML = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
               "<title>Banned</title><body style='background:#0e0e10;color:#efeff1;font-family:system-ui,sans-serif;"
               "text-align:center;padding:80px 20px'><h2>You have been banned from this channel</h2>"
               "<p><a style='color:#7fb0ff' href='/'>Back to all streams</a></p>")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def hash_password(pw, salt, kdf):
    if kdf == "scrypt":
        return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 300000, dklen=32)


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class Accounts:
    """Usernames, passwords (salted scrypt), login sessions and stream keys, kept in out-dir/.accounts.json.
    Passwords and stream keys are only stored hashed. A stream key identifies its account, so the streaming
    client needs nothing but the key."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.path = out_dir / ".accounts.json"
        self.lock = threading.RLock()
        self.users = {}     # lower-case name -> {name, kdf, salt, pw, key, created}
        self.sessions = {}  # sha256(token) -> {u: lower-case name, exp}
        self.keys = {}      # sha256(stream key) -> lower-case name
        self.fails = {}     # lower-case name -> [failed logins in a row, locked until]
        self.regs = {}      # ip -> [times of recent sign-ups]
        self.owner = ""     # lower-case name of the server owner: can't be banned from any channel
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.users = d.get("users", {})
            self.sessions = d.get("sessions", {})
            self.owner = d["owner"] if isinstance(d.get("owner"), str) else ""
        except (OSError, ValueError, AttributeError):
            pass
        self._reindex()

    def _reindex(self):
        self.keys = {u["key"]: low for low, u in self.users.items() if u.get("key")}

    def _save(self):
        now = time.time()
        self.sessions = {h: s for h, s in self.sessions.items() if s.get("exp", 0) > now}
        tmp = self.path.with_name(self.path.name + ".part")
        tmp.write_text(json.dumps({"owner": self.owner, "users": self.users, "sessions": self.sessions}), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def _dir_taken(self, low):
        try:
            return any(d.name.lower() == low for d in self.out_dir.iterdir())
        except OSError:
            return False

    def _new_session(self, low):
        token = secrets.token_urlsafe(32)
        self.sessions[sha(token)] = {"u": low, "exp": time.time() + SESSION_TTL}
        return token

    def register(self, name, password, ip):
        """-> (name, stream_key, session_token)"""
        name = name.strip() if isinstance(name, str) else ""
        if not (ACCOUNT_RE.fullmatch(name) and valid_username(name)
                and name.split(".")[0].lower() not in WIN_DEVICE_NAMES):
            raise ApiError(400, "Username: 3-32 characters, letters, numbers, dot, dash or underscore, "
                                "starting and ending with a letter or number.")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise ApiError(400, "Password must be 8-128 characters.")
        low = name.lower()
        salt = secrets.token_bytes(16)
        pw = hash_password(password, salt, KDF).hex()
        key = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            recent = [t for t in self.regs.get(ip, []) if now - t < 3600]
            if len(recent) >= 10:
                raise ApiError(429, "Too many accounts created from your address. Try again later.")
            if len(self.users) >= MAX_ACCOUNTS:
                raise ApiError(503, "Sign-ups are closed.")
            if low in self.users or self._dir_taken(low):
                raise ApiError(409, "That username is taken.")
            (self.out_dir / name).mkdir(parents=True, exist_ok=True)  # the channel page exists right away
            self.users[low] = {"name": name, "kdf": KDF, "salt": salt.hex(), "pw": pw, "key": sha(key),
                               "keyv": key, "created": int(now)}
            if not self.owner:
                self.owner = low  # the first account on a new server is the owner
            self.regs[ip] = recent + [now]
            self.keys[sha(key)] = low
            token = self._new_session(low)
            self._save()
        return name, key, token

    def _verify(self, name, password):
        """Check a password (with the wrong-password lockout) -> (lower-case name, account entry)."""
        low = name.strip().lower() if isinstance(name, str) else ""
        if not isinstance(password, str) or len(password) > 128:
            raise ApiError(400, "Wrong username or password.")
        now = time.time()
        with self.lock:
            u = self.users.get(low)
            f = self.fails.get(low)
            if u and f and f[1] > now:
                raise ApiError(429, "Too many wrong passwords. Wait a minute and try again.")
        # hash even for unknown names so the answer takes the same time
        h = hash_password(password, bytes.fromhex(u["salt"]) if u else b"\0" * 16, u["kdf"] if u else KDF)
        ok = bool(u) and hmac.compare_digest(h, bytes.fromhex(u["pw"]))
        with self.lock:
            if not ok:
                if u:
                    n = self.fails.get(low, [0, 0])[0] + 1
                    self.fails[low] = [0, now + 60] if n >= 5 else [n, 0]
                raise ApiError(401, "Wrong username or password.")
            self.fails.pop(low, None)
        return low, u

    def login(self, name, password):
        """-> (session_token, name)"""
        low, u = self._verify(name, password)
        with self.lock:
            token = self._new_session(low)
            self._save()
        return token, u["name"]

    def reveal_key(self, name, password):
        """Show the stream key again, but only to someone who can type the account's password."""
        _, u = self._verify(name, password)
        if not u.get("keyv"):
            raise ApiError(409, "This key was made before keys could be viewed. Generate a new one instead.")
        return u["keyv"]

    def session_user(self, token):
        s = self.sessions.get(sha(token)) if token else None
        if not s or s.get("exp", 0) < time.time():
            return None
        u = self.users.get(s.get("u"))
        return u["name"] if u else None

    def logout(self, token):
        if token:
            with self.lock:
                if self.sessions.pop(sha(token), None):
                    self._save()

    def rotate_key(self, name):
        key = secrets.token_urlsafe(24)
        with self.lock:
            low = name.lower()
            self.users[low]["key"] = sha(key)
            self.users[low]["keyv"] = key  # kept so the owner can view it again (after re-entering their password)
            self._reindex()
            self._save()
        return key

    def is_owner(self, name):
        return bool(name) and name.lower() == self.owner

    def set_owner(self, name):
        with self.lock:
            self.owner = name.lower()
            self._save()

    def is_banned(self, channel, viewer):
        """True if `viewer` (an account name, or None) was banned by this channel's streamer.
        The server owner (stored in .accounts.json) can never be banned, and a streamer is never banned from their own channel."""
        if not viewer:
            return False
        v = viewer.lower()
        if v == self.owner or v == channel.lower():
            return False
        u = self.users.get(channel.lower())
        return bool(u) and v in u.get("bans", [])

    def bans(self, channel):
        u = self.users.get(channel.lower(), {})
        return [self.users[b]["name"] for b in u.get("bans", []) if b in self.users]

    def ban(self, channel, target):
        """The streamer `channel` bans the account `target` from seeing their channel -> target's proper name."""
        low = target.strip().lower() if isinstance(target, str) else ""
        with self.lock:
            t = self.users.get(low)
            if not t:
                raise ApiError(404, "There is no account with that username.")
            if low == channel.lower():
                raise ApiError(400, "You can't ban yourself.")
            if low == self.owner:
                raise ApiError(403, "The server owner can't be banned.")
            bans = self.users[channel.lower()].setdefault("bans", [])
            if low not in bans:
                bans.append(low)
            self._save()
            return t["name"]

    def unban(self, channel, target):
        low = target.strip().lower() if isinstance(target, str) else ""
        with self.lock:
            bans = self.users[channel.lower()].get("bans", [])
            if low in bans:
                bans.remove(low)
                self._save()

    def user_for_key(self, key):
        if not key or len(key) > 200:
            return None
        low = self.keys.get(sha(key))
        return self.users[low]["name"] if low in self.users else None


class Chat:
    """One chat room per channel (= per streamer). Last messages are kept in memory and in <channel>/chat.jsonl."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.lock = threading.Lock()
        self.rooms = {}
        self.last = {}  # account -> time of its last message (rate limit)

    def _room(self, ch):
        r = self.rooms.get(ch)
        if r is None:
            msgs, nxt = collections.deque(maxlen=500), 1
            try:
                for ln in (self.out_dir / ch / "chat.jsonl").read_text(encoding="utf-8").splitlines()[-200:]:
                    try:
                        m = json.loads(ln)
                        msgs.append(m)
                        nxt = max(nxt, m["id"] + 1)
                    except (ValueError, KeyError, TypeError):
                        pass
            except OSError:
                pass
            r = self.rooms[ch] = {"msgs": msgs, "next": nxt}
        return r

    def post(self, ch, user, text):
        text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", text if isinstance(text, str) else "")).strip()
        if not text:
            raise ApiError(400, "Message is empty.")
        if len(text) > CHAT_MAX_LEN:
            raise ApiError(400, f"Messages can be up to {CHAT_MAX_LEN} characters.")
        now = time.time()
        with self.lock:
            if now - self.last.get(user.lower(), 0) < 1.0:
                raise ApiError(429, "Slow down a little.")
            self.last[user.lower()] = now
            room = self._room(ch)
            m = {"id": room["next"], "user": user, "text": text, "ts": int(now)}
            room["next"] += 1
            room["msgs"].append(m)
            p = self.out_dir / ch / "chat.jsonl"
            try:
                with open(p, "a", encoding="utf-8") as f:
                    f.write(json.dumps(m) + "\n")
                if p.stat().st_size > 512 * 1024:  # keep the file small: rewrite with just the recent messages
                    tmp = p.with_name("chat.jsonl.part")
                    tmp.write_text("".join(json.dumps(x) + "\n" for x in list(room["msgs"])[-200:]), encoding="utf-8")
                    os.replace(tmp, p)
            except OSError:
                pass
        return m

    def purge(self, ch, user):
        """Remove everything a (just banned) user said in this channel's chat."""
        with self.lock:
            room = self._room(ch)
            keep = [m for m in room["msgs"] if m["user"].lower() != user.lower()]
            room["msgs"] = collections.deque(keep, maxlen=500)
            p = self.out_dir / ch / "chat.jsonl"
            try:
                tmp = p.with_name("chat.jsonl.part")
                tmp.write_text("".join(json.dumps(x) + "\n" for x in keep[-200:]), encoding="utf-8")
                os.replace(tmp, p)
            except OSError:
                pass

    def since(self, ch, after):
        with self.lock:
            msgs = self._room(ch)["msgs"]
            if after <= 0:
                return list(msgs)[-50:]
            return [m for m in msgs if m["id"] > after][-100:]


class State:
    """What the web server needs to know: where the streams folder is.
    Live streams are discovered from the files, so the capture process and the
    web server can be the same process or two separate ones."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.accounts = None  # Accounts: set in --server mode (sign-up, login, stream keys, ingest auth)
        self.chat = None      # Chat: set in --server mode


def live_session(user_dir):
    """(session_id, live_dir) if this user is streaming right now, else None."""
    try:
        dirs = sorted((d for d in user_dir.iterdir() if d.is_dir() and SESSION_RE.fullmatch(d.name)),
                      key=lambda d: d.name, reverse=True)[:5]
    except OSError:
        return None
    for d in dirs:
        try:
            age = time.time() - (d / "live" / "stream.m3u8").stat().st_mtime
        except OSError:
            continue
        if age < LIVE_STALE_SECS:
            return d.name, d / "live"
    return None


def live_users(out_dir):
    """Every user that is live right now: [{"user": ..., "id": ...}]."""
    out = []
    try:
        users = sorted((d for d in out_dir.iterdir() if d.is_dir() and valid_username(d.name)),
                       key=lambda d: d.name.lower())
    except OSError:
        return out
    for u in users:
        s = live_session(u)
        if s:
            try:
                thumb = int((s[1] / "thumb.jpg").stat().st_mtime)
            except OSError:
                thumb = 0
            out.append({"user": u.name, "id": s[0], "thumb": thumb, "title": read_meta(s[1].parent).get("title", "")})
    return out


def clean_title(t):
    return re.sub(r"[\x00-\x1f\x7f]", "", t or "").strip()[:100]


def read_meta(session_dir):
    try:
        m = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def update_meta(session_dir, **kw):
    with META_LOCK:
        m = read_meta(session_dir)
        if all(m.get(k) == v for k, v in kw.items()):
            return
        m.update(kw)
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            tmp = session_dir / "meta.json.part"
            tmp.write_text(json.dumps(m), encoding="utf-8")
            os.replace(tmp, session_dir / "meta.json")
        except OSError:
            pass


def playlist_info(path):
    """(duration_seconds, has_endlist) for an HLS playlist."""
    dur, finished = 0.0, False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#EXTINF:"):
                    try:
                        dur += float(line[8:].split(",")[0])
                    except ValueError:
                        pass
                elif line.startswith("#EXT-X-ENDLIST"):
                    finished = True
    except OSError:
        pass
    return dur, finished


def list_sessions(user_dir, live_id=None):
    out = []
    try:
        dirs = sorted((d for d in user_dir.iterdir() if d.is_dir() and SESSION_RE.fullmatch(d.name)),
                      key=lambda d: d.name, reverse=True)
    except OSError:
        dirs = []
    for d in dirs:
        if d.name != live_id and (d / "live").exists():  # leftovers of a stream that died
            try:
                stale = time.time() - (d / "live" / "stream.m3u8").stat().st_mtime > 60
            except OSError:
                stale = True
            if stale:
                shutil.rmtree(d / "live", ignore_errors=True)
        vod, mp4 = d / "vod.m3u8", d / "recording.mp4"
        has_hls, has_mp4 = vod.exists(), mp4.exists()
        if not has_hls and not has_mp4:
            continue
        meta = read_meta(d)
        in_progress = d.name == live_id
        if has_hls:
            dur, finished = playlist_info(vod)
        else:  # only the compiled MP4 is left
            dur, finished = float(meta.get("duration") or 0), True
        if has_hls and not finished and not in_progress:
            try:
                still_written = time.time() - vod.stat().st_mtime < 90
            except OSError:
                still_written = False
            if still_written:  # the live check is just lagging (slow upload etc.): still in progress
                in_progress = True
            else:  # the stream died without ending properly: close it and compile it like a normal end
                finalize_async(d, True, bool(shutil.which("ffmpeg")))
        out.append({"id": d.name, "title": meta.get("title", ""), "duration": dur, "in_progress": in_progress,
                    "hls": has_hls, "mp4": has_mp4})
    return out


def ingest_playlist(session_dir, text, record):
    """A pushed live playlist arrived: copy newly finished segments into the recording, drop old live segments."""
    live_dir = session_dir / "live"
    entries = re.findall(r"#EXTINF:([\d.]+),[^\n]*\n(seg_\d+\.ts)", text)
    with INGEST_LOCK:
        if record:
            vod = session_dir / "vod.m3u8"
            if vod.exists():
                have = set(re.findall(r"^(rec_\d+\.ts)\s*$", vod.read_text(errors="replace"), re.M))
            else:
                td = re.search(r"#EXT-X-TARGETDURATION:(\d+)", text)
                vod.write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:%s\n#EXT-X-MEDIA-SEQUENCE:0\n"
                               "#EXT-X-PLAYLIST-TYPE:EVENT\n#EXT-X-INDEPENDENT-SEGMENTS\n" % (td.group(1) if td else "4"))
                have = set()
            add = ""
            for dur, name in entries:
                rec = "rec_" + name[4:]
                if rec in have or not (live_dir / name).exists():
                    continue
                try:
                    shutil.copyfile(live_dir / name, session_dir / (rec + ".part"))
                    os.replace(session_dir / (rec + ".part"), session_dir / rec)
                except OSError:
                    continue
                add += f"#EXTINF:{dur},\n{rec}\n"
                have.add(rec)
            if add:
                with open(vod, "a") as f:
                    f.write(add)
        if entries:  # live window moved on: delete segments older than the playlist's first one
            first = min(int(re.search(r"\d+", n).group()) for _, n in entries)
            for f in live_dir.glob("seg_*.ts"):
                if int(re.search(r"\d+", f.name).group()) < first:
                    try:
                        f.unlink()
                    except OSError:
                        pass


def ingest_end(session_dir, record, audio, make_mp4):
    """The pusher said it's done: stop being live, close the recording (and make the MP4 if possible)."""
    shutil.rmtree(session_dir / "live", ignore_errors=True)
    if not record:
        shutil.rmtree(session_dir, ignore_errors=True)
        return
    finalize_async(session_dir, audio, make_mp4 and bool(shutil.which("ffmpeg")))


def make_handler(state):
    """Routes:  /  = browse page (grid of everyone live)   /<user>/...  = that user's player page, live + recordings."""
    out_dir = state.out_dir
    page = PLAYER_HTML.encode("utf-8")
    root_page = ROOT_HTML.encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_bytes(self, body, ctype, head):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _send_file(self, path, head):
            try:
                size = path.stat().st_size
                f = open(path, "rb")
            except OSError:
                self.send_error(404)
                return
            with f:
                start, end, status = 0, size - 1, 200
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
                if m and (m.group(1) or m.group(2)) and size:
                    if m.group(1):
                        start = int(m.group(1))
                        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                    else:
                        start, end = max(0, size - int(m.group(2))), size - 1
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
                self.send_response(status)
                self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
                self.send_header("Content-Length", str(max(0, end - start + 1)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store" if path.suffix in (".m3u8", ".jpg") or path.parent.name == "live" else "public, max-age=3600")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if head:
                    return
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _handle(self, head):
            try:
                path = urllib.parse.unquote(self.path.split("?", 1)[0])
                if path in ("/", "/index.html"):
                    return self._send_bytes(root_page, "text/html; charset=utf-8", head)
                if path == "/api/live.json":
                    body = json.dumps({"live": [x for x in live_users(out_dir) if not self._banned(x["user"])]}).encode()
                    return self._send_bytes(body, "application/json", head)
                if path == "/api/me":
                    return self._json({"enabled": bool(state.accounts), "user": self._me()}, head=head)

                user, _, rel = path.lstrip("/").partition("/")
                user_dir = out_dir / user
                if not valid_username(user) or not user_dir.is_dir():
                    self.send_error(404)
                    return
                if "/" not in path.lstrip("/"):  # /<user> -> /<user>/ so relative URLs resolve
                    self.send_response(301)
                    self.send_header("Location", f"/{user}/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self._banned(user):  # the streamer banned this viewer: no page, no stream, no recordings, no chat
                    body = BANNED_HTML.encode("utf-8")
                    self.send_response(403)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    if not head:
                        self.wfile.write(body)
                    return
                if rel in ("", "index.html"):
                    return self._send_bytes(page, "text/html; charset=utf-8", head)
                if rel == "api/streams.json":
                    s = live_session(user_dir)
                    lm = read_meta(s[1].parent) if s else {}
                    body = json.dumps({"live": {"id": s[0], "title": lm.get("title", ""), "delay": lm.get("delay", 0),
                                                "buffer": lm.get("buffer", 0)} if s else None,
                                       "recordings": list_sessions(user_dir, s[0] if s else None)}).encode()
                    return self._send_bytes(body, "application/json", head)
                if rel == "api/chat":
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    try:
                        after = int(q.get("since", ["0"])[0])
                    except ValueError:
                        after = 0
                    return self._json({"messages": state.chat.since(user, after) if state.chat else []}, head=head)
                m = LIVE_FILE_RE.fullmatch(rel)
                if m:
                    s = live_session(user_dir)
                    if s:
                        return self._send_file(s[1] / m.group(1), head)
                    self.send_error(404)
                    return
                m = VOD_FILE_RE.fullmatch(rel)
                if m:
                    return self._send_file(user_dir / m.group(1) / m.group(2), head)
                self.send_error(404)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the phone stopped listening; normal for video

        def do_GET(self):
            self._handle(False)

        def do_HEAD(self):
            self._handle(True)

        def _ingest_user(self):
            """The account the X-Stream-Key header belongs to (None = unknown key)."""
            return state.accounts.user_for_key(self.headers.get("X-Stream-Key", "")) if state.accounts else None

        def _ip(self):
            # the last X-Forwarded-For entry is the one the proxy (Tailscale Funnel) itself saw
            xff = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            return xff or self.client_address[0]

        def _json(self, obj, code=200, cookie=None, head=False):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _cookie(self, token=None):
            secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
            age = SESSION_TTL if token else 0
            return f"{SESSION_COOKIE}={token or ''}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}{secure}"

        def _token(self):
            m = re.search(rf"(?:^|;\s*){SESSION_COOKIE}=([A-Za-z0-9_-]{{20,100}})", self.headers.get("Cookie") or "")
            return m.group(1) if m else None

        def _banned(self, channel):
            return bool(state.accounts) and state.accounts.is_banned(channel, self._me())

        def _me(self):
            return state.accounts.session_user(self._token()) if state.accounts else None

        def _body_json(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "Bad request.")
            if n < 0 or n > 4096:
                raise ApiError(413, "Request too large.")
            raw = self.rfile.read(n)
            self._body_read = True
            # JSON content type only: a page on another site can't send that without our permission (CSRF)
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                raise ApiError(415, "Send JSON.")
            try:
                d = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                raise ApiError(400, "Bad JSON.")
            if not isinstance(d, dict):
                raise ApiError(400, "Bad JSON.")
            return d

        def _api_post(self, path):
            """Account + chat endpoints. -> True if the request was one of them (and has been answered)."""
            chat_m = CHAT_PATH_RE.fullmatch(path)
            if not (path.startswith("/api/") or chat_m):
                return False
            self._body_read = False
            try:
                acc = state.accounts
                if not acc:
                    raise ApiError(404, "Accounts aren't enabled on this server.")
                data = self._body_json()
                if path == "/api/register":
                    name, key, token = acc.register(data.get("username"), data.get("password"), self._ip())
                    self._json({"user": name, "key": key}, cookie=self._cookie(token))
                elif path == "/api/login":
                    token, name = acc.login(data.get("username"), data.get("password"))
                    self._json({"user": name}, cookie=self._cookie(token))
                elif path == "/api/logout":
                    acc.logout(self._token())
                    self._json({"ok": True}, cookie=self._cookie(None))
                elif path == "/api/show-key":
                    who = self._me()
                    if not who:
                        raise ApiError(401, "Log in first.")
                    self._json({"key": acc.reveal_key(who, data.get("password"))})
                elif path in ("/api/ban", "/api/unban", "/api/bans"):
                    who = self._me()
                    if not who:
                        raise ApiError(401, "Log in first.")
                    if path == "/api/ban":
                        state.chat.purge(who, acc.ban(who, data.get("username")))
                    elif path == "/api/unban":
                        acc.unban(who, data.get("username"))
                    self._json({"banned": acc.bans(who)})
                elif path == "/api/rotate-key":
                    who = self._me()
                    if not who:
                        raise ApiError(401, "Log in first.")
                    self._json({"key": acc.rotate_key(who)})
                elif chat_m:
                    ch = chat_m.group(1)
                    if not valid_username(ch) or not (out_dir / ch).is_dir():
                        raise ApiError(404, "No such channel.")
                    who = self._me()
                    if not who:
                        raise ApiError(401, "Log in to chat.")
                    if acc.is_banned(ch, who):
                        raise ApiError(403, "You are banned from this channel.")
                    self._json({"message": state.chat.post(ch, who, data.get("text"))})
                else:
                    raise ApiError(404, "Not found.")
            except ApiError as e:
                if not self._body_read:
                    self.close_connection = True  # an unread request body would desync a keep-alive connection
                self._json({"error": e.msg}, e.code)
            except OSError as e:
                print(f"[server] account storage error: {e}")
                self._json({"error": "Server error."}, 500)
            return True

        def _reject(self, code):
            self.close_connection = True  # an unread request body would desync a keep-alive connection
            self.send_error(code)

        def _no_content(self):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_PUT(self):  # a pusher uploading a live file
            path = urllib.parse.unquote(self.path.split("?", 1)[0])
            m = INGEST_FILE_RE.fullmatch(path)
            if not state.accounts or not m or not valid_username(m.group(1)):
                return self._reject(404)
            if self._ingest_user() != m.group(1):  # the key must belong to the account whose channel this is
                return self._reject(403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._reject(400)
            if n > MAX_UPLOAD:
                return self._reject(413)
            user, sid, name = m.groups()
            data = self.rfile.read(n)
            live_dir = out_dir / user / sid / "live"
            live_dir.mkdir(parents=True, exist_ok=True)
            tmp = live_dir / (name + ".part")
            tmp.write_bytes(data)
            os.replace(tmp, live_dir / name)  # atomic: viewers never see half a file
            if name == "stream.m3u8":
                title = clean_title(urllib.parse.unquote(self.headers.get("X-Title", "")))
                if title:
                    update_meta(live_dir.parent, title=title)
                try:
                    delay = max(0, min(MAX_DELAY, int(self.headers.get("X-Delay", "0"))))
                    buf = max(0, min(MAX_DELAY, int(self.headers.get("X-Buffer", "0"))))
                except ValueError:
                    delay = buf = 0
                update_meta(live_dir.parent, delay=delay, buffer=buf)
                ingest_playlist(live_dir.parent, data.decode("utf-8", "replace"),
                                self.headers.get("X-Record", "1") != "0")
            self._no_content()

        def do_POST(self):
            path = urllib.parse.unquote(self.path.split("?", 1)[0])
            if self._api_post(path):
                return
            if path == "/ingest/check":
                if not state.accounts:
                    return self._reject(404)
                who = self._ingest_user()
                return self._json({"user": who}) if who else self._reject(403)
            m = INGEST_END_RE.fullmatch(path)
            if m:
                if not state.accounts or not valid_username(m.group(1)):
                    return self._reject(404)
                if self._ingest_user() != m.group(1):
                    return self._reject(403)
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                ingest_end(out_dir / m.group(1) / m.group(2), self.headers.get("X-Record", "1") != "0",
                           q.get("audio", ["1"])[0] == "1", q.get("mp4", ["1"])[0] == "1")
                return self._no_content()
            if path != "/clientlog" and not re.fullmatch(r"/[A-Za-z0-9._-]{1,64}/clientlog", path):
                self.send_error(404)
                return
            n = min(int(self.headers.get("Content-Length") or 0), 2000)
            print("[phone] " + self.rfile.read(n).decode("utf-8", "replace"))
            self._no_content()

        def log_request(self, code="-", size="-"):
            if DEBUG:
                print(f"[http] {self.command} {self.path} -> {code}")

        def log_message(self, *args):
            pass

    return Handler


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Uploader:
    """Tiny keep-alive HTTP client for talking to a --server instance."""

    def __init__(self, base, key, record=True):
        u = urllib.parse.urlsplit(base if "://" in base else "https://" + base)
        self.https = u.scheme == "https"
        self.host, self.port = u.hostname, u.port or (443 if self.https else 80)
        self.prefix = u.path.rstrip("/")
        self.headers = {"X-Stream-Key": key, "X-Record": "1" if record else "0"}
        self.conn = None
        self.last_error = ""
        self.last_body = b""

    def request(self, method, path, body=b"", ctype="application/octet-stream", retries=3):
        """-> HTTP status, or 0 if the server couldn't be reached. 4xx is returned at once; 5xx/network errors retry."""
        status = 0
        for i in range(retries):
            try:
                if self.conn is None:
                    cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
                    self.conn = cls(self.host, self.port, timeout=30)
                self.conn.request(method, self.prefix + path, body=body, headers=dict(self.headers, **{"Content-Type": ctype}))
                r = self.conn.getresponse()
                self.last_body = r.read()
                status = r.status
                if status < 500:
                    return status
            except Exception as e:
                status = 0
                self.last_error = f"{type(e).__name__}: {e}"
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
            time.sleep(0.5 * (i + 1))
        return status


def push_loop(up, user, sid, live_dir, ff):
    """Upload every finished segment, then the playlist (so the server never lists a segment it doesn't have)."""
    base = f"/ingest/{urllib.parse.quote(user)}/{sid}/live/"
    done, last_pl, last_thumb, fails = set(), None, 0, 0
    while True:
        running = ff.poll() is None  # if ffmpeg had already exited, this pass is the final one
        try:
            text = (live_dir / "stream.m3u8").read_text()
        except OSError:
            text = None
        if text:
            for name in re.findall(r"^(seg_\d+\.ts)\s*$", text, re.M):
                if name in done:
                    continue
                try:
                    data = (live_dir / name).read_bytes()
                except OSError:
                    done.add(name)  # ffmpeg already deleted it (we fell far behind)
                    continue
                st = up.request("PUT", base + name, data)
                if st in (401, 403):
                    print("[!] The server rejected the key - stopping.")
                    ff.terminate()
                    return
                if 200 <= st < 300:
                    fails = 0
                else:
                    fails += 1
                    if fails in (1, 10, 100):
                        print(f"[!] Upload problem (HTTP {st or 'no connection'}); skipping {name}")
                done.add(name)
            if text != last_pl and up.request("PUT", base + "stream.m3u8", text.encode(),
                                              "application/vnd.apple.mpegurl") < 300:
                last_pl = text
        try:
            mt = (live_dir / "thumb.jpg").stat().st_mtime
            if mt != last_thumb:
                up.request("PUT", base + "thumb.jpg", (live_dir / "thumb.jpg").read_bytes(), "image/jpeg", retries=1)
                last_thumb = mt
        except OSError:
            pass
        if not running:
            return
        time.sleep(0.4)


def thumb_loop(live_dir, ff):
    """Every few seconds grab one frame from the newest finished segment -> live/thumb.jpg (the browse-page preview)."""
    tmp, dst = live_dir / "thumb_tmp.jpg", live_dir / "thumb.jpg"
    wait = 1.0
    while ff.poll() is None:
        time.sleep(wait)
        try:
            segs = sorted(live_dir.glob("seg_*.ts"))
            if len(segs) < 2:  # the newest segment is still being written
                continue
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(segs[-2]), "-frames:v", "1",
                                "-vf", "scale=480:-2", "-q:v", "6", str(tmp)],
                               capture_output=True, timeout=20)
            if r.returncode == 0 and tmp.exists():
                os.replace(tmp, dst)
                wait = 8.0
        except Exception:
            pass


def start_server(port, state):
    try:
        server = ThreadedServer(("127.0.0.1", port), make_handler(state))
    except OSError as e:
        sys.exit(f"Can't listen on port {port}: {e}\n"
                 f"If a --server instance is already running there, add --no-serve to this one "
                 f"(or pick another --port).")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def watch_output(live_dir, ff):
    """Tell the user whether ffmpeg is actually producing the stream."""
    t0 = time.time()
    while ff.poll() is None:
        if (live_dir / "stream.m3u8").exists() and any(f.suffix == ".ts" for f in live_dir.iterdir()):
            print("[ok] ffmpeg is producing video segments - the stream is live.")
            return
        if time.time() - t0 > 20:
            print("[!] 20s and ffmpeg still hasn't produced any segments. Run again with --debug "
                  "and send me the output.")
            return
        time.sleep(0.5)
    print(f"[!] ffmpeg exited with code {ff.returncode}")


def stop_ffmpeg(ff):
    """Ask ffmpeg to finish cleanly so the recording playlist gets its end marker."""
    if ff.poll() is not None:
        return
    try:
        if ff.stdin:
            ff.stdin.close()
    except Exception:
        pass
    try:
        ff.send_signal(signal.CTRL_BREAK_EVENT if OS == "Windows" else signal.SIGINT)
    except Exception:
        pass
    try:
        ff.wait(timeout=20)
    except subprocess.TimeoutExpired:
        ff.kill()
        ff.wait()


def finalize_recording(session_dir, with_audio, make_mp4, cleanup=None):
    """Close the recording playlist; make recording.mp4; then delete everything but the MP4."""
    if cleanup is None:
        cleanup = not KEEP_SEGMENTS
    vod = session_dir / "vod.m3u8"
    if not vod.exists():
        return
    if not playlist_info(vod)[1]:
        with open(vod, "a") as f:
            f.write("#EXT-X-ENDLIST\n")
    update_meta(session_dir, duration=playlist_info(vod)[0])
    if not make_mp4:
        return
    print("Saving recording.mp4 ...")
    tmp = session_dir / "recording.part.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", "vod.m3u8", "-c", "copy"]
    if with_audio:
        cmd += ["-bsf:a", "aac_adtstoasc"]
    cmd += ["-movflags", "+faststart", tmp.name]
    ok = False
    try:
        ok = subprocess.run(cmd, cwd=session_dir).returncode == 0 and tmp.exists() and tmp.stat().st_size > 0
    except OSError:
        pass
    if not ok:
        try:
            tmp.unlink()
        except OSError:
            pass
        print("  (couldn't make the MP4; the HLS recording is kept and is still playable in the browser page)")
        return
    os.replace(tmp, session_dir / "recording.mp4")
    if cleanup:  # the MP4 is the full VOD: drop the segments, playlists and live files
        for f in session_dir.glob("rec_*.ts"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            vod.unlink()
        except OSError:
            pass
        shutil.rmtree(session_dir / "live", ignore_errors=True)


def finalize_async(session_dir, with_audio, make_mp4):
    with _FIN_LOCK:
        if session_dir in _FINALIZING:
            return
        _FINALIZING.add(session_dir)

    def run():
        try:
            finalize_recording(session_dir, with_audio, make_mp4)
        finally:
            with _FIN_LOCK:
                _FINALIZING.discard(session_dir)
    threading.Thread(target=run, daemon=True).start()


MAX_DELAY = 300  # seconds


def ask_delay():
    while True:
        s = input(f"Viewer delay in seconds, 0-{MAX_DELAY} (Enter = 0, as live as possible): ").strip()
        if not s:
            return 0
        if s.isdigit() and int(s) <= MAX_DELAY:
            return int(s)
        print(f"  Type a whole number from 0 to {MAX_DELAY}.")


def ask_title():
    return clean_title(input("Stream name (Enter to skip): "))


def ask_username():
    """Ask for the name that becomes the stream's URL path."""
    while True:
        name = input("Choose a username for the stream (becomes the URL path): ").strip()
        if valid_username(name):
            return name
        print("  Use only letters, numbers, dot, dash or underscore (max 64 characters; "
              "not 'api' or 'clientlog').")


def funnel_host():
    """Best-effort: this machine's tailnet DNS name (what Funnel serves under)."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=4).stdout
        return json.loads(out)["Self"]["DNSName"].rstrip(".")
    except Exception:
        return None


def read_secret(prompt):
    """Ask for a secret without echoing it (the terminal may be on the screen that is being streamed)."""
    return getpass.getpass(prompt).strip()


def connect_with_key(push, a):
    """Ask for the stream key, check it with the server -> (Uploader, account name). The key is all that's needed."""
    key = (a.key or "").strip()
    attempts = 0
    while True:
        if not key:
            key = read_secret("Stream key (hidden as you type): ")
            if not key:
                print("  Paste the stream key you got when you created your account.")
                continue
        up = Uploader(push, key, record=not a.no_record)
        st = up.request("POST", "/ingest/check", retries=2)
        if st == 0:
            sys.exit(f"Can't reach {push}\n  reason: {up.last_error}\n"
                     f"  * 'Name or service not known' / 'getaddrinfo failed' = the address is wrong. Tailscale names end in "
                     f".ts.net: run `tailscale funnel status` on the server PC to see its real URL, then use --push URL.\n"
                     f"  * timed out / refused = server isn't running (--server), or Funnel is off (`tailscale funnel --bg PORT`), "
                     f"or you're not on the tailnet.")
        if st == 200:
            try:
                user = json.loads(up.last_body.decode("utf-8"))["user"]
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                user = None
            if isinstance(user, str) and valid_username(user):
                return up, user
            sys.exit("The server answered, but not with an account name. Is it running the new PCStream-Server.py?")
        if st in (401, 403):
            attempts += 1
            if a.key or attempts >= 3 or not sys.stdin.isatty():
                sys.exit("That stream key wasn't accepted. Sign in on the website to see or regenerate your key.")
            print("  That stream key wasn't accepted - try again.")
            key = ""
            continue
        if st == 204:
            sys.exit("This server is running an old version without accounts. Update it to the new PCStream-Server.py.")
        if st == 404:
            sys.exit("That server doesn't accept streams. Is it running with --server, and is the URL right?")
        sys.exit(f"Server answered HTTP {st}.")


def main():
    p = argparse.ArgumentParser(description="Stream PC audio/video to your phone via Tailscale Funnel, with recording")
    p.add_argument("--port", type=int, default=8080, help="local port (default 8080)")
    p.add_argument("--source", choices=["screen", "webcam"], default="screen")
    p.add_argument("--audio", choices=["pc", "mic", "both", "none"], default="pc",
                   help="pc = system sound (default), mic, both, or none")
    p.add_argument("--region", help="Windows screen capture area WxH+X+Y, e.g. 1280x720+0+0 (one monitor only)")
    p.add_argument("--video-dev", help="webcam name (Win), device index (mac), /dev/videoN or DISPLAY (Linux)")
    p.add_argument("--pc-dev", help="Windows: part of the loopback device name (default: default speakers)")
    p.add_argument("--mic-dev", help="Windows: part of the microphone name (default: default mic)")
    p.add_argument("--audio-dev", help="macOS index / Linux pulse source override")
    p.add_argument("--mic-gain", type=float, default=1.0, help="mic volume multiplier when mixing (default 1.0)")
    p.add_argument("--no-audio", action="store_true", help="same as --audio none")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--height", type=int, default=720, help="output height (default 720)")
    p.add_argument("--bitrate", default="2500k", help="video bitrate, e.g. 2500k")
    p.add_argument("--segment", type=int, default=2, help="HLS segment seconds (default 2; 1 = lower latency, less stable)")
    p.add_argument("--user", help="local mode only (no --push): username = URL path of the stream. "
                                  "With --push the name comes from your stream key's account")
    p.add_argument("--title", help="name of this stream, shown on the browse page (asked if you start interactively)")
    p.add_argument("--delay", type=int, default=None, metavar="SECONDS",
                   help="viewers watch this many seconds behind live (0-300): a cushion against hiccups, and they can "
                        "rewind that far. Asked for if you start interactively (Enter = 0). It is a player setting, "
                        "not a secret delay: it does not stop a determined viewer from jumping to the live edge")
    p.add_argument("--buffer", type=int, default=0, metavar="SECONDS",
                   help="how many seconds ahead viewers' players download (default: the player's own, about 30)")
    p.add_argument("--keep-segments", action="store_true",
                   help="keep the HLS segments/playlists after the MP4 is made (default: keep only recording.mp4)")
    p.add_argument("--push", nargs="?", const=DEFAULT_PUSH_URL, default=PUSH_DEFAULT, metavar="URL",
                   help=f"send the stream to a --server instance instead of serving it locally "
                        f"(bare --push = {DEFAULT_PUSH_URL}). The server shows it on its browse page and records it")
    p.add_argument("--key", default=os.environ.get("PCSTREAM_KEY"),
                   help="your stream key (or env PCSTREAM_KEY); asked for, hidden as you type, if missing")
    p.add_argument("--server", action="store_true",
                   help="server mode: only run the website (stream browser, live streams and recordings "
                        "found in --out-dir); no capture, no ffmpeg")
    p.add_argument("--owner", metavar="NAME", help="--server: make this account the server owner (stored in .accounts.json); "
                                              "the owner can't be banned from any channel. Default: the first account that signs up")
    p.add_argument("--no-serve", action="store_true",
                   help="capture only: don't start the web server (use with a separate --server instance)")
    p.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "streams"),
                   help="where recordings are saved (default: a 'streams' folder next to this script)")
    p.add_argument("--no-record", action="store_true", help="don't keep a recording (live only)")
    p.add_argument("--no-mp4", action="store_true", help="skip creating recording.mp4 when the stream ends")
    p.add_argument("--debug", action="store_true", help="verbose ffmpeg output and log every HTTP request")
    p.add_argument("--list-devices", action="store_true")
    a = p.parse_args()
    global DEBUG, KEEP_SEGMENTS
    DEBUG = a.debug
    KEEP_SEGMENTS = a.keep_segments
    if a.no_audio:
        a.audio = "none"

    if a.list_devices:
        return list_devices()
    if a.server and a.no_serve:
        sys.exit("--server and --no-serve contradict each other.")
    if a.server and a.push:
        sys.exit("--server hosts the website; --push sends a stream to one. Use them in different instances.")

    out_dir = Path(a.out_dir).expanduser().resolve()
    state = State(out_dir)
    host = funnel_host() or "<your-machine>.<tailnet>.ts.net"

    # ---------------- server mode: just the website (+ accepts pushed streams) ----------------
    if a.server:
        out_dir.mkdir(parents=True, exist_ok=True)
        state.accounts = Accounts(out_dir)
        state.chat = Chat(out_dir)
        if a.owner:
            if not valid_username(a.owner):
                sys.exit("--owner must be a valid username.")
            state.accounts.set_owner(a.owner)
        server = start_server(a.port, state)
        print(f"\nServer mode: serving {out_dir}")
        print(f"Local:  http://localhost:{a.port}/")
        print(f"\nIn another terminal run:\n    tailscale funnel --bg {a.port}\n    tailscale funnel status")
        print(f"\nBrowse page (grid of everything that is live):\n    https://{host}/")
        print(f"\nAccounts: {len(state.accounts.users)} so far (stored in {out_dir / '.accounts.json'} - keep that file private).")
        o = state.accounts.owner
        print("Owner: " + (o if o else "nobody yet - the first account to sign up becomes the owner (or restart with --owner NAME)")
              + "  (kept in .accounts.json; the owner can't be banned from any channel)")
        print(f"People sign up on the website, get a stream key, then run the client (--push https://{host}) and paste it.")
        print("(or capture on this machine with --no-serve and the same --out-dir)")
        print("Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping ...")
        server.shutdown()
        print(f"Stopped. Run `tailscale funnel --bg {a.port} off` to close the public endpoint.")
        return

    # ---------------- capture mode ----------------
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH. Install it (e.g. `winget install ffmpeg`).")

    push = a.push
    up = None
    if push:
        # the stream key is all we ask for: it identifies the account, so no username or password here
        up, user = connect_with_key(push, a)
        print(f"Logged in as {user}.")
        if a.user and a.user != user:
            print(f"(--user {a.user} is ignored: that stream key belongs to '{user}')")
    else:
        user = a.user or ask_username()
        if not valid_username(user):
            sys.exit("--user may only contain letters, numbers, dot, dash, underscore (max 64; not 'api' or 'clientlog').")

    if a.title is not None:
        title = clean_title(a.title)
    elif sys.stdin.isatty():
        title = ask_title()
    else:
        title = ""

    if a.delay is None:
        a.delay = ask_delay() if sys.stdin.isatty() else 0
    a.delay = max(0, min(MAX_DELAY, a.delay))
    a.buffer = max(0, min(MAX_DELAY, a.buffer))

    if push:
        if title:
            up.headers["X-Title"] = urllib.parse.quote(title)
        up.headers["X-Delay"] = str(a.delay)
        up.headers["X-Buffer"] = str(a.buffer)
        print(f"Connected to {push}")

    mixer = None
    win_rate = None
    if OS == "Windows" and a.audio != "none":
        mixer = WinAudioMixer(a.audio in ("pc", "both"), a.audio in ("mic", "both"),
                              a.pc_dev, a.mic_dev, a.mic_gain)
        win_rate = mixer.rate

    record = not a.no_record
    a.local_record = record and not push  # when pushing, the server makes the recording from what it receives
    sid = time.strftime("%Y-%m-%d_%H-%M-%S")
    # Locally the session folder lives under out-dir/<user>/ so a separate --server process can find the
    # live stream; without recording it is deleted again when the stream ends. When pushing it's a temp folder.
    if push:
        session_dir = Path(tempfile.mkdtemp(prefix="pcstream_")) / sid
    else:
        session_dir = out_dir / user / sid
    live_dir = session_dir / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    if title and not push:
        update_meta(session_dir, title=title)
    if not push and (a.delay or a.buffer):
        update_meta(session_dir, delay=a.delay, buffer=a.buffer)

    server = None
    if push:
        print(f"\nStreaming to {push} as '{user}'" + (" (the server records it)." if record else " (not recorded)."))
        print(f"Watch:\n    {push}/            (all live streams)\n    {push}/{user}/   (your stream + recordings)")
    else:
        if not a.no_serve:
            server = start_server(a.port, state)
            print(f"\nServing on http://localhost:{a.port}/   (your stream: http://localhost:{a.port}/{user}/)")
        if record:
            print(f"Recording to: {session_dir}")
        if a.no_serve:
            print("\n--no-serve: capturing only. Run another instance with --server (same --out-dir) to host the website.")
        else:
            print(f"\nIn another terminal run:\n    tailscale funnel --bg {a.port}\n    tailscale funnel status")
            print(f"\nThen open on your phone:\n    https://{host}/            (all live streams)"
                  f"\n    https://{host}/{user}/   (your live stream + recordings)")
    print("Ctrl+C to stop.\n")

    cmd = build_ffmpeg_cmd(a, win_rate, time.time())
    kwargs = {"cwd": str(session_dir), "stdin": subprocess.PIPE if mixer else None}
    if OS == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # lets us send CTRL_BREAK for a clean stop
    ff = None
    pusher = None
    try:
        ff = subprocess.Popen(cmd, **kwargs)
        if mixer:
            threading.Thread(target=mixer.pump, args=(ff.stdin,), daemon=True).start()
        threading.Thread(target=watch_output, args=(live_dir, ff), daemon=True).start()
        threading.Thread(target=thumb_loop, args=(live_dir, ff), daemon=True).start()
        if push:
            pusher = threading.Thread(target=push_loop, args=(up, user, sid, live_dir, ff), daemon=True)
            pusher.start()
        # Poll instead of ff.wait(): on Windows a plain wait() isn't interrupted by Ctrl+C until ffmpeg exits.
        while ff.poll() is None:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nCtrl+C - ending the stream ...")
    finally:
        # From here on a second Ctrl+C must not cut the clean-up short (that would leave the stream half-ended).
        try:
            signal.signal(signal.SIGINT, lambda *_: print("  Already ending the stream - one moment ..."))
        except (ValueError, OSError):
            pass
        if mixer:
            mixer.close()
        if ff is not None:
            stop_ffmpeg(ff)
        if push:
            if pusher is not None:
                print("Finishing upload ...")
                pusher.join(timeout=120)
            if ff is not None:  # tell the server the stream is over: it stops showing it as live and saves the recording
                up.request("POST", f"/ingest/{urllib.parse.quote(user)}/{sid}/end"
                                   f"?audio={int(a.audio != 'none')}&mp4={int(not a.no_mp4)}")
            shutil.rmtree(session_dir.parent, ignore_errors=True)
            print("\nStream ended.")
        else:
            shutil.rmtree(live_dir, ignore_errors=True)
            if record and ff is not None:
                finalize_recording(session_dir, a.audio != "none", not a.no_mp4)
            else:
                shutil.rmtree(session_dir, ignore_errors=True)
            if server:
                server.shutdown()
                print(f"\nStopped. Run `tailscale funnel --bg {a.port} off` to close the public endpoint.")
            else:
                print("\nStopped.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)