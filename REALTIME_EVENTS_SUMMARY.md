# WebSocket 实时事件流 - 快速总结

## 问题
❌ 后台执行了多次工具调用，但前端只能看到最终结果，看不到中间过程

## 原因
1. `nanobot/agent/loop.py`：执行工具时只记录日志，不发送事件
2. `nanobot/channels/websocket.py`：只支持发送最终响应消息

## 解决方案

### 修改 1：websocket.py（支持多种消息类型）

```python
# 从 metadata 读取消息类型
msg_type = msg.metadata.get("type", "message") if msg.metadata else "message"

response = {
    "type": msg_type,  # 可以是 message/tool/event/thinking
    "content": msg.content,
    ...
}

# 工具事件包含额外信息
if msg_type == "tool":
    response["tool"] = msg.metadata.get("tool_name")
    response["arguments"] = msg.metadata.get("arguments")
```

### 修改 2：loop.py（发送工具执行事件）

```python
for tool_call in response.tool_calls:
    # 发送事件到前端
    await self.bus.publish_outbound(OutboundMessage(
        channel=msg.channel,
        chat_id=msg.chat_id,
        content=f"正在执行工具: {tool_call.name}",
        metadata={
            "type": "tool",
            "tool_name": tool_call.name,
            "arguments": tool_call.arguments
        }
    ))
    
    # 执行工具
    result = await self.tools.execute(...)
```

## 效果

### 之前
```
用户: 帮我生成pdf
[等待30秒...]
Bot: 完成了
```

### 现在
```
用户: 帮我生成pdf
🔧 正在执行工具: exec
🔧 正在执行工具: write_file
🔧 正在执行工具: exec
Bot: 完成了
```

## 如何测试

```bash
# 1. 启动服务
nanobot gateway

# 2. 打开调试界面
open examples/websocket-ui/public/debug-connection.html

# 3. 连接并发送消息
"帮我生成一个hello world的pdf"

# 4. 观察实时的工具执行事件
```

## 修改的文件

1. ✅ `nanobot/channels/websocket.py` - 支持多种消息类型
2. ✅ `nanobot/agent/loop.py` - 发送工具执行事件
3. ✅ `docs/WEBSOCKET_EVENTS.md` - 详细文档
4. ✅ `examples/test_realtime_events.md` - 测试指南
5. ✅ `CHANGELOG_WEBSOCKET_EVENTS.md` - 完整变更日志

## 技术细节

- ✅ 向后兼容：不影响现有功能
- ✅ 前端已支持：app.js 已有事件处理逻辑
- ✅ 最小改动：仅约30行核心代码
- ✅ 异步发送：不阻塞工具执行
- ✅ 支持中文：使用 ensure_ascii=False

## 相关文档

- [完整文档](docs/WEBSOCKET_EVENTS.md) - 详细的功能说明和扩展建议
- [测试指南](examples/test_realtime_events.md) - 测试步骤和用例
- [变更日志](CHANGELOG_WEBSOCKET_EVENTS.md) - 完整的技术细节
