# 节省上下文 · 部署说明

## 目录

把整个插件目录放到：

```text
<AstrBot数据目录>/plugins/astrbot_plugin_context_hygiene/
```

Docker 环境使用挂载目录中的：

```text
data/plugins/astrbot_plugin_context_hygiene/
```

## 依赖

- AstrBot >= 4.29.0
- Python 标准库
- 可选：支持图片的 Provider（用于图片入库短描述）

不需要数据库迁移，不需要额外服务。

## 图片功能

新图片首次进入请求时，可由支持图片的 Provider 生成：

```text
4 字内标签 + 15 字内描述
```

原图使用 AES-GCM 简单加密后保存到：

```text
<AstrBot数据目录>/plugin_data/astrbot_plugin_context_hygiene/image_artifacts/
```

保留期和数量上限：

```yaml
images.artifact_retention_days: 15
images.artifact_max_files: 500
```

默认旧图不事后替换：

```yaml
images.clean_old: false
```

如果当前 Provider 不支持图片，描述会失败并保留原图；不会因此删除图片。

图片下载只接受 data URL 或公网 HTTP(S)，拒绝 file:、回环地址和私有地址，减少 SSRF/本地文件读取风险。

## 缓存保护

默认：

```yaml
cache_guard_minutes: 120
cache_prefix_messages: 105
```

工具清理只处理前缀之外、且最近 50 轮之外的内容。

## 升级

用新目录覆盖插件目录，重启 AstrBot。

配置、备份和图片缓存都在 `plugin_data` 下，不在插件目录内，升级不会覆盖。
