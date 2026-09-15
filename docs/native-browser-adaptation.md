# 原生浏览器（bsk）草稿适配说明

> 交接文档 · 2026-09-07 · 脱敏版本：不含 Cookie、Token、账号隐私、正文全文、私人路径与原始浏览器日志。
> 配套实现：`scripts/native_runner.py`；对抗测试：`tests/test_native_runner.py`。

## 0. 结论

公众号后台页面的 CDP evaluate 通道不稳定（接管初期可能返回空 `{}`，早期版本报 `cdp_failed`），但
bsk 的原生命令（`observe` / `get-html` / `fill` / `click` / `upload` / `press` / `wait-ms`）在全部
关键页面上可靠。本适配以**原生命令为权威证据通道**，evaluate 仅作辅助且每次结果强制校验
（空 / `{}` / 解析失败一律视为不可靠并 fail-closed）。

## 1. 会话模型

```text
bsk session start --no-focus
  → bsk tab list --scope user        # 定位唯一带 token 的 mp.weixin.qq.com/cgi-bin 标签
  → bsk tab borrow <tab_id>          # 需人工在 Chrome 确认"允许接管标签"（每会话一次）
  → …所有操作…                       # 编辑页是 window.open 的新标签（agent 窗口内）
  → bsk tab close <编辑页/空白页>     # 自建标签全部关闭
  → bsk tab return <tab_id>          # 必须验证已回到用户窗口（见 §5）
  → bsk session stop
```

- 一个任务 = 一个 bsk 会话 + 一个借用的后台标签 + 一次一个编辑页。
- 接管 RPC 可能超时但实际已生效：超时后用 `tab list --scope agent` 复核，不要盲目重试。
- **归还失败时禁止 stop 会话**：session stop 会连带销毁 Agent Window，标签一起消失
  （实机踩坑）。正确做法是保持会话打开并提示人工处理。

## 2. 页面路由

| 页面 | 路径（query 一律不记录） | 进入方式 |
|---|---|---|
| 后台首页 | `/cgi-bin/home` | 登录后直接打开 |
| 草稿箱（列表） | `/cgi-bin/appmsg` + `action=list&type=77` | 页面内用自身 token 跳转 |
| 文章编辑页 | `/cgi-bin/appmsg` + `action=edit`（window.open 新标签） | 草稿箱 →「新的创作」→ 菜单「文章」 |

- 「新的创作」点击后弹出菜单：文章 / 选择已有内容 / 贴图 / 视频 / 播客 / 转载。
- 菜单「文章」按钮的可访问性文本是 `"文章 文章"`（图标文本+标签），精确文本匹配需同时接受
  `"文章"` 与 `"文章 文章"`。
- 点击后会先出现一个 `about:blank` 标签再完成导航：必须有界轮询等编辑页路由出现；
  未被复用的空白标签是**孤儿**，清理阶段必须关闭。
- 编辑页与列表页同路径（`/cgi-bin/appmsg`），靠 query 的 `action` 区分，不能只看 pathname。

## 3. 稳定选择器（实测有效）

| 目标 | 选择器 / 定位方式 |
|---|---|
| 标题输入 | `div.ProseMirror[contenteditable="true"][data-placeholder="请在这里输入标题"]`（PM 覆盖层；另有同步的隐藏 `textarea#title`） |
| 摘要输入 | `#js_description`（普通 textarea，Vue 包裹层 `data-v-*`） |
| 正文编辑器 | `div.ProseMirror[contenteditable="true"]:not([data-placeholder])`（页面内要求恰好 1 个） |
| 封面入口 | `#js_cover_area .js_imagedialog`（取首个可见） |
| 封面上传 input | `.weui-desktop-upload_global-media input[type=file]`（对话框内恰好 1 个） |
| 图片库条目 | `.weui-desktop-img-picker__item`（可见过滤后取最后一个 = 最新上传） |
| 裁切流程按钮 | 精确可见文本 `下一步` → `确认`（各要求唯一可见匹配） |
| 封面绑定判定 | `js_cover_preview_new` 元素 style 含非 `none` 的 `background-image` |
| 保存按钮 | 精确可见文本 `保存为草稿`（要求唯一可见匹配） |
| 草稿卡片 | `.weui-desktop-card[data-appid]`，`data-appid` 即稳定草稿标识 |
| 草稿标题 | 卡片内 `.weui-desktop-publish__cover__title > span` 文本 |
| 卡片"编辑"入口 | 卡片内 `.weui-desktop-tooltip` 文本 == `编辑` 的相邻 `a` 图标按钮（无文本锚点，需 tooltip 定位） |

**禁点文本（出现即拒绝点击）**：`群发`、`发送给粉丝`、`群发消息`、`发送`、`发表`、`删除`。
草稿卡片上有"发表"图标链接与删除图标（带确认弹层），编辑页有"发表"按钮，均不得触碰。

## 4. 字段写入与回读

| 字段 | 写入 | 回读 |
|---|---|---|
| 标题 | bsk `fill`（CSS 选择器） | `get-html` 中标题 PM 覆盖层序列化文本 == 期望值 |
| 摘要 | `scrollIntoView` + 真实 click 聚焦 + `fill` | evaluate 读 `#js_description.value`（编辑页 evaluate 可用，强制校验） |
| 正文 | evaluate 合成 `ClipboardEvent('paste')` + `DataTransfer`（text/html）→ PM 完整解析结构 | `get-html` 平衡 div 提取正文 PM 内层 HTML → `semantic_html_signature` 比对 + `em.frm_counter` 非零辅助 |
| 封面 | bsk `upload`（input 模式拦截原生文件选择器）→ 图片库新条目 → 下一步 → 确认 | `get-html` 判定 `js_cover_preview_new` 已绑定背景图 |

关键实测结论：

1. **bsk fill 会进入 ProseMirror 内部文档状态**（fill 后按键 `End`+字符插入到文本末尾，正文字数
   计数器从 0 变 N），不会出现"DOM 有字、保存为空"。
2. **fill 对 PM 是纯文本语义**：直接填 HTML 会把标签当字面文字，结构化正文必须走 paste 事件。
3. **CDP 合成 `Meta+V` 不触发原生粘贴**（浏览器不把合成快捷键映射为编辑命令），所以用合成
   `ClipboardEvent`——PM 的 paste 处理器读取 `clipboardData`，实测 h2/p/ul/li 全部正确入状态。
4. 摘要的 Vue 计数器不响应合成 input 事件，需真实聚焦交互后才刷新；计数器只作辅助校验，
   摘要一致性以 `.value` 回读 + 保存后重开回读为准。

## 5. 已知失败情形与处置

| 情形 | 现象 | 处置 |
|---|---|---|
| 登录失效 | 页面含"登录超时/扫码登录"等，或后台 URL 无 token | 停止，提示人工重新登录；不写入 |
| 账号不唯一/不匹配 | 昵称选择器归一化后 ≠1 个，或 ≠ 期望别名 | 停止，不写入；提示切换账号 |
| 接管未确认 | borrow 超时 / Chrome 弹"允许接管标签"未点 | 人工确认后重试（每会话一次） |
| evaluate 空返回 | 接管初期或页面早期返回 `{}` | fail-closed；改用 get-html 证据或中止 |
| 元素多匹配 | 保存按钮/裁切按钮/编辑入口 ≠1 个 | 写入前停止（`blocked_element_ambiguous`） |
| `about:blank` 残留 | window.open 预开标签未被复用 | 有界等待导航；孤儿空白标签清理时关闭 |
| 保存点击响应丢失 | 派发后传输失败 / 草稿箱无法唯一回读 | **`draft_uncertain`**；意图已落盘；禁止自动重发 |
| 同名旧草稿 | 保存前列表已有同标题卡片 | 保存前记录基线 appid；保存后按"标题匹配且不在基线"唯一定位；无法唯一定位 → `draft_uncertain` |
| 封面未绑定 | 裁切确认后预览无背景图 | 写入前停止（`blocked_cover_not_bound`） |
| 正文漂移 | 回读语义签名不一致 | 写入前停止（`blocked_field_readback_mismatch`） |
| 归还失败 | tab return 后标签未回到用户窗口 | **不 stop 会话**，保持现场提示人工（防止标签随窗口销毁） |

## 6. 隐私与记录边界

- 任何输出/日志只保留 URL 的 `scheme://host/path`，query（含 token）永不落盘。
- 结果记录只含字段哈希 / 语义签名 / 长度 / 计数，不含正文全文、Cookie、账号隐私。
- 持久化标记（`native-runner-draft_submit_intent.json`、`native-runner-draft_save_clicked.json`）
  先于/后于保存点击落盘，用于断电级审计与"禁止重发"判定。

## 7. 与既有 BrowserSkill 后端的关系

- 不修改 `scripts/browser_skill.py`、`publisher.py` 状态机与官方 API 路线；`native_runner.py`
  只复用 `publisher.py` 的纯函数（渲染、语义签名、封面校验、错误类型）。
- 后端选择仍然互斥：一次投递只走一条路线，失败不切换。
