# skill-catalog-pipeline

> 🌏 **English** | [中文](README.zh-CN.md)

> An engineering blueprint for turning a **massive pile of third-party skill packs** into a **semantically searchable, loadable** skill catalog foundation:
> fetch → clean & normalize → vectorize & ingest → semantic search → auto-sync, plus a hard-won writeup of every trap we hit along the way.
>
> It covers the full methodology and pitfalls of "how 77K skills become a library you can semantically search in 500ms," and ships a **redacted reference implementation** ([`src/`](src/), 16 scripts + SQL; credentials and the data-source host all come from environment variables — not run-out-of-the-box).

---

## Why this exists

For AI-employee / Agent products to let an agent "actually do things," you need to give it a layer of **searchable, loadable capabilities** (tools / skills).

The good news: the open-source ecosystem already has a massive supply of **Claude Code-style `SKILL.md` skills** — a skill = a chunk of frontmatter metadata + a natural-language instruction body + an optional `scripts/` directory. A certain public skill market alone hosts **77,000+** of them, a ready-made pool of capability supply.

The bad news: it only hands you a paginated API. To turn that into a foundation your product runtime can actually use, you have to solve everything yourself:

- How do you **fetch the whole thing completely** (a single hiccup in API pagination drops thousands of entries, and the dropped ones get misread as "delisted")?
- How do you **clean and normalize** (70% of categories are empty, field semantics are inconsistent, dirty data carries NUL bytes)?
- How do you **do semantic search** (2048-dim vectors blow past pgvector's ceiling, and the index won't even build)?
- How do you **keep it in sync** (the platform's `updated_at` is a dirty signal — everything is "updated" every single day)?

This document walks through all four end to end, and distills every trap into a **symptom → root cause → conclusion** entry.

---

## The five-stage pipeline

```
┌──────────────────────────────────────────────────────────────┐
│  Data source: a public third-party skill-market REST API       │
│               (no auth, ~77,000 entries)                       │
└───────────────┬──────────────────────────────────────────────┘
                │
   ① FETCH             list → probe → build
   │                   multi-pass union vs. pagination jitter + completeness gate
   ▼                   output: _full_raw.jsonl / all-skills/
   │
   ② NORMALIZE         scan → validate(5 hard conditions) → import → package
   │                   dry-run writes nothing; rerun on same snapshot is byte-for-byte identical
   ▼                   output: skills.ndjson (full body text included)
   │
   ③ STORAGE           Postgres + pgvector + object storage
   │                   metadata upsert / embedding ingest at 64 concurrency / zip upload / read-only RLS
   ▼                   end state: all four asset states present (metadata + body + vector + zip)
   │
   ④ RETRIEVAL         query → embed → halfvec cosine nearest-neighbor
   │                   ~500ms/query with IVFFlat index (130s without, ~260× speedup)
   ▼
   ⑤ SYNC ─────────────┘  weekly auto-diff across four states (new/updated/removed/stats_only)
                          trusts version, not updated_at; dual gates keep partial snapshots from rolling the baseline
```

**Human / machine split**: fetch / normalize / ingest / search / weekly diff are all fully automated, idempotent, and resumable from a checkpoint; **building the vector index** (via the server-side SQL Editor), **content-layer ingestion**, **credential rotation**, and **killing a process on the production DB** are the steps that stay anchored to a human.

---

## Engineering-notes quick reference (the most expensive ones)

The full 17 entries live in **[docs/engineering-notes.md](docs/engineering-notes.md)**; here are the easiest to hit and the costliest:

| # | Trap | Conclusion |
|---|---|---|
| ⭐ | 2048-dim embedding can't build an index | exceeds pgvector `vector`'s full-precision ceiling of 2000 → use **`halfvec` half-precision + an IVFFlat expression index**, and cast on both query sides too |
| ⭐⭐ | local index builds always drop the connection (`SSL SYSCALL error: EOF`) | the local proxy kills multi-minute idle long-lived connections by duration → **build the index server-side via the managed console SQL Editor** |
| ⭐ | bulk DB writes keep dying | don't hold long-lived connections → **ephemeral `db_once` connection + reconnect-retry**, or keepalive + bounded timeout |
| ⭐ | every skill is "updated" every day | `updated_at` is a dirty signal (refreshes with downloads) → trust **`version` only** for change detection |
| ⭐ | a single fetch pass drops thousands | API pagination jitters by ±1000+ → **multi-pass union + completeness gate**; a partial snapshot never rolls the baseline |
| ⭐⭐ | all DB writes stall, connections time out | a leftover `CREATE INDEX` process held a table lock for 13h → check `pg_stat_activity` first for active long-running queries |

> In one line: the hard part of this pipeline isn't drawing a five-stage architecture — it's these traps that **only surface once real data flows through**.

---

## Documentation map

| Document | Contents |
|---|---|
| **[docs/architecture.md](docs/architecture.md)** | Full redacted blueprint: each of the five stages unpacked + overall architecture diagram + table / index / retrieval SQL |
| **[docs/engineering-notes.md](docs/engineering-notes.md)** | ⭐ Engineering notes: 17 post-mortem entries (symptom → root cause → conclusion), the highest-value content in this repo |
| **[docs/field-dictionary.md](docs/field-dictionary.md)** | Field dictionary: three sets of semantics across fetch → clean → persist, plus a summary of how fields evolved |
| **[src/](src/)** | Redacted reference implementation: `fetch/` `normalize/` `storage/` `retrieval/` `sync/` — five layers of scripts + SQL, see [src/README.md](src/README.md) |

---

## Who should read this

- Anyone doing **RAG / vector search** who's been burned by high-dimensional embeddings, pgvector ceilings, and index construction
- Anyone giving an **Agent a skill / tool layer** who needs to engineer an external capability pool into a searchable foundation
- Anyone building a **large-scale data pipeline** (fetch → clean → ingest → sync) and wrestling with pagination jitter, dropped connections, and idempotent resumption
- Anyone who wants a **real post-mortem** rather than a happy-path tutorial

---

## Notes

- This repo contains **no real skill data** — the data source is a third-party public service, the raw data belongs to it, and the fetch host must be configured by you via an environment variable.
- `src/` is a **redacted reference implementation**: credentials and the data-source host all come from environment variables, it depends on external services (the data-source API, Postgres+pgvector, object storage, an embedding service), and it is **not run-out-of-the-box** — it's only for understanding the pipeline and as a basis for adaptation.
- SQL / code snippets in the docs are **illustrative**; credentials are always placeholders (`<...>` / `${...}`) — fill in your own environment for real use.
- The case study grew out of the real engineering practice behind an AI-employee / Agent marketplace product, and has been anonymized.

---

MIT © 2026 baomingli (橙研所)
