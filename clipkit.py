#!/usr/bin/env python3
"""
GTA 6 ClipKit — turn a long video into ranked, ready-to-post 9:16 clips.

Free, local, no API keys. Pipeline:
  1. Transcribe the whole video with whisper.cpp (CPU, local).
  2. Find speech blocks, cut candidate windows, snap to scene cuts.
  3. Score windows for clip-worthiness (speech density, GTA 6 keywords,
     loudness/energy, visual cut rate) and rank them.
  4. Export the top clips as 9:16 vertical video: tall sharp scene
     (4:5 smart-crop) over a blurred background — or full-bleed fill —
     with big kinetic captions in the portfolio-reel style (HyperFrames
     + GSAP transparent overlay, or animated ASS fallback), a branded
     hook title in the first 2 seconds, audio levelled to -16 LUFS,
     and a smooth-playback encode (CFR, faststart, phone-friendly
     bitrate) so the clip plays smoothly on a phone.
  5. Write clips.json with every ranked segment, its score breakdown,
     hook line and suggested caption.

Templates: named presets in templates/*.json (viral-hook, bold,
cinematic, minimal) — one --template flag bundles caption engine,
hook style, zoom, speed, framing mode and colours. See README.

Editing layer: re-export any ranked clip with per-clip overrides
(trim, hook text, caption style/colour/position, zoom punch-in,
playback speed, blurred-background vs centre-crop) via CLI flags or
a JSON config file — auto mode still works with zero config.

Requires: ffmpeg + ffprobe, whisper.cpp (whisper-cli) + a ggml model.
HyperFrames caption engine needs the hyperframes CLI (npm) + a
headless Chrome (see README); without it ClipKit falls back to the
animated ASS caption engine automatically.
No GPU, no paid keys, no network at run time.
License: MIT.
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.2.2"

# ---------------------------------------------------------------------------
# Scoring keywords (GTA 6 / Leonida flavour; the tool works on any video,
# these just boost the score when the video is actually about GTA 6).
# ---------------------------------------------------------------------------
KEYWORDS_STRONG = [
    "vice city", "lucia", "jason", "leonida", "gta",
    "grand theft", "duval", "caminos",
]
KEYWORDS_MED = [
    "heist", "wanted", "police", "chase", "prison", "robbery",
    "escape", "money", "score", "crew", "vice", "neon", "beach",
    "bounty", "getaway", "gang",
]

# Brand defaults (overridable per clip via CLI flags / config file /
# templates). Caption type is the portfolio-reel look: condensed
# black caps, big, current word highlighted yellow, red hook.
BRAND = {
    "font": "Noto Sans Display Condensed Black",
    "font_path": "/usr/share/fonts/truetype/noto/"
               "NotoSansDisplay-CondensedBlack.ttf",
    "hook_size": 110,
    "hook_style": "stagger",     # slam | stagger | fade | none
    "caption_size": 84,
    "caption_color": "#FFFFFF",      # words not yet spoken
    "caption_highlight": "#FFD60A",  # the word being spoken right now
    "caption_position": "bottom",     # bottom | top
    "outline_color": "#000000",
    "caption_engine": "hf",      # hf (HyperFrames overlay) | ass
    "progress_bar": True,        # thin red progress bar (hf engine only)
}

# Smooth-playback encode (phone-friendly): CFR at the source frame
# rate, faststart so playback starts immediately, capped bitrate.
# Old v0.1.0 used ultrafast + CRF 18 + 12 Mbps = 90 MB for 59 s and
# stuttered on phones. v0.2.0: medium + CRF 20 + 7 Mbps.
ENCODE_1080 = {"crf": 20, "maxrate": "7M", "bufsize": "14M"}
ENCODE_720 = {"crf": 20, "maxrate": "4M", "bufsize": "8M"}
ENCODE_PRESET = "medium"

MIN_CLIP = 15.0   # seconds (a dense 15 s clip beats a 60 s clip
                   # with a 25 s dead middle — see v0.2.1 audit)
MAX_CLIP = 60.0   # seconds
# Largest speech gap allowed INSIDE one clip window. v0.2.0 built a
# 59 s window across a 25.4 s music-only gap because the greedy
# extension only checked the total span; a window must never span
# a gap longer than this. 5 s keeps trailer montages usable (short
# music beats between lines) while killing dead middles.
MAX_SPEECH_GAP = 5.0
TARGET_LUFS = -16.0
TRUE_PEAK_CEIL = -1.5


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def run(cmd, check=False, capture=True):
    """Run a command; return CompletedProcess (text mode)."""
    kw = {"text": True}
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return subprocess.run(cmd, check=check, **kw)


def die(msg):
    print(f"clipkit: error: {msg}", file=sys.stderr)
    sys.exit(1)


def hex_to_ass(hexcolor):
    """'#FFD60A' -> ASS colour '&H000AD6FF&' (ASS is &HAABBGGRR&)."""
    h = hexcolor.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def ass_time(sec):
    """Seconds -> ASS timestamp 'H:MM:SS.cc'."""
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs == 100:
        cs = 0
        s += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def srt_time_to_sec(ts):
    """'00:01:23,456' -> 83.456"""
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(text):
    """Parse SRT text -> list of {start, end, text}."""
    cues = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 3:
            continue
        m = re.match(r"(\d+:\d+:\d+,\d+)\s*-->\s*(\d+:\d+:\d+,\d+)", lines[1])
        if not m:
            continue
        cues.append({
            "start": srt_time_to_sec(m.group(1)),
            "end": srt_time_to_sec(m.group(2)),
            "text": " ".join(lines[2:]).strip(),
        })
    return cues


def find_whisper_cli(explicit=None):
    """Locate whisper-cli: explicit path, PATH, then the known VM build."""
    if explicit:
        p = Path(explicit)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        die(f"whisper-cli not found at {explicit}")
    found = shutil.which("whisper-cli")
    if found:
        return found
    candidates = [
        Path.home() / "workspace/tools/whisper-cpp/build/bin/whisper-cli",
        Path("/opt/whisper-cpp/build/bin/whisper-cli"),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    die("whisper-cli not found — pass --whisper-bin, or build whisper.cpp "
        "(see README)")


def find_model(explicit=None):
    """Locate a ggml model: explicit path, then the known VM model."""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        die(f"model not found at {explicit}")
    candidates = [
        Path.home() / "workspace/tools/whisper-cpp/models/ggml-base.en.bin",
        Path.home() / "workspace/tools/whisper-cpp/models/ggml-small.en.bin",
        Path.home() / "workspace/tools/whisper-cpp/models/ggml-tiny.en.bin",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    die("no whisper model found — pass --model, or run setup.sh")


def probe(path):
    """ffprobe -> {duration, width, height, fps, out_fps, has_audio,
    audio_codec}.

    out_fps is the CFR the export targets: the source frame rate
    rounded to a whole number (30 for a 30 fps source, 24 for
    23.976). Keeping the source rate avoids the duplicate/drop judder
    of a blind 24->30 or 30->24 conversion.
    """
    r = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,r_frame_rate,"
             "avg_frame_rate",
             "-show_entries", "format=duration",
             "-of", "json", str(path)])
    if r.returncode != 0:
        die(f"ffprobe failed on {path}: {r.stderr.strip()[:300]}")
    data = json.loads(r.stdout)
    info = {"duration": float(data["format"]["duration"]), "has_audio": False}
    for st in data.get("streams", []):
        if st["codec_type"] == "video":
            info["width"] = st["width"]
            info["height"] = st["height"]
            num, den = st["r_frame_rate"].split("/")
            fps = float(num) / float(den) if float(den) else 30.0
            info["fps"] = fps
            info["out_fps"] = 30 if fps > 30.5 else max(24, int(round(fps)))
        elif st["codec_type"] == "audio":
            info["has_audio"] = True
            info["audio_codec"] = st.get("codec_name")
    return info

def detect_letterbox(src):
    """Visible picture rect (x, y, w, h) in source pixels.

    Trailers are usually letterboxed (Trailer 2 is 1920x864 inside a
    1920x1080 frame). cropdetect's most common rect over the video
    is the picture; falls back to the full frame on any failure.
    """
    r = run(["ffmpeg", "-v", "info", "-i", str(src),
             "-vf", "cropdetect=limit=24:round=2:reset=0",
             "-f", "null", "-"])
    counts = {}
    for line in r.stderr.splitlines():
        m = re.search(r"crop=(\d+):(\d+):(\d+):(\d+)", line)
        if m:
            key = tuple(int(x) for x in m.groups())
            counts[key] = counts.get(key, 0) + 1
    if counts:
        w, h, x, y = max(counts, key=counts.get)
        if w > 200 and h > 200:
            return (x, y, w, h)
    info = probe(src)
    return (0, 0, info["width"], info["height"])


def picture_bounds(info, bg_mode, W, H):
    """(top, bottom) of the VISIBLE picture in output-frame pixels.

    v0.2.0 anchored captions at y=1620 assuming the tall 4:5
    picture fills its 1080x1350 box — but a letterboxed source
    leaves the real picture ending near y=1500, so the caption's
    bottom line hung below the picture edge and words looked
    sliced. This maps the detected letterbox through the framing
    mode so captions anchor to the picture that is actually shown.
    """
    iw, ih = info["width"], info["height"]
    lbx, lby, lbw, lbh = info.get(
        "letterbox", (0, 0, iw, ih))
    if bg_mode == "crop":
        return (0, H)
    if bg_mode == "tall":
        fg_h = int(round(W * 5 / 4))
    else:  # blur band
        fg_h = min(H, int(round(W * ih / iw))) if iw else 608
    fg_top = (H - fg_h) / 2.0
    top = fg_top + (lby / ih) * fg_h
    bottom = fg_top + ((lby + lbh) / ih) * fg_h
    return (top, bottom)

# ---------------------------------------------------------------------------
# Transcription (whisper.cpp, local, no key)
# ---------------------------------------------------------------------------
def extract_wav(src, out_wav):
    """Whole-video audio -> mono 16 kHz WAV for whisper."""
    r = run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
             "-vn", "-ar", "16000", "-ac", "1", str(out_wav)])
    if r.returncode != 0:
        die(f"audio extract failed: {r.stderr.strip()[:300]}")


def transcribe(wav_path, whisper_bin, model, workdir):
    """Run whisper-cli with full JSON output; return (cues, words).

    cues: [{start, end, text}] (segment level, reliable boundaries)
    words: [{start, end, text}] (token level, sanitised — see below)
    """
    prefix = str(Path(workdir) / "transcript")
    r = run([whisper_bin, "-m", model, "-f", str(wav_path),
             "-ojf", "--output-file", prefix, "-t", "4",
             "--no-prints"])
    if r.returncode != 0:
        die(f"whisper failed: {r.stderr.strip()[-500:]}")
    data = json.loads(Path(prefix + ".json").read_text())
    cues, words = [], []
    for seg in data.get("transcription", []):
        ts = seg["timestamps"]
        start = srt_time_to_sec(ts["from"].replace(".", ","))
        end = srt_time_to_sec(ts["to"].replace(".", ","))
        text = seg.get("text", "").strip()
        # Drop non-speech markers whisper inserts for silence/music.
        if not text or re.fullmatch(r"(\[[^\]]+\]\s*)+", text):
            continue
        # Strip speaker-label artefacts (">> ..." appears mid-text too).
        text = re.sub(r"\s+", " ", text.replace(">>", "")).strip()
        if not text:
            continue
        cues.append({"start": start, "end": end, "text": text})
        # Merge tokens into words (a new word starts when a token
        # begins with a space); drop special tokens like [_BEG_].
        cur = None
        for tok in seg.get("tokens", []):
            ttext = tok.get("text", "")
            if not ttext or ttext.startswith("[_"):
                continue
            if ttext.strip() == ">>":
                continue  # whisper's speaker-label artefact, not speech
            t0 = tok["offsets"]["from"] / 1000.0
            t1 = tok["offsets"]["to"] / 1000.0
            if ttext.startswith(" ") or cur is None:
                if cur is not None:
                    words.append(cur)
                cur = {"start": t0, "end": t1, "text": ttext.strip()}
            else:
                cur["end"] = t1
                cur["text"] += ttext.strip()
        if cur is not None:
            words.append(cur)
    words = _sanitise_words(words, cues)
    return cues, words


def _sanitise_words(words, cues):
    """Fix whisper's unreliable word timestamps.

    The first token of a segment often spans leading silence with a
    tiny probability; a word must never start before the previous word
    ends, and no single word lasts longer than ~1.2 s.
    """
    clean = []
    prev_end = 0.0
    for w in words:
        s, e = w["start"], w["end"]
        if e - s > 1.2:          # bogus long span: keep the end, shrink start
            s = e - 0.5
        s = max(s, prev_end)
        if e <= s:
            e = s + 0.20
        # Clamp inside the enclosing cue when we can find one.
        for c in cues:
            if c["start"] - 0.05 <= s <= c["end"] + 0.05:
                s = max(s, c["start"])
                e = min(e, c["end"] + 0.30)
                break
        if e > s:
            clean.append({"start": s, "end": e, "text": w["text"]})
            prev_end = e
    return clean


# ---------------------------------------------------------------------------
# Scene cuts + silence (ffmpeg only)
# ---------------------------------------------------------------------------
def scene_cuts(src, threshold=0.35):
    """Scene-change timestamps via ffmpeg scdet."""
    r = run(["ffmpeg", "-v", "info", "-i", str(src),
             "-vf", f"select='gt(scene,{threshold})',showinfo",
             "-f", "null", "-"])
    cuts = []
    for line in r.stderr.splitlines():
        m = re.search(r"showinfo.*pts_time:([0-9.]+)", line)
        if m:
            cuts.append(float(m.group(1)))
    return sorted(set(cuts))


def silence_ranges(src, noise_db=-35, min_dur=0.8):
    """[(silence_start, silence_end)] via silencedetect."""
    r = run(["ffmpeg", "-v", "info", "-i", str(src),
             "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
             "-f", "null", "-"])
    starts, ends = [], []
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: ([0-9.]+)", line)
        if m:
            starts.append(float(m.group(1)))
        m = re.search(r"silence_end: ([0-9.]+)", line)
        if m:
            ends.append(float(m.group(1)))
    return list(zip(starts, ends))


def mean_volume(src, start, dur):
    """Mean volume (dB) of one window — used for the energy score and
    for the -16 LUFS gain applied at export time."""
    r = run(["ffmpeg", "-v", "info", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
             "-i", str(src), "-vn", "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume: (-?[0-9.]+) dB", r.stderr)
    return float(m.group(1)) if m else -30.0


# ---------------------------------------------------------------------------
# Candidates + scoring
# ---------------------------------------------------------------------------
def speech_blocks(cues, merge_gap=2.0, max_block=50.0):
    """Merge transcript cues separated by < merge_gap into speech blocks.

    A block is capped at max_block seconds — whisper's cues are nearly
    contiguous, so without the cap the whole video becomes one block
    and candidate windows blow past the 60 s limit.
    """
    blocks = []
    for c in cues:
        if (blocks and c["start"] - blocks[-1]["end"] < merge_gap
                and c["end"] - blocks[-1]["start"] <= max_block):
            blocks[-1]["end"] = c["end"]
            blocks[-1]["text"] += " " + c["text"]
        else:
            blocks.append(dict(c))
    return blocks


def snap_to_cut(t, cuts, window=1.5):
    """Snap a boundary to the nearest scene cut within `window` seconds."""
    if not cuts:
        return t
    nearest = min(cuts, key=lambda c: abs(c - t))
    return nearest if abs(nearest - t) <= window else t


def build_candidates(cues, cuts, duration):
    """Greedy 15–60 s windows over speech blocks, snapped to scene cuts.

    A window NEVER spans a speech gap longer than MAX_SPEECH_GAP:
    extending stops at the first block boundary whose gap is too
    long (v0.2.0 spanned a 25.4 s music gap and shipped a clip
    that was 43% dead air). The window end is also never cut
    mid-word: if a cue ends within 0.6 s after the snapped end,
    the end moves out to that cue's end.
    """
    blocks = speech_blocks(cues)
    candidates = []
    i = 0
    while i < len(blocks):
        start = blocks[i]["start"]
        j = i
        end = blocks[i]["end"]
        while (j + 1 < len(blocks)
               and blocks[j + 1]["end"] - start <= MAX_CLIP
               and blocks[j + 1]["start"] - blocks[j]["end"]
               <= MAX_SPEECH_GAP):
            j += 1
            end = blocks[j]["end"]
        if end - start >= MIN_CLIP:
            s = snap_to_cut(start, cuts)
            e = snap_to_cut(end, cuts)
            # Don't cut the last word: if a cue ends just after the
            # snapped end, extend to the cue end instead.
            for c in cues:
                if e < c["end"] <= e + 0.6 and c["end"] - s <= MAX_CLIP:
                    e = c["end"]
                    break
            if e - s >= MIN_CLIP:
                candidates.append({"start": s, "end": e})
        i = j + 1 if j > i else i + 1
    # If the greedy pass found nothing (e.g. sparse speech), fall back to
    # fixed windows over the speech-dense half of the video.
    if not candidates and cues:
        t = cues[0]["start"]
        while t + MIN_CLIP <= min(duration, cues[-1]["end"]):
            candidates.append({"start": t, "end": min(t + 45.0, duration)})
            t += 45.0
    return candidates


def _keyword_hits(text):
    low = text.lower()
    strong = sum(low.count(k) for k in KEYWORDS_STRONG)
    med = sum(low.count(k) for k in KEYWORDS_MED)
    found = sorted({k for k in KEYWORDS_STRONG + KEYWORDS_MED if k in low})
    return strong, med, found


def score_window(src, cand, cues, cuts):
    """Score one candidate window; returns (score, parts dict).

    max_gap is the longest speech-free stretch inside the window
    (bracket-only cues like [MUSIC]/[LAUGH] count as silence).
    A window with a long internal gap is penalised hard — density
    alone hid v0.2.0's 25.4 s dead middle (1.2 wps overall).
    """
    s, e = cand["start"], cand["end"]
    dur = e - s
    text = " ".join(c["text"] for c in cues if c["end"] > s and c["start"] < e)
    n_words = len(text.split())
    density = n_words / dur if dur else 0.0
    density_n = min(density / 2.5, 1.0)                     # ~2.5 wps = dense
    # Longest internal speech gap (real speech only).
    speech = [c for c in cues if c["end"] > s and c["start"] < e
              and not re.fullmatch(r"(\[[^\]]+\]\s*)+",
                                   c["text"].strip())]
    max_gap = 0.0
    prev = s
    for c in sorted(speech, key=lambda c: c["start"]):
        max_gap = max(max_gap, c["start"] - prev)
        prev = max(prev, c["end"])
    max_gap = max(max_gap, e - prev)
    strong, med, found = _keyword_hits(text)
    keyword_n = min((strong * 2 + med) / 6.0, 1.0)
    vol = mean_volume(src, s, dur)
    energy_n = min(max((vol + 40.0) / 25.0, 0.0), 1.0)     # -40..-15 dB
    n_cuts = sum(1 for c in cuts if s < c < e)
    cutrate_n = min((n_cuts / dur * 10.0) / 4.0, 1.0)      # 4 cuts/10s = busy
    score = (0.35 * density_n + 0.30 * keyword_n
             + 0.20 * energy_n + 0.15 * cutrate_n)
    if max_gap > MAX_SPEECH_GAP:
        score *= 0.4
    parts = {
        "speech_density_wps": round(density, 2),
        "max_gap_s": round(max_gap, 1),
        "words": n_words,
        "keyword_hits_strong": strong,
        "keyword_hits_med": med,
        "keywords_found": found,
        "mean_volume_db": round(vol, 1),
        "scene_cuts": n_cuts,
        "density": round(density_n, 3),
        "keyword": round(keyword_n, 3),
        "energy": round(energy_n, 3),
        "cut_rate": round(cutrate_n, 3),
    }
    return round(score, 4), parts, text


def select_clips(scored, top_n):
    """Highest scores first, no overlapping windows."""
    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)
    chosen, out = [], []
    for item in ranked:
        s, e = item["start"], item["end"]
        if all(e <= c["start"] or s >= c["end"] for c in chosen):
            chosen.append(item)
            out.append(item)
        if len(out) >= top_n:
            break
    return out


def hook_line(text):
    """Pick the strongest sentence as the hook: most keyword hits,
    else the first sentence with 5+ words. Upper-cased, <= 42 chars."""
    sentences = [x.strip() for x in re.split(r"[.!?]+", text) if x.strip()]
    if not sentences:
        return "GTA 6"
    def key(sent):
        strong, med, _ = _keyword_hits(sent)
        return (strong * 2 + med, min(len(sent.split()), 12))
    best = max(sentences, key=key)
    line = best.upper()
    if len(line) > 42:
        line = line[:42].rsplit(" ", 1)[0] + "…"
    return line


def caption_suggestion(hook, keywords):
    """Suggested post caption: hook + hashtags from what was found."""
    tags = ["#GTA6"]
    for k in keywords:
        tag = "#" + k.replace(" ", "")
        if tag not in tags and len(tags) < 5:
            tags.append(tag)
    tags.append("#ViceCity" if "#ViceCity" not in tags else "#Leonida")
    return f"{hook.title()} {' '.join(tags)}"

# ---------------------------------------------------------------------------
# Kinetic captions + hook title (ASS subtitles, v2 — portfolio-reel look)
# ---------------------------------------------------------------------------
def _pil_font(size, font_path=None):
    """PIL font for measuring text widths (same face as the captions)."""
    from PIL import ImageFont
    path = font_path or BRAND["font_path"]
    try:
        return ImageFont.truetype(path, int(size))
    except Exception:
        return ImageFont.load_default()


def _layout_lines(words, font, max_width):
    """Split cue words into centred lines that fit max_width.

    Returns [(line_words, line_width)] where line_words is a list of
    (word, x_offset_in_line). Widths are measured with the real font,
    so the ASS \\pos coordinates land exactly where the words render.
    """
    space_w = font.getlength(" ")
    lines, cur, cur_w = [], [], 0.0
    for w in words:
        ww = font.getlength(w["text"])
        add = ww if not cur else space_w + ww
        if cur and cur_w + add > max_width:
            lines.append((cur, cur_w))
            cur, cur_w = [w], ww
        else:
            cur.append(w)
            cur_w += add
    if cur:
        lines.append((cur, cur_w))
    out = []
    for line_words, line_w in lines:
        x, positioned = 0.0, []
        for i, w in enumerate(line_words):
            if i:
                x += space_w
            positioned.append((w, x))
            x += font.getlength(w["text"])
        out.append((positioned, line_w))
    return out


def _fit_hook_lines(hook, hook_size, font_path, max_w=864.0, max_lines=2):
    """Fit a hook title into the 864 px centre column.

    Returns (lines, size): one line when it fits, else (when
    max_lines is 2) two lines split at the word boundary nearest
    the middle, then shrunk until the longest line fits (never
    below 56 px). With max_lines=1 the text stays on one line and
    only shrinks. v0.2.1 shipped a 27-char hook at 110 px
    (1546 px wide) that wrapped mid-word and clipped at the right
    edge — hooks must never overflow.
    """
    font = _pil_font(hook_size, font_path)
    if font.getlength(hook) <= max_w:
        return [hook], hook_size
    lines = [hook]
    if max_lines >= 2:
        words = hook.split(" ")
        best, best_diff = None, None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            diff = abs(font.getlength(a) - font.getlength(b))
            if best_diff is None or diff < best_diff:
                best, best_diff = (a, b), diff
        if best:
            lines = list(best)
    size = hook_size
    while size > 56:
        f = _pil_font(size, font_path)
        if max(f.getlength(ln) for ln in lines) <= max_w:
            break
        size -= 2
    return lines, size


def build_ass(clip_start, clip_end, cues, words, hook, style, speed, out_path):
    """Write an .ass file for one clip — portfolio-reel kinetic type.

    - Hook title: condensed black caps in red, top area, first 2 s.
      `stagger` = letter-by-letter slam (v4 style), `slam` = whole
      line slams in, `fade` = plain fade, `none` = no hook.
    - Captions: big condensed black caps, one event per WORD, each
      word pops in (scale 150% -> 100% in 160 ms) in the highlight
      colour, then settles to white for the rest of the cue. Word
      positions are measured with the real font so the phrase stays
      centred as words pop in one by one.
    - Captions sit in the safe zone: 108 px side margins (864 px
      centre column), bottom line ends 300 px above the frame bottom
      (bottom 160 px stay clear, hook zone stays clear at top).
    """
    scale = 1.0 / speed
    font = style["font"]
    font_path = style.get("font_path", BRAND["font_path"])
    cap_size = style["caption_size"]
    hook_size = style["hook_size"]
    # Fit the hook to one line (shrink-to-fit; the HF engine splits
    # long hooks over two lines instead — see _fit_hook_lines).
    if hook:
        _hl, hook_size = _fit_hook_lines(
            hook, hook_size, font_path, max_lines=1)
    # This ffmpeg/libass renders ASS text at 0.724x the nominal size
    # (measured on this VM at two font sizes: ink w/size 1.67 vs
    # PIL 2.31, ink h/size 0.568 vs 0.784 — linear). Compensate the
    # ASS font sizes so the rendered glyphs match the PIL-measured
    # layout widths; positions stay in nominal frame pixels.
    ASS_K = 0.724
    ass_cap_size = int(round(cap_size / ASS_K))
    ass_hook_size = int(round(hook_size / ASS_K))
    hook_style = style.get("hook_style", "stagger")
    cap_col = hex_to_ass(style["caption_color"])
    cap_hl = hex_to_ass(style["caption_highlight"])
    hook_col = hex_to_ass(style.get("hook_color", "#FF3B30"))
    outline = hex_to_ass(style["outline_color"])
    is_black = "Black" in font
    bold = 0 if is_black else -1
    margin_v = 190 if style["caption_position"] == "bottom" else 170

    header = f"""[Script Info]
Title: ClipKit captions
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font},{ass_hook_size},{cap_hl},{cap_hl},{outline},&H00000000&, {bold}, 0, 0, 0, 100, 100, 1, 0, 1, 4, 1, 7, 108, 108, 170, 1
Style: Caption,{font},{ass_cap_size},{cap_hl},{cap_col},{outline},&H00000000&, {bold}, 0, 0, 0, 100, 100, 0, 0, 1, 3, 1, 7, 108, 108, {margin_v}, 1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    n_cap = 0

    def clean(t):
        return (t.replace("{", "").replace("}", "").replace("\\", "")
                 .strip())

    # ---- Hook title: first 2 s of the clip. ----
    if hook and hook_style != "none":
        hook_dur = 2.0 * scale
        hook_esc = clean(hook)
        hfont = _pil_font(hook_size, font_path)
        if hook_style == "stagger":
            widths = [hfont.getlength(ch) for ch in hook_esc]
            total = sum(widths)
            x = (1080 - total) / 2.0
            for i, ch in enumerate(hook_esc):
                if ch == " ":
                    x += widths[i]
                    continue
                t0 = i * 0.055 * scale
                events.append(
                    f"Dialogue: 0,{ass_time(t0)},"
                    f"{ass_time(hook_dur)},Hook,,0,0,0,,"
                    f"{{\\pos({x:.0f},170)\\fscx230\\fscy230"
                    f"\\1c{hook_col}\\fad(40,150)"
                    f"\\t(0,220,\\fscx100\\fscy100)}}"
                    f"{ch}"
                )
                x += widths[i]
            # Red bar under the hook, wipes in at 0.6 s.
            events.append(
                f"Dialogue: 0,{ass_time(0.6 * scale)},"
                f"{ass_time(hook_dur)},Hook,,0,0,0,,"
                f"{{\\p1\\pos(340,{170 + hook_size + 26})"
                f"\\1c&H002721E8&\\fad(150,150)}}"
                f"m 0 0 l 400 0 l 400 8 l 0 8"
            )
        elif hook_style == "slam":
            total = hfont.getlength(hook_esc)
            x = (1080 - total) / 2.0
            events.append(
                f"Dialogue: 0,{ass_time(0)},{ass_time(hook_dur)},"
                f"Hook,,0,0,0,,"
                f"{{\\pos({x:.0f},170)\\fscx230\\fscy230"
                f"\\1c{hook_col}\\fad(60,200)"
                f"\\t(0,300,\\fscx100\\fscy100)}}"
                f"{hook_esc}"
            )
        else:  # fade
            total = hfont.getlength(hook_esc)
            x = (1080 - total) / 2.0
            events.append(
                f"Dialogue: 0,{ass_time(0)},{ass_time(hook_dur)},"
                f"Hook,,0,0,0,,"
                f"{{\\pos({x:.0f},170)\\1c{hook_col}\\fad(150,250)}}"
                f"{hook_esc}"
            )

    # ---- Caption events: cues overlapping the clip. ----
    cfont = _pil_font(cap_size, font_path)
    line_h = int(cap_size * 1.12)
    for cue in cues:
        cs = max(cue["start"], clip_start)
        ce = min(cue["end"], clip_end)
        if ce - cs < 0.2:
            continue
        cue_words = [w for w in words
                     if w["end"] > cs and w["start"] < ce
                     and w["text"].replace(">>", "").strip()]
        if not cue_words:
            continue
        lines = _layout_lines(cue_words, cfont, 864)
        n_lines = len(lines)
        for li, (line_words, line_w) in enumerate(lines):
            x0 = 108 + (864 - line_w) / 2.0
            if style["caption_position"] == "bottom":
                # Bottom line's bottom edge sits 30 px above the
                # VISIBLE picture bottom (letterbox-aware; v0.2.0
                # used a fixed 1620 and hung below the picture).
                cap_bottom = style.get("cap_bottom", 1620)
                y = cap_bottom - (n_lines - li) * line_h
            else:
                y = style.get("cap_top", 340) + li * line_h
            for wi, (w, xoff) in enumerate(line_words):
                ws = max(w["start"], cs)
                we = max(w["end"], ws + 0.05)
                # Active window: this word until the next word starts
                # (or the cue ends) — capped so it never swallows the
                # next word's pop.
                nxt = (line_words[wi + 1][0]["start"]
                       if wi + 1 < len(line_words) else ce)
                if li + 1 < n_lines:
                    nxt = min(nxt, ce)
                active_end = min(max(we + 0.22, ws + 0.15), nxt, ce)
                x = x0 + xoff
                wt = clean(w["text"].replace(">>", ""))
                # Active: highlight colour, scale-slam pop.
                events.append(
                    f"Dialogue: 0,"
                    f"{ass_time((ws - clip_start) * scale)},"
                    f"{ass_time((active_end - clip_start) * scale)},"
                    f"Caption,,0,0,0,,"
                    f"{{\\pos({x:.0f},{y})\\fscx135\\fscy135"
                    f"\\1c{cap_hl}\\fad(30,60)"
                    f"\\t(0,160,\\fscx100\\fscy100)}}"
                    f"{wt}"
                )
                # Resting: same spot, white, until the cue ends.
                if active_end < ce - 0.05:
                    events.append(
                        f"Dialogue: 0,"
                        f"{ass_time((active_end - clip_start) * scale)},"
                        f"{ass_time((ce - clip_start) * scale)},"
                        f"Caption,,0,0,0,,"
                        f"{{\\pos({x:.0f},{y})\\1c{cap_col}}}"
                        f"{wt}"
                    )
                n_cap += 1
    Path(out_path).write_text(header + "\n".join(events) + "\n")
    return n_cap


# ---------------------------------------------------------------------------
# Kinetic captions + hook title (HyperFrames overlay, portfolio-reel style)
# ---------------------------------------------------------------------------
def find_hyperframes_cli():
    """Locate the hyperframes CLI, or None when it is not installed.

    Order: the repo-local install (hf-assets/node_modules, created by
    setup.sh), then PATH, then the old VM checkout as a last resort.
    A fresh clone must NOT depend on the VM-only path.
    """
    repo_root = Path(__file__).resolve().parent
    cands = [
        repo_root / "hf-assets" / "node_modules" / ".bin" / "hyperframes",
        Path.home() / "workspace" / "gta6-clipkit" / "hf-assets"
        / "node_modules" / ".bin" / "hyperframes",
    ]
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    found = shutil.which("hyperframes")
    if found:
        return found
    vm = (Path.home() / "workspace/muse-portfolio-reel/v4-hyperframes"
          / "node_modules/.bin/hyperframes")
    if vm.is_file() and os.access(vm, os.X_OK):
        return str(vm)
    return None


def _html_esc(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def build_hf_overlay(clip_start, clip_end, cues, words, hook, style,
                     speed, fps, out_path, workdir):
    """Render the kinetic caption layer with HyperFrames + GSAP.

    Produces a ProRes 4444 MOV with a transparent background — only
    the text — which export_clip overlays on the video in the same
    single encode. Same look as portfolio reel v4: condensed black
    caps, letter-stagger hook slam, word-by-word pop with the current
    word in yellow, thin red progress bar.

    Returns the MOV path, or None when HyperFrames is not usable here
    (the caller falls back to the animated ASS engine). The MOV is
    cached under workdir and re-rendered only when the inputs change.
    """
    out_path = Path(out_path)
    workdir = Path(workdir)
    dur = (clip_end - clip_start) / speed
    key_src = {
        "hook": hook, "dur": round(dur, 3),
        "style": {k: style.get(k) for k in (
            "font", "hook_size", "hook_style", "caption_size",
            "caption_color", "caption_highlight", "progress_bar",
            "cap_bottom", "cap_top")},
        "cues": cues, "words": words,
    }
    key = hashlib.sha1(json.dumps(key_src, sort_keys=True).encode()
                       ).hexdigest()[:12]
    cache_file = out_path.with_suffix(".hfkey")
    if (out_path.is_file() and cache_file.is_file()
            and cache_file.read_text().strip() == key):
        return str(out_path)

    hf_cli = find_hyperframes_cli()
    if not hf_cli:
        print("clipkit: WARNING — HyperFrames unavailable: hyperframes "
              "CLI not found. The bold template will fall back to ASS "
              "captions and will NOT look like the portfolio reel. To "
              "enable it, install Node.js 18+ and run setup.sh (it runs "
              "npm install in hf-assets/), or npm install -g hyperframes.",
              file=sys.stderr)
        return None
    gsap_src = Path(__file__).resolve().parent / "hf-assets" / "gsap.min.js"
    if not gsap_src.is_file():
        gsap_src = (Path.home() / "workspace/muse-portfolio-reel"
                    / "v4-hyperframes" / "gsap.min.js")
    if not gsap_src.is_file():
        print("clipkit: WARNING — HyperFrames unavailable: gsap.min.js "
              "not found in hf-assets/ (clone the full repo, or run "
              "setup.sh). The bold template will fall back to ASS "
              "captions and will NOT look like the portfolio reel.",
              file=sys.stderr)
        return None

    font = style["font"]
    font_path = style.get("font_path", BRAND["font_path"])
    cap_size = style["caption_size"]
    hook_size = style["hook_size"]
    hook_style = style.get("hook_style", "stagger")
    cap_col = style["caption_color"]
    cap_hl = style["caption_highlight"]
    hook_col = style.get("hook_color", "#FF3B30")
    red = "#D42A2A"

    # ---- Layout: same PIL-measured lines as the ASS engine. ----
    cfont = _pil_font(cap_size, font_path)
    hfont = _pil_font(hook_size, font_path)
    line_h = int(cap_size * 1.12)
    scale = 1.0 / speed

    def rel(t):
        return (t - clip_start) * scale

    hook_html, hook_js = "", ""
    hook_bar_top = 170 + hook_size + 26
    if hook and hook_style != "none":
        hook_lines, hook_size = _fit_hook_lines(
            hook, hook_size, font_path, max_lines=2)
        n_letters = sum(len(ln.replace(" ", "")) for ln in hook_lines)
        # Stagger scaled to the letter count so the animation lands
        # before the 1.75 s fade-out (v0.2.1's 27-letter hook was
        # still flying in when it started fading).
        stag = min(0.055, 1.0 / max(n_letters, 1))
        line_divs_h = []
        for li, ln in enumerate(hook_lines):
            letters = "".join(
                f"<span>{_html_esc(ch)}</span>" if ch != " "
                else "<span>&nbsp;</span>" for ch in ln)
            y = 170 + li * (hook_size + 10)
            line_divs_h.append(
                f'<div id="hook{li}" class="clip cond hookline" '
                f'data-start="0" data-duration="{dur:.2f}" '
                f'style="top:{y}px;font-size:{hook_size}px">'
                f"{letters}</div>")
        hook_bar_top = 170 + len(hook_lines) * (hook_size + 10) + 12
        hook_html = (
            "".join(line_divs_h) +
            f'<div id="hookBar" class="clip" data-start="0" '
            f'data-duration="{dur:.2f}"></div>'
        )
        if hook_style == "stagger":
            hook_js = (
                'tl.fromTo(".hookline span", { y:70, opacity:0, rotation:9 },'
                ' { y:0, opacity:1, rotation:0, duration:0.45,'
                f' ease:"back.out(1.6)", stagger:{stag:.3f} }}, 0.05);'
                'tl.fromTo("#hookBar", { scaleX:0 }, { scaleX:1,'
                ' duration:0.4, ease:"power2.inOut" }, 0.6);'
                'tl.to(".hookline", { opacity:0, duration:0.25 }, 1.75);'
                'tl.to("#hookBar", { opacity:0, duration:0.25 }, 1.75);'
            )
        elif hook_style == "slam":
            hook_js = (
                'tl.fromTo(".hookline", { scale:2.3, opacity:0 },'
                ' { scale:1, opacity:1, duration:0.45,'
                ' ease:"back.out(1.4)" }, 0.05);'
                'tl.fromTo("#hookBar", { scaleX:0 }, { scaleX:1,'
                ' duration:0.4, ease:"power2.inOut" }, 0.6);'
                'tl.to(".hookline", { opacity:0, duration:0.25 }, 1.75);'
                'tl.to("#hookBar", { opacity:0, duration:0.25 }, 1.75);'
            )
        else:  # fade
            hook_js = (
                'tl.fromTo(".hookline", { opacity:0 },'
                ' { opacity:1, duration:0.4 }, 0.05);'
                'tl.to(".hookline", { opacity:0, duration:0.25 }, 1.75);'
            )

    # ---- Caption lines + word timeline. ----
    line_divs, tl_js = [], []
    wid = 0
    for cue in cues:
        cs = max(cue["start"], clip_start)
        ce = min(cue["end"], clip_end)
        if ce - cs < 0.2:
            continue
        cue_words = [w for w in words
                     if w["end"] > cs and w["start"] < ce
                     and w["text"].replace(">>", "").strip()]
        if not cue_words:
            continue
        lines = _layout_lines(cue_words, cfont, 864)
        for li, (line_words, line_w) in enumerate(lines):
            lid = f"l{wid}"
            wid += 1
            x0 = 108 + (864 - line_w) / 2.0
            if style["caption_position"] == "bottom":
                # Bottom line's bottom edge sits 30 px above the
                # VISIBLE picture bottom (letterbox-aware; v0.2.0
                # used a fixed 1620 and hung below the picture).
                cap_bottom = style.get("cap_bottom", 1620)
                y = cap_bottom - (len(lines) - li) * line_h
            else:
                y = style.get("cap_top", 340) + li * line_h
            spans = " ".join(
                f'<span id="w{wid}_{wi}">'
                f'{_html_esc(w["text"].replace(">>", ""))}</span>'
                for wi, (w, _x) in enumerate(line_words))
            line_divs.append(
                f'<div id="{lid}" class="capline" '
                f'style="left:{x0:.0f}px;top:{y}px;width:{line_w:.0f}px" '
                f'data-start="0" data-duration="{dur:.2f}">{spans}</div>'
            )
            tl_js.append(
                f'tl.set("#{lid}", {{ opacity:1 }}, {rel(cs):.3f});')
            for wi, (w, _x) in enumerate(line_words):
                ws = max(w["start"], cs)
                nxt = (line_words[wi + 1][0]["start"]
                       if wi + 1 < len(line_words) else ce)
                we = max(w["end"], ws + 0.05)
                t0 = rel(ws)
                hl_end = min(rel(min(we + 0.22, nxt, ce)), rel(ce))
                tl_js.append(
                    f'tl.fromTo("#w{wid}_{wi}", {{ scale:1.35, opacity:0 }},'
                    f' {{ scale:1, opacity:1, duration:0.28,'
                    f' ease:"back.out(2.2)" }}, {t0:.3f});')
                tl_js.append(
                    f'tl.to("#w{wid}_{wi}", {{ color:"{cap_hl}",'
                    f' duration:0.05 }}, {t0:.3f});')
                tl_js.append(
                    f'tl.to("#w{wid}_{wi}", {{ color:"{cap_col}",'
                    f' duration:0.15 }}, {hl_end:.3f});')
            tl_js.append(
                f'tl.set("#{lid}", {{ opacity:0 }}, {rel(ce):.3f});')

    pbar_html, pbar_js = "", ""
    if style.get("progress_bar"):
        pbar_html = (f'<div id="pbar" class="clip" data-start="0" '
                     f'data-duration="{dur:.2f}"></div>')
        pbar_js = (f'tl.fromTo("#pbar", {{ scaleX:0 }}, {{ scaleX:1,'
                   f' duration:{dur:.2f}, ease:"none" }}, 0);')

    html = f"""<!doctype html>
<html lang="en" data-resolution="portrait">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=1080, height=1920" />
<script src="./gsap.min.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html, body {{ margin:0; width:1080px; height:1920px; overflow:hidden;
  background: transparent;
  font-family:'{font}', 'DejaVu Sans', sans-serif; }}
#root {{ width:100%; height:100%; position:relative; background: transparent; }}
.clip {{ position:absolute; }}
.cond {{ font-stretch: condensed; transform: scaleY(1.14);
  transform-origin:center bottom; }}
.hookline {{ left:108px; width:864px; text-align:center;
  font-weight:900; letter-spacing:2px;
  line-height:1; color:{hook_col}; }}
.hookline span {{ display:inline-block; }}
#hookBar {{ top:{hook_bar_top}px; left:340px; width:400px;
  height:8px; background:{red}; transform-origin:left center; }}
.capline {{ position:absolute; text-align:center; white-space:nowrap;
  font-size:{cap_size}px; font-weight:900; line-height:1.12;
  color:{cap_col}; opacity:0;
  text-shadow: 0 4px 0 rgba(0,0,0,0.9), 0 0 18px rgba(0,0,0,0.55); }}
.capline span {{ display:inline-block; }}
#pbar {{ top:0; left:0; width:1080px; height:10px; background:{red};
  transform-origin:left center; }}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0"
     data-duration="{dur:.2f}" data-width="1080" data-height="1920">
  {hook_html}
  {pbar_html}
  {''.join(line_divs)}
</div>
<script>
const tl = gsap.timeline({{ paused:true,
  defaults:{{ ease:"power3.out", overwrite:"auto" }} }});
gsap.set(".capline span", {{ transformOrigin:"center bottom" }});
{hook_js}
{pbar_js}
{''.join(tl_js)}
window.__timelines["main"] = tl;
tl.seek(0);
</script>
</body>
</html>
"""
    proj = workdir / f"hf-{key}"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "index.html").write_text(html)
    shutil.copyfile(gsap_src, proj / "gsap.min.js")
    (proj / "package.json").write_text(
        '{"name":"clipkit-overlay","version":"1.0.0","license":"MIT",'
        '"dependencies":{"gsap":"^3.13.0","hyperframes":"^0.9.52"}}')
    (proj / "hyperframes.json").write_text(
        '{"$schema":"https://hyperframes.heygen.com/schema/'
        'hyperframes.json","registry":"https://raw.githubusercontent.com/'
        'heygen-com/hyperframes/main/registry","paths":{"blocks":'
        '"compositions","components":"compositions/components","assets":'
        '"assets"},"media":{"autoProxy":true}}')
    nm = proj / "node_modules"
    # Repo-local install first (hf-assets/node_modules, created by
    # setup.sh); the old VM checkout only as a last resort.
    repo_nm = Path(__file__).resolve().parent / "hf-assets" / "node_modules"
    v4_nm = (Path.home() / "workspace/muse-portfolio-reel/v4-hyperframes"
             / "node_modules")
    for src_nm in (repo_nm, v4_nm):
        if not nm.exists() and src_nm.is_dir():
            try:
                nm.symlink_to(src_nm, target_is_directory=True)
                break
            except OSError:
                continue
    if not nm.exists():
        print("clipkit: WARNING — HyperFrames unavailable: no "
              "node_modules for the overlay project. Run setup.sh with "
              "Node.js installed so hf-assets/ is populated. The bold "
              "template will fall back to ASS captions and will NOT "
              "look like the portfolio reel.", file=sys.stderr)
        return None
    tmpdir = workdir / "tmp"
    tmpdir.mkdir(exist_ok=True)
    cachedir = workdir / "hf-cache"
    cachedir.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["TMPDIR"] = str(tmpdir)
    env["HYPERFRAMES_CACHE_DIR"] = str(cachedir)
    cmd = [hf_cli, "render", "--format", "mov", "-f", str(fps),
           "-w", "1", "-o", str(out_path), str(proj)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0 or not out_path.is_file():
        print(f"clipkit: hyperframes render failed — falling back to "
              f"ASS captions:\n{r.stderr[-600:]}", file=sys.stderr)
        return None
    # Verify the MOV really carries alpha.
    pr = run(["ffprobe", "-v", "error", "-show_entries",
              "stream=pix_fmt", "-of", "csv=p=0", str(out_path)])
    if "yuva" not in pr.stdout:
        print("clipkit: hyperframes MOV has no alpha channel — "
              "falling back to ASS captions", file=sys.stderr)
        return None
    cache_file.write_text(key)
    return str(out_path)


# ---------------------------------------------------------------------------
# Export one vertical clip
# ---------------------------------------------------------------------------
def export_clip(src, info, clip, style, out_path, fast_seek=False,
                overlay_mov=None):
    """Export [start, end] as 9:16 vertical video.

    Framing (bg mode):
      tall  — default (v0.2.0): the sharp scene is a 4:5 smart-crop
              of the source, scaled to 1080x1350 with lanczos and
              centred over the blurred background. The picture is
              ~2.2x taller than v0.1.0's thin 1080x608 band, so it
              reads as full quality on a phone instead of a small
              strip.
      blur  — v0.1.0 look: full 16:9 frame as a 1080x608 band.
      crop  — full-bleed 9:16 fill: the frame is centre-cropped to
              fill the whole screen (loses the sides — see README).
    Captions: HyperFrames transparent overlay (overlay_mov given) or
    the animated ASS file — burned in the SAME single encode, so
    there is no double-encode quality loss.
    Smooth playback: CFR at the source frame rate (no fps-conversion
    judder), medium preset, CRF 20, capped bitrate, faststart.
    Returns (ok, measured_lufs, sample_peak_db).
    """
    start = clip["start"] - clip.get("trim_shift", 0.0)
    dur = clip["end"] - clip["start"]
    if "trim" in clip:
        start = clip["trim"][0]
        dur = clip["trim"][1] - clip["trim"][0]
    speed = clip.get("speed", 1.0)
    bg_mode = clip.get("bg", "tall")
    zoom = clip.get("zoom", True)
    zoom_factor = clip.get("zoom_factor", 1.10)
    gain_db = clip.get("gain_db", 0.0)
    res = clip.get("res", 1080)
    out_fps = info.get("out_fps", 30)

    if res == 720:
        W, H = 720, 1280
        enc = ENCODE_720
    else:
        W, H = 1080, 1920
        enc = ENCODE_1080

    iw, ih = info["width"], info["height"]
    ass_file = clip.get("ass_file")

    # Zoom punch-in factor as a function of clip time t: ramps up
    # 0-0.8 s, holds to 2 s, releases by 3 s.
    z = zoom_factor
    zexpr = (f"(1+{z - 1:.2f}*if(lt(t,0.8),t/0.8,"
             f"if(lt(t,2),1,if(lt(t,3),3-t,0))))")

    # --- video chain ------------------------------------------------------
    if bg_mode == "crop":
        # Full-bleed 9:16 fill.
        vchain = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase"
            f":flags=lanczos,crop={W}:{H}"
        )
        if zoom:
            vchain += (
                f",crop=w='{W}/{zexpr}':h='{H}/{zexpr}'"
                f":x='(in_w-out_w)/2':y='(in_h-out_h)/2',"
                f"scale={W}:{H}:flags=lanczos"
            )
        vchain += ",setsar=1"
    else:
        if bg_mode == "tall":
            # 4:5 window of the source, centred (never upscaled
            # beyond the source: 864 px wide crop -> 1080 px out).
            cw = int(round(ih * 4 / 5))
            cx = max(0, (iw - cw) // 2 + int(clip.get("crop_x", 0)))
            cx = min(cx, iw - cw)
            fg_w, fg_h = W, int(round(W * 5 / 4))
            fg_filter = f"crop={cw}:{ih}:{cx}:0,"
        else:  # blur — v0.1.0 band
            fg_w = W
            fg_h = int(round(W * ih / iw)) if iw else 608
            fg_h = min(fg_h, H)
            fg_filter = ""
        vchain = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase"
            f":flags=lanczos,crop={W}:{H},gblur=sigma=32,"
            f"eq=brightness=-0.12:saturation=1.1[bg2];"
            f"[fg]{fg_filter}scale={fg_w}:{fg_h}:flags=lanczos,setsar=1"
        )
        if zoom:
            vchain += (
                f",crop=w='{fg_w}/{zexpr}':h='{fg_h}/{zexpr}'"
                f":x='(in_w-out_w)/2':y='(in_h-out_h)/2',"
                f"scale={fg_w}:{fg_h}:flags=lanczos"
            )
        vchain += "[fg2];[bg2][fg2]overlay=(W-w)/2:(H-h)/2"
    if speed != 1.0:
        vchain += f",setpts=PTS/{speed}"
    vchain += f",fps={out_fps}"
    if overlay_mov:
        vchain += ",format=yuv420p[v0];[1:v]format=yuva420p[ov];" \
                  "[v0][ov]overlay=0:0:format=yuv420[v]"
    elif ass_file:
        vchain += f",ass='{ass_file}'[v]"
    else:
        vchain += ",format=yuv420p[v]"

    # --- audio chain ------------------------------------------------------
    afilters = []
    if speed != 1.0:
        afilters.append(f"atempo={speed}")
    if abs(gain_db) > 0.05:
        afilters.append(f"volume={gain_db:.1f}dB")
    afilters.append("alimiter=limit=-1.5dB:level=false")
    achain = "[0:a]" + ",".join(afilters) + "[a]"

    fc = vchain + ";" + achain
    # -t is always SOURCE seconds: setpts/atempo apply the speedup after
    # the trim. (Dividing -t by speed as well would cut the tail off.)
    inputs = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
    if overlay_mov:
        inputs += ["-i", str(overlay_mov)]
    if fast_seek:
        inputs = ["-ss", f"{start:.3f}", "-i", str(src),
                  "-t", f"{dur:.3f}"]
        if overlay_mov:
            inputs += ["-i", str(overlay_mov)]
    cmd = (["ffmpeg", "-y", "-v", "error"] + inputs +
           ["-filter_complex", fc,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", ENCODE_PRESET,
            "-crf", str(enc["crf"]),
            "-maxrate", enc["maxrate"], "-bufsize", enc["bufsize"],
            "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(out_path)])
    r = run(cmd)
    if r.returncode != 0:
        print(f"clipkit: export failed for {out_path}:\n{r.stderr[-800:]}",
              file=sys.stderr)
        return False, None, None
    lufs, peak = measure_loudness(out_path)
    return True, lufs, peak


def measure_loudness(path):
    """Integrated LUFS (ebur128) + sample peak dB (volumedetect).

    This ffmpeg build's ebur128 prints no True Peak section, so the
    peak reported is the sample peak from volumedetect, not true peak.
    Returns (lufs, peak_db); (None, None) on failure.
    """
    r = run(["ffmpeg", "-v", "info", "-i", str(path),
             "-af", "ebur128=framelog=verbose", "-f", "null", "-"])
    m_lufs = re.search(r"Integrated loudness:\s*\n\s*I:\s*(-?[0-9.]+) LUFS",
                       r.stderr)
    lufs = float(m_lufs.group(1)) if m_lufs else None
    r2 = run(["ffmpeg", "-v", "info", "-i", str(path),
              "-af", "volumedetect", "-f", "null", "-"])
    m_peak = re.search(r"max_volume: (-?[0-9.]+) dB", r2.stderr)
    peak = float(m_peak.group(1)) if m_peak else None
    return lufs, peak


def decode_verify(path):
    """ffmpeg decode check; returns True when zero errors."""
    r = run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"])
    return r.returncode == 0 and not r.stderr.strip()


def av_sync_check(path):
    """Video vs audio duration/stream-start gap; True when in sync."""
    r = run(["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type,start_time,duration",
             "-of", "json", str(path)])
    if r.returncode != 0:
        return False
    data = json.loads(r.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    if not v or not a:
        return True  # no audio stream to drift against
    dv = float(v.get("duration", 0))
    da = float(a.get("duration", 0))
    sv = float(v.get("start_time", 0))
    sa = float(a.get("start_time", 0))
    return abs(dv - da) < 0.15 and abs(sv - sa) < 0.15

# ---------------------------------------------------------------------------
# Analysis (cached so edit mode can re-export without re-transcribing)
# ---------------------------------------------------------------------------
def analyse(src, info, whisper_bin, model, workdir):
    """Full analysis; returns dict with cues, words, cuts, candidates."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    wav = workdir / "audio_16k.wav"
    print("clipkit: extracting audio…")
    extract_wav(src, wav)
    print("clipkit: transcribing with whisper.cpp (local, no key)…")
    cues, words = transcribe(wav, whisper_bin, model, workdir)
    print(f"clipkit: transcript: {len(cues)} cues, {len(words)} words")
    print("clipkit: detecting scene cuts + silence…")
    cuts = scene_cuts(src)
    print(f"clipkit: {len(cuts)} scene cuts")
    candidates = build_candidates(cues, cuts, info["duration"])
    print(f"clipkit: {len(candidates)} candidate windows")
    scored = []
    for cand in candidates:
        score, parts, text = score_window(src, cand, cues, cuts)
        scored.append({**cand, "score": score, "parts": parts, "text": text})
    cache = {
        "input": str(src),
        "duration": info["duration"],
        "width": info["width"],
        "height": info["height"],
        "cues": cues,
        "words": words,
        "cuts": cuts,
        "candidates": scored,
    }
    (workdir / "analysis.json").write_text(json.dumps(cache))
    return cache


def load_cache(workdir):
    p = Path(workdir) / "analysis.json"
    if not p.is_file():
        die(f"no analysis cache at {p} — run auto mode first")
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# One clip: build ASS, export, loudness-fix loop, verify
# ---------------------------------------------------------------------------
def make_clip(src, info, clip, style, out_path, fast_seek=False, tag=""):
    """Export + verify one clip. Loudness is corrected once if needed.

    Caption engine: "hf" renders the kinetic layer with HyperFrames
    + GSAP (portfolio-reel look) and composites it in the same single
    encode; when HyperFrames is unavailable — or the clip is sped up
    (the overlay is timed for 1.0x) — it falls back to the animated
    ASS engine and records which engine was actually used.
    """
    out_path = Path(out_path)
    clip = dict(clip)
    engine = style.get("caption_engine", "hf")
    overlay_mov = None
    n_cap = 0
    # Letterbox-aware caption anchor: captions sit inside the
    # VISIBLE picture, not at a fixed y that can hang below it.
    res = clip.get("res", 1080)
    W, H = (720, 1280) if res == 720 else (1080, 1920)
    p_top, p_bottom = picture_bounds(
        info, clip.get("bg", "tall"), W, H)
    style["cap_bottom"] = int(p_bottom - 30)
    style["cap_top"] = int(p_top + 40)
    if engine == "hf" and clip.get("speed", 1.0) == 1.0:
        overlay_mov = build_hf_overlay(
            clip["start"], clip["end"], clip["_cues"], clip["_words"],
            clip["hook"], style, clip.get("speed", 1.0),
            info.get("out_fps", 30),
            out_path.with_suffix(".mov"), out_path.parent)
        if overlay_mov:
            n_cap = sum(1 for w in clip["_words"]
                        if w["end"] > clip["start"]
                        and w["start"] < clip["end"])
        else:
            engine = "ass"
    if engine == "ass":
        clip["ass_file"] = str(out_path.with_suffix(".ass"))
        n_cap = build_ass(clip["start"], clip["end"], clip["_cues"],
                          clip["_words"], clip["hook"], style,
                          clip.get("speed", 1.0), clip["ass_file"])
    ok, lufs, peak = export_clip(src, info, clip, style, out_path,
                                 fast_seek, overlay_mov)
    if not ok:
        return {"ok": False, "file": str(out_path)}
    # Loudness fix-up: one re-export with corrected gain if > 2 LU off.
    if lufs is not None and abs(lufs - TARGET_LUFS) > 2.0:
        gain = TARGET_LUFS - lufs
        gain = max(-12.0, min(12.0, gain))
        clip["gain_db"] = clip.get("gain_db", 0.0) + gain
        print(f"clipkit: {tag} loudness {lufs} LUFS, re-exporting "
              f"with {gain:+.1f} dB gain…")
        ok, lufs, peak = export_clip(src, info, clip, style, out_path,
                                     fast_seek, overlay_mov)
        if not ok:
            return {"ok": False, "file": str(out_path)}
    verified = {
        "decode_ok": decode_verify(out_path),
        "av_sync_ok": av_sync_check(out_path),
        "caption_events": n_cap,
        "caption_engine": engine,
    }
    size = out_path.stat().st_size
    return {
        "ok": True,
        "file": str(out_path),
        "size_bytes": size,
        "loudness_lufs": lufs,
        "peak_db": peak,
        "caption_engine": engine,
        "verified": verified,
    }


def clip_record(rank, clip, result):
    """One entry for clips.json."""
    dur = clip["end"] - clip["start"]
    if "trim" in clip:
        dur = clip["trim"][1] - clip["trim"][0]
    # Output duration: speedup shortens the clip.
    if clip.get("speed", 1.0) != 1.0:
        dur = dur / clip["speed"]
    return {
        "rank": rank,
        "start": round(clip["start"], 2),
        "end": round(clip["end"], 2),
        "duration": round(dur, 2),
        "score": clip["score"],
        "score_parts": clip["parts"],
        "hook": clip["hook"],
        "hook_line": clip["hook"],
        "suggested_caption": caption_suggestion(
            clip["hook"], clip["parts"]["keywords_found"]),
        "transcript": clip["text"][:600],
        "keywords": clip["parts"]["keywords_found"],
        "speed": clip.get("speed", 1.0),
        "bg": clip.get("bg", "tall"),
        "zoom": clip.get("zoom", True),
        "res": clip.get("res", 1080),
        "caption_engine": result.get("caption_engine"),
        "template": clip.get("template"),
        "file": result.get("file"),
        "size_bytes": result.get("size_bytes"),
        "loudness_lufs": result.get("loudness_lufs"),
        "peak_db": result.get("peak_db"),
        "verified": result.get("verified"),
    }


# ---------------------------------------------------------------------------
# Edit mode: config file + CLI overrides
# ---------------------------------------------------------------------------
def apply_overrides(clip, style, overrides):
    """Apply per-clip edit overrides (from config, template or CLI)."""
    clip = dict(clip)
    style = dict(style)
    if "trim" in overrides:
        clip["trim"] = [float(overrides["trim"][0]),
                        float(overrides["trim"][1])]
    if "hook" in overrides and isinstance(overrides["hook"], str):
        clip["hook"] = str(overrides["hook"])[:60]
    if "speed" in overrides:
        clip["speed"] = float(overrides["speed"])
    if "bg" in overrides:
        clip["bg"] = str(overrides["bg"])
    if "zoom" in overrides:
        clip["zoom"] = bool(overrides["zoom"])
    if "zoom_factor" in overrides:
        clip["zoom_factor"] = float(overrides["zoom_factor"])
    if "res" in overrides:
        clip["res"] = int(overrides["res"])
    if "crop_x" in overrides:
        clip["crop_x"] = int(overrides["crop_x"])
    if "template" in overrides:
        clip["template"] = str(overrides["template"])
    for key in ("caption_color", "caption_highlight", "caption_position",
                "caption_size", "font", "caption_engine", "hook_style",
                "hook_size", "hook_color", "progress_bar"):
        if key in overrides:
            style[key] = overrides[key]
    if "caption" in overrides and isinstance(overrides["caption"], dict):
        for key, val in overrides["caption"].items():
            if key in ("color", "highlight", "position", "size",
                       "engine"):
                style["caption_" + key] = val
    if "hook" in overrides and isinstance(overrides["hook"], dict):
        for key, val in overrides["hook"].items():
            if key in ("style", "size", "color"):
                style["hook_" + key] = val
    return clip, style


# ---------------------------------------------------------------------------
# Templates: named presets in templates/*.json
# ---------------------------------------------------------------------------
TEMPLATES_DIR = Path(__file__).parent / "templates"


def list_templates():
    """Sorted names of the templates shipped in templates/*.json."""
    if not TEMPLATES_DIR.is_dir():
        return []
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.json"))


def load_template(name):
    """Load one template -> (overrides dict, template metadata).

    A template is a JSON file: {"name", "description", "look",
    "when", "overrides": {...}}. Overrides use the same keys as
    apply_overrides (bg, zoom, speed, res, caption_*, hook_* ...).
    """
    path = TEMPLATES_DIR / f"{name}.json"
    if not path.is_file():
        avail = ", ".join(list_templates()) or "none"
        die(f"template not found: {name} (available: {avail})")
    data = json.loads(path.read_text())
    ov = dict(data.get("overrides", {}))
    ov["template"] = data.get("name", name)
    meta = {k: data.get(k) for k in ("name", "description", "look",
                                     "when")}
    return ov, meta


def style_from_args(args):
    """Brand style from CLI flags (defaults = brand)."""
    style = dict(BRAND)
    if args.caption_color:
        style["caption_color"] = args.caption_color
    if args.caption_highlight:
        style["caption_highlight"] = args.caption_highlight
    if args.caption_position:
        style["caption_position"] = args.caption_position
    if args.caption_size:
        style["caption_size"] = args.caption_size
    if args.caption_engine:
        style["caption_engine"] = args.caption_engine
    if args.hook_style:
        style["hook_style"] = args.hook_style
    if args.hook_size:
        style["hook_size"] = args.hook_size
    if args.font:
        style["font"] = args.font
    if getattr(args, "no_progress_bar", False):
        style["progress_bar"] = False
    return style


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        prog="clipkit",
        description="Turn a long video into ranked, ready-to-post 9:16 "
                    "clips — free, local, no API keys.")
    ap.add_argument("input", nargs="?",
                    help="long input video (mp4/mov/mkv/webm)")
    ap.add_argument("--list-templates", action="store_true",
                    help="list the named presets in templates/ and exit")
    ap.add_argument("--out", default="out",
                    help="output directory (default: out)")
    ap.add_argument("--clips", type=int, default=3,
                    help="how many top clips to export (default: 3)")
    ap.add_argument("--model", help="whisper ggml model path")
    ap.add_argument("--whisper-bin", help="path to whisper-cli")
    ap.add_argument("--fast-seek", action="store_true",
                    help="seek by keyframe first (faster, captions may "
                         "drift by up to a GOP — default is accurate seek)")
    # --- edit mode ---
    ap.add_argument("--config",
                    help="JSON config with per-clip edit overrides; "
                         "re-exports listed clips from a previous auto run")
    ap.add_argument("--clip", type=int,
                    help="edit mode: re-export ranked clip N only")
    ap.add_argument("--template",
                    help="use a named preset from templates/ "
                         "(viral-hook, bold, cinematic, minimal); "
                         "CLI flags override the template")
    ap.add_argument("--trim",
                    help="edit: new start:end in source seconds, e.g. 12.0:48.0")
    ap.add_argument("--hook", help="edit: override the hook title text")
    ap.add_argument("--speed", type=float,
                    help="edit: playback speed, e.g. 1.15")
    ap.add_argument("--bg", choices=["tall", "blur", "crop"],
                    help="edit: tall 4:5 scene (default), blurred "
                         "background band, or full-bleed centre-crop")
    ap.add_argument("--res", type=int, choices=[1080, 720], default=1080,
                    help="edit: output height 1080 (default) or 720 "
                         "(smaller file, smoothest phone playback)")
    ap.add_argument("--caption-engine", choices=["hf", "ass"],
                    help="edit: hf = HyperFrames+GSAP kinetic overlay "
                         "(portfolio-reel look), ass = animated ASS captions")
    ap.add_argument("--hook-style", choices=["slam", "stagger", "fade",
                                             "none"],
                    help="edit: hook title animation")
    ap.add_argument("--hook-size", type=int,
                    help="edit: hook title font size, e.g. 110")
    ap.add_argument("--no-progress-bar", action="store_true",
                    help="edit: hide the thin red progress bar (hf engine)")
    ap.add_argument("--crop-x", type=int, default=0,
                    help="edit: shift the tall 4:5 crop window left/right "
                         "in source pixels, e.g. -200 or 200")
    ap.add_argument("--no-zoom", action="store_true",
                    help="edit: disable the hook-moment zoom punch-in")
    ap.add_argument("--zoom-factor", type=float, default=1.10,
                    help="edit: zoom punch-in factor (default: 1.10)")
    ap.add_argument("--caption-color", help="edit: caption colour, e.g. #FFFFFF")
    ap.add_argument("--caption-highlight",
                    help="edit: current-word highlight colour, e.g. #FFD60A")
    ap.add_argument("--caption-position", choices=["bottom", "top"],
                    help="edit: caption position")
    ap.add_argument("--caption-size", type=int,
                    help="edit: caption font size, e.g. 52")
    ap.add_argument("--font", help="edit: caption font family")
    args = ap.parse_args()

    if args.list_templates:
        for name in list_templates():
            _, meta = load_template(name)
            print(f"{name:12} {meta.get('description', '')}")
        return

    if not args.input:
        ap.error("the following arguments are required: input")

    src = Path(args.input)
    if not src.is_file():
        die(f"input not found: {src}")
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    if not info["has_audio"]:
        die("input has no audio track — ClipKit needs speech to score clips")
    info["letterbox"] = detect_letterbox(src)
    print(f"clipkit: letterbox {info['letterbox']} "
          f"(captions anchor to the visible picture)")
    print(f"clipkit v{VERSION}: {src} "
          f"({info['duration']:.0f}s, {info['width']}x{info['height']})")

    # Template is the base layer; CLI flags override it. Config-file
    # items sit between the two (template < config < CLI).
    template_ov = {}
    if args.template:
        template_ov, template_meta = load_template(args.template)
        print(f"clipkit: template '{template_meta['name']}' — "
              f"{template_meta['description']}")
    cli_ov = {}
    if args.trim:
        cli_ov["trim"] = [float(x) for x in args.trim.split(":")]
    if args.hook:
        cli_ov["hook"] = args.hook
    if args.speed:
        cli_ov["speed"] = args.speed
    if args.bg:
        cli_ov["bg"] = args.bg
    if args.res != 1080:
        cli_ov["res"] = args.res
    if args.no_zoom:
        cli_ov["zoom"] = False
    if args.zoom_factor != 1.10:
        cli_ov["zoom_factor"] = args.zoom_factor
    if args.caption_engine:
        cli_ov["caption_engine"] = args.caption_engine
    if args.hook_style:
        cli_ov["hook_style"] = args.hook_style
    if args.hook_size:
        cli_ov["hook_size"] = args.hook_size
    if args.no_progress_bar:
        cli_ov["progress_bar"] = False
    if args.crop_x:
        cli_ov["crop_x"] = args.crop_x
    for key, val in (("caption_color", args.caption_color),
                     ("caption_highlight", args.caption_highlight),
                     ("caption_position", args.caption_position),
                     ("caption_size", args.caption_size),
                     ("font", args.font)):
        if val is not None:
            cli_ov[key] = val
    base_ov = {**template_ov, **cli_ov}

    edit_mode = bool(args.config or args.clip)
    clips_json = outdir / "clips.json"

    if edit_mode:
        # --- edit mode: reuse the cached analysis, re-export with edits ---
        cache = load_cache(outdir)
        ranked = cache["candidates"]
        ranked = sorted(ranked, key=lambda x: x["score"], reverse=True)
        base = []
        if clips_json.is_file():
            base = json.loads(clips_json.read_text()).get("clips", [])
        overrides_list = []
        if args.config:
            cfg = json.loads(Path(args.config).read_text())
            defaults = cfg.get("defaults", {})
            for item in cfg.get("clips", []):
                merged = dict(template_ov)
                merged.update(defaults)
                merged.update(item)
                merged.update(cli_ov)
                overrides_list.append(merged)
        if args.clip:
            ov = dict(base_ov)
            ov["index"] = args.clip
            overrides_list.append(ov)
        if not overrides_list:
            die("edit mode needs --config or --clip with at least one "
                "override (e.g. --hook, --speed, --trim)")
        results = []
        for ov in overrides_list:
            idx = int(ov.get("index", 1)) - 1
            if idx < 0 or idx >= len(ranked):
                die(f"clip index {idx + 1} out of range "
                    f"(1..{len(ranked)})")
            clip = dict(ranked[idx])
            clip["hook"] = hook_line(clip["text"])
            clip["_cues"] = cache["cues"]
            clip["_words"] = cache["words"]
            clip, st = apply_overrides(clip, dict(BRAND), ov)
            name = f"clip_{idx + 1:02d}_edit.mp4"
            res = make_clip(src, info, clip, st, outdir / name,
                            args.fast_seek, tag=f"edit clip {idx + 1}")
            rec = clip_record(idx + 1, clip, res)
            rec["edits_applied"] = {k: v for k, v in ov.items()
                                    if k != "index"}
            results.append(rec)
            print(f"clipkit: edited clip {idx + 1} -> {name} "
                  f"({'OK' if res['ok'] else 'FAILED'})")
        data = {"tool": "gta6-clipkit", "version": VERSION,
                "input": str(src), "edits": results}
        (outdir / "clips_edited.json").write_text(
            json.dumps(data, indent=2))
        print(f"clipkit: wrote {outdir / 'clips_edited.json'}")
        return

    # --- auto mode ---
    whisper_bin = find_whisper_cli(args.whisper_bin)
    model = find_model(args.model)
    cache = analyse(src, info, whisper_bin, model, outdir)
    scored = cache["candidates"]
    if not scored:
        die("no candidate windows found — is there enough speech?")
    chosen = select_clips(scored, args.clips)
    print(f"clipkit: exporting top {len(chosen)} clips…")
    records = []
    for rank, clip in enumerate(chosen, 1):
        clip = dict(clip)
        clip["hook"] = hook_line(clip["text"])
        clip["_cues"] = cache["cues"]
        clip["_words"] = cache["words"]
        clip["gain_db"] = max(-12.0, min(12.0, TARGET_LUFS -
                                         clip["parts"]["mean_volume_db"]))
        clip, st = apply_overrides(clip, dict(BRAND), base_ov)
        name = f"clip_{rank:02d}.mp4"
        res = make_clip(src, info, clip, st, outdir / name,
                        args.fast_seek, tag=f"clip {rank}")
        rec = clip_record(rank, clip, res)
        records.append(rec)
        print(f"clipkit: [{rank}] {clip['start']:.1f}–{clip['end']:.1f}s "
              f"score {clip['score']:.3f} hook '{clip['hook']}' -> {name} "
              f"({'OK' if res['ok'] else 'FAILED'})")
    data = {
        "tool": "gta6-clipkit",
        "version": VERSION,
        "input": str(src),
        "input_duration": info["duration"],
        "model": Path(model).name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "clips": records,
    }
    clips_json.write_text(json.dumps(data, indent=2))
    print(f"clipkit: done — {len(records)} clips, see {clips_json}")


if __name__ == "__main__":
    main()
