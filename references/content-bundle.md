# 内容包契约 1.1

内容包先以 `content_reviewed` 保存文章审核哈希，封面验证完成后才成为 `finalized` 并写入最终 `content_hash`。正文仅允许语义块，不接受原始 HTML、脚本或外部 CSS。

```json
{
  "schema_version": "1.1",
  "stage": "content_reviewed",
  "brief": {"theme":"", "audience":"", "content_type":"tutorial", "intent":"", "target_length":1800, "voice_profile":"", "conversion_goal":"none"},
  "sources": [{"id":"S1", "url":"https://…", "title":"", "publisher":"", "retrieved_at":""}],
  "facts": [{"id":"F1", "claim":"", "source_id":"S1", "date":"", "scope":"", "unit":"", "confidence":"high"}],
  "insights": [{"id":"I1", "evidence_refs":["F1"], "evidence_cluster":"", "data_relation":"", "mechanism":"", "alternative":"", "limitation":"", "implication":"", "direction":"", "confidence":""}],
  "article": {"title_candidates":[], "selected_title":"", "summary":"", "author":"", "body_blocks":[{"type":"paragraph", "text":"", "fact_refs":["F1"]}], "cta":{"primary":"none", "secondary":"none"}},
  "visual": {"provider":"", "cover_prompt":"", "cover_path":"", "cover_hash":"", "aspect_ratio":"2.35:1"},
  "delivery": {"account_alias":"", "comments":"open", "fans_only":false, "source_url":"", "delivery_choice":"draft"},
  "article_review_hash":"", "content_hash":""
}
```

`fact_refs` 和 `evidence_refs` 说明正文依赖哪项证据。直接引文可在事实中提供 `quote`，此时其文字必须保持不变；普通事实不再被要求逐字复制进正文。

`review` 生成 `article_review_hash`；`finalize --cover` 验证图片、绑定封面哈希并生成 `content_hash`。投递前会重新计算封面哈希；任何正文、事实、洞察、投递设置或封面文件变化都会使对应哈希失效。
