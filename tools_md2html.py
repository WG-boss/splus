# -*- coding: utf-8 -*-
"""
极简 Markdown → 自包含 HTML（仅覆盖本项目报告用到的语法，零外部依赖）。

支持：# 标题、段落、无序/有序列表、表格、引用、分隔线、**粗体**、`代码`、*斜体*。
不支持嵌套列表与脚注——报告写作时避免使用即可。

用法：
    python tools_md2html.py docs/DB_ASSESSMENT_20261108.md -o data/db_assessment.html --title "标题"
"""
from __future__ import annotations

import argparse
import html
import re
import sys

CSS = """
:root{--bg:#f5f7fa;--panel:#fff;--line:#e3e8ef;--tx:#1f2937;--tx2:#6b7280;
--green:#0e9f6e;--green-bg:#def7ec;--blue:#1c64f2;--blue-bg:#e1effe;
--amber:#c27803;--amber-bg:#fdf6b2;--red:#e02424;--red-bg:#fde8e8;
--gray:#6b7280;--gray-bg:#e5e7eb;--mono:"JetBrains Mono",Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{font:14.5px/1.85 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--tx);padding:26px 18px}
.wrap{max-width:980px;margin:0 auto;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:38px 44px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
h1{font-size:25px;margin:0 0 16px;letter-spacing:-.3px}
h2{font-size:18px;margin:30px 0 12px;padding-bottom:7px;border-bottom:1px solid var(--line)}
h3{font-size:15px;margin:20px 0 8px;color:#111827}
p{margin:9px 0}
strong{font-weight:700;color:#0b1220}
em{font-style:normal;background:var(--amber-bg);padding:0 3px;border-radius:3px}
code{font-family:var(--mono);font-size:12.5px;background:#f3f4f6;padding:1px 5px;border-radius:4px}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:13px;display:block;overflow-x:auto}
th{background:#f9fafb;color:var(--tx2);font-size:11.5px;font-weight:600;text-align:left;padding:8px 10px;border-bottom:2px solid var(--line);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:hover td{background:#f8fafc}
ul,ol{margin:10px 0 10px 22px}
li{margin:5px 0}
blockquote{border-left:3px solid var(--blue);background:var(--blue-bg);padding:10px 14px;margin:14px 0;border-radius:0 6px 6px 0;color:#1e3a8a;font-size:13.5px}
hr{border:none;border-top:1px dashed var(--line);margin:24px 0}
.foot{color:var(--tx2);font-size:12px;margin-top:26px;text-align:center}
@media(max-width:720px){.wrap{padding:22px 18px}}
"""


def inline(t: str) -> str:
    t = html.escape(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"`([^`]+?)`", r"<code>\1</code>", t)
    return t


def convert(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_ul = in_ol = in_tbl = False
    para: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para = []

    def close_list() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def close_tbl() -> None:
        nonlocal in_tbl
        if in_tbl:
            out.append("</tbody></table>")
            in_tbl = False

    i = 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()

        if not s:
            flush_para()
            close_list()
            close_tbl()
            i += 1
            continue

        # 表格：表头行 + 分隔行
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            flush_para()
            close_list()
            close_tbl()
            cells = [c.strip() for c in s.strip("|").split("|")]
            out.append("<table><thead><tr>" + "".join("<th>%s</th>" % inline(c) for c in cells) + "</tr></thead><tbody>")
            in_tbl = True
            i += 2
            continue
        if in_tbl:
            if s.startswith("|"):
                cells = [c.strip() for c in s.strip("|").split("|")]
                out.append("<tr>" + "".join("<td>%s</td>" % inline(c) for c in cells) + "</tr>")
                i += 1
                continue
            close_tbl()

        if s.startswith("#"):
            flush_para()
            close_list()
            m = re.match(r"^(#{1,6})\s+(.*)$", s)
            if m:
                lv = len(m.group(1))
                out.append("<h%d>%s</h%d>" % (lv, inline(m.group(2)), lv))
                i += 1
                continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            flush_para()
            close_list()
            out.append("<hr>")
            i += 1
            continue

        if s.startswith(">"):
            flush_para()
            close_list()
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote>" + inline(" ".join(buf)) + "</blockquote>")
            continue

        m_ul = re.match(r"^[-*+]\s+(.*)$", s)
        m_ol = re.match(r"^(\d+)\.\s+(.*)$", s)
        if m_ul or m_ol:
            flush_para()
            if m_ul:
                if in_ol:
                    out.append("</ol>")
                    in_ol = False
                if not in_ul:
                    out.append("<ul>")
                    in_ul = True
                out.append("<li>" + inline(m_ul.group(1)) + "</li>")
            else:
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                if not in_ol:
                    out.append("<ol>")
                    in_ol = True
                out.append("<li>" + inline(m_ol.group(2)) + "</li>")
            i += 1
            continue

        close_list()
        para.append(s)
        i += 1

    flush_para()
    close_list()
    close_tbl()
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--title", default="")
    args = ap.parse_args()

    with open(args.src, "r", encoding="utf-8") as fh:
        md = fh.read()
    body = convert(md)
    title = args.title or (md.lstrip().splitlines()[0].lstrip("# ").strip() if md.strip() else "报告")
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">{body}
<div class="foot">由 tools_md2html.py 生成 · 源文件 {html.escape(args.src)}</div>
</div></body></html>
"""
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print("[md2html] %s -> %s" % (args.src, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
