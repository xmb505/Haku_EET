#!/usr/bin/env python3
"""Convert markdown to PDF with nice styling."""
import markdown
from weasyprint import HTML
import os

BASE = os.path.dirname(os.path.abspath(__file__))
MD_FILE = os.path.join(BASE, '..', 'docs', '西门子2026总结.md')
PDF_FILE = os.path.join(BASE, '..', 'docs', '西门子2026总结.pdf')

with open(MD_FILE, 'r', encoding='utf-8') as f:
    md_text = f.read()

html_body = markdown.markdown(
    md_text,
    extensions=['tables', 'fenced_code', 'toc', 'md_in_html'],
    output_format='html5',
)

html_full = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
@page {{
    size: A4;
    margin: 2cm 1.6cm;
    @bottom-center {{
        content: counter(page) " / " counter(pages);
        font-size: 9px;
        color: #999;
    }}
}}
body {{
    font-family: "Noto Sans CJK SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "PingFang SC", "Helvetica Neue", Arial, sans-serif;
    font-size: 11px;
    line-height: 1.7;
    color: #222;
}}
h1 {{
    font-size: 24px;
    text-align: center;
    border-bottom: 3px solid #333;
    padding-bottom: 10px;
    margin-bottom: 6px;
}}
h2 {{
    font-size: 17px;
    color: #1a3a6b;
    border-bottom: 2px solid #1a3a6b;
    padding-bottom: 4px;
    margin-top: 22px;
    page-break-after: avoid;
}}
h3 {{
    font-size: 13.5px;
    color: #2a5a9b;
    margin-top: 16px;
    page-break-after: avoid;
}}
table {{
    border-collapse: collapse;
    width: 100%;
    margin: 8px 0;
    font-size: 10.5px;
    page-break-inside: avoid;
}}
th {{
    background: #1a3a6b;
    color: #fff;
    padding: 5px 8px;
    text-align: left;
    border: 1px solid #1a3a6b;
}}
td {{
    padding: 5px 8px;
    border: 1px solid #ccc;
    vertical-align: top;
}}
tr:nth-child(even) td {{
    background: #f5f7fa;
}}
blockquote {{
    border-left: 4px solid #d9534f;
    background: #fdf6f5;
    margin: 8px 0;
    padding: 6px 12px;
    color: #555;
    font-size: 10.5px;
    page-break-inside: avoid;
}}
blockquote p {{
    margin: 3px 0;
}}
code {{
    background: #f0f2f5;
    padding: 1px 4px;
    border-radius: 3px;
    font-family: "Cascadia Code", "Consolas", "Courier New", monospace;
    font-size: 10px;
    color: #c7254e;
}}
pre {{
    background: #2b2b2b;
    color: #e6e6e6;
    padding: 10px 14px;
    border-radius: 6px;
    font-size: 9.5px;
    line-height: 1.5;
    overflow-x: auto;
    page-break-inside: avoid;
}}
pre code {{
    background: none;
    color: inherit;
    padding: 0;
}}
hr {{
    border: none;
    border-top: 1px solid #ddd;
    margin: 16px 0;
}}
ul, ol {{
    padding-left: 22px;
}}
li {{
    margin: 2px 0;
}}
a {{
    color: #1a6bb5;
    text-decoration: none;
}}
strong {{
    color: #111;
}}
</style>
</head>
<body>
{html_body}
</body>
</html>"""

HTML(string=html_full).write_pdf(PDF_FILE)
print(f"PDF generated: {PDF_FILE}")
