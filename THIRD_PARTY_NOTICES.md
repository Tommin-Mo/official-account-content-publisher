# 第三方基线与使用边界

本 Skill 的 Chrome/CDP 适配思路以 JimLiu/baoyu-skills 的 `baoyu-post-to-wechat` 为审计基线：commit `6b7a2e417500561a5ecdd0b168332f4142584617`，MIT License。归档 SHA-256 在构建锁定清单中记录。

浏览器运行时以 Tencent/BrowserSkill commit `49744233f75734e58a51ba187fc5a06826f7e13e`（MIT License）为固定来源，运行时要求 `bsk 0.2.0`。Skill 使用其原生文件输入上传能力；公开包不包含 BrowserSkill 二进制、扩展、浏览器 Profile、Cookie 或配对数据。当前仍为内部候选版，浏览器草稿与发布能力须通过独立端到端验收后才能对外声明。

本仓库不复制上游的全部实现、主题或个人配置。若未来纳入任何上游文件，必须保留 MIT 许可证与来源，并将文件哈希追加到锁定清单。已知的弱草稿验证不可直接使用；本 Skill 要求稳定草稿标识、标题、摘要、正文和封面的重新打开回读。

`wechatpy`（MIT）仅作为官方 API 行为参考；`wenyan-cli`（Apache-2.0）与 Sakura 的组件化思路仅作设计参考。未经明确许可证授权的主题、模板或个人文风不得纳入。
