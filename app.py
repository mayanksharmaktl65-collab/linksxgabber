import os, re, glob, time, uuid, threading
from flask import Flask, request, jsonify, send_file
import yt_dlp

app = Flask(__name__)
TMP = "/tmp/dl"
os.makedirs(TMP, exist_ok=True)
FILES = {}
GATE = threading.Semaphore(2)


@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return r


def cleaner():
    while True:
        time.sleep(120)
        now = time.time()
        for k, (p, n, t) in list(FILES.items()):
            if now - t > 1200:
                FILES.pop(k, None)
                try:
                    os.remove(p)
                except OSError:
                    pass


threading.Thread(target=cleaner, daemon=True).start()


def err(code):
    return jsonify({"status": "error", "error": {"code": code}})


@app.route("/", methods=["GET", "POST", "OPTIONS"])
def root():
    if request.method == "OPTIONS":
        return ("", 204)
    if request.method == "GET":
        return jsonify({"server": "ytdlp-grabber", "ok": True, "yt_dlp": yt_dlp.version.__version__})
    d = request.get_json(silent=True) or {}
    url = (d.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return err("link.invalid")
    audio = d.get("downloadMode") == "audio"
    q = re.sub(r"\D", "", str(d.get("videoQuality") or "1080")) or "1080"
    br = re.sub(r"\D", "", str(d.get("audioBitrate") or "128")) or "128"
    fid = uuid.uuid4().hex[:12]
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "outtmpl": f"{TMP}/{fid}.%(ext)s", "socket_timeout": 30,
        "restrictfilenames": True, "retries": 3,
    }
    if audio:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": br}]
    else:
        opts["format"] = (f"bv*[height<={q}][ext=mp4]+ba[ext=m4a]/bv*[height<={q}]+ba/"
                          f"b[height<={q}]/b")
        opts["merge_output_format"] = "mp4"
    if not GATE.acquire(timeout=60):
        return err("api.busy")
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
        if info and "entries" in info and info["entries"]:
            info = info["entries"][0]
        want = ".mp3" if audio else ".mp4"
        found = [p for p in glob.glob(f"{TMP}/{fid}.*") if not p.endswith((".part", ".ytdl"))]
        if not found:
            return err("fetch.empty")
        path = next((p for p in found if p.endswith(want)), max(found, key=os.path.getsize))
        ext = os.path.splitext(path)[1]
        title = re.sub(r"[^\w\-]+", "_", (info or {}).get("title") or "video")[:60].strip("_") or "video"
        name = f"{title}{ext}"
        FILES[fid] = (path, name, time.time())
        proto = request.headers.get("X-Forwarded-Proto", "https")
        base = f"{proto}://{request.host}/"
        return jsonify({"status": "tunnel", "url": f"{base}file/{fid}", "filename": name})
    except yt_dlp.utils.DownloadError as e:
        m = str(e).lower()
        if "unsupported url" in m:
            return err("link.unsupported")
        if "sign in" in m or "bot" in m or "login" in m or "cookies" in m:
            return err("youtube.login")
        if "private" in m or "unavailable" in m or "removed" in m or "not found" in m:
            return err("content.unavailable")
        return err("fetch.failed")
    except Exception:
        return err("fetch.failed")
    finally:
        GATE.release()


@app.route("/file/<fid>")
def file(fid):
    it = FILES.get(fid)
    if not it or not os.path.exists(it[0]):
        return err("file.expired"), 404
    return send_file(it[0], as_attachment=True, download_name=it[1])
