"""把改版前的逐页截图排成一张可翻页的索引，供进哥圈选。

产物：shots/old_ui_20261101_c/改版前界面-逐页圈选.html
  · 按左栏 7 个一级分类分区
  · 每页一张图（懒加载），点图在新标签打开原图
  · 每张图下标「编号 · 中文名 · tab id」，方便口头指认（"第23页右上那个卡片"）
用法：python tools/make_old_ui_index.py --dir shots/old_ui_20261101_c
"""
from __future__ import annotations

import argparse
import html
import json
import os

CSS = """
:root{--bg:#f4f6f8;--card:#fff;--ink:#1d2733;--sub:#5c6b7a;--line:#e3e8ee;
      --brand:#10898f;--brand-soft:#e6f5f5;--warn:#b26a00;--warnbg:#fff6e5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:32px 24px 80px}
h1{font-size:24px;margin:0 0 6px;letter-spacing:.3px}
.lede{color:var(--sub);margin:0 0 18px;font-size:14px}
.howto{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--brand);
       border-radius:10px;padding:14px 18px;margin:0 0 28px;font-size:14px}
.howto b{color:var(--brand)}
.howto ol{margin:8px 0 0;padding-left:20px}
.howto li{margin:3px 0}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 30px}
.toc a{background:var(--card);border:1px solid var(--line);border-radius:999px;
       padding:5px 13px;font-size:13px;color:var(--ink);text-decoration:none}
.toc a:hover{border-color:var(--brand);color:var(--brand)}
.toc a span{color:var(--sub);margin-left:5px}
section{margin:0 0 46px;scroll-margin-top:16px}
.cathead{display:flex;align-items:baseline;gap:10px;border-bottom:2px solid var(--brand);
         padding-bottom:7px;margin-bottom:18px}
.cathead h2{font-size:18px;margin:0}
.cathead em{font-style:normal;color:var(--sub);font-size:12px;letter-spacing:.5px}
.cathead .n{margin-left:auto;color:var(--sub);font-size:13px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;
      box-shadow:0 1px 2px rgba(16,32,48,.04)}
.card .thumb{display:block;background:#eef1f4;line-height:0;position:relative;overflow:hidden}
.card img{width:100%;height:620px;object-fit:cover;object-position:top center;display:block;cursor:zoom-in;
          transition:opacity .15s}
.card img:hover{opacity:.9}
.card .more{position:absolute;right:10px;bottom:10px;background:rgba(16,32,48,.72);color:#fff;
            font-size:11.5px;border-radius:6px;padding:3px 9px;line-height:1.5}
.card .cap{display:flex;align-items:center;gap:8px;padding:10px 13px;border-top:1px solid var(--line);
           font-size:13.5px}
.no{background:var(--brand-soft);color:var(--brand);font-weight:700;border-radius:6px;
    padding:1px 7px;font-size:12.5px;letter-spacing:.4px}
.nm{font-weight:600}
.tid{color:var(--sub);font-family:ui-monospace,Consolas,monospace;font-size:12px}
.badge{margin-left:auto;font-size:11.5px;color:var(--warn);background:var(--warnbg);
       border-radius:5px;padding:1px 7px}
.badge.ok{color:var(--sub);background:#f2f5f7}
footer{color:var(--sub);font-size:12.5px;border-top:1px solid var(--line);padding-top:16px}
"""

JS = """
document.querySelectorAll('.card img').forEach(function(img){
  img.addEventListener('click',function(){ window.open(img.src,'_blank'); });
});
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="shots/old_ui_20261101_c")
    ap.add_argument("--out", default="改版前界面-逐页圈选.html")
    a = ap.parse_args()

    idx = json.load(open(os.path.join(a.dir, "_index.json"), encoding="utf-8"))
    pages = idx["pages"]

    # 按大类归组（保持 plan 顺序）
    groups: list[dict] = []
    for c in idx["plan"]:
        items = [p for p in pages if p["cat"] == c["cat"]]
        if items:
            groups.append({"cat": c["cat"], "label": c["catLabel"], "items": items})

    total = sum(len(g["items"]) for g in groups)
    toc = "".join(
        f'<a href="#cat-{html.escape(g["cat"])}">{html.escape(g["label"])}'
        f'<span>{len(g["items"])}</span></a>' for g in groups
    )

    parts: list[str] = []
    for g in groups:
        cards = []
        for p in g["items"]:
            badge = ""
            if not p.get("ok"):
                if p["tab"] == "fundmasters":
                    badge = '<span class="badge ok">iframe 面板 · 画面正常</span>'
                else:
                    badge = '<span class="badge">文字量偏少 · 可能需手动加载</span>'
            cards.append(
                f'<div class="card"><a class="thumb" href="{html.escape(p["file"])}" target="_blank">'
                f'<img loading="lazy" src="{html.escape(p["file"])}" alt="{html.escape(p["label"])}">'
                f'<span class="more">点开看完整长图</span></a>'
                f'<div class="cap"><span class="no">{p["idx"]:02d}</span>'
                f'<span class="nm">{html.escape(p["label"])}</span>'
                f'<span class="tid">{html.escape(p["tab"])}</span>{badge}</div></div>'
            )
        parts.append(
            f'<section id="cat-{html.escape(g["cat"])}">'
            f'<div class="cathead"><h2>{html.escape(g["label"])}</h2>'
            f'<em>{html.escape(g["cat"])}</em><span class="n">{len(g["items"])} 页</span></div>'
            f'<div class="grid">{"".join(cards)}</div></section>'
        )

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>改版前界面 · 逐页圈选</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>改版前界面 · 按左栏逐页</h1>
<p class="lede">版本：改版前最后一次可用状态（git <code>1a58511</code>，2026-10-31 23:35）。
共 {len(groups)} 个一级分类、{total} 页。现在跑的新版是 6 组，这 {total} 项<strong>一项都没删</strong>，
但其中 33 项被折进了「研究工具」折叠区、默认收起，所以用起来像不见了。</p>

<div class="howto">
<b>怎么指给我看</b>
<ol>
<li>翻到你想找回的那一页，看图片下方的<strong>编号</strong>（比如 <code>23</code>）和名字；</li>
<li>要指具体位置，就在图上圈一下，把圈好的图发我，同时说一句「第 23 页这个」；</li>
<li>一次圈几个都行 —— 我会先把它们归成一张清单跟你确认，再动手加，不会直接改现有板块。</li>
</ol>
</div>

<div class="toc">{toc}</div>
{"".join(parts)}
<footer>截图方式：真实浏览器逐页冷启动（1440×1000，整页）。首屏的「欢迎使用」引导与全屏遮罩已在截图前收起，
只为露出正文，没有改动任何界面逻辑。勾选画面为空的那一页是「基金经理策略」，它整块是 iframe，
父文档读不到子文档文字，页面本身有内容。</footer>
</div><script>{JS}</script></body></html>"""

    path = os.path.join(a.dir, a.out)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"已生成 {path}（{len(doc)/1024:.0f} KB，{total} 页 / {len(groups)} 类）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
