import os
import shutil
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from yt_dlp import YoutubeDL
from urllib.parse import urlparse
import subprocess
import tempfile
import uuid
import re
import time
import threading

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

app = Flask(__name__)

APP_DIR = Path(__file__).resolve().parent
INPUT = APP_DIR / "input"
OUTPUT = APP_DIR / "output"

INPUT.mkdir(parents=True, exist_ok=True)
OUTPUT.mkdir(parents=True, exist_ok=True)

PROGRESS = {
    "stage": "idle",
    "percent": 0,
    "clip_percent": 0,
    "speed": 0,
    "eta": None,
    "downloaded": 0,
    "total": 0,
    "clip_index": 0,
    "clip_count": 0,
    "message": "Ready"
}

PROGRESS_LOCK = threading.Lock()

QUALITIES = {
    "source": None,
    "2160": 2160,
    "1440": 1440,
    "1080": 1080,
    "720": 720,
    "480": 480,
    "360": 360
}


def update_progress(**kwargs):
    with PROGRESS_LOCK:
        PROGRESS.update(kwargs)


def valid_youtube_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return (
            host == "youtu.be"
            or host == "youtube.com"
            or host.endswith(".youtube.com")
        )
    except Exception:
        return False


def to_seconds(value):
    parts = str(value).strip().split(":")
    if not all(p.isdigit() for p in parts):
        raise ValueError("Use HH:MM:SS or MM:SS")

    nums = [int(p) for p in parts]
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0, nums[0], nums[1]
    elif len(nums) == 1:
        h, m, s = 0, 0, nums[0]
    else:
        raise ValueError("Invalid timestamp")

    if m >= 60 or s >= 60:
        raise ValueError("Invalid timestamp")

    return h * 3600 + m * 60 + s


def run_ffmpeg_with_progress(cmd, duration_sec, clip_index, total_clips, estimated_total_bytes):
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1
    )

    start_wall = time.time()
    out_time_sec = 0.0
    total_size = 0

    for line in process.stdout:
        line = line.strip()
        if "=" not in line:
            continue

        key, val = line.split("=", 1)

        if key == "total_size" and val.isdigit():
            total_size = int(val)
        elif key in ("out_time_us", "out_time_ms") and val.isdigit():
            out_time_sec = int(val) / 1_000_000.0

        if key == "progress":
            elapsed = max(0.1, time.time() - start_wall)
            speed_bps = total_size / elapsed if total_size > 0 else 0

            clip_pct = min(99.9, (out_time_sec / duration_sec) * 100) if duration_sec > 0 else 0
            if val == "end":
                clip_pct = 100.0

            overall_pct = round(((clip_index - 1) + (clip_pct / 100.0)) / total_clips * 100, 1)

            remaining_sec = max(0.0, duration_sec - out_time_sec)
            processing_rate = out_time_sec / elapsed if out_time_sec > 0 else 0
            eta_sec = int(remaining_sec / processing_rate) if processing_rate > 0 else None

            est_total = (
                int(total_size / (clip_pct / 100.0))
                if clip_pct > 5 and total_size > 0
                else estimated_total_bytes
            )

            update_progress(
                stage="downloading",
                percent=overall_pct,
                clip_percent=round(clip_pct, 1),
                speed=speed_bps,
                eta=0 if val == "end" else eta_sec,
                downloaded=total_size,
                total=est_total,
                clip_index=clip_index,
                message=f"Extracting clip {clip_index}/{total_clips} ({round(clip_pct)}%)"
            )

    stderr_output = process.stderr.read()
    process.wait()

    if process.returncode != 0:
        raise RuntimeError("FFmpeg error: " + stderr_output[-800:])


@app.route("/")
def home():
    return send_from_directory(APP_DIR, "index.html")


@app.route("/api/progress")
def get_progress():
    with PROGRESS_LOCK:
        return jsonify(PROGRESS.copy())


@app.route("/api/cut", methods=["POST"])
def cut_video():
    data = request.get_json(silent=True) or {}

    url = data.get("url", "").strip()
    quality = data.get("quality", "source")
    clips = data.get("clips", [])

    if not valid_youtube_url(url):
        return jsonify(error="Valid YouTube URL required"), 400

    if quality not in QUALITIES:
        return jsonify(error="Invalid quality"), 400

    if not isinstance(clips, list) or not clips or len(clips) > 30:
        return jsonify(error="Add 1 to 30 timestamp ranges"), 400

    try:
        ranges = []
        for clip in clips:
            if not isinstance(clip, dict) or "start" not in clip or "end" not in clip:
                raise ValueError("Each clip requires start and end timestamps")

            start = to_seconds(clip["start"])
            end = to_seconds(clip["end"])
            if end <= start:
                raise ValueError("End must be after start")
            ranges.append((start, end))

        job_id = uuid.uuid4().hex[:10]
        workdir = Path(tempfile.mkdtemp(prefix="vinaash_", dir=INPUT))

        height = QUALITIES[quality]
        if height is None:
            fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        else:
            fmt = (
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}]/best"
            )

        total_clips = len(ranges)

        update_progress(
            stage="starting",
            percent=2,
            clip_percent=0,
            speed=0,
            eta=None,
            downloaded=0,
            total=0,
            clip_index=0,
            clip_count=total_clips,
            message="Resolving YouTube stream URLs..."
        )

        # Android/iOS client bypass for cloud hosting (Render)
        ydl_opts = {
            "format": fmt,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios", "web"],
                    "player_skip": ["webpage", "configs"],
                }
            },
        }

        if (APP_DIR / "cookies.txt").is_file():
            ydl_opts["cookiefile"] = str(APP_DIR / "cookies.txt")

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        requested = info.get("requested_formats")
        if requested and len(requested) >= 2:
            video_fmt, audio_fmt = requested[0], requested[1]
            video_url = video_fmt["url"]
            audio_url = audio_fmt["url"]
            http_headers = video_fmt.get("http_headers") or info.get("http_headers") or {}
            bitrate_kbps = (video_fmt.get("tbr") or 0) + (audio_fmt.get("tbr") or 0)
        else:
            video_url = info["url"]
            audio_url = None
            http_headers = info.get("http_headers") or {}
            bitrate_kbps = info.get("tbr") or 0

        header_lines = "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())
        results = []

        for index, (start, end) in enumerate(ranges, 1):
            duration = end - start
            estimated_bytes = int((bitrate_kbps * 1000 / 8) * duration) if bitrate_kbps else 0

            filename = f"{job_id}_clip_{index:02d}.mp4"
            destination = OUTPUT / filename
            temporary_output = workdir / f"final_{index:02d}.mp4"

            update_progress(
                stage="downloading",
                clip_index=index,
                clip_percent=0,
                downloaded=0,
                total=estimated_bytes,
                message=f"Starting clip {index}/{total_clips}..."
            )

            cmd = [
                FFMPEG,
                "-y",
                "-loglevel", "error",
                "-progress", "pipe:1",
                "-nostats",
            ]

            if header_lines:
                cmd += ["-headers", header_lines]
            cmd += ["-ss", str(start), "-to", str(end), "-i", video_url]

            if audio_url:
                if header_lines:
                    cmd += ["-headers", header_lines]
                cmd += ["-ss", str(start), "-to", str(end), "-i", audio_url]
                cmd += ["-map", "0:v:0", "-map", "1:a:0"]
            else:
                cmd += ["-map", "0:v:0", "-map", "0:a?"]

            cmd += [
                "-c", "copy",
                "-movflags", "+faststart",
                str(temporary_output)
            ]

            run_ffmpeg_with_progress(
                cmd=cmd,
                duration_sec=duration,
                clip_index=index,
                total_clips=total_clips,
                estimated_total_bytes=estimated_bytes
            )

            shutil.move(str(temporary_output), str(destination))

            results.append({
                "filename": filename,
                "url": f"/download/{filename}"
            })

            update_progress(
                percent=round(index / total_clips * 100, 1),
                clip_percent=100,
                clip_index=index,
                message=f"Completed clip {index}/{total_clips}"
            )

        update_progress(
            stage="completed",
            percent=100,
            clip_percent=100,
            speed=0,
            eta=0,
            message="All clips exported successfully!"
        )

        return jsonify(
            success=True,
            message="All clips exported!",
            clips=results
        )

    except Exception as error:
        update_progress(
            stage="error",
            speed=0,
            message=str(error)
        )
        return jsonify(error=str(error)), 500

    finally:
        if "workdir" in locals():
            shutil.rmtree(workdir, ignore_errors=True)


@app.route("/download/<filename>")
def download(filename):
    if not re.fullmatch(r"[a-f0-9]{10}_clip_[0-9]{2}\.mp4", filename):
        return jsonify(error="Invalid filename"), 400

    return send_from_directory(
        OUTPUT,
        filename,
        as_attachment=True
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True
    )
