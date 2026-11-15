#!/usr/bin/env python3
"""
Bulk Video Downloader: a small web app that runs on this Mac.

Paste TikTok, Instagram, or YouTube links, drop in a Google Sheet link, or upload a CSV/Excel file, and
every video is saved in the best quality available to ~/Downloads/Videos/<date time>/.

Run:  python3 server.py      then open http://127.0.0.1:8765
"""

import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import shutil

import requests

PORT = 8765
HERE = Path(__file__).resolve().parent
# Where small state files live. The Mac app points this at Application Support.
DATA_DIR = Path(os.environ.get("VBD_DATA") or HERE)
OUT_ROOT = Path.home() / "Downloads" / "Videos"
WORKERS = 3

# Apps launched from Finder get a bare PATH, so make sure ffmpeg and rclone can be found.
os.environ["PATH"] = os.pathsep.join(
    [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
     os.environ.get("PATH", "")])

RCLONE = shutil.which("rclone") or str(Path.home() / ".local/bin/rclone")


def find_ffmpeg():
    """System ffmpeg if there is one, otherwise the copy bundled with imageio-ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()

# ---------------------------------------------------------------- Google Sheet subscription
# Google Sheet import is $4.99/month. Payments and the Stripe key live on the
# website's server; this app only keeps the subscriber's key and asks that
# server whether it's still active.
LICENSE_API = os.environ.get("VBD_API", "https://video-downloader-site.vercel.app/api").rstrip("/")
LICENSE_FILE = DATA_DIR / "license.json"
RECHECK_S = 6 * 3600      # ask the server at most every 6 hours
OFFLINE_GRACE_S = 7 * 86400  # keep working offline for a week after the last good check


def load_license() -> dict:
    try:
        return json.loads(LICENSE_FILE.read_text())
    except Exception:
        return {}


def save_license(lic: dict):
    LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LICENSE_FILE.write_text(json.dumps(lic))


def sheets_unlocked(force: bool = False) -> bool:
    lic = load_license()
    if not lic.get("key"):
        return False
    now = time.time()
    if not force and now - lic.get("checked", 0) < RECHECK_S:
        return bool(lic.get("active"))
    try:
        r = requests.get(f"{LICENSE_API}/license", params={"key": lic["key"]}, timeout=10)
        r.raise_for_status()
        lic.update(active=bool(r.json().get("active")), checked=now)
        if lic["active"]:
            lic["last_ok"] = now
        save_license(lic)
        return lic["active"]
    except Exception:
        return bool(lic.get("active")) and now - lic.get("last_ok", 0) < OFFLINE_GRACE_S


def license_url(path: str, payload: dict = None) -> str:
    try:
        r = requests.post(f"{LICENSE_API}/{path}", json=payload or {}, timeout=20)
        data = r.json()
        if r.ok and data.get("url"):
            return data["url"]
    except Exception:
        pass
    raise ValueError("Couldn't reach checkout. Check your internet and try again.")


def finish_checkout(session_id: str) -> str:
    """Called when Stripe sends the buyer back. Returns where to send them next."""
    try:
        r = requests.get(f"{LICENSE_API}/license", params={"session_id": session_id}, timeout=15)
        data = r.json()
    except Exception:
        return "/?sheets=pending"
    if not data.get("key"):
        return "/?sheets=pending"
    now = time.time()
    save_license({"key": data["key"], "active": bool(data.get("active")), "checked": now,
                  **({"last_ok": now} if data.get("active") else {})})
    return "/?sheets=unlocked" if data.get("active") else "/?sheets=pending"

VIDEO_RE = re.compile(
    # TikTok
    r"https?://(?:(?:www|m)\.)?tiktok\.com/(?:@[\w.-]+/(?:video|photo)/\d+|t/\w+|v/\d+)"
    r"|https?://(?:vm|vt)\.tiktok\.com/\w+"
    # Instagram (reels, posts, stories, TV)
    r"|https?://(?:www\.)?instagram\.com/(?:p|reel|tv|stories|igtv)/[\w-]+"
    # YouTube (standard, shorts, youtu.be)
    r"|https?://(?:www\.)?youtube\.com/watch\?v=[\w-]+"
    r"|https?://(?:www\.)?youtube\.com/shorts/[\w-]+"
    r"|https?://youtu\.be/[\w-]+", re.I)


def find_links(text: str) -> list:
    """Every TikTok video link in some text, in order, without duplicates."""
    seen, out = set(), []
    for m in VIDEO_RE.finditer(text or ""):
        url = m.group(0)
        key = url.lower().rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(url)
    return out


# ---------------------------------------------------------------- sheets / files

def drive_token() -> str:
    """Access token for the Google account connected through rclone."""
    subprocess.run([RCLONE, "about", "gdrive:"], capture_output=True, timeout=60)
    dump = subprocess.run([RCLONE, "config", "dump"], capture_output=True,
                          text=True, check=True).stdout
    return json.loads(json.loads(dump)["gdrive"]["token"])["access_token"]


def fetch_sheet_export(sheet_id: str, fmt: str, gid: str = None) -> bytes:
    """Export a sheet. Tries it as a public sheet first, then as the user's own."""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    params = {"format": fmt}
    if gid:
        params["gid"] = gid

    def ok(r):
        return r.status_code == 200 and "text/html" not in r.headers.get("content-type", "")

    r = requests.get(url, params=params, timeout=60)
    if ok(r):
        return r.content
    try:
        token = drive_token()
    except Exception:
        token = None
    if token:
        r = requests.get(url, params=params, timeout=60,
                         headers={"Authorization": f"Bearer {token}"})
        if ok(r):
            return r.content
    raise ValueError("Couldn't open that sheet. Set sharing to \"Anyone with the link\", "
                     "or connect Google Drive (see the README) and share it with that account.")


def links_from_xlsx(data: bytes) -> list:
    """Links from cell text and from hyperlinks hidden behind cell text."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=False, data_only=True)
    parts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None:
                    parts.append(str(cell.value))
                if cell.hyperlink is not None and cell.hyperlink.target:
                    parts.append(cell.hyperlink.target)
    return find_links("\n".join(parts))


def links_from_sheet(url: str) -> list:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url or "")
    if not m:
        raise ValueError("That doesn't look like a Google Sheet link.")
    sheet_id = m.group(1)
    gid = re.search(r"[#&?]gid=(\d+)", url)
    # The tab that's open in the link first.
    data = fetch_sheet_export(sheet_id, "csv", gid.group(1) if gid else None)
    links = find_links(data.decode("utf-8-sig", "replace"))
    if links:
        return links
    # Nothing as plain text: the links may be hidden behind cell text, so look at
    # the hyperlinks in the whole workbook.
    return links_from_xlsx(fetch_sheet_export(sheet_id, "xlsx"))


def links_from_file(name: str, data: bytes) -> list:
    if name.lower().endswith((".xlsx", ".xlsm")):
        return links_from_xlsx(data)
    return find_links(data.decode("utf-8-sig", "replace"))


# ---------------------------------------------------------------- downloading

JOBS = {}
UPDATED = threading.Event()  # downloads wait for the daily downloader update
POOL = ThreadPoolExecutor(max_workers=WORKERS)


def tidy_error(stderr: str) -> str:
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    err = next((l for l in reversed(lines) if l.startswith("ERROR:")), "")
    low = err.lower()
    if not err:
        return "Couldn't download this one"
    if "photo" in low or "no video formats" in low:
        return "Photo slideshow, not a video"
    if ("not available" in low or "404" in low or "status code 10204" in low
            or "ip address is blocked" in low):
        return "Video unavailable (deleted, private, or blocked in your country)"
    if re.search(r"\blog ?in\b|\bsign in\b|\bage\b", low):
        return "Needs a login (private or age-restricted)"
    if "unsupported url" in low or "unable to extract" in low:
        return "Link not recognised as a TikTok video"
    if "429" in low or "rate" in low:
        return "TikTok slowed us down, hit Retry in a minute"
    msg = re.sub(r"^ERROR:\s*(\[[^]]+\]\s*)?", "", err.split(";")[0])
    return msg[:120]


def download_one(job: dict, item: dict):
    UPDATED.wait(timeout=120)
    item.update(status="downloading", error=None)
    template = str(Path(job["folder"]) / f"{item['n']:03d} - %(uploader)s - %(id)s.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist", "--no-progress",
        # biggest resolution first; at equal resolution prefer h264 (plays everywhere)
        "-S", "res,vcodec:h264,br",
        # "download" is TikTok's own watermarked copy, so never pick it
        "-f", "bv*[format_id!=download]+ba/b[format_id!=download]/bv*+ba/b",
        "--merge-output-format", "mp4", "--trim-filenames", "140",
        "--retries", "5", "--fragment-retries", "5",
        *(["--ffmpeg-location", FFMPEG] if FFMPEG else []),
        "-o", template,
        "--print", "after_move:%(.{filepath,uploader,title,thumbnail,width,height})j",
        item["url"],
    ]
    for attempt in range(2):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            break
        time.sleep(3)
    if proc.returncode != 0:
        item.update(status="failed", error=tidy_error(proc.stderr))
        return
    try:
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        path = Path(info["filepath"])
        item.update(
            status="done",
            file=str(path),
            name=path.name,
            creator=info.get("uploader") or "",
            title=(info.get("title") or "")[:90],
            thumb=info.get("thumbnail") or "",
            res=f"{info.get('width')}x{info.get('height')}" if info.get("height") else "",
            size=path.stat().st_size,
        )
    except Exception:
        item.update(status="failed", error="Download finished but the file is missing")


def start_job(links: list) -> dict:
    stamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    folder = OUT_ROOT / stamp
    folder.mkdir(parents=True, exist_ok=True)
    job = {
        "id": uuid.uuid4().hex[:10],
        "folder": str(folder),
        "items": [{"n": i + 1, "url": u, "status": "queued"} for i, u in enumerate(links)],
    }
    JOBS[job["id"]] = job
    for item in job["items"]:
        POOL.submit(download_one, job, item)
    return job


def retry_failed(job: dict):
    job.pop("drive", None)  # newly saved videos can then be sent to Drive too
    for item in job["items"]:
        if item["status"] == "failed":
            item.update(status="queued", error=None)
            POOL.submit(download_one, job, item)


def save_to_drive(job: dict):
    """Copy the job's saved videos into Google Drive and record the folder link."""
    name = Path(job["folder"]).name
    dest = f"gdrive:Videos/{name}"
    job["drive"] = {"status": "uploading"}
    try:
        proc = subprocess.run([RCLONE, "copy", job["folder"], dest, "--include", "*.mp4"],
                              capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            raise RuntimeError("Couldn't upload to Google Drive. Check your internet and try again.")
        listing = subprocess.run([RCLONE, "lsjson", "--dirs-only", "gdrive:Videos"],
                                 capture_output=True, text=True, timeout=120).stdout
        folder = next(d for d in json.loads(listing) if d["Name"] == name)
        job["drive"] = {"status": "done",
                        "url": f"https://drive.google.com/drive/folders/{folder['ID']}"}
    except Exception as e:
        msg = str(e) if isinstance(e, RuntimeError) else "Couldn't upload to Google Drive. Try again."
        job["drive"] = {"status": "failed", "error": msg}


def update_ytdlp_daily():
    """TikTok changes often; keep the downloader fresh (at most once a day)."""
    stamp = DATA_DIR / ".last_update"
    try:
        if stamp.exists() and time.time() - stamp.stat().st_mtime < 86400:
            return
        subprocess.run([sys.executable, "-m", "pip", "install", "-U", "--quiet", "yt-dlp"],
                       capture_output=True, timeout=110)
        stamp.touch()
    except Exception:
        pass
    finally:
        UPDATED.set()


# ---------------------------------------------------------------- web server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/ping":
            self.send_json({"ok": True})
        elif path == "/api/plan":
            self.send_json({"sheets": sheets_unlocked(), "price": "$4.99/month"})
        elif path == "/unlocked":
            session_id = (parse_qs(urlparse(self.path).query).get("session_id") or [""])[0]
            self.send_response(302)
            self.send_header("Location", finish_checkout(session_id) if session_id else "/")
            self.end_headers()
        elif path.startswith("/api/job/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                return self.send_json({"error": "Unknown job"}, 404)
            self.send_json(job)
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/sheet":
                url = json.loads(self.body()).get("url", "")
                if not sheets_unlocked():
                    return self.send_json(
                        {"error": "Google Sheet import needs a subscription ($4.99/month)."}, 402)
                self.send_json({"links": links_from_sheet(url)})
            elif path == "/api/unlock":
                self.send_json({"url": license_url("checkout")})
            elif path == "/api/manage":
                key = load_license().get("key")
                if not key:
                    return self.send_json({"error": "No subscription on this Mac."}, 400)
                self.send_json({"url": license_url("portal", {"key": key})})
            elif path == "/api/file":
                name = self.headers.get("X-Filename", "file.csv")
                self.send_json({"links": links_from_file(name, self.body())})
            elif path == "/api/start":
                links = find_links("\n".join(json.loads(self.body()).get("links", [])))
                if not links:
                    return self.send_json({"error": "No TikTok links to download."}, 400)
                self.send_json(start_job(links))
            elif path.startswith("/api/retry/"):
                job = JOBS.get(path.rsplit("/", 1)[-1])
                if job:
                    retry_failed(job)
                self.send_json({"ok": bool(job)})
            elif path.startswith("/api/drive/"):
                job = JOBS.get(path.rsplit("/", 1)[-1])
                if not job:
                    return self.send_json({"error": "Unknown job"}, 404)
                if (job.get("drive") or {}).get("status") != "uploading":
                    threading.Thread(target=save_to_drive, args=(job,), daemon=True).start()
                    job["drive"] = {"status": "uploading"}
                self.send_json({"ok": True})
            elif path == "/api/reveal":
                target = Path(json.loads(self.body()).get("path", "")).resolve()
                if OUT_ROOT.resolve() not in [target, *target.parents]:
                    return self.send_json({"error": "Not allowed"}, 403)
                # a file is highlighted in Finder, a folder is opened
                subprocess.run(["open", "-R", str(target)] if target.is_file()
                               else ["open", str(target)])
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "Not found"}, 404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": f"Something went wrong: {e}"}, 500)


def main():
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=update_ytdlp_daily, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"TikTok Bulk Downloader running at http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
