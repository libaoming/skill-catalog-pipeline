# Field Dictionary — Fetch → Clean → Persist, End to End

> 🌏 **English** | [中文](field-dictionary.zh-CN.md)

Fields have three different sets of conventions across three stages. Understanding the differences between these three conventions is the key to curing "false reports of field mismatch during development."

---

## A.1 Raw API Fields (① obtained by the fetch layer)

Each line of `_full_raw.jsonl` (one list entry + probe write-back):

| Field | Meaning | Notes |
|---|---|---|
| `slug` | Unique identifier (primary key for download / install / diff) | Globally unique |
| `name` | Display name | May be in Chinese |
| `category` | Platform top-level category | ~70% empty (only ~10% empty in the curated tier) |
| `description_zh` / `description` | Chinese / English description | Chinese present ≈99%; English often empty |
| `downloads` / `installs` / `stars` | Popularity | Self-reported by the platform, no third-party verification; the `verified` field is all 0 and unusable |
| `version` | Version | **The only trustworthy change signal** (diff trusts it, not `updated_at`) |
| `source` | Origin (community / enterprise, etc.) | — |
| `author` / `ownerName` | Author | — |
| `labels.requires_api_key` | Whether an external key is required | **Raw value is the string `"true"`/`"false"`** |
| `iconUrl` / `homepage` | Icon / detail page | — |
| `updated_at` | Update time | ⚠️ **Dirty signal**: refreshed alongside downloads, cannot be used as a basis for change |
| `has_package` / `_probe_code` | probe detection result | 302/200 = has a package, 404 = no package |

---

## A.2 Cleaned NDJSON Fields (② each line of the cleaning layer's `skills.ndjson` + fallback chain)

Reads only `metadata.json` (primary source) + `SKILL.md` (body).

| NDJSON Field | Source / fallback chain | Definition |
|---|---|---|
| `slug` | `metadata.slug` | Primary key |
| `title` | `metadata.name` → `fm.name` → `slug` | Display name |
| `description` | `description_zh` ‖ `description` ‖ `fm.description` | Display description after fallback |
| `description_zh` / `description_en` | metadata (raw values) | Raw Chinese / English description values |
| `category` | `metadata.category` | Platform top-level category (often empty) |
| `tags` | `metadata.tags` | Array of tags |
| `downloads` / `installs` / `stars` | metadata | Popularity |
| `version` | `metadata.version` | Feeds SkillVersion |
| `source` / `author` | metadata | — |
| `requires_api_key` | metadata (already normalized to bool) | Determines whether a direct connection is possible |
| `icon_url` / `homepage` | metadata | — |
| `fm_name` | `SKILL.md` frontmatter `name` | Skill id / trigger name (`_skill_name`) |
| `fm_description` | `SKILL.md` frontmatter `description` | Trigger / retrieval basis |
| `skill_md_path` | Derived from scan | Relative path of the disambiguated `SKILL.md` (provenance) |
| `warnings` | Derived from validation | Soft warnings (comma-separated) |
| `body` | `SKILL.md` full text minus frontmatter | Full text + feeds embedding |

> **The three-field convention (critical, cures false reports during development)**:
> - `slug` = platform primary key (dedup / diff)
> - `title` / `display_name` = display name (`metadata.name`)
> - `fm_name` / `_skill_name` = the identifier for installing into Claude Code (`SKILL.md` frontmatter `name`)
>
> The latter two are **inherently a different dimension from `slug` and are not validated for equality**. There were once false reports during development — "name does not match slug" and "Expected exactly one SKILL.md" — all of which were over-strict validation, not data defects.

---

## A.3 DB Persistence Fields (③ the storage layer's `public.skills` + `skill_versions`)

### `public.skills`

| Column | Type | Source | Description |
|---|---|---|---|
| `slug` | text **PK** | NDJSON.slug | Primary key / diff / dedup |
| `title` | text | NDJSON.title | = API name display name |
| `description` | text | NDJSON.description | Description after fallback |
| `description_zh` | text | metadata | Chinese description |
| `category` | text | metadata | + index `idx_skills_category` |
| `tags` | jsonb | metadata | Default `[]` |
| `downloads` / `installs` / `stars` | integer | metadata | `downloads` has a descending index |
| `version` | text | metadata | **Change signal** |
| `source` | text | metadata | Community / enterprise, etc. |
| `author` | text | metadata | — |
| `requires_api_key` | boolean | metadata | Which distribution package it goes into; + index |
| `has_package` | boolean | probe | 302/200 = has a package |
| `icon_url` / `homepage` | text | metadata | — |
| `is_active` | boolean | Derived from sync | **Soft-delete**: delisted = false (no hard delete); + index |
| `first_seen_at` / `updated_at` | timestamptz | Derived on insertion | — |
| `body` | text | `SKILL.md` body | Full version (~97% backfilled) |
| `embedding` | **vector(2048)** | multimodal embedding | Full version; retrieval goes through `::halfvec(2048)` + IVFFlat `halfvec_cosine_ops` index |
| `storage_path` | text | Storage upload | `skills/<slug>.zip` |
| `enriched_at` | timestamptz | Derived from backfill | Time of last body/vector/package backfill |

### `public.skill_versions` (version history)

| Column | Type | Description |
|---|---|---|
| `id` | bigserial PK | — |
| `slug` | text FK→skills | `on delete cascade` |
| `version` | text | — |
| `seen_at` | timestamptz | — |
| — | `unique(slug, version)` | The `updated` state appends a new version, preserving version history |

---

## Summary of Field Evolution Across the Three Stages

```
Raw API               Cleaned NDJSON            DB Persistence
─────────             ───────────               ──────────────
name           ──►    title (with fallback)──►  title
labels.requires_api_key
  "true"(str)  ──►    requires_api_key(bool) ─► requires_api_key
(none)         ──►    body (SKILL.md body) ──►  body
(none)         ──►    (none)                ──► embedding vector(2048)
(none)         ──►    (none)                ──► storage_path
version        ──►    version              ──►  version  +  skill_versions(history)
updated_at(dirty)──►  (dropped, not a change basis)──► (unused)
```
