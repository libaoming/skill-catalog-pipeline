# 字段字典 — 抓取 → 清洗 → 落库 全链路

> 🌏 [English](field-dictionary.en.md) | **中文**

字段在三个阶段有三套口径。理解这三套口径的差异，是对治「研发误报字段不匹配」的关键。

---

## A.1 API 原始字段（① 抓取层拿到的）

`_full_raw.jsonl` 每行（列表单条 + probe 回写）：

| 字段 | 含义 | 备注 |
|---|---|---|
| `slug` | 唯一标识（下载 / 安装 / diff 主键） | 全局唯一 |
| `name` | 展示名 | 可中文 |
| `category` | 平台大类 | 约 70% 为空（精选层仅约 10% 空） |
| `description_zh` / `description` | 中文 / 英文描述 | 中文 ≈99% 有；英文常空 |
| `downloads` / `installs` / `stars` | 热度 | 平台自报，无第三方校验；`verified` 字段全 0 不可用 |
| `version` | 版本 | **唯一可信变更信号**（diff 认它不认 `updated_at`） |
| `source` | 来源（社区 / 企业等） | — |
| `author` / `ownerName` | 作者 | — |
| `labels.requires_api_key` | 是否需外部 key | **原始是字符串 `"true"`/`"false"`** |
| `iconUrl` / `homepage` | 图标 / 详情页 | — |
| `updated_at` | 更新时间 | ⚠️ **脏信号**：随 downloads 刷新，不可作变更依据 |
| `has_package` / `_probe_code` | probe 探测结果 | 302/200 = 有包，404 = 无包 |

---

## A.2 清洗 NDJSON 字段（② 清洗层 `skills.ndjson` 每行 + fallback 链）

只读 `metadata.json`（主源）+ `SKILL.md`（正文）。

| NDJSON 字段 | 来源 / fallback 链 | 释义 |
|---|---|---|
| `slug` | `metadata.slug` | 主键 |
| `title` | `metadata.name` → `fm.name` → `slug` | 显示名 |
| `description` | `description_zh` ‖ `description` ‖ `fm.description` | fallback 后的展示描述 |
| `description_zh` / `description_en` | metadata（原值） | 中 / 英描述原值 |
| `category` | `metadata.category` | 平台大类（常空） |
| `tags` | `metadata.tags` | 标签数组 |
| `downloads` / `installs` / `stars` | metadata | 热度 |
| `version` | `metadata.version` | 喂 SkillVersion |
| `source` / `author` | metadata | — |
| `requires_api_key` | metadata（已规范 bool） | 判是否可直连 |
| `icon_url` / `homepage` | metadata | — |
| `fm_name` | `SKILL.md` frontmatter `name` | 技能 id / 触发名（`_skill_name`） |
| `fm_description` | `SKILL.md` frontmatter `description` | 触发 / 检索依据 |
| `skill_md_path` | 扫描派生 | 消歧后 `SKILL.md` 相对路径（溯源） |
| `warnings` | 校验派生 | 软警告（逗号分隔） |
| `body` | `SKILL.md` 去 frontmatter 全文 | 全文 + 喂 embedding |

> **三字段口径（关键，对治研发误报）**：
> - `slug` = 平台主键（去重 / diff）
> - `title` / `display_name` = 展示名（`metadata.name`）
> - `fm_name` / `_skill_name` = 装 Claude Code 的标识符（`SKILL.md` frontmatter `name`）
>
> 后两者**与 `slug` 本就不同维度，不校验相等**。曾有研发误报「name does not match slug」「Expected exactly one SKILL.md」——全是校验过严，非数据缺陷。

---

## A.3 DB 落库字段（③ 存储层 `public.skills` + `skill_versions`）

### `public.skills`

| 列 | 类型 | 来源 | 说明 |
|---|---|---|---|
| `slug` | text **PK** | NDJSON.slug | 主键 / diff / 去重 |
| `title` | text | NDJSON.title | = API name 展示名 |
| `description` | text | NDJSON.description | fallback 后描述 |
| `description_zh` | text | metadata | 中文描述 |
| `category` | text | metadata | + 索引 `idx_skills_category` |
| `tags` | jsonb | metadata | 默认 `[]` |
| `downloads` / `installs` / `stars` | integer | metadata | `downloads` 建降序索引 |
| `version` | text | metadata | **更新信号** |
| `source` | text | metadata | 社区 / 企业等 |
| `author` | text | metadata | — |
| `requires_api_key` | boolean | metadata | 进哪个分发包；+ 索引 |
| `has_package` | boolean | probe | 302/200 = 有包 |
| `icon_url` / `homepage` | text | metadata | — |
| `is_active` | boolean | sync 派生 | **软删**：下架 = false（不物删）；+ 索引 |
| `first_seen_at` / `updated_at` | timestamptz | 入库派生 | — |
| `body` | text | `SKILL.md` 正文 | 彻底版（约 97% 已回填） |
| `embedding` | **vector(2048)** | 多模态 embedding | 彻底版；检索经 `::halfvec(2048)` + IVFFlat `halfvec_cosine_ops` 索引 |
| `storage_path` | text | Storage 上传 | `skills/<slug>.zip` |
| `enriched_at` | timestamptz | 回填派生 | 上次回填 body/向量/包时间 |

### `public.skill_versions`（版本史）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | bigserial PK | — |
| `slug` | text FK→skills | `on delete cascade` |
| `version` | text | — |
| `seen_at` | timestamptz | — |
| — | `unique(slug, version)` | `updated` 态追加新版本，留版本史 |

---

## 字段在三阶段的演变小结

```
API 原始              清洗 NDJSON               DB 落库
─────────             ───────────               ───────
name           ──►    title (带 fallback)  ──►  title
labels.requires_api_key
  "true"(str)  ──►    requires_api_key(bool) ─► requires_api_key
（无）          ──►    body (SKILL.md 正文)  ──► body
（无）          ──►    （无）                 ──► embedding vector(2048)
（无）          ──►    （无）                 ──► storage_path
version        ──►    version              ──►  version  +  skill_versions(版本史)
updated_at(脏) ──►    （丢弃，不作变更依据）  ──► （不用）
```
