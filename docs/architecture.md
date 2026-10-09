# 架构与开发

这是独立部署的QQ终端转发工具，通过固定编号控制本地和SSH远端的tmux窗格，支持文件收发和可选额度查询。命令由确定性路由处理，不调用大模型。

## 运行依赖

系统分为宿主桥接和QQ容器。宿主桥接负责tmux、SSH、稳定编号和认证HTTP，只依赖Python标准库、tmux及SSH客户端。QQ容器负责官方Gateway/REST、身份绑定、投递队列、文件与指令路由。

QQ连接层使用固定基础镜像中提供的上游`gateway`运行类型和安全辅助组件，仓库只保留经过审查的QQ适配器快照，并未复制整套运行库。因此`tmux_bot.app`不能作为无依赖Python程序直接启动：QQ bot与完整集成回归必须走Docker；宿主配置、桥接和对应测试可以裸机运行。容器入口只运行本项目的`tmux_bot.app`，不启动Agent服务或模型流程。

第三方来源和MIT版权声明保留在`NOTICE`及`vendor/`。这是一条明确的运行时依赖，不应表述为完全无依赖的独立QQ客户端。更换适配器或基础镜像时，应核对接口、运行类型、安全辅助依赖，并运行完整回归。

## 消息链路

```text
QQ私聊 / 已绑定群的本人@消息
  → TerminalAdapter：授权在附件下载之前
  → MultiGateway.dispatch：按公开命令分流
      ├ /help、/tmux help、/file help → 只返回Markdown帮助
      ├ /tmux sel 编号 操作 → 127.0.0.1宿主桥接
      │    → MultiRelay：固定编号、收据、独立租约
      │    → 本地tmux / 严格密钥SSH → 远端tmux
      │    ← 每编号Channel持续观察 → 清洗 → 段落 → QQ
      ├ 附件 / /file dl / /file rm → 登记缓存与受限快照
      └ /sub2api usage → 可选的独立宿主额度服务
```

输入、输出和文件路径不经过大模型解释。终端里运行Codex时，其模型和上下文仍由Codex自己管理。

## 修改入口

| 需求 | 主要源码 | 对应回归 |
| --- | --- | --- |
| QQ本人/群接入、媒体 | `app.py`、`owner.py` | `test_bot.py` |
| 指令格式、编号、租约和远端清单 | `multiplex.py`、`remote.py` | `test_multi.py` |
| 终端按键与实际输入 | `bridge.py` | `test_multi.py`、`test_relay.py` |
| 逐窗格监听、段落投递、重试恢复 | `multi_relay.py` | `test_multi.py` |
| 噪音、软换行、菜单与结束提示 | `terminal_relay.py` | `test_relay.py` |
| 上传登记、下载快照和缓存清理 | `terminal_files.py`、`files.py` | `test_files.py` |
| 最新额度和积分 | `sub2api/` | `test_usage_reader.py`、`test_usage_service.py` |
| QQ输入框面板 | `qq_commands.py` | `test_panel.py` |
| 初始化、私有文件与发布安全 | `scripts/` | `test_setup.py` |

本机端口、socket、tmux路径、闲置时间和锁目录由`scripts/manage.py`管理，生成内容位于实例目录。跨机器或目录迁移使用`configure`重建绝对路径，不修改凭据、绑定、编号和投递状态；参见[配置与迁移](configuration.md)。

上表源码均相对`src/tmux_bot/`，回归均相对`tests/`。单窗格旧协议兼容类保留用于历史回归；当前`TerminalAdapter`实际创建`MultiGateway`，QQ入口使用多编号协议，不开放旧版无编号选择模式。

## 状态和恢复

- `tmux-relay/multi.sqlite3`保存永久编号、聊天目的地和输入收据；编号不回收。
- `tmux-relay/channels/编号/bridge.sqlite3`保存该终端的输入状态和静默计时。
- `tmux-relay/channels/编号/delivery.json`保存QQ投递基线、尚未完成的正文及进度。
- 上传索引只记录缓存文件身份；下载通过宿主创建临时快照，发送结束后释放。

所有状态位于初始化实例的`data/`，升级应成套备份。恢复订阅会重新授权，并继续各自的观察任务；SSH失败不把旧编号分配给别的窗格。`tail N`仅重置指定连接的追加基线，`ext`不停止终端任务。

## 开发和上线

先运行[验收清单](verification.md)中的独立测试；修改清洗算法时用真实结构的脱敏终端样本，不为某个测试问句添加生产分支。桥接源码修改需重启对应用户服务，QQ运行代码修改需重建并更新实例镜像，离线助手修改不需要重启bot。

QQ容器只挂自身数据。SSH私钥与Sub2API管理员密钥由宿主读取，不能放进QQ消息或公开仓库。群输出全群可见，QQ官方接受回执不等于已经验证每个手机客户端的视觉效果。
