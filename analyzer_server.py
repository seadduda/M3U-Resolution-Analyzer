from __future__ import annotations

import glob
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8767
UA = "VLC/3.0.20 LibVLC/3.0.20"
MAX_LIST_BYTES = 300 * 1024 * 1024
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

lock = threading.Lock()
plock = threading.Lock()
procs: set = set()
stop_event = threading.Event()
state = {"name": "", "channels": [], "version": 0, "running": False, "done": 0, "target": 0}


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    pattern = os.path.join(local, "Microsoft", "WinGet", "Packages", "**", name + ".exe")
    hits = glob.glob(pattern, recursive=True)
    return hits[0] if hits else None


FFPROBE = find_tool("ffprobe")
FFMPEG = find_tool("ffmpeg")


# ---------- M3U parsing ----------

def parse_m3u(text: str) -> list[dict]:
    channels = []
    extinf = None
    extra: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            extinf = line
            extra = []
        elif line.startswith("#"):
            if not line.upper().startswith("#EXTM3U"):
                extra.append(line)
        else:
            name = line
            group = ""
            if extinf:
                name = extinf.rsplit(",", 1)[-1].strip() or line
                m = re.search(r'group-title="([^"]*)"', extinf, re.IGNORECASE)
                group = m.group(1).strip() if m else ""
            channels.append({
                "id": len(channels), "name": name, "group": group, "url": line,
                "extinf": extinf, "extra": extra,
                "vod": "/movie/" in line or "/series/" in line,
                "status": "pending", "vcodec": "", "acodec": "", "w": 0, "h": 0, "fps": 0.0,
                "ms": None, "mbps": None, "err": "", "res": "",                 "cat": "",
            })
            extinf = None
            extra = []
    return channels


def category(ch: dict) -> str:
    if not ch["w"] or not ch["h"]:
        return "AUDIO"
    if ch["h"] >= 2160 or ch["w"] >= 3840:
        return "4K"
    if ch["h"] >= 1080 or ch["w"] >= 1920:
        return "FHD"
    if ch["h"] >= 720 or ch["w"] >= 1280:
        return "HD"
    return "SD"


# ---------- checking ----------

def run_proc(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         creationflags=NO_WINDOW)
    with plock:
        procs.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise
    finally:
        with plock:
            procs.discard(p)
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def last_line(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1][:160] if lines else ""


def parse_fps(value: str) -> float:
    try:
        a, b = value.split("/")
        return round(float(a) / float(b), 2) if float(b) else 0.0
    except (ValueError, AttributeError):
        return 0.0


def probe(url: str, timeout: float) -> dict:
    cmd = [FFPROBE, "-v", "error", "-user_agent", UA, "-rw_timeout", str(int(timeout * 1_000_000)),
           "-probesize", "3000000", "-analyzeduration", "3000000",
           "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,bit_rate",
           "-of", "json", url]
    started = time.monotonic()
    try:
        code, out, err = run_proc(cmd, timeout + 5)
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": "timeout", "ms": int((time.monotonic() - started) * 1000)}
    ms = int((time.monotonic() - started) * 1000)
    try:
        streams = json.loads(out).get("streams", []) if out.strip() else []
    except ValueError:
        streams = []
    if code != 0 or not streams:
        return {"ok": False, "err": last_line(err) or "nema streamova", "ms": ms}
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("width")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    result = {"ok": True, "ms": ms, "acodec": (audio or {}).get("codec_name", "")}
    if video:
        fps = parse_fps(video.get("avg_frame_rate", "")) or parse_fps(video.get("r_frame_rate", ""))
        result.update(vcodec=video.get("codec_name", ""), w=int(video["width"]), h=int(video["height"]), fps=fps)
        bit_rate = video.get("bit_rate")
        if bit_rate and str(bit_rate).isdigit():
            result["mbps"] = round(int(bit_rate) / 1_000_000, 2)
    return result


def measure_mbps(url: str, seconds: float, timeout: float) -> float | None:
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-user_agent", UA, "-rw_timeout", str(int(timeout * 1_000_000)),
           "-t", str(seconds), "-i", url, "-map", "0:v:0?", "-map", "0:a?", "-c", "copy", "-f", "null", "-"]
    try:
        _, _, err = run_proc(cmd, seconds + timeout + 5)
    except subprocess.TimeoutExpired:
        return None
    sizes = re.search(r"video:(\d+)\w+\s+audio:(\d+)\w+", err)
    times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not sizes or not times:
        return None
    h, m, s = times[-1]
    duration = int(h) * 3600 + int(m) * 60 + float(s)
    if duration < 0.5:
        return None
    total_bits = (int(sizes.group(1)) + int(sizes.group(2))) * 1024 * 8
    return round(total_bits / duration / 1_000_000, 2)


def check_channel(ch: dict, settings: dict) -> None:
    if stop_event.is_set():
        return
    with lock:
        ch["status"] = "checking"
        state["version"] += 1
    timeout = settings["timeout"]
    result = probe(ch["url"], timeout)
    mbps = result.get("mbps")
    if result["ok"] and settings["measure"] and not stop_event.is_set():
        measured = measure_mbps(ch["url"], settings["sample"], timeout)
        if measured is not None:
            mbps = measured
    with lock:
        if stop_event.is_set():
            ch["status"] = "pending"
        else:
            ch["ms"] = result["ms"]
            if result["ok"]:
                ch.update(status="ok", vcodec=result.get("vcodec", ""), acodec=result.get("acodec", ""),
                          w=result.get("w", 0), h=result.get("h", 0), fps=result.get("fps", 0.0),
                          mbps=mbps, err="")
                ch["res"] = f"{ch['w']}x{ch['h']}" if ch["w"] else "bez videa"
                ch["cat"] = category(ch)
            else:
                ch.update(status="fail", err=result["err"], res="", cat="")
            state["done"] += 1
        state["version"] += 1


def run_checks(targets: list[dict], settings: dict) -> None:
    with ThreadPoolExecutor(max_workers=settings["workers"]) as pool:
        for ch in targets:
            pool.submit(check_channel, ch, settings)
    with lock:
        state["running"] = False
        state["version"] += 1


def start_checks(options: dict) -> dict:
    if not FFPROBE:
        return {"error": "ffprobe nije pronađen. Instaliraj: winget install Gyan.FFmpeg.Essentials"}
    settings = {
        "workers": max(1, min(int(options.get("workers", 5)), 40)),
        "timeout": max(3.0, min(float(options.get("timeout", 15)), 120)),
        "measure": bool(options.get("measure", True)) and bool(FFMPEG),
        "sample": max(2.0, min(float(options.get("sample", 5)), 30)),
    }
    group = options.get("group") or ""
    wanted = {"pending", "fail"} if options.get("retry") else {"pending"}
    with lock:
        if state["running"]:
            return {"error": "Provjera je već pokrenuta"}
        targets = [c for c in state["channels"]
                   if c["status"] in wanted
                   and (not options.get("skipVod", True) or not c["vod"])
                   and (not group or c["group"] == group)]
        if not targets:
            return {"error": "Nema kanala za provjeru (svi su već provjereni ili filtrirani)"}
        for c in targets:
            c["status"] = "pending"
        stop_event.clear()
        state.update(running=True, done=0, target=len(targets))
        state["version"] += 1
    threading.Thread(target=run_checks, args=(targets, settings), daemon=True).start()
    return {"started": True, "target": len(targets)}


def stop_checks() -> None:
    stop_event.set()
    with plock:
        for p in list(procs):
            try:
                p.kill()
            except OSError:
                pass


# ---------- loading ----------

def load_text(text: str, name: str) -> dict:
    channels = parse_m3u(text)
    if not channels:
        return {"error": "Lista nema prepoznatljivih kanala"}
    with lock:
        if state["running"]:
            return {"error": "Zaustavi provjeru prije učitavanja nove liste"}
        state.update(name=name, channels=channels, done=0, target=0)
        state["version"] += 1
    return {"total": len(channels), "name": name}


def clear_list() -> dict:
    stop_checks()
    deadline = time.monotonic() + 10
    while state["running"] and time.monotonic() < deadline:
        time.sleep(0.1)
    with lock:
        if state["running"]:
            return {"error": "Provjera se još zaustavlja, pokušaj ponovo"}
        state.update(name="", channels=[], done=0, target=0)
        state["version"] += 1
    return {"cleared": True}


def fetch_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as response:
        data = response.read(MAX_LIST_BYTES)
        charset = response.headers.get_content_charset() or "utf-8"
    return data.decode(charset, errors="replace")


# ---------- export ----------

def safe_name(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", value).strip() or "lista"


CODECS = {"H265": ("hevc", "h265"), "H264": ("h264",)}


def codec_group(ch: dict) -> str:
    for label, names in CODECS.items():
        if ch["vcodec"] in names:
            return label
    return "OTHER"


def select(key: str) -> list[dict]:
    with lock:
        chans = list(state["channels"])
    ok = [c for c in chans if c["status"] == "ok"]
    parts = key.split(":")
    if parts[0] == "cc" and len(parts) == 3:
        _, codec, cat = parts
        return [c for c in ok if codec_group(c) == codec and (cat == "ALL" or c["cat"] == cat)]
    if key == "all":
        return ok
    return []


def build_m3u(chans: list[dict], tag: bool) -> str:
    out = ["#EXTM3U"]
    for c in chans:
        extinf = c["extinf"] or f"#EXTINF:-1,{c['name']}"
        if tag and c["status"] == "ok" and c["res"]:
            parts = [c["res"], (c["vcodec"] or "").upper()]
            if c["mbps"] is not None:
                parts.append(f"{c['mbps']} Mbps")
            head, sep, title = extinf.rpartition(",")
            extinf = f"{head}{sep}{title} [{' '.join(p for p in parts if p)}]"
        out.append(extinf)
        out.extend(c["extra"])
        out.append(c["url"])
    return "\n".join(out) + "\n"


def build_zip(tag: bool) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for codec in ("H265", "H264"):
            whole = select(f"cc:{codec}:ALL")
            if not whole:
                continue
            archive.writestr(f"{codec}/{codec}_SVE.m3u", build_m3u(whole, tag))
            for cat in ("4K", "FHD", "HD", "SD"):
                items = select(f"cc:{codec}:{cat}")
                if items:
                    archive.writestr(f"{codec}/{cat}_{codec}.m3u", build_m3u(items, tag))
    return buffer.getvalue()


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def send_bytes(self, body: bytes, content_type: str, status: int = 200, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict, status: int = 200) -> None:
        self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8", status)

    def read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            self.send_bytes((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif parsed.path == "/api/state":
            since = int(query.get("v", ["-1"])[0])
            with lock:
                payload = {"version": state["version"], "running": state["running"], "name": state["name"],
                           "total": len(state["channels"]), "done": state["done"], "target": state["target"],
                           "tools": {"ffprobe": bool(FFPROBE), "ffmpeg": bool(FFMPEG)}}
                if since != state["version"]:
                    payload["channels"] = [
                        {k: c[k] for k in ("id", "name", "group", "status", "vcodec", "acodec", "w", "h", "fps",
                                           "ms", "mbps", "err", "res", "cat", "vod")}
                        for c in state["channels"]]
            self.send_json(payload)
        elif parsed.path == "/api/export":
            key = query.get("key", [""])[0]
            tag = query.get("tag", ["0"])[0] == "1"
            items = select(key)
            if not items:
                self.send_json({"error": "Nema kanala za taj izbor"}, 404)
                return
            parts = key.split(":")
            filename = f"{parts[2]}_{parts[1]}.m3u" if len(parts) == 3 else safe_name(key) + ".m3u"
            self.send_bytes(build_m3u(items, tag).encode("utf-8"), "audio/x-mpegurl; charset=utf-8",
                            filename=filename)
        elif parsed.path == "/api/export.zip":
            tag = query.get("tag", ["0"])[0] == "1"
            name = safe_name(Path(state["name"]).stem or "lista")
            self.send_bytes(build_zip(tag), "application/zip", filename=f"{name}_po_rezoluciji.zip")
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/load-file":
                if int(self.headers.get("Content-Length", "0")) > MAX_LIST_BYTES:
                    self.send_json({"error": "Fajl je prevelik"}, 413)
                    return
                name = urllib.parse.unquote(self.headers.get("X-Filename", "lista.m3u"))
                self.send_json(load_text(self.read_body().decode("utf-8", errors="replace"), name))
            elif self.path == "/api/load-url":
                url = json.loads(self.read_body() or b"{}").get("url", "").strip()
                if not url.lower().startswith(("http://", "https://")):
                    self.send_json({"error": "Unesi ispravan http(s) URL"}, 400)
                    return
                self.send_json(load_text(fetch_url(url), urllib.parse.urlsplit(url).netloc))
            elif self.path == "/api/start":
                self.send_json(start_checks(json.loads(self.read_body() or b"{}")))
            elif self.path == "/api/clear":
                self.send_json(clear_list())
            elif self.path == "/api/stop":
                stop_checks()
                self.send_json({"stopped": True})
            else:
                self.send_error(404)
        except Exception as error:
            self.send_json({"error": str(getattr(error, "reason", error))[:200]}, 400)


if __name__ == "__main__":
    print(f"ffprobe: {FFPROBE or 'NIJE PRONAĐEN'}")
    print(f"ffmpeg:  {FFMPEG or 'NIJE PRONAĐEN (mjerenje Mbps nedostupno)'}")
    print(f"Dashboard: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
