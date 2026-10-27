"""Static file server that adds a page menu to every HTML page it serves.

The menu lists every *.html file in the served folder, grouped by name rules,
so a new page shows up without editing anything. Files on disk are never
changed; the menu is injected into the response only.

    python tools/nav_server.py --port 8767 --directory PATH
"""
import argparse
import functools
import html
import json
import re
from io import BytesIO
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# (group label, test on the file stem); the first match wins.
GROUPS = [
    ("Cảnh 3D", lambda s: s == "3d"),
    ("Cây và cỏ", lambda s: s.startswith(("trees", "grass", "shrubs"))),
    ("Nhà Việt Nam", lambda s: s.startswith("vnhouse")),
    ("Chiều cao nhà", lambda s: s.startswith("heights")),
    ("Dựng phố từ ảnh", lambda s: s in ("app", "index", "training", "synth_gen") or s.endswith("_test")),
]
# The home page: the hub with the shared tab bar. "/" serves it and the menu links to it first.
HOME = "app.html"
OLD = "Bản cũ và bản thử"
OTHER = "Khác"
FOLDERS = "Thư mục ảnh và báo cáo"
# Numbered versions and the scene's saved intermediate copies.
OLD_RE = re.compile(r"(_v\d+$|^3d_)")
TITLE_RE = re.compile(rb"<title>(.*?)</title>", re.I | re.S)


def page_title(path):
    """Return the page's <title>, read from the first 8 KB, or ''."""
    try:
        with open(path, "rb") as f:
            m = TITLE_RE.search(f.read(8192))
    except OSError:
        return ""
    return html.unescape(m.group(1).decode("utf-8", "replace")).strip() if m else ""


def list_pages(root):
    """Group every top-level *.html file in root, plus each subfolder's index.html.

    Current pages come before old ones; hrefs are root-absolute so the menu
    also works on pages inside subfolders.
    """
    root = Path(root)
    groups = {}
    for p in sorted(root.glob("*.html")):
        stem = p.stem
        if stem.endswith("_old") or OLD_RE.search(stem):
            label = OLD
        else:
            label = next((g for g, test in GROUPS if test(stem)), OTHER)
        groups.setdefault(label, []).append({"href": "/" + p.name, "title": page_title(p)})
    for p in sorted(root.glob("*/index.html")):
        groups.setdefault(FOLDERS, []).append(
            {"href": "/" + p.parent.name + "/index.html", "title": page_title(p)})
    order = [g for g, _ in GROUPS] + [FOLDERS, OTHER, OLD]
    return [{"group": g, "pages": groups[g]} for g in order if g in groups]


NAV_JS = r"""
(function(){
 if (window.__navInjected || window.top !== window) return; window.__navInjected = true;
 var data = %s, home = %s, here = location.pathname === '/' ? home : location.pathname.replace(/\/$/, '/index.html');
 var css = '#pgnav-btn{position:fixed;top:8px;right:8px;z-index:2147483646;font:600 13px system-ui,sans-serif;'
  + 'background:#1c2220;color:#fff;border:0;border-radius:6px;padding:6px 10px;cursor:pointer;opacity:.85}'
  + '#pgnav-btn:hover{opacity:1}'
  + '#pgnav{position:fixed;top:40px;right:8px;z-index:2147483647;max-height:calc(100vh - 56px);overflow:auto;'
  + 'width:min(340px,calc(100vw - 16px));background:#fff;color:#1c2220;border:1px solid #ccd;border-radius:8px;'
  + 'box-shadow:0 6px 24px rgba(0,0,0,.25);font:13px system-ui,sans-serif;padding:6px 0;display:none}'
  + '#pgnav h4{margin:8px 12px 4px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#667}'
  + '#pgnav a{display:block;padding:4px 12px;color:#1c2220;text-decoration:none}'
  + '#pgnav a:hover{background:#eef2f6}#pgnav a.cur{background:#dde8f5;font-weight:600}'
  + '#pgnav small{display:block;color:#778;font-size:11px}'
  + '#pgnav details summary{margin:8px 12px 4px;font-size:11px;text-transform:uppercase;color:#667;cursor:pointer}';
 var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
 var btn = document.createElement('button'); btn.id = 'pgnav-btn'; btn.textContent = '☰ Trang';
 var box = document.createElement('div'); box.id = 'pgnav';
 function link(p){
  var a = document.createElement('a'); a.href = p.href; if (p.href === here) a.className = 'cur';
  a.textContent = p.title || p.href;
  var s = document.createElement('small'); s.textContent = p.href; a.appendChild(s); return a;
 }
 var h0 = link({href: home, title: '🏠 Trang chủ'}); h0.style.fontWeight = '600'; box.appendChild(h0);
 data.forEach(function(g){
  var wrap = box;
  if (g.group === %s) { var d = document.createElement('details'); var su = document.createElement('summary');
   su.textContent = g.group + ' (' + g.pages.length + ')'; d.appendChild(su); box.appendChild(d); wrap = d; }
  else { var h = document.createElement('h4'); h.textContent = g.group; box.appendChild(h); }
  g.pages.forEach(function(p){ wrap.appendChild(link(p)); });
 });
 btn.onclick = function(e){ e.stopPropagation(); box.style.display = box.style.display === 'block' ? 'none' : 'block'; };
 box.addEventListener('click', function(e){ e.stopPropagation(); });
 document.addEventListener('click', function(){ box.style.display = 'none'; });
 document.addEventListener('keydown', function(e){ if (e.key === 'Escape') box.style.display = 'none'; });
 document.body.appendChild(btn); document.body.appendChild(box);
})();
"""


class NavHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        path = Path(self.translate_path(self.path))
        if path.is_dir():
            home = path / HOME
            path = home if self.path.split("?")[0].split("#")[0] == "/" and home.is_file() else path / "index.html"
        if path.suffix.lower() != ".html" or not path.is_file():
            return super().send_head()
        body = path.read_bytes()
        js = NAV_JS % (json.dumps(list_pages(self.directory), ensure_ascii=False), json.dumps("/" + HOME),
                       json.dumps(OLD, ensure_ascii=False))
        tag = ("<script>" + js + "</script>").encode("utf-8")
        i = body.lower().rfind(b"</body>")
        body = body[:i] + tag + body[i:] if i >= 0 else body + tag
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        return BytesIO(body)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--directory", required=True)
    a = ap.parse_args()
    handler = functools.partial(NavHandler, directory=a.directory)
    ThreadingHTTPServer((a.bind, a.port), handler).serve_forever()


if __name__ == "__main__":
    main()
