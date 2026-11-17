"""Build dist/: a single-page static demo (inline CSS/JS, forced demo mode) + web/sample data.

Run after scripts/build_demo.py:  python3 scripts/build_static.py
"""
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB, DIST = ROOT / "web", ROOT / "dist"

if DIST.exists():
    shutil.rmtree(DIST)
DIST.mkdir()
shutil.copytree(WEB / "sample", DIST / "sample")
html = (WEB / "index.html").read_text(encoding="utf-8")
css = (WEB / "styles.css").read_text(encoding="utf-8")
js = (WEB / "app.js").read_text(encoding="utf-8").replace("</script>", "<\\/script>")
body = re.search(r"<body>(.*?)</body>", html, re.S).group(1)
body = body.replace('<script src="app.js"></script>',
                    '<script>document.documentElement.dataset.demo = "1";</script>\n<script>\n' + js + "\n</script>")
page = ("<meta charset=\"utf-8\">\n<title>PlanWell</title>\n"
        '<meta name="description" content="A gentle check before each walk, class or game, based on your own past days.">\n'
        '<meta name="color-scheme" content="light dark">\n<style>\n' + css + "\n</style>\n" + body)
(DIST / "index.html").write_text(page, encoding="utf-8")
print("dist/index.html + %d sample files" % sum(1 for _ in (DIST / "sample").rglob("*.json")))
