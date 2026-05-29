"""ffmpeg transcode logic: probe, decide, build args, run with progress."""

from __future__ import annotations

import json
import logging
import os
import re
import select
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".mpg", ".mpeg",
              ".flv", ".webm", ".wmv", ".ts", ".m2ts", ".vob"}

SUB_KEEP_CODECS = {"subrip", "srt", "ass", "ssa", "hdmv_pgs_subtitle",
                   "dvd_subtitle", "dvb_subtitle", "webvtt"}
SUB_CONVERT = {"mov_text": "srt"}

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


@dataclass
class Probe:
    path: Path
    duration_sec: float
    container: str
    video: dict
    audios: list[dict]
    subs: list[dict]
    raw: dict = field(repr=False)

    @property
    def height(self) -> int:
        return int(self.video.get("height") or 0)

    @property
    def video_codec(self) -> str:
        return (self.video.get("codec_name") or "").lower()

    @property
    def video_bitrate_bps(self) -> int:
        """Best-effort bitrate. Falls back to file_size/duration if stream tag missing."""
        if br := self.video.get("bit_rate"):
            try:
                return int(br)
            except ValueError:
                pass
        bps_tag = (self.video.get("tags") or {}).get("BPS")
        if bps_tag:
            try:
                return int(bps_tag)
            except ValueError:
                pass
        fmt_br = (self.raw.get("format") or {}).get("bit_rate")
        if fmt_br:
            try:
                return int(fmt_br)
            except ValueError:
                pass
        return 0

    @property
    def is_hdr(self) -> bool:
        transfer = (self.video.get("color_transfer") or "").lower()
        return transfer in HDR_TRANSFERS

    @property
    def fps(self) -> float:
        """Source video frame rate. Used to convert ffmpeg's frame counter
        into a content-time pct (out_time_us is unreliable on multi-stream
        encodes in ffmpeg 7.x -- often reported as 'N/A' for the whole run)."""
        for key in ("avg_frame_rate", "r_frame_rate"):
            v = self.video.get(key)
            if not v or "/" not in str(v):
                continue
            num, den = str(v).split("/", 1)
            try:
                n, d = int(num), int(den)
                if d > 0 and n > 0:
                    return n / d
            except ValueError:
                continue
        return 24.0

    @property
    def total_frames(self) -> int:
        return max(1, int(self.duration_sec * self.fps))

    def find_default_english_audio_idx(self) -> int | None:
        """Returns the index (within self.audios) of the first audio stream
        tagged English. None if no English track exists."""
        for i, s in enumerate(self.audios):
            lang = ((s.get("tags") or {}).get("language") or "").lower()
            if lang.startswith("eng") or lang == "en":
                return i
        return None


def probe(path: Path) -> Probe:
    """Run ffprobe and return a Probe."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and (s.get("disposition") or {}).get("attached_pic", 0) == 0
                  and s.get("codec_name") not in ("mjpeg", "png")), None)
    if not video:
        raise RuntimeError(f"No video stream in {path}")
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    fmt = data.get("format") or {}
    duration = float(fmt.get("duration") or 0.0)
    return Probe(
        path=path,
        duration_sec=duration,
        container=(fmt.get("format_name") or "").split(",")[0],
        video=video,
        audios=audios,
        subs=subs,
        raw=data,
    )


def maxrate_for_video(p: Probe) -> tuple[int, int]:
    """Returns (maxrate_kbps, bufsize_kbps) sized for the larger video
    dimension, so cinemascope 4K (e.g. 3840x1608) gets the 4K tier rather
    than being lumped in with 1080p. Thresholds are set just under the
    canonical longest-side of each tier to handle slightly-cropped masters."""
    longest = max(int(p.video.get("width") or 0), int(p.video.get("height") or 0))
    if longest >= 3200:  # 4K UHD (3840 wide)
        return 20_000, 40_000
    if longest >= 1700:  # 1080p (1920 wide)
        return 8_000, 16_000
    if longest >= 1200:  # 720p (1280 wide)
        return 4_000, 8_000
    return 2_000, 4_000


def needs_transcode(p: Probe) -> tuple[bool, str]:
    """Decide whether this file needs transcoding. Returns (needs, reason)."""
    if p.container != "matroska" and p.path.suffix.lower() != ".mkv":
        return True, f"container={p.container}, want mkv"
    if p.video_codec not in ("hevc", "vp9"):
        return True, f"codec={p.video_codec}, want hevc/vp9"
    cap_kbps, _ = maxrate_for_video(p)
    bitrate_kbps = p.video_bitrate_bps // 1000
    if bitrate_kbps > cap_kbps * 1.1:
        return True, f"bitrate={bitrate_kbps}kbps > cap={cap_kbps}kbps"
    return False, "already hevc/vp9 mkv within bitrate cap"


def build_ffmpeg_cmd(
    p: Probe,
    output_path: Path,
    preset: str = "fast",
    crf: int = 22,
) -> list[str]:
    """Construct the full ffmpeg command for transcoding p to output_path."""
    maxrate_kbps, bufsize_kbps = maxrate_for_video(p)

    cmd: list[str] = [
        "ffmpeg", "-y", "-hide_banner",
        "-i", str(p.path),
        "-map_metadata", "0",
        "-map_chapters", "0",
        # Video: first non-cover video stream
        "-map", f"0:{p.video['index']}",
    ]

    # Audio: all audio streams in original order
    for a in p.audios:
        cmd += ["-map", f"0:{a['index']}"]

    # Subtitles: keep compatible ones (per-stream mapping below also sets codec)
    kept_subs: list[tuple[int, str]] = []  # (input_index, output_codec)
    for s in p.subs:
        codec = (s.get("codec_name") or "").lower()
        if codec in SUB_KEEP_CODECS:
            kept_subs.append((s["index"], "copy"))
        elif codec in SUB_CONVERT:
            kept_subs.append((s["index"], SUB_CONVERT[codec]))
        # else: dropped (not mapped)

    for in_idx, _ in kept_subs:
        cmd += ["-map", f"0:{in_idx}"]

    # Video codec settings
    cmd += [
        "-c:v", "libx265",
        "-preset", preset,
        "-crf", str(crf),
    ]

    # VBV cap: pass via -x265-params so libx265 actually enforces it.
    # ffmpeg's top-level -maxrate/-bufsize don't always propagate to libx265
    # under CRF mode; vbv-maxrate/vbv-bufsize go straight to the encoder.
    x265_params: list[str] = [
        f"vbv-maxrate={maxrate_kbps}",
        f"vbv-bufsize={bufsize_kbps}",
    ]

    # HDR10 preservation (libx265 needs explicit hdr-opt + color params)
    if p.is_hdr:
        cmd += [
            "-pix_fmt", "yuv420p10le",
            "-color_primaries", (p.video.get("color_primaries") or "bt2020"),
            "-color_trc", (p.video.get("color_transfer") or "smpte2084"),
            "-colorspace", (p.video.get("color_space") or "bt2020nc"),
            "-color_range", (p.video.get("color_range") or "tv"),
        ]
        x265_params += [
            "hdr-opt=1",
            "repeat-headers=1",
            f"colorprim={p.video.get('color_primaries') or 'bt2020'}",
            f"transfer={p.video.get('color_transfer') or 'smpte2084'}",
            f"colormatrix={p.video.get('color_space') or 'bt2020nc'}",
        ]
        master_disp = _extract_master_display(p.video)
        if master_disp:
            x265_params.append(f"master-display={master_disp}")
        max_cll = _extract_max_cll(p.video)
        if max_cll:
            x265_params.append(f"max-cll={max_cll}")
    else:
        cmd += ["-pix_fmt", "yuv420p"]

    if x265_params:
        cmd += ["-x265-params", ":".join(x265_params)]

    # Audio: copy (no quality loss)
    cmd += ["-c:a", "copy"]

    # Subtitle codecs per output sub index
    for out_idx, (_, codec) in enumerate(kept_subs):
        cmd += [f"-c:s:{out_idx}", codec]

    # English audio default disposition
    eng_idx = p.find_default_english_audio_idx()
    if eng_idx is not None and p.audios:
        cmd += ["-disposition:a", "0"]
        cmd += [f"-disposition:a:{eng_idx}", "default"]

    # Strip the embedded title so Jellyfin shows the filename
    cmd += ["-metadata", "title="]

    cmd += [
        "-max_muxing_queue_size", "9999",
        "-progress", "pipe:1",
        "-nostats",
        str(output_path),
    ]
    return cmd


def _extract_master_display(video_stream: dict) -> str | None:
    """Extract x265 master-display string from ffprobe side_data_list."""
    for sd in video_stream.get("side_data_list", []) or []:
        if sd.get("side_data_type") == "Mastering display metadata":
            # ffprobe gives values as fractions like "13250/50000"
            try:
                def f(k):
                    return _frac_to_int(sd[k], scale=50000)
                r = f("red_x"), f("red_y")
                g = f("green_x"), f("green_y")
                b = f("blue_x"), f("blue_y")
                wp = f("white_point_x"), f("white_point_y")
                # luminance scaled to 10000 per x265 spec
                lmin = _frac_to_int(sd["min_luminance"], scale=10000)
                lmax = _frac_to_int(sd["max_luminance"], scale=10000)
                return (f"G({g[0]},{g[1]})B({b[0]},{b[1]})R({r[0]},{r[1]})"
                        f"WP({wp[0]},{wp[1]})L({lmax},{lmin})")
            except (KeyError, ValueError, ZeroDivisionError):
                return None
    return None


def _extract_max_cll(video_stream: dict) -> str | None:
    """Extract x265 max-cll string from ffprobe side_data_list."""
    for sd in video_stream.get("side_data_list", []) or []:
        if sd.get("side_data_type") == "Content light level metadata":
            try:
                return f"{int(sd['max_content'])},{int(sd['max_average'])}"
            except (KeyError, ValueError):
                return None
    return None


def _frac_to_int(value, scale: int) -> int:
    """Convert ffprobe fractional string (e.g. '13250/50000') to scaled int."""
    if isinstance(value, (int, float)):
        return int(round(float(value) * scale))
    s = str(value)
    if "/" in s:
        num, den = s.split("/", 1)
        return int(round(int(num) * scale / int(den)))
    return int(round(float(s) * scale))


@dataclass
class ProgressUpdate:
    frame: int = 0
    fps: float = 0.0
    out_time_us: int = 0  # microseconds processed
    speed: float = 0.0    # 1.0 = realtime
    bitrate: str = ""
    total_size: int = 0
    done: bool = False


def run_ffmpeg(
    cmd: list[str],
    on_progress: Callable[[ProgressUpdate], None],
    on_stderr_line: Callable[[str], None] | None = None,
) -> int:
    """Run ffmpeg, streaming progress key=value pairs from stdout."""
    log.info("ffmpeg: %s", " ".join(_shellish(a) for a in cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        text=True,
    )
    assert proc.stdout and proc.stderr
    current = ProgressUpdate()
    # Watchdog: kill the process if neither the frame counter nor total_size
    # has advanced in STUCK_TIMEOUT_SEC. We can't rely on out_time_us alone
    # because ffmpeg 7.x reports it as 'N/A' for the entire run on many
    # multi-stream encodes (video + audio + subs). frame and total_size both
    # advance under normal operation; a hang freezes both.
    STUCK_TIMEOUT_SEC = 900  # 15 minutes
    last_advance = time.time()
    last_frame = 0
    last_total_size = 0
    killed_for_stuck = False
    try:
        # Use select to multiplex stdout (progress) + stderr (log lines).
        # Drop fds as they hit EOF so we don't busy-loop on closed pipes.
        fds = [proc.stdout, proc.stderr]
        while fds:
            ready, _, _ = select.select(fds, [], [], 0.5)
            if not ready:
                if proc.poll() is not None:
                    break
                if time.time() - last_advance > STUCK_TIMEOUT_SEC:
                    log.error("ffmpeg stuck (frame=%d total_size=%d) for >%ds, killing",
                              last_frame, last_total_size, STUCK_TIMEOUT_SEC)
                    killed_for_stuck = True
                    proc.kill()
                    break
                continue
            for f in ready:
                line = f.readline()
                if not line:
                    fds.remove(f)
                    continue
                if f is proc.stdout:
                    if "=" in line:
                        k, _, v = line.strip().partition("=")
                        if k == "frame":
                            current.frame = _safe_int(v)
                        elif k == "fps":
                            current.fps = _safe_float(v)
                        elif k == "out_time_us" or k == "out_time_ms":
                            # ffmpeg's out_time_ms is actually microseconds (named badly).
                            # Treat "N/A" (common in multi-stream encodes) as "no update"
                            # rather than overwriting with 0.
                            new_ot = _safe_int(v)
                            if new_ot > 0:
                                current.out_time_us = new_ot
                        elif k == "speed":
                            current.speed = _safe_float(v.rstrip("x"))
                        elif k == "bitrate":
                            current.bitrate = v.strip()
                        elif k == "total_size":
                            current.total_size = _safe_int(v)
                        elif k == "progress":
                            current.done = (v.strip() == "end")
                            on_progress(current)
                            # Mark progress as advancing if either signal moved.
                            if (current.frame > last_frame
                                    or current.total_size > last_total_size):
                                last_frame = current.frame
                                last_total_size = current.total_size
                                last_advance = time.time()
                            current = ProgressUpdate(frame=current.frame,
                                                    fps=current.fps,
                                                    out_time_us=current.out_time_us,
                                                    speed=current.speed,
                                                    bitrate=current.bitrate,
                                                    total_size=current.total_size,
                                                    done=current.done)
                elif on_stderr_line is not None:
                    on_stderr_line(line.rstrip("\n"))
            # Watchdog check after handling a batch of progress lines too,
            # so we don't have to wait for a select timeout to notice.
            if time.time() - last_advance > STUCK_TIMEOUT_SEC:
                log.error("ffmpeg stuck (frame=%d total_size=%d) for >%ds, killing",
                          last_frame, last_total_size, STUCK_TIMEOUT_SEC)
                killed_for_stuck = True
                proc.kill()
                break
    finally:
        proc.wait()
    rc = proc.returncode
    # Surface the stuck-kill as a distinct non-zero return so the worker can
    # log a useful error_msg rather than a generic ffmpeg failure.
    if killed_for_stuck and rc != 0:
        rc = 124  # convention: 124 = watchdog timeout (matches GNU `timeout`)
    return rc


def verify_output(src: Probe, dst_path: Path) -> tuple[bool, str]:
    """Verify dst is a playable file with duration matching src within tolerance.
    Also runs a 'null' decode pass to catch corruption."""
    try:
        dst = probe(dst_path)
    except Exception as e:
        return False, f"probe failed: {e}"
    if abs(dst.duration_sec - src.duration_sec) > 1.5:
        return False, (f"duration mismatch: src={src.duration_sec:.2f}s "
                       f"dst={dst.duration_sec:.2f}s")
    if not dst.audios:
        return False, "no audio in output"
    # Full decode pass (catches mid-file corruption)
    cmd = ["ffmpeg", "-v", "error", "-i", str(dst_path), "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        return False, f"decode check failed: {out.stderr[:500]}"
    if out.stderr.strip():
        # Errors went to stderr even with rc=0 -- e.g. invalid timestamps
        log.warning("decode check noted errors: %s", out.stderr[:500])
    return True, "ok"


def _safe_int(s: str) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _safe_float(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _shellish(arg: str) -> str:
    """Quote arg for log readability (not for re-execution)."""
    if any(c in arg for c in " \t\"'\\$"):
        return '"' + arg.replace('"', '\\"') + '"'
    return arg
