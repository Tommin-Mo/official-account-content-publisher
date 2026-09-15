# Baoyu 上游审计结论

审计对象：`JimLiu/baoyu-skills` commit `6b7a2e417500561a5ecdd0b168332f4142584617` 的 `skills/baoyu-post-to-wechat`。许可证为 MIT；具体哈希在 `third_party/LOCK.json`。

可复用的设计：独立 Chrome Profile、CDP 元素定位、标题和编辑器回读、正文图片上传、草稿保存及草稿 ID 读取。

不得直接复用的路径：上游 `wechat-article.ts` 含可选 Telegram 二维码传递逻辑，并且 `--submit` 的语义是保存草稿，不包含本 Skill 所需的公开发布、群发拒绝和作品列表精确验证。它也不能代替“标题、摘要、正文、封面与更新时间”的强回读。

因此当前 Skill 只将 Baoyu 作为固定审计基线，不将它的运行时直接打包、自动执行或配置为投递后端。浏览器投递仅允许经过独立审计的 BrowserSkill 适配器；后台的手动草稿流程可作为选择器研究证据，但自动适配器仍需独立完成上传、选择器与回读验收，公开发布还需单次点击与作品列表回读验收。该限制是发布门禁，而不是降级为不验证的浏览器自动化。
