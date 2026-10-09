# Sub2API额度插件（可选）

同一个bot提供`/sub2api usage`，仅绑定本人可查询；群中需@bot。无需已连接终端，不调用LLM，不将查询文字写进tmux。

每次强制刷新本机Sub2API账号窗口及Codex积分，核对更新时间推进，不把缓存或失败显示成最新0%。报告包含名称、等宽8格剩余进度、实际5h/7d/30d窗口、北京时间重置时间及积分；429/5xx保留错误标签，账号凭据不返回。

## 部署

这是可选宿主服务，终端主功能不要求安装Sub2API。示例位于`deploy/sub2api.example.json`及`deploy/qq-tmux-usage.service.example`。

```bash
umask 077
mkdir -p instances/default/host-services/sub2api
cp deploy/sub2api.example.json instances/default/host-services/sub2api/config.json
nano instances/default/host-services/sub2api/config.json
nano instances/default/host-services/sub2api/admin-key
chmod 600 instances/default/host-services/sub2api/*

PYTHONPATH=src python3 -m tmux_bot.sub2api.service --init-token \
  --token-file instances/default/data/sub2api-usage/token

cp deploy/qq-tmux-usage.service.example instances/default/qq-tmux-usage.service
nano instances/default/qq-tmux-usage.service
systemctl --user enable --now "$PWD/instances/default/qq-tmux-usage.service"
```

在本地编辑器填写本机Sub2API原点和管理员密钥路径，密钥正文只写admin-key文件，不写入JSON、聊天、Git或命令行。
示例用户单元假定仓库为`%h/QQbot-Tmux`，实际路径和实例名必须改成自己的值；宿主需要Python3.11+，不用额外Python依赖。
服务默认监听127.0.0.1:18014。需要换端口时，在service的ExecStart中设置`--port 18214`，并执行`python3 scripts/manage.py configure default --usage-port 18214`，随后重新加载service、重建QQ容器。两端端口必须一致；不能与桥接端口共用。
bot只持有独立查询令牌，管理员密钥放在数据挂载外的host-services目录，不挂入QQ容器。上游只允许环回或私有IP原点，拒绝重定向，不暴露任意管理员API或执行命令接口。

## 验证和限制

发送`/sub2api usage`会先确认刷新，再发送完整Markdown报告。未配置、刷新失败、积分未提供、无限积分和0积分分别显示，不做猜测。
多人/并发重复查询会合并限制；后台任务结束前若本人授权撤销，结果不再发送。群报告对全群可见。
当前对应Sub2API的账号批量usage和OpenAI quota接口；上游版本或账号类型不支持的字段标为未知。
可测试`tests/test_usage_reader.py`及`test_usage_service.py`，覆盖强制刷新、快照新鲜度、凭据白名单、积分精度、固定宽度排版、鉴权和错误状态。
