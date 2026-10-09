(() => {
  'use strict';
  const STORAGE = 'qqbot-tmux-guide-v1';
  const repo = 'https://github.com/wan-6767/QQbot-Tmux-CodeX';
  const views = { setup: '接入工作台', overview: '产品概览', commands: '指令手册', operations: '运行维护', troubleshooting: '故障排查', security: '安全边界' };
  const checks = ['服务器环境就绪', 'QQ bot 已创建', '桥接与接入已启动', '本人绑定并验收'];
  const hints = ['Linux · tmux · Docker Compose', '正式接口可用 · 凭据已填', '查看服务状态与 QQ READY', '列出真实终端 · 收到新增输出'];
  let state = { name: 'default', port: '18010', step: 0, checks: [false, false, false, false] };
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE));
    if (saved && typeof saved === 'object') {
      if (typeof saved.name === 'string' && validName(saved.name)) state.name = saved.name;
      if (typeof saved.port === 'string' && validPort(saved.port)) state.port = saved.port;
      if (Number.isInteger(saved.step) && saved.step >= 0 && saved.step < 4) state.step = saved.step;
      if (Array.isArray(saved.checks) && saved.checks.length === 4) state.checks = saved.checks.map(v => v === true);
    }
  } catch { /* The guide also works with browser storage disabled. */ }
  let category = '全部', query = '', toastTimer;
  const main = document.querySelector('#main');
  const icon = name => `<i data-lucide="${name}" aria-hidden="true"></i>`;
  const link = (label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}${icon('arrow-up-right')}</a>`;
  const valid = () => validName(state.name) && validPort(state.port);
  function validName(value) { return /^[a-z][a-z0-9-]{0,39}$/.test(value); }
  function validPort(value) { return /^\d+$/.test(value) && Number(value) >= 1024 && Number(value) <= 65535; }
  function save() { try { localStorage.setItem(STORAGE, JSON.stringify(state)); } catch {} }
  function icons() { window.lucide.createIcons(); }
  function notify(message) {
    const toast = document.querySelector('#toast');
    toast.textContent = message; toast.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.hidden = true; }, 2800);
  }
  function heading(title, text, eyebrow = 'QQBOT-TMUX / 0.2.2') {
    return `<div class="page-heading"><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p class="subheading">${text}</p></div>`;
  }
  function codeTool(label, key) {
    return `<div class="code-tool"><div class="code-toolbar"><span>${label}</span><button class="icon-button" data-copy-code="${key}" title="复制命令" aria-label="复制${label}">${icon('copy')}</button></div><pre data-code="${key}"></pre></div>`;
  }
  function commands() {
    const n = state.name, p = state.port;
    const compose = `docker compose -f deploy/compose.yaml --env-file instances/${n}/compose.env -p qq-tmux-${n}`;
    const unit = `qq-tmux-bridge-${n}.service`;
    return {
      prepare: 'python3 --version\ntmux -V\ndocker version\ndocker compose version',
      initialize: `git clone https://github.com/wan-6767/QQbot-Tmux-CodeX.git QQbot-Tmux\ncd QQbot-Tmux\n\n# 已有 tmux 会话时，跳过下一行。\ntmux new-session -d -s work\npython3 scripts/manage.py init ${n} --port ${p} \\\n  --socket "$(tmux display-message -p '#{socket_path}')" \\\n  --tmux-binary "$(command -v tmux)"`,
      credentials: `nano instances/${n}/bot.env`,
      launch: `systemctl --user enable --now \\\n  "$PWD/instances/${n}/${unit}"\n${compose} up -d --build`,
      linger: 'sudo loginctl enable-linger "$USER"',
      pairing: `python3 scripts/manage.py pairing ${n}`,
      acceptance: '/tmux ls\n/tmux sel 001 ent\n/tmux sel 001 tail 100\n/tmux sel 001 ext',
      status: `systemctl --user status ${unit} --no-pager\n${compose} ps\n${compose} logs --tail 50`,
      restart: `${compose} up -d --build`,
      stop: `${compose} stop\nsystemctl --user stop ${unit}`,
    };
  }
  function updateCode() {
    const data = commands();
    document.querySelectorAll('[data-code]').forEach(el => { el.textContent = valid() ? data[el.dataset.code] : '请先填写有效的实例名称和端口。'; });
    document.querySelectorAll('[data-copy-code]').forEach(el => { el.disabled = !valid(); });
  }
  function fields() {
    return '<div class="settings"><label class="field">实例名称<input id="instance-name" autocomplete="off" spellcheck="false" maxlength="40" aria-describedby="name-hint name-error"><small id="name-hint">小写字母开头，可含数字和连字符</small><span class="error" id="name-error"></span></label><label class="field">本机桥接端口<input id="bridge-port" type="number" min="1024" max="65535" step="1" inputmode="numeric" aria-describedby="port-hint port-error"><small id="port-hint">1024–65535 · 每个 bot 独立</small><span class="error" id="port-error"></span></label></div>';
  }
  function bindFields() {
    const name = document.querySelector('#instance-name'), port = document.querySelector('#bridge-port');
    name.value = state.name; port.value = state.port;
    function validate() {
      name.setAttribute('aria-invalid', String(!validName(state.name)));
      port.setAttribute('aria-invalid', String(!validPort(state.port)));
      document.querySelector('#name-error').textContent = validName(state.name) ? '' : '需为 1–40 位小写字母、数字或连字符。';
      document.querySelector('#port-error').textContent = validPort(state.port) ? '' : '请输入 1024–65535 之间的整数。';
      updateCode();
    }
    name.addEventListener('input', () => { state.name = name.value; validate(); if (valid()) save(); });
    port.addEventListener('input', () => { state.port = port.value; validate(); if (valid()) save(); });
    validate();
  }
  function setup() {
    const stages = ['准备环境', '创建 QQ bot', '部署接入', '绑定与验收'];
    const bodies = [
      `<div class="step-intro"><h2>先让服务器准备好</h2><p>在运行 tmux 的 Linux 服务器上，以同一个普通用户执行。</p></div><div class="requirements"><span>${icon('monitor')}Linux</span><span>${icon('terminal')}Python ≥ 3.11 · tmux</span><span>${icon('container')}Docker Compose v2</span></div>${codeTool('检查运行环境', 'prepare')}${fields()}${codeTool('下载源码并初始化', 'initialize')}<p class="code-caption">${icon('info')}已有会话不必重建；初始化不会覆盖同名实例。Docker 安装见官方文档。</p>`,
      `<div class="step-intro"><h2>创建一个属于你的 QQ bot</h2><p>使用腾讯官方接口，不需要域名、证书或公网回调服务。</p></div><ol class="guide-list"><li><strong>注册并创建机器人</strong><p>在 QQ 开放平台登录、创建 bot，按控制台要求完成资料和接入资格。</p></li><li><strong>确认可用场景</strong><p>需要消息列表单聊；群聊另行确认使用范围。启用 IP 白名单时填写服务器公网出口 IP。</p></li><li><strong>取得 AppID 与 AppSecret</strong><p>只在服务器的 bot.env 中填写 QQ_APP_ID 和 QQ_CLIENT_SECRET，不要发送到群或 Issue。</p></li><li><strong>添加机器人</strong><p>使用控制台提供的资料卡或二维码添加，保留私聊以完成本人绑定。</p></li></ol>${fields()}${codeTool('在服务器填写凭据', 'credentials')}<div class="notice"><strong>正式接口与沙箱不同。</strong> 本项目固定使用正式接口，目前没有沙箱切换。入群资格、审核和主动消息额度由 QQ 平台决定。</div><div class="link-row">${link('QQ 开放平台', 'https://q.qq.com/')}${link('官方接入文档', 'https://bot.q.qq.com/wiki/')}</div>`,
      `<div class="step-intro"><h2>启动桥接与 QQ 接入</h2><p>本机桥接操作 tmux，容器负责 QQ 收发。桥接端口不对公网开放。</p></div>${fields()}${codeTool('启动服务', 'launch')}${codeTool('退出 SSH 后继续运行', 'linger')}<p class="code-caption">${icon('shield-check')}仅这一项由管理员执行一次。不要用 sudo 初始化实例，也不要将桥接改成 root。</p>${codeTool('检查服务与日志', 'status')}<div class="notice">容器健康不等于绑定成功。需要下一步在 QQ 实际列出窗格，并收到终端输出。</div>`,
      `<div class="step-intro"><h2>把本人 QQ 连接到终端</h2><p>口令由你的服务器生成，页面不生成、不读取，也不保存绑定密钥。</p></div>${fields()}${codeTool('获取一次性绑定指令', 'pairing')}<p>将输出的完整 <code>/bind …</code> 发到 bot 的 QQ 私聊。24 小时有效，绑定成功后销毁。</p>${codeTool('在 QQ 逐条验收', 'acceptance')}<div class="section"><h3>可选：在群里使用</h3><p class="muted">先将 bot 加入群，再私聊发 <code>/group bind</code>，将返回的完整指令 @该 bot 发到群里。票据 10 分钟有效，之后每条输入都要 @bot。</p><div class="notice">只有绑定本人能操作，但输出对全群可见。多个 bot 必须用独立凭据和端口；同一仓库下共享窗格互斥锁。</div></div>`,
    ];
    main.innerHTML = heading('QQbot-Tmux', '从创建 bot 到连接终端，让你现有的工作会话在 QQ 中继续。') +
      `<div class="workspace"><div class="workspace-main"><div class="stepper" role="tablist" aria-label="接入步骤">${stages.map((name, i) => `<button role="tab" id="step-${i}" aria-controls="step-body" aria-selected="${state.step === i}" data-step="${i}"><span class="step-number">0${i + 1}</span>${name}</button>`).join('')}</div><div id="step-body" role="tabpanel" aria-labelledby="step-${state.step}">${bodies[state.step]}</div><div class="step-actions"><button class="button" data-prev ${state.step === 0 ? 'disabled' : ''}>${icon('arrow-left')}上一步</button>${state.step < 3 ? '<button class="button primary" data-next>下一步' + icon('arrow-right') + '</button>' : '<a class="button primary" href="#commands">指令手册' + icon('arrow-right') + '</a>'}</div></div><aside class="checklist" aria-label="手动接入确认"><div class="checklist-heading"><h2>接入检查</h2><button class="icon-button" id="reset-progress" title="重置本地接入记录" aria-label="重置本地接入记录">${icon('rotate-ccw')}</button></div><div class="progress-count"></div><div class="progress-track"><span></span></div>${checks.map((label, i) => `<label><input type="checkbox" data-check="${i}"><span>${label}<small>${hints[i]}</small></span></label>`).join('')}<div class="aside-note">${icon('lock-keyhole')}<p>仅保存本机的实例名称、端口和手动检查记录。</p><p>AppSecret 请始终留在服务器。</p></div><div class="link-row">${link('完整接入文档', repo + '/blob/main/docs/getting-started.md')}${link('Docker 安装', 'https://docs.docker.com/engine/install/')}</div></aside></div>`;
    bindFields();
    document.querySelectorAll('[data-step]').forEach(el => el.addEventListener('click', () => { state.step = Number(el.dataset.step); save(); render(); }));
    document.querySelector('[data-prev]').addEventListener('click', () => { state.step--; save(); render(); });
    document.querySelector('[data-next]')?.addEventListener('click', () => { state.step++; save(); render(); });
    document.querySelectorAll('[data-check]').forEach(el => {
      el.checked = state.checks[Number(el.dataset.check)];
      el.addEventListener('change', () => { state.checks[Number(el.dataset.check)] = el.checked; save(); progress(); });
    });
    document.querySelector('#reset-progress').addEventListener('click', () => {
      state = { name: 'default', port: '18010', step: 0, checks: [false, false, false, false] }; save(); render(); notify('本地接入记录已重置');
    });
    progress();
  }
  function progress() {
    const n = state.checks.filter(Boolean).length;
    document.querySelector('.progress-count').textContent = `已手动确认 ${n} / 4`;
    document.querySelector('.progress-track span').style.width = `${n * 25}%`;
  }
  function overview() {
    main.innerHTML = heading('QQbot-Tmux', '通过 QQ 操作现有 tmux 会话的开源工具。手机上接着聊，服务器上接着跑。', 'OPEN SOURCE / MIT') +
      `<div class="content-width"><p class="lead">不新建 AI 对话，不让模型代按键。选择一个已有窗格，QQ 消息直接送进终端，程序的新增回答再回到聊天。</p><div class="link-row"><a class="button primary" href="#setup">${icon('plug-zap')}开始接入</a>${link('查看源码', repo)}</div><div class="pipeline"><div>${icon('messages-square')}<strong>QQ 私聊 / 群聊</strong><small>官方 bot · 本人绑定</small></div>${icon('arrow-right')}<div>${icon('cable')}<strong>认证本机桥接</strong><small>私有令牌 · 窗格互斥</small></div>${icon('arrow-right')}<div>${icon('terminal')}<strong>原有 tmux 会话</strong><small>现有任务 · 原有权限</small></div></div><div class="feature-grid">${[
        ['text', '回答按段落追加', '进入时返回最近 100 行，之后继续接收新内容。隐藏操作噪音，需要时查看完整快照。'],
        ['keyboard', '菜单也能操作', '模型选择、确认框完整回显。通过 enter、方向键和 backspace 控制，不依赖消息按钮。'],
        ['panels-top-left', '一个 bot，多个终端', '本地和SSH远端统一编号。每条连接独立追加、计时和恢复，退出一个不影响其他。'],
        ['rotate-cw', '重启后仍可接续', '保存输入收据与待投递队列。双方静默 30 分钟才断开，终端任务继续运行。'],
      ].map(([i, title, text]) => `<div class="feature">${icon(i)}<h3>${title}</h3><p>${text}</p></div>`).join('')}</div><section class="section"><h2>先把接入做好</h2><figure class="preview"><img src="assets/workbench.png" width="1440" height="1000" alt="QQbot-Tmux 接入工作台的实际桌面截图" loading="lazy"><figcaption>实际接入工作台 · 实例命令生成与逐步检查</figcaption></figure></section><section class="section"><h2>轻量，不代表没有边界</h2><p class="muted">不调用大模型，不需要模型 API Key。支持本机文件收发，可选插件查询Sub2API额度。转发来自终端屏幕采样，不是程序原生事件流；QQ 主动消息权限仍受平台约束。</p><div class="link-row"><a href="#security">安全边界 ${icon('arrow-right')}</a><a href="#troubleshooting">常见问题 ${icon('arrow-right')}</a></div></section></div>`;
  }
  const entries = [
    ['全部', '/help', '查看全局功能总览', '终端、文件、额度与群绑定；面板为/tmux ls、/tmux sel、/tmux help'],
    ['窗格', '/tmux sel', '编号和操作的输入前缀', '补上001 ent或001 send 消息后再发送'],
    ['全部', '/tmux help', '查看终端操作帮助', '所有公开指令统一使用 /'],
    ['窗格', '/tmux ls', '本地及远端窗格、编号和占用情况', '固定001–999，不随列表排序变化'],
    ['窗格', '/tmux sel 001 ent', '接入或重连这一窗格', '返回100行并持续追加，可同时接入多个'],
    ['窗格', '/tmux sel 001 ext', '仅断开这一连接', '其他编号及终端任务继续运行'],
    ['输入', '/tmux sel 001 send 文字', '原样输入指定终端，并自动回车', '所有输入都需要三位编号，发送内容使用send'],
    ['输入', '/tmux sel 001 send /model', '向终端输入程序命令', 'bot不调用模型、不代选菜单；/goal resume也需加编号和send'],
    ['输入', '/tmux sel 001 type 文字', '只输入，不回车', '可继续输入或单独发送key enter'],
    ['输入', '/tmux sel 001 send ent', '把保留词作为普通文字输入', 'ent是接入，ext是断开，tail 100是快照'],
    ['按键', '/tmux sel 001 key enter', '回车确认', '一次发送一个按键，不支持重复次数'],
    ['按键', '/tmux sel 001 key up', '向上选择或移动', 'down / left / right 同样可用'],
    ['按键', '/tmux sel 001 key backspace', '删除光标前的字符', 'delete删除光标后的字符'],
    ['按键', '/tmux sel 001 key esc', '取消菜单或返回', '菜单整体回显，不拆成变化的一行'],
    ['按键', '/tmux sel 001 key ctrl-c', '发送中断', '可能停止终端任务，谨慎操作'],
    ['按键', '/tmux sel 001 key tab', '发送Tab', '还支持space / home / end / pgup / pgdn / ctrl-d'],
    ['按键', '/tmux key ctrl+shift+left sel 001', '组合键，操作与目标可互换', 'Ctrl / Alt / Shift，支持字母、符号和F1–F24'],
    ['屏幕', '/tmux sel 001 tail 100', '最近N行原始快照', 'N可选1–5000，包含工具日志，仅重置001进度'],
    ['文件', '/file help', '查看文件收发帮助', '下载、全部或指定缓存清理及权限边界'],
    ['文件', '/file dl /home/alice/project/result.zip', '下载本机普通文件', '最多100 MiB；上传后返回绝对路径，不自动输入终端'],
    ['文件', '/file rm', '清理本bot登记的上传缓存', '不清空/tmp，不删除项目文件'],
    ['额度', '/sub2api usage', '刷新本机Sub2API窗口及积分', '可选插件，等宽进度条；管理员密钥仅由宿主读取'],
    ['群聊', '/group bind', '私聊获取一次性群绑定指令', '将完整指令 @bot 发到目标群，10 分钟有效'],
    ['群聊', '/group status', '私聊查看群绑定状态', '不同 AppID 的群成员身份不能互用'],
    ['群聊', '/group unbind', '私聊解除群绑定', '先在原群对所有连接执行/tmux sel 编号 ext'],
  ];
  function commandView() {
    main.innerHTML = heading('指令手册', '群里每条指令先 @bot；所有输入带三位编号，多个终端独立转发。') +
      `<div class="command-controls"><div class="tabs" aria-label="指令分类">${['全部', '窗格', '输入', '按键', '屏幕', '群聊'].map(c => `<button data-category="${c}" aria-pressed="${category === c}">${c}</button>`).join('')}</div><label class="search">${icon('search')}<input id="command-search" type="search" placeholder="搜索指令或用途" aria-label="搜索指令或用途"></label></div><div id="command-list" aria-live="polite"></div><p class="code-caption">${icon('info')}选择菜单整体回显；自动追加只转发正文。429 / 5xx 错误不会隐藏。</p>`;
    document.querySelector('#command-search').value = query;
    document.querySelector('#command-search').addEventListener('input', e => { query = e.target.value; commandList(); });
    document.querySelectorAll('[data-category]').forEach(el => el.addEventListener('click', () => {
      category = el.dataset.category;
      document.querySelectorAll('[data-category]').forEach(btn => btn.setAttribute('aria-pressed', String(btn === el)));
      commandList();
    }));
    commandList();
  }
  function commandList() {
    const list = document.querySelector('#command-list'); list.replaceChildren();
    entries.filter(row => (category === '全部' || row[0] === category) && row.join(' ').toLowerCase().includes(query.toLowerCase())).forEach(row => {
      const item = document.createElement('div'); item.className = 'command-row';
      const code = document.createElement('code'); code.textContent = row[1];
      const body = document.createElement('p'); body.textContent = row[2];
      const small = document.createElement('small'); small.textContent = row[3]; body.append(small);
      const button = document.createElement('button'); button.className = 'icon-button'; button.title = '复制指令'; button.setAttribute('aria-label', '复制 ' + row[1]); button.dataset.copyText = row[1]; button.innerHTML = icon('copy');
      item.append(code, body, button); list.append(item);
    });
    if (!list.childElementCount) { const p = document.createElement('p'); p.className = 'empty-state'; p.textContent = '没有匹配的指令'; list.append(p); }
    icons();
  }
  function operations() {
    main.innerHTML = heading('运行维护', '状态要看真实链路：服务在线、QQ 绑定、窗格可见、新内容可达。') +
      `<div class="content-width">${fields()}<section class="section"><h2>查看状态</h2>${codeTool('服务与 QQ 接入日志', 'status')}<p class="muted">QQ 日志出现 READY 后，还要私聊执行 /tmux ls 并观察新输出。不要将 docker inspect 或完整环境变量贴到公开问题中。</p></section><section class="section"><h2>更新与停止</h2>${codeTool('修改源码后重建接入', 'restart')}<p class="muted">镜像不热挂载源码。升级前先停服务、备份自己的 instances，再切换已审核的版本。不要直接覆盖运行数据。</p>${codeTool('停止这一实例', 'stop')}<p class="muted">停止转发不会停止 tmux 里的任务。恢复时回到“部署接入”启动同一实例。</p></section><section class="section"><h2>需要备份什么</h2><p class="muted">instances/&lt;实例名&gt;/ 保存密钥、所有者、群绑定和投递状态；共享 pane-locks 只用于运行期互斥。凭据备份应加密、限制权限，绝不能提交到 Git。</p><div class="link-row">${link('升级、备份与回滚', repo + '/blob/main/docs/operations.md')}</div></section></div>`;
    bindFields();
  }
  function troubleshooting() {
    const items = [
      ['bot 没有回复 /bind 或 /tmux ls', '先查看 QQ 接入日志是否出现 READY。确认正式环境资格、AppID/AppSecret、服务器出站网络和控制台 IP 白名单；未绑定时应先在服务器获取 /bind 指令。不要并行启动同一 AppID 的第二个接入进程。'],
      ['机器人能添加，为什么进不了目标群？', 'QQ 平台的入群范围、审核及管理员条件，与本项目的本人权限不是一回事。以开放平台控制台为准，服务器代码不能绕过平台限制。平台添加完成后，仍需私聊 /group bind 建立群成员身份。'],
      ['群里进入成功，但新输出没有继续发送', '被动回复窗口和次数耗尽后，需要平台允许主动群消息。被拒绝的内容保留队列、退避重试。重新 @bot 可提供新被动窗口；用 /tmux sel 编号 tail 100 查看即时原始屏幕，不要误以为任务已停止。'],
      ['连接不到 tmux，或列表里没有窗格', '桥接用户必须与 tmux 用户一致。检查生成的 service 中 --socket 路径确实对应已有 socket，服务和 Compose 实例名称、端口、令牌必须配套。不要改成 root 或将端口开放到公网。'],
      ['显示窗格被其他 bot 占用', '互斥正在生效。先在原 bot /tmux sel 编号 ext，再用目标 bot 连接；也可以选择不同窗格。双方静默 30 分钟会自动释放，停止原桥接进程也会释放内核锁。'],
      ['输出清洗不准确，或内容重复、遗漏', '这是终端屏幕采样，不是程序原生事件流。快速重复输出和未知 TUI 可能不易区分。先用 /tmux sel 编号 tail 100 核对，注意该命令会重置自动追加基线；提交经过脱敏的可复现片段，不上传真实终端历史。'],
      ['手机输入 / 没有快捷命令提示', '面板需要平台能力及客户端同步。默认注册 /tmux ls、/tmux sel 和 /tmux help，注册失败不影响手动发送。群聊仍需 @bot，菜单按键使用 /tmux sel 编号 key 指令。'],
      ['发送超时，是否应该重新发送？', '先查看 /tmux sel 编号 tail 100，确认终端是否已收到输入。桥接有输入收据查询，但如果最终状态仍未知，重复提交可能造成重复执行。不要为了测试把破坏性命令反复发送。'],
      ['Docker 或用户 systemd 提示权限不足', '按 Docker 官方文档配置普通用户权限；Docker 组本身具有高宿主权限。退出 SSH 后要常驻，可由管理员启用该用户 linger。不要 sudo 初始化，也不要将所有数据改成 777。'],
    ];
    main.innerHTML = heading('故障排查', '从真实故障点检查，不用重建绑定或盲目重启所有服务。') +
      `<div class="content-width">${items.map(([title, body]) => `<details class="diagnostic"><summary>${title}${icon('chevron-down')}</summary><p>${body}</p></details>`).join('')}<div class="link-row">${link('完整排障文档', repo + '/blob/main/docs/troubleshooting.md')}${link('提交脱敏问题', repo + '/issues')}</div></div>`;
  }
  function security() {
    main.innerHTML = heading('安全边界', '绑定你的 QQ，就是提供一个可信远程终端入口。') +
      `<div class="content-width"><ul class="boundary-list">${[
        ['user-round-check', '只有绑定本人可以控制', '私聊一次性绑定；群里另绑定本人群成员身份。不是面向陌生人的多用户终端服务。'],
        ['terminal', '终端有多少权限，QQ 就能操作多少', '桥接以 tmux 普通用户运行，不进行额外沙箱隔离。终端本身有 sudo 或 Docker 权限时，绑定账号也可能使用这些权限。'],
        ['eye', '群输出对所有群成员可见', '本人操作不等于本人可见。不要在群中连接含密钥、客户资料或私人对话的终端。'],
        ['lock-keyhole', '令牌和 AppSecret 只留在服务器', '实例目录权限 700、凭据文件 600，Git 和镜像构建默认排除。备份同样含有密钥，需要保护。'],
        ['cable', '本机桥接不开放公网', '环回监听、Bearer 认证，不挂载 Docker socket。QQ 接入只需要出站连接。'],
        ['database', '这个页面不读取运行数据', '只在当前浏览器保存非敏感实例名称、端口和手动确认记录。不请求终端、不收集凭据、没有遥测服务。'],
      ].map(([i, title, text]) => `<li>${icon(i)}<div><strong>${title}</strong><p>${text}</p></div></li>`).join('')}</ul><div class="link-row">${link('安全说明与报告方式', repo + '/blob/main/SECURITY.md')}${link('MIT License', repo + '/blob/main/LICENSE')}</div></div>`;
  }
  async function copy(text) {
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else {
        const input = document.createElement('textarea'); input.value = text; input.style.position = 'fixed'; input.style.opacity = '0'; document.body.append(input); input.select();
        let ok; try { ok = document.execCommand('copy'); } finally { input.remove(); }
        if (!ok) throw new Error('copy denied');
      }
      notify('已复制');
    } catch { notify('复制未成功，请手动选择文本'); }
  }
  document.addEventListener('click', e => {
    const code = e.target.closest('[data-copy-code]');
    if (code && !code.disabled && valid()) copy(commands()[code.dataset.copyCode]);
    const text = e.target.closest('[data-copy-text]');
    if (text) copy(text.dataset.copyText);
  });
  function render() {
    const key = location.hash.slice(1);
    if (key === 'main' && main.childElementCount) { main.focus(); return; }
    const view = Object.hasOwn(views, key) ? key : 'setup';
    document.querySelectorAll('[data-view]').forEach(a => { if (a.dataset.view === view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
    document.querySelector('#breadcrumb').textContent = views[view];
    document.title = `QQbot-Tmux · ${views[view]}`;
    ({ setup, overview, commands: commandView, operations, troubleshooting, security })[view]();
    icons();
  }
  addEventListener('hashchange', render);
  render();
})();
