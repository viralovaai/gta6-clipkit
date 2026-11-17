#!/usr/bin/env python3
"""Build shareable demo assets from a verified ClipKit run.

Usage:
    python3 demo.py INPUT_VIDEO --out OUT_DIR [--clip 1]

Outputs in OUT_DIR:
    demo-before-after.jpg  — input frame vs vertical clip frame, side by side
    demo-clip.gif          — 5 s GIF of the best clip (360x640, 10 fps)

Only run this after clipkit.py has produced verified clips.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run(cmd):
    r = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if r.returncode != 0:
        print(f"demo: failed: {' '.join(cmd)}\n{r.stderr[-600:]}",
              file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="ClipKit demo assets")
    ap.add_argument("input", help="original long input video")
    ap.add_argument("--out", required=True, help="ClipKit output dir")
    ap.add_argument("--clip", type=int, default=1,
                    help="which ranked clip to feature (default: 1)")
    args = ap.parse_args()

    outdir = Path(args.out)
    data = json.loads((outdir / "clips.json").read_text())
    clips = data["clips"]
    idx = min(max(args.clip, 1), len(clips)) - 1
    clip = clips[idx]
    clip_file = Path(clip["file"])
    if not clip_file.is_file():
        sys.exit(f"demo: clip file missing: {clip_file}")

    work = outdir / "demo_tmp"
    work.mkdir(exist_ok=True)

    # --- before frame (input, at the clip's start) -----------------------
    before = work / "before.jpg"
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{clip['start']:.2f}",
         "-i", str(args.input), "-frames:v", "1",
         "-vf", "scale=540:304", str(before)])
    # --- after frame (the vertical clip, mid-hook) -----------------------
    after = work / "after.jpg"
    run(["ffmpeg", "-y", "-v", "error", "-ss", "2.5",
         "-i", str(clip_file), "-frames:v", "1",
         "-vf", "scale=270:480", str(after)])
    # --- side-by-side with labels (PIL — reliable, no filter escaping) --
    from PIL import Image, ImageDraw, ImageFont
    combo = outdir / "demo-before-after.jpg"
    before_im = Image.open(before).convert("RGB")
    after_im = Image.open(after).convert("RGB")
    canvas = Image.new("RGB", (810, 480), "black")
    canvas.paste(before_im, (0, 88))
    canvas.paste(after_im, (540, 0))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    for (x, y, label) in ((12, 12, "BEFORE - long video"),
                          (552, 12, "AFTER - ClipKit clip")):
        draw.rectangle([x - 6, y - 4, x + 8 + draw.textlength(label, font=font),
                        y + 28], fill=(0, 0, 0))
        draw.text((x, y), label, fill="white", font=font)
    canvas.save(combo, quality=90)

    # --- 5 s GIF of the best clip -----------------------------------------
    pal = work / "palette.png"
    gif = outdir / "demo-clip.gif"
    run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-t", "5",
         "-i", str(clip_file),
         "-vf", "fps=10,scale=360:640:flags=lanczos,palettegen",
         str(pal)])
    run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-t", "5",
         "-i", str(clip_file), "-i", str(pal),
         "-lavfi", "fps=10,scale=360:640:flags=lanczos[x];[x][1:v]paletteuse",
         str(gif)])
    print(f"demo: {combo} ({combo.stat().st_size // 1024} KB)")
    print(f"demo: {gif} ({gif.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
