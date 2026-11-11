# -*- coding: utf-8 -*-
"""
Meridian 品牌字体子集化脚本(一次性工具,产物提交入库)。

产出(web/public/fonts/):
  SpaceGrotesk-var.woff2   拉丁展示字体(Logo/标题/大数字),可变字重 300-700
  JetBrainsMono-var.woff2  等宽(arxiv_id / payload / 分数),可变字重 100-800
  NotoSerifSC-var.woff2    中文衬线标题(h1 中文自动回落),子集≈1.5MB

子集字符集:扫描 web/app + web/components 全部 .tsx/.ts 源码中实际出现的字符
(页面 UI 全是静态文案,覆盖即充分),再补 ASCII 可打印区与常用中英标点。
未来新增文案若含未收录汉字,自动回落系统衬线,不会破版。

用法:
  python scripts/subset-fonts.py
依赖: pip install fonttools brotli(子集与 woff2 压缩)
"""
from __future__ import annotations

import io
import string
import subprocess
import sys
import urllib.request
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = WEB_DIR / "public" / "fonts"
CACHE_DIR = WEB_DIR / "scripts" / ".font-cache"

# google/fonts GitHub 原始文件;镜像走 jsdelivr(任一可用即可)
SOURCES = {
    "SpaceGrotesk": {
        "urls": [
            "https://raw.githubusercontent.com/google/fonts/main/ofl/spacegrotesk/SpaceGrotesk%5Bwght%5D.ttf",
            "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/spacegrotesk/SpaceGrotesk[wght].ttf",
        ],
        "out": "SpaceGrotesk-var.woff2",
    },
    "JetBrainsMono": {
        "urls": [
            "https://raw.githubusercontent.com/google/fonts/main/ofl/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf",
            "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/jetbrainsmono/JetBrainsMono[wght].ttf",
        ],
        "out": "JetBrainsMono-var.woff2",
    },
    "PlayfairDisplay": {
        "urls": [
            "https://raw.githubusercontent.com/google/fonts/main/ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf",
            "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/playfairdisplay/PlayfairDisplay[wght].ttf",
        ],
        "out": "PlayfairDisplay-var.woff2",
    },
    "NotoSerifSC": {
        "urls": [
            "https://raw.githubusercontent.com/google/fonts/main/ofl/notoserifsc/NotoSerifSC%5Bwght%5D.ttf",
            "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/notoserifsc/NotoSerifSC[wght].ttf",
        ],
        "out": "NotoSerifSC-var.woff2",
    },
}

# 品牌词兜底(防止源码扫描遗漏)
BRAND_TEXT = (
    "MERIDIAN Meridian SCHOLAR 循证 · 可审计的科研证据系统 "
    "文献库 溯源问答 阅读器 引用图谱 检索发现 研究缝隙 写作 Copilot "
    "评审模式 透明度中心 0123456789"
)
EXTRA = "！？。，、；：「」『』（）《》〈〉【】·—–…％℃→←↗⇒✓✗⚠📌💡🪨🌿◎⭐👍👇↑↓⟳"

def collect_chars() -> str:
    """扫描前端源码字符 + ASCII + 品牌词兜底,合并去重。"""
    chars: set[str] = set(string.printable)
    chars.update(BRAND_TEXT)
    chars.update(EXTRA)
    for pattern in ("app/**/*.tsx", "app/**/*.ts", "components/**/*.tsx", "lib/**/*.ts"):
        for f in WEB_DIR.glob(pattern):
            try:
                chars.update(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
    # 控制字符剔除
    return "".join(sorted(c for c in chars if c.isprintable() or c in "\n\t"))

def fetch(name: str, urls: list[str]) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    dst = CACHE_DIR / f"{name}.ttf"
    if dst.exists() and dst.stat().st_size > 1_000_000:
        print(f"[cache] {dst}")
        return dst
    for url in urls:
        try:
            print(f"[fetch] {name} ← {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=120).read()
            dst.write_bytes(data)
            return dst
        except Exception as e:  # noqa: BLE001 - 换下一个镜像
            print(f"        failed: {e}")
    raise RuntimeError(f"{name} 所有镜像下载失败——检查网络后重试")

def subset(src: Path, out_path: Path, text: str, extra_args: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "fontTools.subset", str(src),
        f"--text={text}",
        "--flavor=woff2",
        "--layout-features=*",
        "--unicodes=U+0000-00FF,U+2000-206F,U+3000-303F,U+FF00-FFEF",
        f"--output-file={out_path}",
        *extra_args,
    ]
    subprocess.run(cmd, check=True)
    size_kb = out_path.stat().st_size / 1024
    print(f"[done] {out_path.name}: {size_kb:.0f} KB")

def main() -> None:
    text = collect_chars()
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    print(f"corpus: {len(text)} chars(CJK {cjk})")
    for name, spec in SOURCES.items():
        src = fetch(name, spec["urls"])
        subset(src, OUT_DIR / spec["out"], text, [])
    print("全部完成 →", OUT_DIR)

if __name__ == "__main__":
    main()
