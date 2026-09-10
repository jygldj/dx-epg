#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
道玄 EPG 镜像器（纯标准库，无第三方依赖）
=========================================
全量镜像 fanmingming 的 XMLTV，落地：
  epg.xml / epg.xml.gz / epg.gz
  epg/YYYY-MM-DD/{channel-id}.json   （昨天、今天、明天，东八区）
GitHub Actions 每 12 小时执行；有变化才提交。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from collections import defaultdict

EPG_SOURCES = [
    "https://raw.githubusercontent.com/fanmingming/live/main/e.xml",
    "https://raw.githubusercontent.com/sparkssssssssss/epg/main/pp.xml",
    "https://live.fanmingming.cn/e.xml",
    "https://epg.pw/xmltv/epg_CN.xml",  # 回退④：epg.pw 内地源（参考资料证仍可用；频道 id 可能与主源不同）
]

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(OUT_DIR, "epg.xml")
OUT_GZ_PATH = os.path.join(OUT_DIR, "epg.xml.gz")
OUT_GZ_SHORT = os.path.join(OUT_DIR, "epg.gz")
JSON_ROOT = os.path.join(OUT_DIR, "epg")

UA = "Mozilla/5.0 (compatible; dx-epg/1.0)"
TIMEOUT = 60
RETRIES = 3
MIN_BYTES = 1024 * 1024
RETRY_SLEEP = 3
TZ8 = timezone(timedelta(hours=8))
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def cctv_aliases(name: str) -> list[str]:
    aliases: list[str] = []
    m = re.fullmatch(r"CCTV(\d+)(\+)?", name, re.I)
    if not m:
        return aliases
    n = m.group(1)
    plus = bool(m.group(2))
    if plus:
        aliases.extend([f"CCTV-{n}+", f"CCTV{n}+", f"CCTV-{n}＋", "CCTV5+"])
    else:
        aliases.extend([f"CCTV-{n}", f"CCTV{n}", f"CCTV-{n}综合"])
    seen: list[str] = []
    for a in aliases:
        if a not in seen and a != name:
            seen.append(a)
    return seen


def enrich_display_names(data: bytes) -> bytes:
    root = ET.fromstring(data)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}", 1)[0] + "}"
    for ch in root.findall(f"{ns}channel"):
        cid = ch.get("id") or ""
        existing = {
            (el.text or "").strip()
            for el in ch.findall(f"{ns}display-name")
        }
        for alias in [cid, *cctv_aliases(cid), *cctv_aliases(next(iter(existing), ""))]:
            if alias and alias not in existing:
                el = ET.SubElement(ch, f"{ns}display-name")
                el.set("lang", "zh")
                el.text = alias
                existing.add(alias)
    ET.register_namespace("", "")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def existing_digest() -> str | None:
    if not os.path.isfile(OUT_PATH):
        return None
    with open(OUT_PATH, "rb") as f:
        return sha256(f.read())


def parse_xmltv_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^(\d{14})(?:\s*([+-]\d{4}))?", raw)
    if not m:
        return None
    wall = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    off = m.group(2)
    if off:
        sign = 1 if off[0] == "+" else -1
        hours = int(off[1:3])
        mins = int(off[3:5])
        tz = timezone(sign * timedelta(hours=hours, minutes=mins))
        return wall.replace(tzinfo=tz).astimezone(TZ8)
    return wall.replace(tzinfo=TZ8)


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return cleaned or "_"


def write_daily_json(xml_bytes: bytes) -> int:
    root = ET.fromstring(xml_bytes)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}", 1)[0] + "}"
    today = datetime.now(TZ8).date()
    keep = {today + timedelta(days=d) for d in (-1, 0, 1)}
    keep_str = {d.isoformat() for d in keep}

    buckets: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    skipped = 0
    for prog in root.findall(f"{ns}programme"):
        cid = (prog.get("channel") or "").strip()
        start = parse_xmltv_dt(prog.get("start") or "")
        stop = parse_xmltv_dt(prog.get("stop") or "")
        if not cid or start is None:
            skipped += 1
            continue
        day = start.date()
        if day not in keep:
            continue
        title_el = prog.find(f"{ns}title")
        desc_el = prog.find(f"{ns}desc")
        title = (title_el.text or "").strip() if title_el is not None else ""
        desc = (desc_el.text or "").strip() if desc_el is not None else ""
        end_s = stop.strftime("%H:%M") if stop is not None else ""
        buckets[day.isoformat()][cid].append(
            {
                "start": start.strftime("%H:%M"),
                "end": end_s,
                "title": title,
                "desc": desc,
            }
        )

    os.makedirs(JSON_ROOT, exist_ok=True)
    if os.path.isdir(JSON_ROOT):
        for name in os.listdir(JSON_ROOT):
            path = os.path.join(JSON_ROOT, name)
            if os.path.isdir(path) and DATE_DIR_RE.match(name) and name not in keep_str:
                shutil.rmtree(path)

    files = 0
    for day, channels in buckets.items():
        day_dir = os.path.join(JSON_ROOT, day)
        os.makedirs(day_dir, exist_ok=True)
        for cid, items in channels.items():
            items.sort(key=lambda x: x["start"])
            payload = {
                "channel": cid,
                "date": day,
                "epg_data": items,
            }
            out = os.path.join(day_dir, f"{safe_filename(cid)}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            files += 1
    print(f"[epg] JSON 写出 {files} 个文件，覆盖 {sorted(keep_str)}，跳过 {skipped} 条")
    return files


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
                data = enrich_display_names(data)
                digest = sha256(data)
                size_kb = len(data) // 1024
                json_ready = os.path.isdir(JSON_ROOT)
                if (
                    existing_digest() == digest
                    and os.path.isfile(OUT_GZ_PATH)
                    and os.path.isfile(OUT_GZ_SHORT)
                    and json_ready
                ):
                    print(f"[epg] 与现有 {OUT_PATH} 内容相同，跳过写入")
                    return 0
                with open(OUT_PATH, "wb") as f:
                    f.write(data)
                gz_blob = gzip.compress(data, compresslevel=9)
                with open(OUT_GZ_PATH, "wb") as f:
                    f.write(gz_blob)
                with open(OUT_GZ_SHORT, "wb") as f:
                    f.write(gz_blob)
                write_daily_json(data)
                gz_kb = len(gz_blob) // 1024
                print(f"[epg] 成功写入 {OUT_PATH}（{size_kb} KB）与 gzip（{gz_kb} KB）")
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
