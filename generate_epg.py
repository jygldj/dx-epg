#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
道玄 EPG 镜像器（纯标准库，无第三方依赖）
=========================================
全量镜像 fanmingming 的 XMLTV 节目单，落地为仓库根目录 epg.xml。
由 GitHub Actions 每 12 小时执行一次；提交后 Cloudflare Pages 自动发布为：
    https://dx-iptv.pages.dev/epg.xml
该地址即为「自己的 EPG 仓库」对外端点，供 live.m3u / dxtv.m3u 的 url-tvg
头与 TVBox api.json 的 epg 字段引用。

设计取舍：
- 全量镜像（非按频道精选）：实现最简，与上游保持完全一致；
  代价是 epg.xml 体积较大，若后续嫌大可改为按自有频道表精选（见 README）。
- 单源（fanmingming）为主，其余源列入 EPG_SOURCES 作顺序回退。
"""

import os
import sys
import time
import urllib.request
import urllib.error

# 上游 XMLTV 源（按优先级排列，前一个失败才回退下一个）
# ① 主源：fanmingming/live 仓根 e.xml（约 8MB），GitHub 原生 raw，Actions 运行器 100% 可达。
# ② 异源回退：sparkssssssssss/epg 仓根 pp.xml（112114 EPG，约 1.7MB，标准 XMLTV）。
#    112114 是 fanmingming 的上游数据源（同源不同托管）——fanmingming 仓被删时仍可更新，
#    避免 dx-iptv 退化为纯快照。
# ③ 末位回退：Cloudflare 镜像 live.fanmingming.cn/e.xml（真机可用，Actions 侧常被拦 522）。
# 注：epg.fanmingming.com/xmltv.xml 同为 Cloudflare 前域名，已弃用为主源（前次工作流失败根因）。
EPG_SOURCES = [
    "https://raw.githubusercontent.com/fanmingming/live/main/e.xml",      # 主源：fanmingming（8MB）
    "https://raw.githubusercontent.com/sparkssssssssss/epg/main/pp.xml",  # 异源回退：112114（1.7MB）
    "https://live.fanmingming.cn/e.xml",                                # 末位：Cloudflare 镜像
]

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg.xml")

UA = "Mozilla/5.0 (compatible; dx-iptv-epg/1.0)"
TIMEOUT = 300          # 单次拉取超时（秒）；全量 XMLTV 可能较大，给足时间
RETRIES = 5           # 每个源重试次数
MIN_BYTES = 1024 * 1024  # 小于 1MB 视为异常页（防止把错误页写进 epg.xml）


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        status = resp.status
        ctype = resp.headers.get("Content-Type", "unknown")
        clen = resp.headers.get("Content-Length", "unknown")
        print(f"[epg] HTTP {status} | Content-Type={ctype} | Content-Length={clen}")
        return resp.read()


def main() -> int:
    last_err = None
    for src in EPG_SOURCES:
        for attempt in range(1, RETRIES + 1):
            try:
                print(f"[epg] 拉取 {src}（第 {attempt}/{RETRIES} 次）")
                data = fetch(src)
                if len(data) < MIN_BYTES:
                    raise ValueError(f"体积过小 {len(data)}B，疑似错误页")
                text = data.decode("utf-8", "ignore")
                if "<tv" not in text or "</tv>" not in text:
                    raise ValueError("响应非合法 XMLTV 内容")
                with open(OUT_PATH, "wb") as f:
                    f.write(data)
                size_kb = len(data) // 1024
                print(f"[epg] 成功写入 {OUT_PATH}（{size_kb} KB）")
                return 0
            except urllib.error.HTTPError as e:
                body_preview = e.read(256).decode("utf-8", "ignore").replace("\n", " ")[:256]
                last_err = f"HTTP {e.code} {e.reason} | body preview: {body_preview}"
                print(f"[epg] 失败：{last_err}")
                time.sleep(3)
            except urllib.error.URLError as e:
                last_err = f"URL error: {e.reason}"
                print(f"[epg] 失败：{last_err}")
                time.sleep(3)
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f"[epg] 失败：{e}")
                time.sleep(3)
    print(f"[epg] 全部上游源均失败：{last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
