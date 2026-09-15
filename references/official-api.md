# 官方 API 后端

官方 API 是明确选择后的备用投递路径；默认使用预登录 BrowserSkill。需要公众号已开通并配置允许的接口能力。AppID/AppSecret/Access Token 只能通过 WorkBuddy 安全凭据引用或环境变量提供，运行时只在内存使用，不写入内容包、任务账本、日志或分发包。

投递路径：上传正文图片与封面 → `draft/add` → `draft/get` 语义回读 → 草稿；公开发布才继续 `freepublish/submit` → 轮询 `freepublish/get` → `freepublish/getarticle` 标题与作品链接验证。不得调用群发或删除接口。

素材上传在草稿创建前失败时属于可修复错误；只有草稿创建或发布提交的响应丢失才进入 uncertain，且不得自动重新投递。上传后的 media ID 必须记录在任务账本，避免重复上传。
