# 离线回归 fixture

这些文件仅用于不联网的自动化测试和 HTML 渲染测试，不授权模型调用，也不是新模型的质量成绩。

- `traces/`：经原安全边界导出的固定 trace，覆盖 fixture 流程、旧版价格与模型协议兼容。历史模型记录只保留测试所需的安全导出，不包含认证 header、key 或任务数据库。
- `source-github.json`：公开 GitHub 来源的安全 trace，供 HTML 测试使用；测试不会重新获取 URL。
- `evaluation/`：用于标注版本、历史漏检口径和零评论工具判定的固定输入。
- `review-tools-semantic-s2-frozen.md`：S2 原文回归基准。
- `duplicate-findings-legacy.txt`：原安全正文，用于验证旧协议接受、新协议拒绝重复 JSON 键；未修改原结论或追认新成绩。

[provenance.json](provenance.json) 记录原工作区相对来源与复制后 SHA-256。内容逐字保留；来源路径只是历史出处，不是测试运行依赖。原始实验目录、审批、聊天、任务库和完整交付包保留在本机。安全测试中的假 key、密码与攻击文本均为测试数据，请勿替换为真实凭证。
