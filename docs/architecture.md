# Skill Catalog Engineering Plan — Fetch · Normalize · Store · Retrieve · Sync

> 🌏 **English** | [中文](architecture.zh-CN.md)

> A complete engineering plan for turning a massive collection of third-party skill packages into a "semantically searchable, loadable" skill-catalog foundation.
> Data scale: ~77,000 Claude Code-style `SKILL.md` skills.

---

## 1. Background & Goals

**Business origin**: AI-employee / agent products need their agents to "get hands-on," which requires attaching a retrievable, loadable capability layer (tools / skills). The open ecosystem already contains a massive supply of Claude Code-style `SKILL.md` skills (one skill = a block of frontmatter metadata + a body of natural-language instructions + optional `scripts/`), which form a ready-made capability pool. This plan uses a public, unauthenticated third-party skill-market REST API (~77,000+ skills) as a worked example.

**Goal**: **fetch the full set, normalize it, load it into a database (including semantic search), and keep it incrementally synced** — serving as the skill retrieval / loading foundation for the product runtime.

**Definition of the "full version"**: a single skill carries all four asset states in the catalog —

1. **Metadata** (21 fields: category / downloads / version / requires-key flag, etc.) — used for retrieval filtering
2. **Body text** (`SKILL.md` with frontmatter stripped) — full-text search + embedding input
3. **Embedding vector** (multimodal embedding, 2048-dim, pgvector) — semantic search
4. **Full zip** (the entire directory including `scripts/` and `references/`, stored in object storage) — used for loading / distribution

**Tech choice for the validation phase**: Supabase all-in-one (Postgres + pgvector + Storage). Rationale: pgvector gives one-stop hybrid retrieval and is the fastest to get going. Migration to a self-hosted / domestic vector store will be re-evaluated when the production runtime makes real-time calls.

---

## 2. Overall Architecture

A five-layer pipeline. Each layer persists its outputs, is idempotent, and is resumable from a checkpoint. The source is a public HTTP API; the destination is Postgres (structured data + vectors + object storage).

```
┌──────────────────────────────────────────────────────────────────────┐
│  Data source: a public third-party skill-market REST API (no auth)     │
│   GET /api/skills?page=&pageSize=100      list metadata (incl. version/requires-key) │
│   GET /api/v1/download?slug=  → 302 zip (has package) / 404 (no package)│
│   GET /api/v1/skillsets                   official curated scenario packs │
└───────────────┬──────────────────────────────────────────────────────┘
                │
   ┌────────────▼─────────────┐  ① FETCH layer
   │ fetch_all                 │   list→probe→build, multi-round union refetch + integrity check
   │ fetch_bodies              │   download zip bodies
   │ extract                   │   unzip → all-skills/<slug>/
   └────────────┬─────────────┘   outputs: _full_raw.jsonl / all-skills/
                │
   ┌────────────▼─────────────┐  ② NORMALIZE layer (dry-run, no DB writes)
   │ scan                      │   scan & classify import|skip|review
   │ validate_structure        │   5 hard-condition filter → installable.tsv
   │ import (mapping)          │   field mapping → skills.ndjson / versions.ndjson
   │ package                   │   per-skill standalone zip → catalog.ndjson (distribution package)
   └────────────┬─────────────┘
                │
   ┌────────────▼─────────────┐  ③ STORAGE layer (Postgres + pgvector + Storage)
   │ schema.sql                │   skills + skill_versions tables
   │ enrich_schema.sql         │   body / embedding(vector 2048) / storage_path
   │ import_to_db              │   metadata upsert + soft-delete + versions table
   │ embed_all                 │   embed everything (64 concurrency, idempotent resume)
   │ storage_all               │   zip whole dir, upload to Storage (64 concurrency, disconnect-resilient)
   │ rls.sql                   │   read-only RLS before going live (anon/auth select only)
   └────────────┬─────────────┘   final state: public.skills (all 4 asset states + RLS)
                │
   ┌────────────▼─────────────┐  ④ RETRIEVAL layer
   │ search                    │   query→embed→ embedding<=>qvec cosine nearest-neighbor
   └───────────────────────────┘
                ▲
   ┌────────────┴─────────────┐  ⑤ SYNC layer (weekly, automatic)
   │ sync_diff                 │   diff four states: new/updated/removed/stats_only
   │ sync_weekly               │   fetch new snapshot→diff→probe→report→notify→roll baseline
   │ + scheduled trigger       │   triggered locally at a fixed time every Monday
   │ → import_to_db.sync_plan / enrich_slugs (incremental backfill)
   └───────────────────────────┘
```

**Technical constraints (apply across the whole pipeline)**:

- Background Python **must** run with `-u`, otherwise output is buffered and the process is mistaken for hung.
- Credentials are read from environment variables or a local `.env` (env vars take priority), and are **never hardcoded into scripts / the repo**.

---

## 3. Data Source & API

Three endpoints, verified in practice (public, no auth):

| Endpoint | Purpose | Key fields / behavior |
|---|---|---|
| `GET /api/skills?page=N&pageSize=100` | List metadata | `slug` / `category` / `description_zh` / `version` / `downloads` / `source` / `labels.requires_api_key` (string `"true"`/`"false"`) / `iconUrl`; pageSize caps at 100 (>100 returns null) |
| `GET /api/v1/download?slug=X` | Download package | 302→object-storage zip (has package) / 404 (no package). The zip contains `SKILL.md` + `metadata.json` + `scripts/` |
| `GET /api/v1/skillsets` | Official scenario packs | Only a small curated set of scenarios, **not a taxonomy**; the primary categorization can only use `category` |

**Real-world API pitfalls (confirmed)**:

- **Unstable pagination**: three fetches at the same instant return counts that swing by ±1000+. A single-round fetch is guaranteed to miss data → multi-round union is mandatory (see §4.1).
- **`updated_at` is a dirty signal**: it is refreshed on every sync along with downloads, so it cannot distinguish "content update" vs. "download count went up." **The only trusted change signal is `version`** (see §8.1).
- **`requires_api_key`** is the string `"true"` in the raw `labels`, and must be normalized to a bool locally.

---

## 4. ① FETCH Layer

Three stages (independently runnable, idempotent, resumable):

```
list   fetch the full list metadata → _full_raw.jsonl + distribution stats
probe  probe download per skill (302 has package / 404 none) → write back has_package into _full_raw.jsonl
build  filter by "no key needed + has package" → a curated set developers can use directly
```

### 4.1 Fetch robustness design (the core challenge = fighting API pagination jitter)

Multiple layers of defense:

1. **Retry failed pages ×3 rounds**: a single failed page is not dropped — retried over 3 rounds.
2. **Multi-round union refetch**: after a full round, take the union of slugs; if it has not reached 99.5% of `total`, refetch the whole round and merge into the union, until it converges.
3. **Integrity-check gate**: if the final count is < 98% of `total` → raise an error and **do not write a partial snapshot** (a partial snapshot must never be rolled into the baseline).

> Effect: removal noise dropped from tens of thousands (partial-snapshot false positives) down to a few dozen (≈ 0.06%, inherent API jitter, acceptable for ops).

### 4.2 Download & Extract

- Download the zip bodies (download needs no key; only runtime does).
- Unzip → `all-skills/<slug>/` (contains `metadata.json` + `SKILL.md` + `scripts/`).

---

## 5. ② NORMALIZE Layer

A four-script pipeline, fully dry-run (produces reports, no DB writes); rerunning on the same snapshot is byte-for-byte identical.

| Stage | Input | Output | Responsibility |
|---|---|---|---|
| scan | `all-skills/` | `manifest.json` | Scan & classify `import\|skip\|review`; `metadata.json` = the only trusted primary-key source |
| validate | `manifest.json` | `installable.tsv` | 5 hard-condition filter |
| import | `installable.tsv` | `skills.ndjson` / `versions.ndjson` | Field mapping (incl. full body text) |
| package | `installable.tsv` | `catalog.ndjson` + `packages/<slug>.zip` | Per-skill standalone distribution package |

**The 5 hard filter conditions** (can it be loaded by Claude Code):

1. `metadata.json` parses and contains a slug
2. There is exactly one disambiguatable `SKILL.md`
3. Valid YAML frontmatter
4. Non-empty `name`
5. Non-empty `description`

**Disambiguation (multiple SKILL.md)**: prefer the root `./SKILL.md`; otherwise take the first by `(depth, lexicographic order)`.

**Result**: ~54k raw → ~48k compliant (~6500 filtered out: no frontmatter / no SKILL.md / no desc / no name).

### 5.1 The three-field convention (to counter developer false positives)

| Field | Meaning | Source | Validation |
|---|---|---|---|
| `slug` | Platform primary key / dedup / diff | API (globally unique) | — |
| `display_name` | Shown to users | `metadata.name` (when empty / == slug, fall back to the first sentence of `description_zh` → then slug) | — |
| `_skill_name` | The identifier used when loading into Claude Code as a skill | `SKILL.md` frontmatter `name` | **Not validated to == slug** (different dimension by design) |

> Developers once raised false alarms like `name does not match slug` and `Expected exactly one SKILL.md` — all of them stemmed from over-strict validation, not data defects. `slug` (the platform primary key) and `frontmatter.name` (the skill-loading identifier) are different dimensions by design and should not be cross-validated.

---

## 6. ③ STORAGE Layer (Postgres + pgvector + Storage)

### 6.1 Schema

Metadata across two tables:

```sql
public.skills(
  slug PK, title, description, description_zh, category, tags(jsonb),
  downloads, installs, stars,
  version,                  -- update signal (diff trusts version, not updated_at)
  source, author,
  requires_api_key bool,    -- which distribution package it goes into
  has_package bool,         -- probe result
  icon_url, homepage,
  is_active bool default true,   -- soft-delete: removed = false (no physical delete)
  first_seen_at, updated_at
)  -- + indexes: category / req_key / is_active / downloads desc

public.skill_versions(
  id bigserial PK, slug FK→skills, version, seen_at,
  unique(slug, version)     -- the updated state appends a new version, keeping version history
)
```

Columns added for the full version:

```sql
create extension if not exists vector;
alter table public.skills
  add column body         text,          -- SKILL.md body
  add column embedding    vector(2048),  -- multimodal embedding dimension
  add column storage_path text,          -- skills/<slug>.zip
  add column enriched_at  timestamptz;
```

> ⚠️ **Vector index approach (corrected in practice; the original `hnsw (embedding vector_cosine_ops)` is dropped)**:
>
> - **2048 dimensions exceeds pgvector's full-precision `vector` limit of 2000 for HNSW/IVFFlat** → you must use **`halfvec` half-precision** (limit 4000; the precision loss is essentially imperceptible for ranking).
> - In practice we use an **IVFFlat-over-halfvec expression index** (HNSW consumes more memory to build on a small instance and is more easily killed):
>
> ```sql
> set statement_timeout = '30min';
> set max_parallel_maintenance_workers = 0;   -- serial, to avoid parallel workers opening segments in /dev/shm and hitting DiskFull
> create index idx_skills_embedding_ivf on public.skills
>   using ivfflat ((embedding::halfvec(2048)) halfvec_cosine_ops) with (lists = 200);
> ```
>
> - 🔴 **The index must be built in the managed database console's SQL Editor (server-side execution)**, **not from a local long-lived connection** — a local network proxy (e.g. Clash, where the DB resolves to a fake-IP) will cut multi-minute idle long-lived connections by duration (`SSL SYSCALL error: EOF detected`; keepalive is ineffective; the absence of OOM/FATAL in server-side logs proves it is not a server-side problem). Loading data / querying have a data stream and stay alive; building an index that just waits for several minutes is doomed. See [engineering-notes.md](engineering-notes.md).

### 6.2 Metadata Ingestion

- Read the baseline snapshot → clean (recursively strip NUL `0x00`, which PG text/jsonb rejects) → batch upsert (5000/batch) + `skill_versions`.
- Connect via the **Session pooler** (free IPv4 proxy).
- Provide `sync_plan(plan, new, enrich=False)` for the sync layer to do four-state incremental updates (see §8.3).

**`connect()` hardening** — batch DB writes (storage/enrich/upsert) repeatedly died under load; a three-part fix:

```python
# SET immediately after each connection is established (persists within the Session-pooler session)
set statement_timeout = '10min'   # prevents 256-row batch UPDATEs from being canceled by the DB default timeout; don't set 0 (0 makes a lock-colliding UPDATE hang forever)
set lock_timeout      = '30s'     # fail fast on leftover row locks instead of hanging
# + TCP keepalive (keepalives_idle=10s): survives the tens-of-seconds network gaps during embed/upload, preventing the proxy from cutting idle connections
```

> Index building (minutes-scale) still runs server-side in the SQL Editor and does not go through this path (see the §6.1 note).

### 6.3 Embedding Ingestion

**Hard constraints of the multimodal embedding (crystallized from the pitfalls)**:

- ⚠️ **Calling the model name directly returns 404 → you must create an inference endpoint and use its endpoint-id** (placeholder `${EMBED_MODEL}`).
- The endpoint `/api/v3/embeddings/multimodal`, with input = `[{type:text, text}]`, **fuses everything into 1 vector per call → one request per skill; you cannot batch multiple texts into one call**.
- Dimension **2048**.

Script design:

1. The DB only queries slugs where `embedding is null and body is not null` (**do not pull the large body field**, to avoid statement timeouts).
2. Read body locally → embed at 64 concurrency → batch `update ... from (values %s) v(slug,emb) where s.slug=v.slug`, committing every 256 rows.
3. **Idempotent resume**: only process nulls; skip failed slugs without blocking. After a crash, a rerun automatically resumes ingestion.
4. **Tail backfill**: for rows missing a local body but having a body in the DB, read the body from the DB and embed it with the same convention to fill it in.

> After ingestion, build the index: see the §6.1 note — **halfvec + IVFFlat, and it must run server-side in the SQL Editor**.
> **Measured result**: ~75k embeddings (all rows that have a body), with the IVFFlat index built.

### 6.4 Storage Upload (acceleration + disconnect resilience)

To counter the serial bottleneck (75k serial PUTs would take hours):

1. The DB queries slugs where `storage_path is null` → zip the whole local directory in memory.
2. Upload at 64 concurrency to object storage (headers must carry `apikey` + `Authorization: Bearer <service_key>` + `x-upsert:true`; missing apikey → 400).
3. Batch-update `storage_path`; slugs with no local directory are automatically skipped.

**bucket** is private. **Write permission**: must be service_role / secret key (anon won't work).

**⭐ Key disconnect-resilience refactor: hold no DB connection during upload**

The original version held one long-lived DB connection across the entire upload loop → during each chunk's tens-of-seconds upload network gap the connection went idle and was cut by the local proxy (`SSL SYSCALL error: EOF detected`); then `conn.rollback()` on the dead connection raised `InterfaceError` and brought the whole job down. Refactored into **`db_once(fn)`**:

- The upload phase (concurrent PUTs via `ThreadPoolExecutor`) **holds no DB connection at all**.
- Only after a chunk finishes uploading does it briefly open a connection to do the UPDATE → commit → **close immediately**; **the connection lives only a few seconds and is never idle → the proxy can't catch it**.
- On connection death (SSL EOF / InterfaceError / OperationalError), **reconnect and retry 4 times** (exponential backoff); only if retries still fail is the value left null, to be idempotently backfilled next round.

> General principle: **don't hold a long-lived connection for local batch DB writes** — use either ephemeral connections, or the §6.2 keepalive + bounded timeout.

**Measured completion**: the refactored version withstood proxy disconnects and ran through in one pass; non-empty `storage_path` is ~75k (the remaining ~2000 = the tail with no local package, which needs to be downloaded and ingested first before it can be filled in).

---

## 7. ④ RETRIEVAL Layer

Retrieval SQL (the query uses the same convention as the in-catalog vectors and hits the IVFFlat halfvec index):

```sql
set ivfflat.probes = 10;   -- larger = more accurate but slower
-- qvec = embed(query) yields a 2048-dim vector
select slug, title,
       (embedding::halfvec(2048) <=> :qvec::halfvec(2048)) as dist
from public.skills
where embedding is not null
order by embedding::halfvec(2048) <=> :qvec::halfvec(2048)
limit k;
```

- Use the same embed function so the query vector and the in-catalog vectors live in the **same space** (critical for correctness).
- **Cast `::halfvec(2048)` on both sides** to hit the IVFFlat halfvec expression index (matching the index-build expression).
- **Measured (~75k rows)**: with the index and `probes=10`, **~500ms/query** (an exact scan without the index is **130s** → ~260× speedup); results are highly relevant — outbound marketing → customer follow-up / customer-service scripts; data wrangling → data-report generator; customer complaint → e-commerce after-sales reply.
- Ships with a set of Chinese-scenario queries as a smoke test.

---

## 8. ⑤ SYNC Layer (weekly, automatic · detect + notify)

Designed as a "detect + notify only" layer (no download / no ingestion / no repackaging; ingestion is manually triggered) + local scheduling + weekly + IM notification.

### 8.1 Incremental diff

Diff the old vs. new catalog by the slug primary key, four states:

| State | Decision | Action |
|---|---|---|
| `new` | slug added and has a package | download + ingest |
| `updated` | **version changed** | re-download + append to `skill_versions` |
| `removed` | slug disappeared | soft-delete `is_active=false` (no physical delete) |
| `stats_only` | only downloads/stars changed | update only the stats columns (no download) |

> The selftest includes a mock four-state regression (including the guard case "updated_at changed but version unchanged + downloads changed → must be judged stats_only").

### 8.2 Weekly schedule

```
fetch new snapshot → diff(baseline, new) → probe the newly added slugs
→ generate report → IM notify (sent to the ops owner)
→ roll the baseline (overwrite _baseline.jsonl, archive the old one to snapshots/)
```

- **Trigger**: a local scheduler, at a fixed time every week.
- **Dual gate against contamination**: if the new snapshot is < 95% of the baseline → abort, do not roll the baseline, and send an alert (validated by two rollbacks already).
- **Ops self-notification does not go through the outbound-contact gate**: outbound contact (sending messages to customers) must pass a human-confirmation gate; ops self-notifications go to the owner themselves — routing them through the outbound gate would defeat being "automatic."

### 8.3 sync → DB auto-backfill (incl. content-layer enrich)

After the sync report, it calls `sync_plan(plan, new, enrich=False)`:

- **Metadata layer (default)**: new/updated → upsert (+versions), removed → soft-delete, stats_only is not written by default (too high-volume). Failures do not block the report / notification.
- **Content layer (when `enrich=True`)**: after upsert, backfill body/vector/Storage package for the slugs among new/updated that **already exist locally**; slugs **missing a local package are safely skipped** (the content layer depends on the local package, which must first be manually "ingested" — downloaded and extracted). The whole block is wrapped in try/except; a backfill failure only logs and does not block.

> The weekly automatic sync still only sends `enrich=False` (newly added slugs have no local package at that moment, so enrich is meaningless). Content-layer backfill goes through the manual "ingest" path: download zip → extract → `sync_plan(plan, new, enrich=True)`.

---

## 9. Ops · Cost · Security

**Cost** (ballpark): managed Postgres Pro tier ~$25/mo + a one-time embedding first-load of roughly tens to a bit over a hundred RMB (batch discount) + weekly increments ~¥10/mo.

**Security**:

- Credentials are read from environment variables / `.env`, and are **never hardcoded into the repo**; rotate before going live.
- **Read-only RLS**: `skills` + `skill_versions` each `enable row level security` + `revoke insert/update/delete from anon,authenticated` + `grant select` + a read-only select policy (`using(is_active)` → soft-deleted skills are invisible to public reads). **Writes are limited to service_role / postgres** (both are BYPASSRLS, so the sync/enrich/storage pipeline is unaffected).
- The bucket is private; access goes through a service key or signed URLs.

**Required credentials** (filled into a local `.env`, placeholders):

```
DB_URL=<postgres-connection-string>
STORAGE_URL=<object-storage-endpoint>
SERVICE_KEY=<service-role-key>
EMBED_API_KEY=<embedding-api-key>
EMBED_MODEL=<embedding-endpoint-id>
```

---

## Appendix: Field Dictionary

For the three field conventions across fetch → normalize → load, see [field-dictionary.md](field-dictionary.md).

## Appendix: Engineering Crystallizations

For the retrospective compiling every pitfall hit across the whole pipeline, see [engineering-notes.md](engineering-notes.md) — the highest-value part of this plan.
