# dx-epg · 道玄自有 EPG 仓库

把公开 EPG 源全量镜像为一份标准 XMLTV，并拆成 TVBox 用的按日 JSON。
Cloudflare Pages 托管。

实测可用的直播源（OK影视 / 酷9）：

```
http://dxdszb.pages.dev/dxtv.m3u
```

## 对外端点

| 产物 | 地址 | 用途 |
|---|---|---|
| 直播源 | `http://dxdszb.pages.dev/dxtv.m3u` | OK影视 / 酷9 / 影视仓 |
| EPG gzip | `https://dx-epg.pages.dev/epg.gz` | 酷9 / OK影视，优先 |
| EPG gzip | `https://dx-epg.pages.dev/epg.xml.gz` | 备用 |
| EPG XMLTV | `https://dx-epg.pages.dev/epg.xml` | 原始 XML |
| TVBox JSON | `https://dx-epg.pages.dev/epg/{date}/{name}.json` | TVBox / 拾光 / DIYP |

`{date}` 为 `YYYY-MM-DD`，`{name}` 为频道 id（如 `CCTV1`）。只保留昨天、今天、明天。

## 原理

```
GitHub Actions（每 12h + 手动）
      python generate_epg.py
      拉取上游 XMLTV（fanmingming，失败则回退）
      体积下限 / ElementTree / sha256 / CCTV 别名
      写出 epg.xml、epg.xml.gz、epg.gz、epg/日期/*.json
      推送 main → Cloudflare Pages
```

## 接入

酷9 / OK影视，m3u 头：

```
#EXTM3U x-tvg-url="https://dx-epg.pages.dev/epg.gz"
```

频道行 `tvg-name` 用 channel id：

```
#EXTINF:-1 tvg-name="CCTV1" group-title="央视咪咕",CCTV-1综合
```

TVBox / 拾光 `api.json`：

```json
{ "epg": "https://dx-epg.pages.dev/epg/{date}/{name}.json" }
```

换源时先删旧直播源再添加。

## 维护

- 推送 `main` 即触发 Pages；也可 Actions 手动 Run workflow
- 上游失效或内容未变时不提交
- 工作流使用普通 commit
- Settings → Actions → General → Workflow permissions = Read and write
