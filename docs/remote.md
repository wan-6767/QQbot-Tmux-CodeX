# 远程服务器接入

## 配置

一个bot可同时连接本机及多台远程服务器。在宿主桥接的普通用户下建立 `instances/<name>/data/tmux-relay/hosts.json`，参考`deploy/hosts.example.json`：

```json
{
  "version": 1,
  "servers": [
    {
      "name": "research",
      "host": "server.example.com",
      "port": 22,
      "user": "alice",
      "identity_file": "/home/alice/.ssh/id_ed25519",
      "known_hosts_file": "/home/alice/.ssh/known_hosts",
      "socket": null,
      "enabled": true
    }
  ]
}
```

`host` 为IP或主机名。`name` 使用小写字母开头、最多32位字母/数字/连字符，不能用 `local`。`port` 默认22。密钥路径是**宿主用户的文件**，不是容器路径，不能填密钥正文。`socket: null` 使用远端该用户的默认tmux；非默认socket填远端绝对路径。`enabled: false` 暂停列出该服务器。

```bash
chmod 600 instances/default/data/tmux-relay/hosts.json
chmod 600 ~/.ssh/id_ed25519
python3 scripts/manage.py hosts default
systemctl --user restart qq-tmux-bridge-default.service
```

远端默认从SSH会话的`PATH`发现tmux。若远端使用`/usr/local`、Nix store或自定义安装，在服务器条目增加`"tmux_binary": "/绝对路径/tmux"`；该路径属于远端，不是运行bot的本机。完整字段见[配置与迁移](configuration.md)。

配置重启桥接后生效。远端需要SSH密钥登录权限、Python 3.10+、tmux及该用户已有的会话，不需要常驻服务、远端安装文件或额外公网端口。不要为了连接把用户改成root。

## 安全边界

先通过可信控制台核对远端SSH指纹，再加入指定 `known_hosts`。非默认端口记录为 `[主机]:端口`。不要把未经核对的 `ssh-keyscan` 输出当可信指纹。

桥接强制 `StrictHostKeyChecking=yes`、密钥认证和 `BatchMode=yes`；未知或变更的主机密钥拒绝连接。它不读取用户SSH配置，以避免意外应用 `RemoteForward` 等副作用；此版不支持ProxyJump或密码登录。

密钥须为桥接用户所有、组和其他人不可访问。SSH控制连接目录权限700，hosts配置权限600。密钥不挂进QQ容器、不发到QQ、不上传Git。真实配置及运行数据默认忽略。

输入作为JSON经stdin传输，不拼到shell命令中。远端使用同一套tmux实现；QQ能操作的权限由SSH用户及终端程序决定。

## 使用和验收

```text
/tmux ls
/tmux sel 012 ent
/tmux sel 012 send 检查训练进度
/tmux sel 012 tail 100
/tmux sel 012 ext
```

本地和远端编号统一管理，每条回显带服务器名。重命名标签不改变真实端点编号，更换主机/用户/端口/socket视为新终端。远端暂时不可达会在列表给诊断，本地仍可用，旧远程编号保留。SSH超时不能证明输入没执行，先核对快照，不能盲目重发。

隔离验收会创建一次性本地及远端tmux服务器，测试完成后只清理测试socket，不操作已有窗格：

```bash
python3 scripts/smoke_remote.py --hosts-file instances/default/data/tmux-relay/hosts.json --server research
# 或仅为测试读取已有的可信SSH别名，不修改SSH配置：
python3 scripts/smoke_remote.py --ssh-alias S2 --server research
```
