# dx-epg · 道玄自有 EPG 仓库

把公开 EPG 源**全量镜像**为一份标准 XMLTV 节目单，由 Cloudflare Pages 托管，
供道玄电视的播放列表（m3u 的 `url-tvg` 头）与 TVBox（`api.json` 的 `epg` 字段）引用。

> 本仓前身为直播源聚合器，现改造为**专属 EPG 节目单源**。旧聚合逻辑已退役，
> 仅保留 Pages 连接（构建命令留空、输出目录=仓库根）。

## 对外端点

| 产物 | 地址 | 用途 |
|---|---|---|
| EPG（XMLTV） | `https://dx-epg.pages.dev/epg.xml` | 播放器 `url-tvg` 头 / TVBox `epg` 字段 |

## 原理

```
GitHub Actions（每 12h + 手动）
      │  python generate_epg.py
      ▼
拉取上游 XMLTV（fanmingming，失败则回退）
      │  体积下限 / ElementTree 解析 / sha256
      ▼
有变化才写 epg.xml → 普通 commit → 推送 main
      │
      ▼
Cloudflare Pages 自动发布 → https://dx-epg.pages.dev/epg.xml
```

## 文件结构

| 文件 | 作用 |
|---|---|
| `generate_epg.py` | 纯标准库生成器：全量镜像上游 XMLTV，落地 `epg.xml`（重试、XML 解析、哈希跳过） |
| `.github/workflows/epg.yml` | 每 12 小时 + 手动触发；`epg.xml` 有变化才提交推送 |
| `epg.xml` | 产出物，由 Pages 托管 |

## 上游与策略

- **上游源**：`fanmingming/live` 仓根 `e.xml`（全量 XMLTV，约 8MB）。
  **主源走 GitHub 原生 raw**：`https://raw.githubusercontent.com/fanmingming/live/main/e.xml`
  —— GitHub 自身域名在 Actions 运行器 100% 可达，不被 Cloudflare 拦截。
- **回退源**：`sparkssssssssss/epg` 的 `pp.xml`（异源，频道 id 可能不同）；
  `live.fanmingming.cn/e.xml` 为 Cloudflare 镜像，Actions 侧常被拦 522，仅作末位备用。
  `epg.fanmingming.com/xmltv.xml` 已不再使用。
- **策略**：全量镜像（实现最简，与上游完全一致）；若日后嫌 `epg.xml` 体积过大，
  可改为「按自有频道表精选合并」（见历史讨论），缩小体积并提升频道名匹配率。

## 接入示例

m3u 头部：

```
#EXTM3U url-tvg="https://dx-epg.pages.dev/epg.xml"
```

TVBox `api.json` 顶层：

```json
{ "epg": "https://dx-epg.pages.dev/epg.xml" }
```

## 维护

- 推送 `main` 即触发 Pages 重新部署；也可在 Actions 页面手动 `Run workflow`。
- 上游失效或内容未变时不提交；上次有效 `epg.xml` 保留。
- 工作流使用普通 commit（不再 amend / force push）。
- 推送需仓库 **Settings → Actions → General → Workflow permissions = Read and write**。
