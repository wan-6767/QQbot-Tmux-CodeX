# 故障排查

按「服务器环境 → QQ平台资格 → QQ连接 → 本人绑定 → 终端桥接 → 输出投递」检查。不要只看到HTTP200就认为QQ链路正常，也不要通过重置绑定或清空数据来试错。

| 现象 | 先查哪里 | 处理方式 |
| --- | --- | --- |
| `Permission denied` 访问Docker | 当前普通用户的Docker权限 | 正确配置Docker使用权限，重新登录；不要把桥接改成root |
| `tmux socket does not exist` | 当前用户是否有tmux会话 | `tmux ls`；使用 `tmux display-message -p '#{socket_path}'` 的真实结果 |
| `systemctl --user` 无法连接 | 用户systemd会话 | 从正常用户SSH登录；无用户systemd时用进程管理器启动生成unit中的相同命令 |
| SSH退出后桥接停止 | 用户linger | 管理员执行 `sudo loginctl enable-linger "$USER"` |
| QQ token获取失败/401 | AppID、AppSecret是否属于同一个bot | 核对本地 `bot.env`；轮换后重新创建该容器，不在Issue贴原值 |
| 接口访问源IP不在白名单 | QQ平台IP白名单 | 配置实际出口公网IP；代理或更换网络后重新核对 |
| QQ连接起来但收不到消息 | 正式环境资格、使用范围和客户端资料卡 | 当前项目不切换沙箱；检查消息列表/群场景，确认添加的是正确AppID |
| 别的群能拉，这个群不能拉 | 平台允许范围和添加者身份 | 对照QQ控制台实际资格；代码不能绕过平台审核和群权限 |
| 私聊没回复，无法成为所有者 | 是否发送了本地的一次性 `/bind ...` | `python3 scripts/manage.py pairing default`，只在本人私聊发送；已绑定后不再生成新所有者 |
| 群里无回复 | 是否@正确bot、是否用它自己的群票据绑定 | 私聊 `/group status`；票据10分钟有效，QQ号/群号不能代替OpenID |
| `/tmux ls` 提示桥接失败 | 本机桥接服务、令牌、端口、socket | 检查用户unit日志及client.json中的地址；不要暴露桥接到公网 |
| 窗格被占用 | 另一个bot是否仍连接 | 在原bot退出或等双方静默30分钟；不要删除正在使用的锁文件 |
| 发送超时或提交状态不明 | 输入收据与终端原始画面 | 先 `/tmux sel 编号 list100` 核对，避免重复提交 |
| 远程服务器无法连接 | hosts配置、文件权限、已核验主机指纹、出站SSH | 见[SSH接入](remote.md)，不关闭主机校验；本地窗格可继续使用 |
| 终端有输出但QQ没有追加 | 是否只出现被隐藏的工具日志，或QQ主动消息被拒绝 | List100核对；重新@提供被动窗口；平台限制未解除时保留队列重试 |
| 段落似乎漏了/未知TUI | 屏幕采样与菜单识别限制 | List100核对，提交脱敏最小复现；不要提供真实终端历史 |
| 输入框 `/` 没有面板 | QQ能力/客户端同步、panel日志 | 手动发 `/tmux help` 不受影响；等待客户端同步或检查能力 |
| 上传图片/文件被拒绝 | 不是识图或文件服务 | 这是明确边界；本项目只接受文字终端输入 |

## 收集最少必要诊断

在仓库根目录：

```bash
python3 --version
tmux -V
tmux ls
systemctl --user status qq-tmux-bridge-default.service --no-pager
journalctl --user -u qq-tmux-bridge-default.service -n 40 --no-pager
docker compose --env-file instances/default/compose.env -p qq-tmux-default ps
docker compose --env-file instances/default/compose.env -p qq-tmux-default logs --tail 50
```

公开问题附系统/版本、执行步骤、预期与实际行为、**脱敏**错误即可。不要附 `docker inspect` 原始输出、完整 `.env`、owner/group JSON、数据库、聊天记录或截图中的密钥。状态文件不是可以随便粘贴的诊断包。

敏感问题按 [SECURITY.md](../SECURITY.md) 私下报告。QQ资格与接口规则参考 [官方文档](https://bot.q.qq.com/wiki/)，不要用客户端表现推断平台已授权全部能力。
