"""One-shot: new object, run the config list once each (same seed), build a labeled sheet, open the folder.
Usage: python bench_object.py "<subject>" "<materials>" "<palette>" [seed]"""
import sys, json, time, subprocess, os
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
import comfy_bench as b
from PIL import Image, ImageDraw

subject, materials, palette = sys.argv[1:4]
seed = int(sys.argv[4]) if len(sys.argv) > 4 else 913371
tag = subject.split()[-1]
base_prompt = open(os.path.join(os.path.dirname(__file__), "prompt_toaster.txt"), encoding="utf-8").read().strip()
b.PROMPT = (base_prompt.replace("toaster.", subject + ".", 1)
            .replace("Materials: painted metal, brushed copper accents, matte concrete.", f"Materials: {materials}.")
            .replace("Colour palette: teal, copper, warm grey.", f"Colour palette: {palette}."))
assert subject in b.PROMPT and materials in b.PROMPT
b.DEST = Path(os.environ.get("BENCH_ROOT", "bench-out")) / tag
b.DEST.mkdir(parents=True, exist_ok=True)
(b.DEST / "prompt.txt").write_text(b.PROMPT, encoding="utf-8")

order = ["nvfp4_50", "nvfp4_light8", "nvfp4_light4", "fp8_50", "fp8_light8", "fp8_light4"]  # grouped to load each model once
recs = [b.run(n, seed) for n in order]

# labeled sheet: 3 columns x 2 rows, label = config + time
size = 640
sheet = Image.new("RGB", (size * 3, size * 2), "white")
for i, r in enumerate(recs):
    if not r["files"]:
        continue
    im = Image.open(b.DEST / r["files"][0]).resize((size, size), Image.LANCZOS)
    d = ImageDraw.Draw(im); d.rectangle((0, 0, 330, 24), fill="black")
    d.text((5, 5), f"{r['config']}  {r['wall_s']} s  ({r['steps']} steps, cfg {r['cfg']})", fill="white")
    sheet.paste(im, ((i % 3) * size, (i // 3) * size))
sheet.save(b.DEST / f"sheet_{tag}.jpg", quality=90)
print("\n".join(f"{r['config']:14s} {r['wall_s']:7.1f} s" for r in recs))
subprocess.Popen(["explorer", str(b.DEST)])
