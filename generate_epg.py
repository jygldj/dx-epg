#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
道玄 EPG 镜像器（纯标准库，无第三方依赖）
=========================================
全量镜像 fanmingming 的 XMLTV 节目单，落地为仓库根目录 epg.xml。
由 GitHub Actions 每 12 小时执行一次；有变化才提交，Cloudflare Pages 发布为：
    https://dx-epg.pages.dev/epg.xml
"""

import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

# 按优先级排列，前一个失败才回退下一个。
# 主源：fanmingming/live 仓根 e.xml（约 8MB），GitHub 原生 raw。
# 异源回退：sparkssssssssss/epg 仓根 pp.xml（约 1.7MB），频道 id 可能不同。
# 末位：Cloudflare 镜像，Actions 侧常被拦 522。
EPG_SOURCES = [
    "https://raw.githubusercontent.com/fanmingming/live/main/e.xml",
    "https://raw.githubusercontent.com/sparkssssssssss/epg/main/pp.xml",
    "https://live.fanmingming.cn/e.xml",
]

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg.xml")

UA = "Mozilla/5.0 (compatible; dx-epg/1.0)"
TIMEOUT = 60
RETRIES = 3
MIN_BYTES = 1024 * 1024
RETRY_SLEEP = 3


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        status = resp.status
        ctype = resp.headers.get("Content-Type", "unknown")
        clen = resp.headers.get("Content-Length", "unknown")
        print(f"[epg] HTTP {status} | Content-Type={ctype} | Content-Length={clen}")
        return resp.read()


def validate_xmltv(data: bytes) -> None:
    root = ET.fromstring(data)
    tag = root.tag.rsplit("}", 1)[-1]
    if tag != "tv":
        raise ValueError(f"根元素是 {tag}，期望 tv")
    if next(iter(root), None) is None:
        raise ValueError("XMLTV 根节点为空")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def existing_digest() -> str | None:
    if not os.path.isfile(OUT_PATH):
        return None
    with open(OUT_PATH, "rb") as f:
        return sha256(f.read())


def main() -> int:
    last_err = None
    primary = EPG_SOURCES[0]
    for src in EPG_SOURCES:
        for attempt in range(1, RETRIES + 1):
            try:
                print(f"[epg] 拉取 {src}（第 {attempt}/{RETRIES} 次）")
                data = fetch(src)
                if len(data) < MIN_BYTES:
                    raise ValueError(f"体积过小 {len(data)}B，疑似错误页")
                validate_xmltv(data)
                digest = sha256(data)
                size_kb = len(data) // 1024
                if src != primary:
                    print(
                        f"[epg] 告警：采用回退源 {src}（{size_kb} KB），"
                        "频道 id 可能与主源不一致"
                    )
                else:
                    print(f"[epg] 采用主源 {src}（{size_kb} KB）")
                print(f"[epg] sha256={digest}")
                if existing_digest() == digest:
                    print(f"[epg] 与现有 {OUT_PATH} 内容相同，跳过写入")
                    return 0
                with open(OUT_PATH, "wb") as f:
                    f.write(data)
                print(f"[epg] 成功写入 {OUT_PATH}（{size_kb} KB）")
                return 0
            except urllib.error.HTTPError as e:
                body_preview = e.read(256).decode("utf-8", "ignore").replace("\n", " ")[:256]
                last_err = f"HTTP {e.code} {e.reason} | body preview: {body_preview}"
                print(f"[epg] 失败：{last_err}")
                time.sleep(RETRY_SLEEP)
            except urllib.error.URLError as e:
                last_err = f"URL error: {e.reason}"
                print(f"[epg] 失败：{last_err}")
                time.sleep(RETRY_SLEEP)
            except ET.ParseError as e:
                last_err = f"XML 解析失败: {e}"
                print(f"[epg] 失败：{last_err}")
                time.sleep(RETRY_SLEEP)
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f"[epg] 失败：{e}")
                time.sleep(RETRY_SLEEP)
    print(f"[epg] 全部上游源均失败：{last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
