# dx-iptv · 道玄自有 EPG 仓库

把公开 EPG 源**全量镜像**为一份标准 XMLTV 节目单，由 Cloudflare Pages 托管，
供道玄电视的播放列表（m3u 的 `url-tvg` 头）与 TVBox（`api.json` 的 `epg` 字段）引用。

> 本仓前身为直播源聚合器，现改造为**专属 EPG 节目单源**。旧聚合逻辑已退役，
> 仅保留 Pages 连接（构建命令留空、输出目录=仓库根）。

## 对外端点

| 产物 | 地址 | 用途 |
|---|---|---|
| EPG（XMLTV） | `https://dx-iptv.pages.dev/epg.xml` | 播放器 `url-tvg` 头 / TVBox `epg` 字段 |

## 原理

```
GitHub Actions（每 12h + 手动）
      │  python generate_epg.py
      ▼
拉取上游 XMLTV（fanmingming）
      │  校验体积 / 合法性
      ▼
写 epg.xml → 提交（amend 单提交）→ 推送 main
      │
      ▼
Cloudflare Pages 自动发布 → https://dx-iptv.pages.dev/epg.xml
```

## 文件结构

| 文件 | 作用 |
|---|---|
| `generate_epg.py` | 纯标准库生成器：全量镜像上游 XMLTV，落地 `epg.xml`（含重试与合法性校验） |
| `.github/workflows/epg.yml` | 每 12 小时 + 手动触发，运行生成器并提交（单提交历史防膨胀） |
| `epg.xml` | 产出物，由 Pages 托管 |

## 上游与策略

- **上游源**：`fanmingming/live` 仓根 `e.xml`（全量 XMLTV，约 8MB）。
  **主源走 GitHub 原生 raw**：`https://raw.githubusercontent.com/fanmingming/live/main/e.xml`
  —— GitHub 自身域名在 Actions 运行器 100% 可达，不被 Cloudflare 拦截。
- **弃用项**：`epg.fanmingming.com/xmltv.xml` 与 `live.fanmingming.cn/e.xml` 均为
  Cloudflare 前域名，Actions 侧抓取常被拦（522/超时），仅作回退备用。
- **策略**：全量镜像（实现最简，与上游完全一致）；若日后嫌 `epg.xml` 体积过大，
  可改为「按自有频道表精选合并」（见历史讨论），缩小体积并提升频道名匹配率。

## 接入示例

m3u 头部：

```
#EXTM3U url-tvg="https://dx-iptv.pages.dev/epg.xml"
```

TVBox `api.json` 顶层：

```json
{ "epg": "https://dx-iptv.pages.dev/epg.xml" }
```

## 维护

- 推送 `main` 即触发 Pages 重新部署；也可在 Actions 页面手动 `Run workflow`。
- 若上游失效，生成器会跳过提交（不写回错误页），上次有效 `epg.xml` 保留。
- 工作流推送需仓库 **Settings → Actions → General → Workflow permissions = Read and write**。
