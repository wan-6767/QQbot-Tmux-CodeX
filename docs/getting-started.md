# 从零接入 QQbot-Tmux

本页就是完整接入教程，直接在 GitHub 阅读并依次操作即可。所有命令都在运行 tmux 的 Linux 服务器上以普通用户执行，除非明确标注 `sudo`。下载仓库后，也可以用浏览器打开 `site/index.html` 作为可选离线助手，生成自己的部署命令。

## 先确认适合你

你需要 Linux、Python 3.11及以上、tmux、Git、Docker Engine 和 Compose v2，以及一个已获 QQ 私聊场景权限的机器人。群聊是可选能力。不需要模型 API Key、域名、SSL证书或公网回调服务器；QQ接入主动建立出站 HTTPS/WebSocket，桥接只监听本机。

这是一套可信本人使用的远程终端工具，不是公网多用户服务。绑定账号可以输入终端命令，实际权限等同所选终端用户，详见 [安全边界](../SECURITY.md)。

## 1. 创建并配置 QQ bot

打开 [QQ机器人开放平台](https://q.qq.com/)，按当前入口注册/登录、创建机器人，并在开发设置取得 AppID、AppSecret。确认控制台允许你的账号使用消息列表单聊；需要群聊时另确认群场景和可使用范围。若启用 IP 白名单，填服务器实际访问 QQ 接口的公网出口 IP，而不是内网地址或域名。

通过控制台提供的资料卡或二维码添加机器人。开发者身份、审核和可添加范围以控制台为准，不保证个人账号默认开放全部能力。详细条件参考 [QQ官方接入文档](https://bot.q.qq.com/wiki/)。

本项目适配器固定连接正式接口 `api.sgroup.qq.com`，**尚未提供沙箱环境切换**。只有沙箱能力时不能靠新增一个未实现的环境变量解决，需要先确认正式环境使用资格。不要把 AppSecret 发到 QQ 群、Issue、截图或本页面。

## 2. 准备服务器

安装系统依赖，例如 Debian/Ubuntu：

```bash
sudo apt-get update
sudo apt-get install -y python3 tmux git
```

Docker安装按照 [Docker官方文档](https://docs.docker.com/engine/install/) 完成，不运行来源不明的一键脚本。确认当前普通用户有 Docker 使用权限，再检查：

```bash
python3 --version
tmux -V
docker version
docker compose version
```

Docker组本身有很高的宿主权限。不要为了绕过权限错误把整个 bot 或桥接改成 root。当前不支持 Windows/macOS 原生宿主、QQ频道或无 tmux socket 的远程转发。

## 3. 初始化独立实例

```bash
git clone https://github.com/Wan-zone/QQbot-Tmux.git
cd QQbot-Tmux

# 已有 tmux 会话时不必创建这一项。
tmux new-session -d -s work
python3 scripts/manage.py init default --port 18010 \
  --socket "$(tmux display-message -p '#{socket_path}')"

nano instances/default/bot.env
```

只在本机编辑器中填写生成的文件：

```dotenv
QQ_APP_ID=你的AppID
QQ_CLIENT_SECRET=你的AppSecret
```

`instances/default/` 保存该 bot 的凭据、令牌、所有者与群绑定及投递状态。它已被Git和Docker构建忽略；初始化拒绝覆盖已存在的实例。忘记密钥时在QQ平台轮换，然后仅更新自己的 `bot.env`。

## 4. 启动桥接与 QQ 接入

```bash
systemctl --user enable --now \
  "$PWD/instances/default/qq-tmux-bridge-default.service"
docker compose --env-file instances/default/compose.env \
  -p qq-tmux-default up -d --build
```

要在退出SSH后持续运行普通用户systemd服务，管理员执行一次：

```bash
sudo loginctl enable-linger "$USER"
```

检查服务及日志：

```bash
systemctl --user status qq-tmux-bridge-default.service --no-pager
docker compose --env-file instances/default/compose.env -p qq-tmux-default ps
docker compose --env-file instances/default/compose.env -p qq-tmux-default logs --tail 50
```

不得将18010映射到公网或反向代理。端口只用于容器与宿主的认证通信。Docker健康检查通过只说明接入活着，下一步仍要在QQ实际绑定并列出终端。

## 5. 绑定本人并完成首次使用

```bash
python3 scripts/manage.py pairing default
```

将服务器输出的完整 `/bind ...` 指令发到机器人的 **QQ私聊**，不是群里，也不是终端里。口令24小时有效、一次性使用；绑定前任何其他来信都不能取得终端权限。绑定后该命令会被销毁，重复查看会显示已绑定。

在QQ依次发送：

```text
/tmux ls
/tmux select 1
/tmux list100
/tmux exit
```

看到真实窗格、进入时约100行上下文、能够获取新输出、退出后tmux任务仍在，就是首次验收。编号对应最近一次列表；不想依赖编号时可用完整位置 `/tmux select work:0.0` 或稳定窗格ID。

## 6. 可选：在群里使用

先按QQ平台规则将bot加入目标群，再在已绑定本人的私聊发 `/group bind`。将它返回的完整一次性指令 **@该bot** 发到目标群，10分钟有效。之后群里每条指令或文字都需 @对应bot。

只有绑定的群成员身份能够控制终端，但所有群成员都能看见输出。平台允许入群与本项目允许操作是两层不同权限；管理员身份不能绕过平台使用范围。不要直接填写群号或把另一AppID的OpenID复制过来。

查看 `/group status`；解除时私聊 `/group unbind`。若此bot仍在群内连接终端，先在原群 `/tmux exit`，再修改绑定。

## 7. 可选：多个 bot

```bash
python3 scripts/manage.py init second --port 18011 \
  --socket "$(tmux display-message -p '#{socket_path}')"
nano instances/second/bot.env
systemctl --user enable --now "$PWD/instances/second/qq-tmux-bridge-second.service"
docker compose --env-file instances/second/compose.env -p qq-tmux-second up -d --build
python3 scripts/manage.py pairing second
```

每个实例用不同AppID、AppSecret、端口和Compose项目名，同一AppID不能同时启动两条QQ连接。一个bot同一时刻连接一个窗格，多个bot可以加入同一群；相同仓库实例共享窗格锁，不能抢占同一个终端。

完成接入后看 [使用手册](usage.md)、[故障排查](troubleshooting.md) 和 [维护部署](operations.md)。
