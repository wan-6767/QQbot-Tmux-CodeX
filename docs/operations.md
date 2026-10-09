# 维护和部署

## 源码、配置、数据

源码位于 `src/tmux_bot/`，QQ依赖快照位于 `vendor/`。运行数据只在 `instances/<name>/`。容器不绑定宿主源码，因此修改Python文件不会热更新已运行的bot；升级必须重新构建镜像并重建对应实例。

一个AppID只运行一个实例。多个实例各用独立端口、Compose项目名和数据目录，桥接使用同一个共享锁目录。跨仓库部署时也要指定同一个锁目录、同一个socket；不要删除活跃锁文件。

## 状态和停止

```bash
docker compose --env-file instances/default/compose.env -p qq-tmux-default ps
systemctl --user status qq-tmux-bridge-default.service --no-pager

# 停止QQ入口和桥接，但不关闭tmux任务。
docker compose --env-file instances/default/compose.env -p qq-tmux-default stop
systemctl --user stop qq-tmux-bridge-default.service
```

平台连接健康不等于用户已收到内容。升级后核对QQ READY、本人绑定、`/tmux ls`、进入上下文、文字/按键回显和自动追加；没有真实QQ回执时不要宣称投递已验证。

## 备份、升级、回滚

先停对应QQ入口及桥接，再备份它的整个实例目录；SQLite、发送进度、所有者和令牌必须成套保留。备份含密钥和终端历史，放在受保护目录、权限0600，绝不能提交Git或公开网盘。

```bash
umask 077
mkdir -p backups
tar -czf backups/default-before-upgrade.tar.gz instances/default

git fetch --tags
# 审查对应版本后再切换，不覆盖自己未提交的改动。
git switch --detach v0.2.0
docker compose --env-file instances/default/compose.env -p qq-tmux-default up -d --build
systemctl --user start qq-tmux-bridge-default.service
```

上例中的 `backups/` 已被忽略。升级验证失败，停止新版本，切回旧提交或镜像，并恢复一致的实例备份；已成功输入终端的命令不能通过代码回滚撤销。不要盲目覆盖仍在运行的数据库，也不要删除所有者绑定来“修复”连接。

## v0.2 多终端升级

升级QQ镜像及宿主桥接必须同步，HTTP协议变为v2；旧v1接口不再提供运行入口。升级保留本人和群绑定，旧单窗格连接停止订阅，不自动复制为多条连接；先 `/tmux ls`，再用三位编号 `ent` 接入。

`data/tmux-relay/multi.sqlite3`保存固定编号和全局输入收据，`channels/001/bridge.sqlite3`保存该连接，`channels/001/delivery.json`保存QQ投递进度；两侧均需备份。编号永不复用，长期用尽999个编号时须先备份、断开所有连接并维护注册表，不能直接覆盖活跃编号库。

远程密钥只由宿主读取，按[SSH接入](remote.md)配置和核验。远端SSH超时不自动重新提交输入。单连接的窗口关闭/静默断开通知确认后删除订阅，其余连接继续；所有窗格共用QQ额度和发送调度。

## 离线接入助手（可选）

产品介绍与接入教程统一在 GitHub 的 [README](../README.md) 和 `docs/` 阅读，不需要部署独立网站。

`site/index.html` 是可选的离线接入助手，可以直接用浏览器打开，无需bot服务、大模型、Node.js 或网页服务器。它只保存实例名、端口和手动勾选的接入进度，不请求AppSecret、不连接终端、不做自动安装、不收集遥测。

仅开发和测试此助手时需要 Node.js：

```bash
npm --prefix site ci
./site/node_modules/.bin/playwright install chromium
npm --prefix site test
npm --prefix site run build
```

构建只导出离线资源到 `dist/site/`，不复制任何 `instances/` 或服务器配置。`Guide Checks` 工作流仅执行浏览器回归、构建并保存离线 artifact，没有 Pages 部署任务或网站发布权限。

## 发布检查

```bash
python3 scripts/check_release.py --indexed
docker build --target test -t qq-tmux-relay-test .
docker run --rm --network none qq-tmux-relay-test
```

只发布已审查的源码和静态页面。检查器读取真实Git暂存blob，不能替代人工审阅所有历史提交和许可证。
