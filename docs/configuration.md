# 配置与迁移

QQbot-Tmux把源码、私有实例数据和生成的运行配置分开。迁移时复制实例目录，再用新机器上的真实路径重新生成运行配置；不要在源码里写AppSecret、端口或绝对路径。

## 配置边界

| 配置 | 文件或入口 | 是否包含密钥 | 维护方式 |
| --- | --- | --- | --- |
| QQ AppID、AppSecret | `instances/<name>/bot.env` | 是 | 本机编辑器修改 |
| 本机端口、tmux socket、tmux路径、超时、锁目录、额度查询端口 | `data/tmux-relay/bridge.json` | 否 | `manage.py init/configure`生成 |
| 容器路径和用户ID | `instances/<name>/compose.env` | 否 | 跟随`init/configure`生成 |
| 桥接认证令牌 | `data/tmux-relay/token` | 是 | 初始化生成，不手改、不共享 |
| 远程SSH主机、端口、用户、密钥路径、远端tmux路径 | `data/tmux-relay/hosts.json` | 含敏感路径 | 参照公开示例，本机编辑 |
| 可选Sub2API地址和管理员密钥文件路径 | `host-services/sub2api/config.json` | JSON不含密钥正文 | 参照[额度插件](sub2api.md) |

`instances/`全部被Git和Docker构建上下文忽略。生成的`compose.env`和systemd unit可以查看，但应通过管理命令重建，不要把手工修改当成永久配置。

## 初始化参数

```bash
python3 scripts/manage.py init default \
  --port 18010 \
  --socket "$(tmux display-message -p '#{socket_path}')" \
  --tmux-binary "$(command -v tmux)" \
  --idle-seconds 1800 \
  --lock-dir "$PWD/instances/pane-locks"
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `name` | `default` | 小写字母开头，只允许小写字母、数字和连字符 |
| `--bot-name` | 实例名 | 消息与互斥提示使用的显示名；迁移沿用原显示名，不更改QQ平台资料 |
| `--port` | `18010` | 容器到宿主桥接的环回端口；每实例唯一 |
| `--socket` | 必填 | 当前普通用户的tmux Unix socket |
| `--tmux-binary` | `TMUX_BINARY`或`PATH`中的`tmux` | 可执行文件名或绝对路径；最终保存解析后的绝对路径 |
| `--idle-seconds` | `1800` | 双方静默断开时间，允许60–86400秒 |
| `--lock-dir` | `instances/pane-locks` | 多实例共享窗格互斥目录 |
| `--usage-port` | `18014` | 可选Sub2API查询服务的环回端口，与桥接端口不同；需和查询service的`--port`一致 |

初始化在写入实例前检查tmux、socket、端口范围、桥接实时端口占用和已有实例桥接端口配置。上述检查失败不会留下凭据或半成品实例。额度服务是独立可选服务，初始化只校验其端口范围与桥接端口不冲突，不要求它当时已运行。桥接只监听`127.0.0.1`，该地址不是开放配置项；远程QQ请求也不能指定桥接地址或令牌。

## 查看和修改

查看非敏感运行配置：

```bash
python3 scripts/manage.py show default
```

修改一个或多个字段并重生成service、`compose.env`和客户端地址：

```bash
python3 scripts/manage.py configure default \
  --port 18010 \
  --socket "$(tmux display-message -p '#{socket_path}')" \
  --tmux-binary "$(command -v tmux)" \
  --idle-seconds 1800

systemctl --user daemon-reload
systemctl --user enable --now \
  "$PWD/instances/default/qq-tmux-bridge-default.service"
systemctl --user restart qq-tmux-bridge-default.service
docker compose -f deploy/compose.yaml --env-file instances/default/compose.env \
  -p qq-tmux-default up -d --build
```

未传入的字段沿用现值。`configure`保留QQ凭据、桥接令牌、本人/群绑定、固定编号、输入收据、文件索引和发送进度。它只更新配置文件，不重启进程；已有服务必须`daemon-reload`后`restart`才能使用新配置，`enable --now`不会重启已经在运行的进程。旧版本没有`bridge.json`时必须提供`--socket`；建议同时显式提供`--tmux-binary`，完成一次配置迁移。

## 迁移到另一台机器或目录

1. 停止当前实例的QQ容器和桥接，不关闭业务tmux任务。
2. 使用`umask 077`完整备份`instances/<name>/`；它含密钥和终端状态，不得上传公开仓库。
3. 在目标机克隆相同版本源码，以将要运行tmux的普通用户恢复实例目录。
4. 启动或确认目标tmux，重新执行`manage.py configure`，传入目标机的socket、tmux路径、端口和共享锁目录。
5. 更新`hosts.json`中的SSH私钥、known_hosts和远端路径；运行`manage.py hosts <name>`验证。
6. 用生成文件的绝对路径`enable --now`桥接，再重建Compose项目。
7. 实际验证`/tmux ls`、接入、输入、自动追加、文件传输和退出；健康状态不能代替QQ真实回执。

仓库路径、Python路径、用户UID/GID和实例数据绝对路径会在`configure`时按当前机器重建，默认`instances/pane-locks`随仓库迁移。自定义的共享锁目录保持原值，需要显式用`--lock-dir`指定目标位置。不要直接复制旧systemd unit后启动，也不要只执行`systemctl start`一个尚未链接的unit名称。

## 远端tmux路径

远端默认通过SSH会话的`PATH`寻找tmux。NixOS、自定义安装或非登录shell路径不完整时，在对应服务器配置中指定：

```json
{
  "name": "research",
  "host": "server.example.com",
  "port": 22,
  "user": "alice",
  "identity_file": "~/.ssh/id_ed25519",
  "known_hosts_file": "~/.ssh/known_hosts",
  "socket": null,
  "tmux_binary": "/usr/local/bin/tmux"
}
```

`tmux_binary`是远端机器上的绝对路径；省略或设为`null`时由远端`PATH`发现。每次`/tmux ls`都会重新探测服务器，连接失败会显示服务器名和错误，其他终端仍可使用。

## QQ和接口地址

QQ凭据只从实例`bot.env`读取。QQ官方REST、鉴权和Gateway地址固定在经过测试的适配器版本中，当前不提供任意基址覆盖，以免凭据被发送到非官方地址。需要沙箱、代理或替换协议端点时，应作为适配器变更开发并重新运行完整回归，不能通过未审计环境变量绕过。

Sub2API属于可选本机插件。bot到查询服务的`SUB2API_USAGE_URL`由`--usage-port`生成，只接受`127.0.0.1`和固定查询路径；查询服务的上游`base_url`可配置为环回或私有IP原点，并拒绝重定向。管理员密钥正文独立存放。远程SSH、Sub2API和QQ三组凭据互不复用。

## 运行依赖

宿主配置工具和桥接只依赖Python标准库、tmux及SSH客户端。QQ进程依赖固定Docker基础镜像中提供的上游运行类型和安全组件；仓库中的适配器快照不是完整的裸机运行库。因此：

- 初始化、迁移、桥接和宿主测试可以直接运行。
- 启动QQ bot及完整集成回归必须使用Docker与Compose v2。
- 没有Docker时，测试发现会明确跳过QQ运行时模块，不应把跳过项报告为已验证。

完整验证命令见[验收清单](verification.md)，故障信息见[排查手册](troubleshooting.md)。
