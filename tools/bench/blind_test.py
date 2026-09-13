"""Blind test: shuffles the PNGs of a folder under anonymous labels, serves a picker page on 127.0.0.1:8765 and saves the
picks to picks.json; the answer key (_key.json) is written next to it but never served. Usage: python blind_test.py <folder>
Image file names must start with the model name followed by "_s<seed>" so the reveal can group them."""
import json, random, http.server, socketserver, webbrowser, sys
from pathlib import Path
from PIL import Image

D = Path(sys.argv[1]); PORT = 8765
imgs = sorted(p for p in D.glob("*.png"))
random.seed(20260913); random.shuffle(imgs)
thumbs = D / "_thumbs"; thumbs.mkdir(exist_ok=True)
items = []
for i, p in enumerate(imgs):
    label = f"#{i+1:02d}"
    t = thumbs / (p.stem + ".jpg")
    if not t.exists():
        Image.open(p).convert("RGB").resize((768, 768), Image.LANCZOS).save(t, quality=88)
    items.append({"label": label, "thumb": f"_thumbs/{t.name}", "full": p.name, "model": p.name.split("_s")[0]})
json.dump(items, open(D / "_key.json", "w"), indent=1)  # answer key (not exposed to the page)

PAGE = """<!doctype html><meta charset=utf-8><title>Blind pick</title>
<style>
body{margin:0;background:#111;color:#ddd;font:14px system-ui}
header{position:sticky;top:0;background:#111c;backdrop-filter:blur(6px);padding:10px 16px;display:flex;gap:16px;align-items:center;border-bottom:1px solid #333}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;padding:12px}
.card{position:relative;border:3px solid #222;border-radius:8px;overflow:hidden;cursor:pointer;background:#000}
.card img{display:block;width:100%;aspect-ratio:1}
.card .l{position:absolute;left:8px;top:8px;background:#000a;padding:2px 8px;border-radius:4px;font-weight:600}
.card .r{position:absolute;right:8px;top:8px;background:#000a;padding:2px 8px;border-radius:4px}
.card.fav{border-color:#ffb020}.card.ok{border-color:#3fa7ff}.card.bad{border-color:#e0413f;opacity:.55}
button{background:#2b6;border:0;color:#fff;padding:8px 14px;border-radius:6px;font-weight:600;cursor:pointer}
.hint{color:#999}
#big{position:fixed;inset:0;background:#000e;display:none;align-items:center;justify-content:center}
#big img{max-width:96vw;max-height:96vh}
</style>
<header><b>Pick your favourites</b><span class=hint>click = cycle: <span style="color:#ffb020">fav</span> → <span style="color:#3fa7ff">ok</span> → <span style="color:#e0413f">bad</span> → none. Right-click = full size.</span>
<span id=count></span><button onclick="save()">Save picks</button><span id=msg></span></header>
<div class=grid id=g></div><div id=big onclick="this.style.display='none'"><img id=bigimg></div>
<script>
const items=ITEMS; const st={}; const g=document.getElementById('g');
for(const it of items){const c=document.createElement('div');c.className='card';c.dataset.l=it.label;
c.innerHTML=`<img src="${it.thumb}"><span class=l>${it.label}</span><span class=r id="r${it.label.slice(1)}"></span>`;
c.onclick=()=>{const cyc=['','fav','ok','bad'];const cur=st[it.label]||'';const nx=cyc[(cyc.indexOf(cur)+1)%4];st[it.label]=nx;c.className='card '+nx;document.getElementById('r'+it.label.slice(1)).textContent=nx;upd();};
c.oncontextmenu=e=>{e.preventDefault();document.getElementById('bigimg').src=it.full;document.getElementById('big').style.display='flex';};
g.appendChild(c);}
function upd(){const v=Object.values(st);document.getElementById('count').textContent=`fav ${v.filter(x=>x=='fav').length} · ok ${v.filter(x=>x=='ok').length} · bad ${v.filter(x=>x=='bad').length}`;}
async function save(){const r=await fetch('/save',{method:'POST',body:JSON.stringify(st)});document.getElementById('msg').textContent=r.ok?'saved ✓ — tell Claude you are done':'save failed';}
</script>"""

class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=str(D), **k)
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = PAGE.replace("ITEMS", json.dumps([{k: v for k, v in it.items() if k != "model"} for it in items])).encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        elif self.path.startswith("/_key.json"):
            self.send_error(403)
        else:
            super().do_GET()
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); data = json.loads(self.rfile.read(n) or b"{}")
        (D / "picks.json").write_text(json.dumps(data, indent=1)); self.send_response(200); self.end_headers()
    def log_message(self, *a): pass

with socketserver.TCPServer(("127.0.0.1", PORT), H) as srv:
    print(f"http://127.0.0.1:{PORT}/", flush=True)
    webbrowser.open(f"http://127.0.0.1:{PORT}/")
    srv.serve_forever()
