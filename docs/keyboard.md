# 键盘映射

目标与操作保持各自完整，顺序可换：

```text
/tmux sel 001 key up
/tmux key up sel 001
/tmux sel 001 key ctrl+shift+left
```

一次发送一个键或组合键；修饰符用`+`或`-`，大小写不敏感，单独字母的大小写保留。

| 键类 | 接受的名称 | tmux映射 |
| --- | --- | --- |
| 回车 | enter、return | Enter |
| 取消 | esc、escape | Escape |
| 方向 | up、down、left、right | Up、Down、Left、Right |
| 制表/空格 | tab、space | Tab、Space |
| 退格 | backspace、bs、bspace | BSpace |
| 删除 | delete、del、dc | DC |
| 插入 | insert、ins、ic | IC |
| 行首/行尾 | home、end | Home、End |
| 翻页 | pgup、pageup、ppage；pgdn、pagedown、npage | PPage；NPage |
| 反向制表 | backtab、btab、shift+tab | BTab |
| 功能键 | F1–F24 | 对应F键，实际程序/TERM支持决定响应 |
| 普通字符 | a–z、A–Z、0–9、ASCII标点 | 对应字符 |
| 容易歧义的符号 | plus、minus，或单独+、- | +、- |
| Ctrl修饰符 | ctrl、control、C | C- |
| Alt修饰符 | alt、meta、option、M | M- |
| Shift修饰符 | shift、S | S-或标准US布局大写/符号 |

组合示例：`ctrl+c`、`ctrl-d`、`C-a`、`alt+enter`、`shift+tab`、`ctrl+shift+left`、`ctrl+alt+delete`、`shift+1`、`ctrl+plus`。
组合顺序会规范化；重复修饰符、多个键堆在一起、未知名称及控制字符会拒绝，不能让tmux将错误的键名当普通文本写入。

Ctrl+C可能中断任务，Ctrl+D可能结束程序。通过终端协议发送的是按键序列，不是操作系统物理键盘事件；Fn、音量、亮度、Win/Command等系统键不能保证映射到终端。Shift符号按US布局；扩展组合键是否被区分还取决于tmux版本、TERM及目标程序。
普通文字和中文仍使用`/tmux sel 001 文字`或`type 文字`，不要用key输入整段话。Enter仍保留粘贴后等待机制，避免TUI把快速回车误判为粘贴的一部分。

映射依据：[tmux官方键名与send-keys文档](https://man.openbsd.org/tmux#KEY_BINDINGS)。
