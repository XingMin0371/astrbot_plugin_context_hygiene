# 节省上下文 · 更新说明

## v1.1.8

### 命名

- 插件中文显示名：`节省上下文`
- 主命令：`/节省上下文`
- 旧命令：`/上下文清理` 仍保留为别名

### 安全

- 持久清理默认关闭
- `admin_ids` / `admin_names` 显式管理员白名单；名称区分大小写
- `run confirm` / `restore confirm` 二次确认
- 持久命令冷却
- 本地审计日志，不记录消息正文
- 备份默认 AES-GCM 加密
- 备份 SHA-256 校验
- 默认拒绝恢复旧明文备份
- 恢复校验路径、会话、对话 ID、校验和
- 强力工具描述清理只处理官方工具
- 第三方插件工具不参与门控

### 功能

- 旧 `system_reminder` 去重，保留当前最新提醒
- 完全相同的旧用户消息去重，并处理对应旧 assistant
- 完全相同旧工具结果按完整工具对去重
- 保守删除孤立 `tool` 消息
- 工具结果截断只作用于旧消息，保留开头和结尾
- 旧图片替换为 2–4 字标签或占位符，保留数量
- 空 assistant/think 和只剩 reminder 的旧消息清理
- 上下文超阈值时做确定性压缩，不调用模型
- 官方工具门控按消息内容执行，默认关闭
- 保留最近 60 分钟使用过的官方工具和进行中调用

### Prompt Cache

- 前 150 条消息默认保持原样
- 清理后的历史前缀缓存复用
- 新消息只清理尾部
- 最近 20 分钟内有请求时跳过旧上下文清理，空闲后完整清理一次
- 活跃期间不清理工具消息
- 前缀变化时缓存自动失效
- 持久清理/恢复后主动清除缓存

### 性能

- 去掉逐条 JSON 序列化统计
- 浅拷贝消息，只复制 content/tool_calls
- 工具去重索引化
- 备份 IO 线程化
- 最近工具状态过期清理

### 默认阈值

面向 300–500 条/日消息的群聊：

```yaml
threshold_chars: 70000
threshold_tokens: 38000
keep_recent_turns: 15
cache_guard_minutes: 20

compression:
  keep_ratio: 0.35
  min_chars: 12000
  cache_friendly: true
  cache_prefix_messages: 150
```

### 兼容

- 目标 AstrBot >= 4.29.0
- `StarTools` 不可用时回退到 AstrBot 数据路径
- `ConversationManager` 不可用时拒绝持久操作
- 兼容 `update_conversation` 关键字和位置参数签名
- 旧命令 `/上下文清理` 继续可用

### 已知限制

- 多平台真实适配器未逐一实机测试
- Token 数是字符估算，不是模型 tokenizer 精确值
- `ttl_minutes` 依赖消息时间戳
- 前缀缓存是进程内缓存，重启丢失
- 持久清理仍会修改会话历史，建议仅自用或管理员开启
