# 节省上下文 · context_hygiene

给 AstrBot 的**请求级节省上下文插件**。

目标不是删除聊天记录，而是让每轮真正发给模型的上下文更短、更干净：

- 去重历史 `system_reminder`
- 保留当前消息的最新身份/时间提醒
- 清理超过保留数量的工具调用对
- 删除孤立 `tool` 消息
- 旧图片替换为占位文本
- 上下文过大时做确定性压缩
- 默认不调用模型，不产生额外 Token
- 默认不写会话数据库

## 安全边界

### 默认模式：只改本轮请求

插件通过：

```python
@filter.on_llm_request()
```

修改本轮 `ProviderRequest.contexts` 的副本。

不修改：

- `data_v4.db`
- 会话历史
- memory5 日记
- emotion_pics 图库
- 原始图片文件

所以即使插件出错，也只影响本轮请求，不会破坏持久数据。

### 持久清理：默认关闭

配置中：

```yaml
persistent:
  allow: false
  require_admin: true
  admin_ids: []
  admin_names: []
  require_confirmation: true
  encrypt_backups: true
  allow_legacy_plaintext_backups: false
  audit_log: true
  command_cooldown_seconds: 30
  backup_retention_days: 15
```

- `admin_ids` / `admin_names` 非空时，任一命中即可执行；名称区分大小写
- 两者都为空时回退 AstrBot 的 `is_admin()`
- `require_confirmation` 开启时必须 `/节省上下文 run confirm`
- 备份默认 AES-GCM 加密，密钥保存在插件数据目录并限制为 600
- 默认拒绝恢复旧明文 `.json` 备份
- 持久命令有冷却时间
- 审计日志只记录操作和统计，不记录消息正文
- 备份带 SHA-256 校验，恢复时验证

只有管理员显式开启 `persistent.allow` 后，才允许：

```text
/节省上下文 run confirm
/节省上下文 restore <文件名> confirm
```

持久清理使用 AstrBot 官方：

```python
self.context.conversation_manager.get_conversation(...)
self.context.conversation_manager.update_conversation(...)
```

不使用 SQLite，不直接改 `data_v4.db`。

写前会生成本地逻辑备份：

```text
data/plugin_data/astrbot_plugin_context_hygiene/backups/
```

默认保留 15 天。

## 命令

```text
/节省上下文 status
/节省上下文 dry-run
/节省上下文 run confirm
/节省上下文 backups
/节省上下文 restore <文件名> confirm
```

- `status`：只读统计
- `dry-run`：只预览，不写入
- `run`：需要开启持久清理并具备管理员权限
- `backups`：列出逻辑备份
- `restore`：从备份恢复当前会话

## 默认规则

```yaml
enabled: true
threshold_chars: 60000
threshold_tokens: 24000
keep_recent_turns: 10

reminder:
  dedupe: true
  keep_latest: true

tools:
  ttl_minutes: 10
  keep_recent_pairs: 3
  remove_orphan: true
  summarize_long: false
  max_result_chars: 4000

images:
  clean_old: true
  placeholder: "[图片已省略]"

compression:
  enabled: true
  keep_ratio: 0.28
  min_chars: 12000

persistent:
  allow: false
  require_admin: true
  backup_retention_days: 15
```

### 压缩比例

`keep_ratio: 0.28` 表示旧文本保留约 28%，不是删除 28%。

最近 `keep_recent_turns` 轮不压缩；当前消息永远保留。

压缩是确定性的首尾截断，不调用 LLM。

### 工具调用

工具调用必须成对处理：

```text
assistant tool_calls
tool result
```

默认保留最近 3 对，删除更早的完整对和孤立 `tool` 消息。

`ttl_minutes` 只在消息带有时间戳时参与判断；请求上下文通常没有时间戳，因此主要按保留对数控制。

超长工具结果默认不压缩；开启后只做确定性截断。

## 安全去重与残留清理

只处理最近窗口之外的旧消息：

- 完全相同的旧用户消息只保留最新一条
- 完全相同的旧工具结果只保留最新一对（完整 assistant + tool）
- 删除空的 assistant/think 残留
- 删除只剩 `system_reminder` 的旧消息

当前消息和最近 `keep_recent_turns` 轮不参与去重/删除。

## 工具结果与图片

工具结果截断只作用于旧消息，并保留开头和结尾，避免丢失错误码/退出状态：

```yaml
tools:
  summarize_long: false
  max_result_chars: 4000
```

旧图片：

- 当前/最近轮不替换
- 从 `alt` / `caption` / `emotion` / `tag` 等字段提取 2–4 字短标签
- 同一旧消息多张图片合并成一个占位符，并保留数量
- 不删除原始图片文件

## Prompt Cache

为减少前缀变化：

```yaml
compression:
  cache_friendly: true
  cache_prefix_messages: 4
```

开启后前 N 条消息保持原样，不参与压缩、去重、图片替换或工具裁剪；压缩仍然只做确定性截断，不调用模型。

插件还会缓存已经清理过的历史前缀：新消息只清理尾部，旧前缀保持稳定。前缀内容发生变化时缓存自动失效并重新清理。

这不是保证命中上游缓存，只是尽量避免无意义地改写稳定前缀。

## 隐私

- 不记录消息正文
- 不上传任何内容
- 不访问外部网络
- 备份只保存在本地插件数据目录
- 备份文件权限尽量设为 600
- 备份目录权限尽量设为 700

## 限制

- 请求级清理不会减少数据库体积，只减少发给模型的上下文
- 持久清理会改写当前会话历史，必须备份后再用
- 不支持跨版本数据库结构
- 不处理 memory5 的日记数据
- 不删除原始图片文件

## 工具定义清理

这是**独立于工具门控**的第一项功能。

### 微弱模式（默认开启）

清理所有工具定义，但只做不改变能力的操作：

- 移除 inactive 工具
- 按工具名去重
- 清理描述里的多余空白
- 不修改参数 schema
- 不修改 handler

```yaml
tools:
  cleanup:
    enabled: true
    mode: weak
    remove_inactive: true
    normalize_whitespace: true
    deduplicate: true
```

### 强力模式（cleanup 的子模式）

在微弱模式基础上，额外截断超长工具描述：

```yaml
tools:
  cleanup:
    enabled: true
    mode: strong
    strong:
      max_description_chars: 1200
      protected_tool_patterns:
        - memory
        - remember
        - 记忆
        - 日记
        - rag
```

仍然不修改参数 schema、handler 和工具名。  
默认只截断官方工具描述，第三方插件工具和记忆相关工具不截断。  
每个工具独立包裹，单个工具异常不会影响整个 ToolSet。

## 官方工具门控

这是**独立的第二项功能**，默认关闭。

根据消息内容，只提供相关官方工具：

- 代码/运行 → Python/IPython/Shell
- URL/网页 → Browser
- 普通/不确定 → 按 `fallback_all` 保留全部
- 正在进行的工具调用 → 必留
- 最近使用过的官方工具 → 保留
- 第三方插件工具 → **永远保留，不参与门控**

```yaml
tools:
  gating:
    enabled: false
    fallback_all: true
    keep_recent_minutes: 10
    official_tool_names:
      - astrbot_execute_shell
      - astrbot_execute_python
      - astrbot_execute_ipython
      - astrbot_execute_browser
      - astrbot_execute_browser_batch
```

`official_tool_names` 是**补充名单**。  
插件还会根据工具的 `handler_module_path` 自动识别 AstrBot 官方工具：

```text
astrbot.core.*
astrbot.builtin_stars.*
astrbot.astrbot.*
```

这样官方版本新增/改名工具时，不需要每次都手工补全名单。  
第三方插件工具、MCP 工具、没有模块路径的工具不参与门控。

## 兼容性

- 目标版本：AstrBot >= 4.29.0
- `StarTools` 不可用时回退到 AstrBot 数据路径
- `ConversationManager` 不可用时，持久命令安全拒绝
- 兼容 `update_conversation` 关键字和位置参数两种签名
- 请求过滤不依赖数据库或私有字段

## 版本

`v1.1.8`
