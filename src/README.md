# skill-catalog-pipeline (redacted reference implementation)

> 🌏 **English** | [中文](README.zh-CN.md)

> ## ⚠️ This is a redacted reference implementation, **not run-out-of-the-box**
>
> This repo is a **redacted code reference** for a "skill-catalog fetch → clean → ingest → semantic search → incremental sync" pipeline,
> meant for understanding the pipeline design and as a basis for adaptation — it **cannot be run directly**:
> - All credentials (DB password, object-storage service key, embedding API key) and all third-party hosts
>   **have been extracted into environment variables**; the repo contains no real values;
> - It depends on a set of **external services you must provide yourself**: the data-source API, Postgres+pgvector, object storage, an embedding service;
> - The repo contains **no real data** (no skill metadata, no body text, no vectors, no snapshots).

## Data-source statement

The raw skill data comes from **a third-party public skill-market service**, and that data belongs to the service and the individual skill authors.
This repo **does not include** any of the fetched real data, and **does not disclose** the third party's real domain —
the fetch host must be configured by you via the `SKILL_MARKET_API_BASE` environment variable before the fetch layer can run.
Whether and how to fetch is on you to confirm against the target service's terms and compliance boundaries.

## Environment-variable placeholder table

| Variable | Description |
|------|------|
| `DB_URL` | Standard Postgres connection string (with host/user/password), e.g. `postgresql://<user>:<pwd>@<pooler-host>:5432/postgres`. Ingestion/retrieval/backfill all read from here; the code hard-codes no host/account |
| `SUPABASE_URL` | Object-storage / REST host (for uploading complete zip packs), of the form `https://<project>.<host>` |
| `SUPABASE_PROJECT_REF` | Managed Postgres project identifier (only when you need to reference the project ref separately; the connection itself goes through `DB_URL`) |
| `SERVICE_KEY` | Object-storage write-permission key (service_role level; the public anon key has no write access and won't work) |
| `EMBED_API_KEY` | The embedding service's API key (sent as a `Bearer` header) |
| `EMBED_API_BASE` | The embedding service endpoint base (without it, the code uses the placeholder `<embedding-api-base>` and requests will fail) |
| `EMBED_MODEL` | Embedding model identifier (inference-endpoint id or model name); the **vector dimension must match `vector(N)` in `enrich_schema.sql`** |
| `SKILL_MARKET_API_BASE` | The data-source skill-market API host (used by the fetch layer; without it, the placeholder `<skill-market-api>` is used and fetching will fail) |

You can put the above variables in a root-level `.env` (scripts read it relatively), or export them straight into the environment.

## Directory map (the five-stage pipeline)

```
src/
  fetch/        Fetch layer: pull full metadata + body packs from the data-source API
    fetch_all.py      list/probe/build three phases: fetch the list → probe for packs → produce a "ready-to-use" delivery manifest
    fetch_bodies.py   concurrently download each body zip for entries that are "no-key + have-pack" (resumable, with retry on failure)
    extract.py        unpack body zips into the all-skills/<slug>/ directory structure + merge metadata

  normalize/    Clean layer: scan, validate against install standards, normalize, score and package
    scan.py                 dry-run scan, emitting import|skip|review decisions per metadata/SKILL.md
    validate_structure.py   filter by "installable/loadable" hard conditions, producing installable.tsv / filtered.tsv
    import_skills.py        produce the normalized NDJSON intermediate (with body, for ingestion/retrieval)
    package_skills.py       package each skill into a standalone zip + a catalog.ndjson index

  storage/      Storage layer: create tables, ingest, vectorize, object storage, backfill
    schema.sql          skills / skill_versions table schema (metadata only)
    enrich_schema.sql   pgvector upgrade: body + embedding vector + storage_path columns + HNSW index
    rls.sql             read-only RLS (the public role can only SELECT; writes are service_role only)
    import_to_db.py     metadata → Postgres (full ingestion + incremental upsert for the sync layer, four-state ↔ DB)
    enrich_skills.py    content-layer backfill: body text + embedding vector + complete zip to object storage
    embed_all.py        full embedding, accelerated (64 concurrency; only processes embedding=null)
    storage_all.py      full object-storage upload, accelerated (64 concurrency; db_once ephemeral connection resists drops)

  retrieval/    Retrieval layer
    search.py     query → embedding → pgvector cosine nearest-neighbor (halfvec/vector retrieval SQL)

  sync/         Sync layer
    sync_diff.py  diff by slug primary key into a new/updated/removed/stats_only four-state incremental plan
```

## Run-order hint

```
fetch (list → probe → build → fetch_bodies → extract)
  → normalize (scan → validate_structure → import_skills / package_skills)
  → storage (schema.sql → import_to_db → enrich_schema.sql → embed_all / storage_all / enrich_skills)
  → build the vector index (for large volumes, run the HNSW index from enrich_schema.sql via the
                managed console SQL Editor; loading vectors first and then building the index is faster)
  → before going live, run rls.sql to tighten write permissions
  → retrieval (search.py smoke-tests semantic search)

sync (once a week):
  fetch a fresh catalog → sync_diff diffs old/new → four-state incremental plan
  → feed new+updated to the downloader/extract → import_to_db.sync_plan writes to DB (can carry enrich backfill)
```
