#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
道玄 EPG 镜像器（纯标准库，无第三方依赖）
=========================================
全量镜像 fanmingming 的 XMLTV，落地：
  epg.xml / epg.xml.gz / epg.gz
  epg/YYYY-MM-DD/{channel-id}.json   （昨天、今天、明天，东八区）
GitHub Actions 每 24 小时执行；有变化才提交。
广播 EPG：模板（radio_template.xml）+ 蜻蜓FM playbills 实况覆盖（已映射台），
未映射台与抓取失败逐台逐日自动回落模板。
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

# —— 广播电台 EPG（自包含，无网络依赖，并入现役管线统一生成/部署）——
RADIO_TEMPLATE = os.path.join(OUT_DIR, "radio_template.xml")
RADIO_OUT = os.path.join(OUT_DIR, "radio.xml")
RADIO_GZ = os.path.join(OUT_DIR, "radio.xml.gz")
RADIO_GZ_SHORT = os.path.join(OUT_DIR, "radio.gz")
RADIO_DAYS = 7
RADIO_MARKER = "<!-- @PROGRAMMES@ -->"

# —— 蜻蜓FM 实况节目单（rapi.qingting.fm playbills，2026-09-26 勘察实证）——
# day 参数：周日=1、周一=2 … 周六=7（官网 JS getDay()+1）
# 映射仅收 gbdt.txt 已验证源实测有数据的频道号；337/648 等邻近号实测空数据故不映射
QTING_RAPI = "https://rapi.qingting.fm/v2/channels/{cid}/playbills?day={day}"
QTING_MAP = {
    "bj-1006": 339,   # 北京新闻广播
    "bj-974": 332,    # 北京音乐广播
    "bj-1039": 336,   # 北京交通广播
    "cq-968": 1498,   # 重庆新闻广播
    "cq-955": 1500,   # 重庆交通广播
    "cq-881": 647,    # 重庆音乐广播
    "gs-1035": 3939,  # 甘肃交通广播
}
QTING_SLEEP = 0.6   # 请求间隔，控频防封
QTING_RETRIES = 2
QTING_TIMEOUT = 30

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


def expand_radio_dt(m: "re.Match", base_day: datetime.date) -> str:
    s = m.group(1)
    hh = int(s[0:2])
    mm = int(s[2:4])
    ss = int(s[4:6])
    carry = hh // 24
    hh = hh % 24
    day = base_day + timedelta(days=carry)
    # XMLTV 标准：14 位连写（与电视 epg.xml 一致），日期与时间之间不能有空格
    return day.strftime("%Y%m%d") + f"{hh:02d}{mm:02d}{ss:02d}"


def write_radio_daily_json(xml_bytes: bytes) -> int:
    """广播按日 JSON：与电视同目录 epg/{日期}/{tvg-id}.json。
    只写今天/明天——电视 write_daily_json 会清理 keep(昨天/今天/明天)之外的日期目录，
    广播若写 +2 天以后的文件会被误删，故与此对齐。"""
    root = ET.fromstring(xml_bytes)
    today = datetime.now(TZ8).date()
    keep = {today.isoformat(), (today + timedelta(days=1)).isoformat()}
    buckets: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for prog in root.findall("programme"):
        cid = (prog.get("channel") or "").strip()
        start = parse_xmltv_dt(prog.get("start") or "")
        stop = parse_xmltv_dt(prog.get("stop") or "")
        if not cid or start is None:
            continue
        day = start.date().isoformat()
        if day not in keep:
            continue
        title_el = prog.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        buckets[day][cid].append(
            {
                "start": start.strftime("%H:%M"),
                "end": stop.strftime("%H:%M") if stop is not None else "",
                "title": title,
                "desc": "",
            }
        )
    files = 0
    for day, channels in buckets.items():
        day_dir = os.path.join(JSON_ROOT, day)
        os.makedirs(day_dir, exist_ok=True)
        for cid, items in channels.items():
            items.sort(key=lambda x: x["start"])
            payload = {"channel": cid, "date": day, "epg_data": items}
            out = os.path.join(day_dir, f"{safe_filename(cid)}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            files += 1
    print(f"[radio] JSON 写出 {files} 个文件，覆盖 {sorted(keep)}")
    return files


def _qting_day_num(d: datetime.date) -> int:
    """蜻蜓 day 参数：周日=1、周一=2 … 周六=7（官网 JS getDay()+1）。"""
    return (d.weekday() + 1) % 7 + 1


def fetch_qting_playbills(cid: int, qday: int) -> list[dict]:
    """取某频道某星期编号的实况节目单；失败/空数据返回空列表（回落模板）。"""
    url = QTING_RAPI.format(cid=cid, day=qday)
    last: object = None
    for _ in range(1, QTING_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=QTING_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("errcode") != 0:
                raise ValueError(f"errcode={payload.get('errcode')}")
            items = (payload.get("data") or {}).get(str(qday)) or []
            if items:
                return items
            last = "空数据"
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(QTING_SLEEP)
    print(f"[radio] 蜻蜓 cid={cid} day={qday} 取数失败（{last}），该日回落模板")
    return []


def _qting_dt(raw: str, base: datetime.date) -> datetime:
    """HH:MM:SS → 当日 datetime；hh>=24（如 24:00:00）进位次日。"""
    hh, mm, ss = (int(x) for x in raw.split(":"))
    carry, hh = divmod(hh, 24)
    return datetime(base.year, base.month, base.day, hh, mm, ss, tzinfo=TZ8) + timedelta(
        days=carry
    )


def qting_programme_lines(tvg_id: str, items: list[dict], day: datetime.date) -> list[str]:
    """实况条目 → 单行 XMLTV programme（14 位连写 +0800，标题做 XML 转义）。"""
    from xml.sax.saxutils import escape

    lines: list[str] = []
    for it in items:
        try:
            s_dt = _qting_dt(it["start_time"], day)
            e_dt = _qting_dt(it["end_time"], day)
            if e_dt <= s_dt:  # 跨午夜（如 23:00~00:00）止点进一日
                e_dt += timedelta(days=1)
        except Exception:  # noqa: BLE001
            continue
        title = escape(str(it.get("title", "")).strip()) or "节目"
        lines.append(
            f'  <programme start="{s_dt.strftime("%Y%m%d%H%M%S")} +0800" '
            f'stop="{e_dt.strftime("%Y%m%d%H%M%S")} +0800" '
            f'channel="{tvg_id}"><title>{title}</title></programme>'
        )
    return lines


def generate_radio_epg() -> int:
    if not os.path.isfile(RADIO_TEMPLATE):
        print("[radio] 模板 radio_template.xml 缺失，跳过")
        return 1
    text = open(RADIO_TEMPLATE, encoding="utf-8").read()
    if RADIO_MARKER not in text:
        print("[radio] 模板缺少分隔标记，跳过")
        return 1
    head, _, tail = text.partition(RADIO_MARKER)
    tail = tail.replace("</tv>", "").strip()
    today = datetime.now(TZ8).date()
    days_blocks: list[str] = []
    for i in range(RADIO_DAYS):
        day = today + timedelta(days=i)
        block = re.sub(r"DATE(\d{6})", lambda m, d=day: expand_radio_dt(m, d), tail)
        days_blocks.append(block)

    # 蜻蜓实况覆盖：逐日逐台替换模板节目；取数失败该台该日回落模板
    if QTING_MAP:
        covered: dict[str, int] = {}
        for i in range(RADIO_DAYS):
            day = today + timedelta(days=i)
            qday = _qting_day_num(day)
            lines = days_blocks[i].splitlines()
            for tvg_id, cid in QTING_MAP.items():
                items = fetch_qting_playbills(cid, qday)
                if not items:
                    continue
                real = qting_programme_lines(tvg_id, items, day)
                if not real:
                    continue
                pat = re.compile(r'<programme\s[^>]*channel="' + re.escape(tvg_id) + '"')
                lines = [ln for ln in lines if not pat.search(ln)] + real
                covered[tvg_id] = covered.get(tvg_id, 0) + 1
                time.sleep(QTING_SLEEP)
            days_blocks[i] = "\n".join(lines)
        print(
            f"[radio] 蜻蜓实况覆盖 {sum(covered.values())} 台·日 "
            f"（{len(covered)}/{len(QTING_MAP)} 台命中），其余回落模板"
        )

    xml = head.rstrip() + "\n" + "\n".join(days_blocks) + "\n</tv>\n"
    data = xml.encode("utf-8")
    try:
        ET.fromstring(data)
    except ET.ParseError as e:
        print(f"[radio] XML 解析失败: {e}")
        return 1
    with open(RADIO_OUT, "wb") as f:
        f.write(data)
    gz_blob = gzip.compress(data, compresslevel=9)
    with open(RADIO_GZ, "wb") as f:
        f.write(gz_blob)
    with open(RADIO_GZ_SHORT, "wb") as f:
        f.write(gz_blob)
    write_radio_daily_json(data)
    gz_kb = len(gz_blob) // 1024
    print(
        f"[radio] 写出 radio.xml（{len(data) // 1024} KB）与 gzip（{gz_kb} KB），"
        f"铺开 {RADIO_DAYS} 天（{today.isoformat()} 起）"
    )
    return 0


def _run_tv() -> int:
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


def main() -> int:
    # 广播 EPG 自包含（无网络依赖），先生成，确保即便电视上游临时失效也照常部署
    radio_ok = False
    try:
        generate_radio_epg()
        radio_ok = True
    except Exception as e:  # noqa: BLE001
        print(f"[radio] 生成异常: {e}", file=sys.stderr)
    tv_rc = _run_tv()
    if not radio_ok:
        return 1
    # 广播产物为本任务核心交付；电视上游临时失败不阻断 Pages 部署（已在日志告警）
    return 0


if __name__ == "__main__":
    sys.exit(main())
