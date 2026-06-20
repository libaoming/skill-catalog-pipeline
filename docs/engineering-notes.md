# Engineering Crystallization — Lessons from Turning 77K Skills into a Searchable Library

> 🌏 **English** | [中文](engineering-notes.zh-CN.md)

> This is the **highest-value** document in the repo. Anyone can draw an architecture diagram; what's actually expensive are the pitfalls below that only surfaced once we ran against real data.
> Each entry: **Symptom → Root cause → Takeaway**. The ⭐ ones are the easiest to hit and the most costly.

---

## I. Vector Search

### ⭐ 1. 2048 dimensions exceed pgvector's full-precision `vector` cap of 2000

- **Symptom**: With an embedding dimension of 2048, building an `hnsw (embedding vector_cosine_ops)` index errors out immediately.
- **Root cause**: pgvector's HNSW / IVFFlat have a **2000-dimension cap** on the full-precision `vector` type.
- **Takeaway**: Use **`halfvec` half precision** (cap of 4000; the precision loss is essentially imperceptible for ranking). The index is built on an expression, and **both sides of the query must also be cast** for it to be used:

```sql
-- Build the index
create index idx_skills_embedding_ivf on public.skills
  using ivfflat ((embedding::halfvec(2048)) halfvec_cosine_ops) with (lists = 200);

-- Query (cast on both sides, otherwise the index is not used)
order by embedding::halfvec(2048) <=> :qvec::halfvec(2048)
```

> Choosing IVFFlat over HNSW: on a small instance, HNSW index builds consume more memory and are more likely to be cut off by resource limits.

### ⭐⭐ 2. Building a vector index locally always drops the connection (the nastiest one)

- **Symptom**: Running an index build from a local script, after a few minutes it always fails with `SSL SYSCALL error: EOF detected`; the server logs show **no OOM / FATAL** (which rules out a server-side problem).
- **Root cause**: A local proxy (e.g. Clash, which maps the DB to a fake-IP `198.18.x.x`) **kills multi-minute idle long connections by duration**, and TCP keepalive is ineffective. During data loading / querying there's a data stream on the connection so it stays alive, but an index build is "just waiting for several minutes," so it always gets killed.
- **Takeaway**: **Always build vector indexes via the managed database console's SQL Editor (executed server-side)**, bypassing the local connection — succeeds in one shot. Alternative: configure the proxy to connect directly to the database host.

### 3. The resource wall when building indexes

- **Symptom**: A server-side index build is cut off by `statement_timeout`; or parallel workers allocating segments in `/dev/shm` cause `DiskFull`.
- **Takeaway**: Set up two things before building:

```sql
set statement_timeout = '30min';            -- don't let the timeout cut it off
set max_parallel_maintenance_workers = 0;   -- serial, to avoid parallel workers blowing up /dev/shm
```

---

## II. Database Connections and Batch Writes

### ⭐ 4. Don't hold a long-lived connection for batch writes

- **Symptom**: storage upload / enrich kept dying under load. A single long connection was held across the entire upload loop; during each chunk's tens-of-seconds network gap the connection sat idle and got killed by the proxy, after which `rollback()` on the dead connection threw `InterfaceError` and crashed the whole job.
- **Takeaway**: Two ways to survive — pick one:
  1. **Ephemeral connection `db_once`**: hold no DB connection at all during the upload phase; only after each chunk finishes uploading do you briefly open a connection to do the UPDATE → commit → **close immediately**, so the connection lives only a few seconds and is never idle; retry up to 4 times on connection death (exponential backoff).
  2. **keepalive + bounded timeout**: after establishing the connection, set `statement_timeout='10min'` (don't set 0; 0 lets a lock-contending UPDATE hang forever), `lock_timeout='30s'`, and TCP `keepalives_idle=10s`.

### 5. Querying large fields times out

- **Symptom**: A full-table `select` that pulls the large `body` field times out at the statement level.
- **Takeaway**: Query only `slug` (lightweight) from the DB, and read `body` from local files. For rows that need embedding, query only `embedding is null and body is not null`, without pulling the large field.

### 6. NUL bytes in dirty data

- **Symptom**: A skill field contained `NUL` (`0x00`), and the entire batch upsert was rejected by PG.
- **Takeaway**: Recursively `_clean` to strip NUL before insertion (PG text/jsonb does not accept `0x00`).

### ⭐⭐ 7. A leftover CREATE INDEX process holding a table lock drags down the entire DB

- **Symptom**: All UPDATEs (batch writes) are blocked, the connection pool fills up with `ECHECKOUTTIMEOUT`, the console reports "exhausting multiple resources," and even the SQL Editor itself times out on connect.
- **Root cause**: A `create index ... hnsw` process from a previous session was **stuck active for 13 hours** and still held the lock on the `skills` table.
- **Takeaway**: **Whenever "all DB writes are blocked / connections time out," first check `pg_stat_activity` for an old active query holding a lock** (query it via a path that goes through the management channel and doesn't occupy the Session pool). `CREATE INDEX` can block for hours, and even an ordinary UPDATE, after being killed, can leave an IO-bound remnant that keeps the DB pinned.

### 8. Don't kill a healthy batch job

- **Symptom**: To speed things up, we `pkill`'d a running batch-write job, which made things worse.
- **Root cause**: Killing within the ~3s window of a write leaves an IO-bound remnant UPDATE (the server keeps running it; the session pool does not stop just because the client died), which pins the DB again and refills the pool.
- **Takeaway**: To stop it, let it finish the commit gap of a chunk first. Batch jobs are inherently idempotent and resumable from a checkpoint — **slow but steady**. Killing a production backend (`pg_terminate_backend`) should be done by the owner directly in the SQL Editor (be cautious killing processes on a shared production DB).

---

## III. Data Fetching and Sync

### ⭐ 9. Trust `version` as the change signal, not `updated_at`

- **Symptom**: `updated_at` is refreshed alongside downloads on every sync, so every skill looks "updated" every day.
- **Root cause**: The source's `updated_at` is a dirty signal — it can't distinguish "content updated" from "download count went up."
- **Takeaway**: The diff's change signal **trusts only `version`**. Guard case: "`updated_at` changed but `version` didn't + downloads changed → must be judged stats_only (update stats only, don't re-download)."
  > In one line: **a target without a verification mechanism is just a wish** — run it against real data and the dirty signal surfaces instantly.

### ⭐ 10. API pagination jitter → multi-round union + completeness gate

- **Symptom**: Three full crawls at the same moment return counts that swing by ±1000+. A single-round crawl is bound to miss items, and the missed ones get misjudged by the diff as "delisted," producing tens of thousands of delisting noise entries.
- **Takeaway**: Three layers of defense — retry failed pages ×3 rounds; after each full round take the union of slugs, and if it hasn't reached 99.5% of total, re-crawl the whole round and merge into the union until it converges; if the final count is < 98% of total, throw an error outright — **an incomplete snapshot must never be rolled into the baseline**. Delisting noise converged from tens of thousands down to a few dozen (≈ 0.06%, the API's inherent jitter).

### 11. Soft-delete, not hard-delete

- **Takeaway**: A delisted skill is marked `is_active=false` rather than physically deleted. Combined with RLS's `using(is_active)`, a delisted skill is invisible to public reads, but its version history and slug remain, making it easy to trace back and revive.

---

## IV. Embedding and Object Storage

### 12. Three hard constraints of multimodal embedding

- **Calling by model name directly returns 404** → you must first create an inference endpoint and call it by endpoint-id.
- **Multimodal fuses into 1 vector per request** → one request per item; you can't batch-stuff multiple texts.
- Dimension is **2048** (exactly what triggered the pgvector cap in entry 1).

### 13. Storage upload returns 400 if a header is missing

- **Symptom**: Uploading to object storage returns 400.
- **Takeaway**: Headers must include `apikey` + `Authorization: Bearer <service_key>` + `x-upsert:true` (a missing apikey is an immediate 400); write permission requires the service_role / secret key — anon won't work.

### 14. Serial upload is too slow → 64-way concurrency

- **Symptom**: 75K serial PUTs would take hours.
- **Takeaway**: Build an independent 64-way concurrent accelerated version following the embed pattern (`ThreadPoolExecutor`), paired with entry 4's `db_once` to withstand connection drops, and run it all in one go.

---

## V. Platform and Collaboration

### 15. The managed DB's MCP is read-only; writes need a direct connection

- **Takeaway**: A managed database's MCP tools are mostly read-only; creating tables / writing must use direct DB connection credentials (MCP's migration / execute write operations get rejected). However, **diagnostics like querying `pg_stat_activity` are better done via the MCP management channel** — it doesn't occupy the Session pool (see entry 7).

### 16. Session pooler long connections get cut off

- **Takeaway**: Under a free IPv4 Session pooler, long-running task connections may be cut off → scripts commit per batch + resume idempotently + wrap with an auto-rerun fallback. This is one of the root reasons "why all batch scripts must be idempotent."

### 17. Credential management discipline

- **Symptom**: Early on, writing credentials into `.env` was flagged by a security classifier.
- **Takeaway**: Always read credentials from environment variables / `.env` (environment variables take priority), keeping only placeholders in scripts; rotate before going live; put `.env` in `.gitignore` — **it must never enter the repo**.

---

## One-line Wrap-up

What's actually hard about this pipeline isn't "drawing a five-layer architecture," it's:

1. **Dimension over the cap** forced the switch from `vector` to `halfvec` + an expression index;
2. **The local proxy killing idle connections** forced moving index builds to the server side and rewriting batch writes to use ephemeral connections;
3. **The dirty `updated_at`** forced trusting only `version` as the change signal;
4. **API pagination jitter** forced a multi-round union + completeness gate;
5. **A leftover index lock** forced building the muscle memory of "check `pg_stat_activity` first."

Every one of these came out of running real data — none were foreseeable at the design stage.
