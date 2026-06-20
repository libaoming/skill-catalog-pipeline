# 技能库工程方案 — 抓取 · 清洗 · 存储 · 检索 · 同步

> 🌏 [English](architecture.md) | **中文**

> 把海量第三方技能包做成「可语义检索、可装载」的技能库底座的完整工程方案。
> 数据规模：~77,000 条 Claude Code 风格 SKILL.md 技能。

---

## 1. 背景与目标

**业务来源**：AI 员工 / Agent 产品要让 agent「能动手」，需要给它挂一层可检索、可装载的能力（tools / skills）。开源生态里已经有海量 Claude Code 风格的 `SKILL.md` 技能（一个技能 = 一段 frontmatter 元数据 + 一篇自然语言指令正文 + 可选 `scripts/` 脚本），是现成的能力供给池——本方案以某个公开、无鉴权的技能市场 REST API（约 77,000+ 条技能）为例。

**目标**：把全量技能**抓下来、清洗规范化、入库（含语义检索）、并持续增量同步**，作为产品 runtime 的技能检索 / 装载底座。

**「彻底版」定义**：单条技能在库内同时具备四态资产 ——

1. **元数据**（21 字段：分类 / 下载量 / 版本 / 需 key 标识…）— 检索筛选用
2. **body 正文**（`SKILL.md` 去 frontmatter）— 全文检索 + 喂 embedding
3. **embedding 向量**（多模态 embedding，2048 维，pgvector）— 语义检索
4. **完整 zip**（整目录含 `scripts/` `references/`，存对象存储）— 装载 / 分发用

**验证期技术选型**：Supabase 一站式（Postgres + pgvector + Storage）。理由：pgvector 混合检索一站式、上手最快。生产 runtime 实时调用时再评估迁到自建 / 境内向量库。

---

## 2. 总体架构

五层管线，每层产物落盘、幂等、可断点续跑。源头是公开 HTTP API，终点是 Postgres（结构化 + 向量 + 对象存储）。

```
┌──────────────────────────────────────────────────────────────────────┐
│  数据源：某公开技能市场 REST API（无鉴权）                                │
│   GET /api/skills?page=&pageSize=100      列表元数据（含 version/需key） │
│   GET /api/v1/download?slug=  → 302 zip(有包) / 404(无包)               │
│   GET /api/v1/skillsets                   官方策展场景包                 │
└───────────────┬──────────────────────────────────────────────────────┘
                │
   ┌────────────▼─────────────┐  ① 抓取层 FETCH
   │ fetch_all                 │   list→probe→build，多轮并集补抓+完整性校验
   │ fetch_bodies              │   下载 zip 正文
   │ extract                   │   解压 → all-skills/<slug>/
   └────────────┬─────────────┘   产物：_full_raw.jsonl / all-skills/
                │
   ┌────────────▼─────────────┐  ② 清洗层 NORMALIZE（dry-run，不写库）
   │ scan                      │   扫描判定 import|skip|review
   │ validate_structure        │   5 硬条件过滤 → installable.tsv
   │ import (映射)             │   字段映射 → skills.ndjson / versions.ndjson
   │ package                   │   每技能独立 zip → catalog.ndjson（分发包）
   └────────────┬─────────────┘
                │
   ┌────────────▼─────────────┐  ③ 存储层 STORAGE（Postgres + pgvector + Storage）
   │ schema.sql                │   skills + skill_versions 表
   │ enrich_schema.sql         │   body / embedding(vector 2048) / storage_path
   │ import_to_db              │   元数据 upsert + 软删 + 版本表
   │ embed_all                 │   embedding 全量灌入（64 并发，幂等续跑）
   │ storage_all               │   整目录 zip 上传 Storage（64 并发，抗断连）
   │ rls.sql                   │   上线前只读 RLS（anon/auth 只 select）
   └────────────┬─────────────┘   终态：public.skills（4 态资产齐 + RLS）
                │
   ┌────────────▼─────────────┐  ④ 检索层 RETRIEVAL
   │ search                    │   query→embed→ embedding<=>qvec 余弦最近邻
   └───────────────────────────┘
                ▲
   ┌────────────┴─────────────┐  ⑤ 更新层 SYNC（每周自动）
   │ sync_diff                 │   diff 四态：new/updated/removed/stats_only
   │ sync_weekly               │   抓新快照→diff→probe→报告→通知→滚基准
   │ + 定时触发                │   每周一固定时刻本地触发
   │ → import_to_db.sync_plan / enrich_slugs（增量回填）
   └───────────────────────────┘
```

**技术约束（贯穿全链路）**：

- 后台跑 Python 必须带 `-u`，否则输出被 buffer，误判卡死。
- 凭证从环境变量或本地 `.env` 读（环境变量优先），**绝不硬编码进脚本 / 仓库**。

---

## 3. 数据源与 API

实测三端点（公开、无鉴权）：

| 端点 | 用途 | 关键字段 / 行为 |
|---|---|---|
| `GET /api/skills?page=N&pageSize=100` | 列表元数据 | `slug` / `category` / `description_zh` / `version` / `downloads` / `source` / `labels.requires_api_key`（字符串 `"true"`/`"false"`）/ `iconUrl`；pageSize 上限 100（>100 返 null） |
| `GET /api/v1/download?slug=X` | 下载包 | 302→对象存储 zip（有包）/ 404（无包）。zip 内含 `SKILL.md` + `metadata.json` + `scripts/` |
| `GET /api/v1/skillsets` | 官方场景包 | 仅少量策展场景，**非分类体系**；主分类只能用 `category` |

**API 现实坑（已坐实）**：

- **分页不稳定**：同一时刻三次抓取返回数量波动 ±1000+。单轮抓必然漏 → 必须多轮并集（见 §4.1）。
- **`updated_at` 是脏信号**：跟随 downloads 每次同步刷新，无法区分「内容更新」vs「下载量涨」。**变更信号只认 `version`**（见 §8.1）。
- **`requires_api_key`** 在原始 `labels` 里是字符串 `"true"`，本地需规范成 bool。

---

## 4. ① 抓取层 FETCH

三阶段（可分跑、幂等、断点续抓）：

```
list   抓全量列表元数据 → _full_raw.jsonl + 分布统计
probe  逐条探 download(302有包/404无包) → 写回 _full_raw.jsonl 的 has_package
build  按「不需key + 有包」筛 → 研发可直接用的精选集
```

### 4.1 抓取健壮性设计（核心难点 = 对抗 API 分页抖动）

多层防御：

1. **失败页重试 ×3 轮**：单页失败不丢，重试 3 轮。
2. **多轮并集补抓**：整轮抓完取 slug 并集，若未达 total 的 99.5% → 整轮重抓并入并集，直到收敛。
3. **完整性校验闸门**：最终 < total 的 98% → 抛错，**不写残缺快照**（残缺快照绝不能滚成基准）。

> 效果：下架噪音从上万（残缺误报）收敛到几十（≈ 0.06%，是 API 固有波动，运维可接受）。

### 4.2 下载与解压

- 下载 zip 正文（下载不需 key，仅运行时需）。
- 解压 → `all-skills/<slug>/`（含 `metadata.json` + `SKILL.md` + `scripts/`）。

---

## 5. ② 清洗层 NORMALIZE

四脚本流水，全 dry-run（产报告，不写库），同快照重跑逐字节一致。

| 阶段 | 输入 | 产物 | 职责 |
|---|---|---|---|
| scan | `all-skills/` | `manifest.json` | 扫描判定 `import\|skip\|review`；`metadata.json` = 唯一可信主键源 |
| validate | `manifest.json` | `installable.tsv` | 5 硬条件过滤 |
| import | `installable.tsv` | `skills.ndjson` / `versions.ndjson` | 字段映射（含 body 全文） |
| package | `installable.tsv` | `catalog.ndjson` + `packages/<slug>.zip` | 每技能独立分发包 |

**5 条硬过滤条件**（能否被 Claude Code 装载）：

1. `metadata.json` 可解析且含 slug
2. 有且能消歧出唯一 `SKILL.md`
3. 合法 YAML frontmatter
4. 非空 `name`
5. 非空 `description`

**消歧（多 SKILL.md）**：优先根 `./SKILL.md`，否则按 `(depth, 字典序)` 取首个。

**结果**：约 5.4 万原始 → 约 4.8 万合规（过滤约 6500：无 frontmatter / 无 SKILL.md / 无 desc / 无 name）。

### 5.1 三字段口径（对治研发误报）

| 字段 | 含义 | 来源 | 校验 |
|---|---|---|---|
| `slug` | 平台主键 / 去重 / diff | API（全局唯一） | — |
| `display_name` | 展示给用户 | `metadata.name`（空 / == slug 时用 `description_zh` 首句 → slug 兜底） | — |
| `_skill_name` | 装 Claude Code 当 skill 用的标识符 | `SKILL.md` frontmatter `name` | **不校验 == slug**（本就不同维度） |

> 研发曾误报 `name does not match slug`、`Expected exactly one SKILL.md`——全是校验过严的非数据缺陷。`slug`（平台主键）和 `frontmatter.name`（技能装载标识符）本就是不同维度，不该相互校验。

---

## 6. ③ 存储层 STORAGE（Postgres + pgvector + Storage）

### 6.1 表结构

元数据双表：

```sql
public.skills(
  slug PK, title, description, description_zh, category, tags(jsonb),
  downloads, installs, stars,
  version,                  -- 更新信号（diff 认 version 不认 updated_at）
  source, author,
  requires_api_key bool,    -- 进哪个分发包
  has_package bool,         -- probe 结果
  icon_url, homepage,
  is_active bool default true,   -- 软删：下架=false（不物删）
  first_seen_at, updated_at
)  -- + 索引: category / req_key / is_active / downloads desc

public.skill_versions(
  id bigserial PK, slug FK→skills, version, seen_at,
  unique(slug, version)     -- updated 态追加新版本，留版本史
)
```

彻底版增列：

```sql
create extension if not exists vector;
alter table public.skills
  add column body         text,          -- SKILL.md 正文
  add column embedding    vector(2048),  -- 多模态 embedding 维度
  add column storage_path text,          -- skills/<slug>.zip
  add column enriched_at  timestamptz;
```

> ⚠️ **向量索引方案（实战修正，原 `hnsw (embedding vector_cosine_ops)` 作废）**：
>
> - **2048 维超 pgvector HNSW/IVFFlat 全精度 `vector` 上限 2000** → 必须用 **`halfvec` 半精度**（上限 4000，精度损失对排序基本无感）。
> - 实际采用 **IVFFlat over halfvec 表达式索引**（HNSW 在小实例上建索引耗内存更高、更易被掐）：
>
> ```sql
> set statement_timeout = '30min';
> set max_parallel_maintenance_workers = 0;   -- 串行，避开并行 worker 在 /dev/shm 开段 DiskFull
> create index idx_skills_embedding_ivf on public.skills
>   using ivfflat ((embedding::halfvec(2048)) halfvec_cosine_ops) with (lists = 200);
> ```
>
> - 🔴 **建索引必须在数据库托管控制台的 SQL Editor 里跑（服务端执行）**，**不能从本地长连接建**——本地代理（如 Clash，DB 连到 fake-IP）会按时长砍多分钟空闲长连接（`SSL SYSCALL error: EOF detected`，keepalive 无效；服务端日志无 OOM/FATAL 可证非服务端问题）。灌数据 / 查询有数据流能活，纯等待几分钟的建索引必死。详见 [engineering-notes.zh-CN.md](engineering-notes.zh-CN.md)。

### 6.2 元数据入库

- 读基准快照 → 清洗（递归去 NUL `0x00`，PG text/jsonb 拒收）→ 批量 upsert（5000/批）+ `skill_versions`。
- 连接走 **Session pooler**（IPv4 免费代理）。
- 提供 `sync_plan(plan, new, enrich=False)` 给 sync 层做四态增量（见 §8.3）。

**`connect()` 加固** —— 批量写库（storage/enrich/upsert）在负载下反复死，三件套修：

```python
# 每个连接建立后立即 SET（Session pooler 会话内持续）
set statement_timeout = '10min'   # 防 256 行批量 UPDATE 撞 DB 默认超时被取消；不设 0（0 会让撞锁 UPDATE 无限挂）
set lock_timeout      = '30s'     # 撞残留行锁快速失败，不挂死
# + TCP keepalive(keepalives_idle=10s)：撑过 embed/上传几十秒网络间隙，防代理砍空闲连接
```

> 索引构建（分钟级）仍走 SQL Editor 服务端，不经此路径（见 §6.1 注）。

### 6.3 Embedding 灌入

**多模态 embedding 的硬约束（踩坑结晶）**：

- ⚠️ **model 名直调 404 → 必须建推理接入点用 endpoint-id**（占位 `${EMBED_MODEL}`）。
- 端点 `/api/v3/embeddings/multimodal`，input = `[{type:text, text}]`，**多模态一次融合成 1 向量 → 每条单请求，不能批量塞多文本**。
- 维度 **2048**。

脚本设计：

1. DB 只查 `embedding is null and body is not null` 的 slug（**不拉大 body 字段**，避免 statement 超时）。
2. body 本地读 → 64 并发 embed → 批量 `update ... from (values %s) v(slug,emb) where s.slug=v.slug`，每 256 条 commit。
3. **幂等续跑**：只处理 null，失败 slug 跳过不阻塞。崩溃后重跑自动续灌。
4. **尾巴补灌**：本地缺 body 但 DB 有 body 的，单独从 DB body 读、同口径 embed 补上。

> 灌完建索引：见 §6.1 注 —— **halfvec + IVFFlat，且必须在 SQL Editor 服务端跑**。
> **实测结果**：约 7.5 万条 embedding（全部有 body 的行），IVFFlat 索引建成。

### 6.4 Storage 上传（加速 + 抗断连）

对治串行瓶颈（7.5 万串行 PUT 要数小时）：

1. DB 查 `storage_path is null` 的 slug → 本地整目录打内存 zip。
2. 64 并发上传到对象存储（header 必带 `apikey` + `Authorization: Bearer <service_key>` + `x-upsert:true`，缺 apikey → 400）。
3. 批量 update `storage_path`，无本地目录的 slug 自动跳过。

**bucket** 私有。**写权限**：必须 service_role / secret key（anon 不行）。

**⭐ 抗断连关键重构：上传期不持 DB 连接**

原版持一个长命 DB 连接贯穿整个上传循环 → 在每 chunk 几十秒的上传网络间隙里连接空闲，被本地代理砍掉（`SSL SYSCALL error: EOF detected`），随后 `conn.rollback()` 在死连接上又抛 `InterfaceError` 把整个 job 带崩。重构成 **`db_once(fn)`**：

- 上传阶段（`ThreadPoolExecutor` 并发 PUT）**完全不持 DB 连接**。
- 每 chunk 上传完，才临时开连接做 UPDATE → commit → **立即关**；**连接只活几秒，永不空闲 → 代理砍不到**。
- 对连接死亡（SSL EOF / InterfaceError / OperationalError）**重连重试 4 次**（指数退避）；重试仍失败才留 null，下轮幂等补记。

> 通用原则：**本地批量写库别持长命连接**，要么临时连接，要么 §6.2 的 keepalive + 有限 timeout。

**完成实测**：重构版扛住代理断连一口气跑完，`storage_path` 非空约 7.5 万（剩约 2000 = 本地无包的尾巴，需先下载入库才能补）。

---

## 7. ④ 检索层 RETRIEVAL

检索 SQL（query 与库内向量同口径、走 IVFFlat halfvec 索引）：

```sql
set ivfflat.probes = 10;   -- 越大越准越慢
-- qvec = embed(query) 得到 2048 维向量
select slug, title,
       (embedding::halfvec(2048) <=> :qvec::halfvec(2048)) as dist
from public.skills
where embedding is not null
order by embedding::halfvec(2048) <=> :qvec::halfvec(2048)
limit k;
```

- 用同一个 embed 函数保证 query 向量与库内向量在**同一空间**（关键正确性）。
- **两侧都 cast `::halfvec(2048)`** 才能命中 IVFFlat halfvec 表达式索引（与建索引表达式一致）。
- **实测（约 7.5 万条）**：带索引 `probes=10` **~500ms/查询**（无索引精确扫描 **130s** → 提速 ~260×）；结果高相关——外呼营销 → 客户跟进 / 客服话术；数据整理 → 数据报告生成器；客户投诉 → 电商售后回复。
- 内置一组中文场景查询做冒烟。

---

## 8. ⑤ 更新层 SYNC（每周自动 · 检测 + 通知）

设计成「只检测 + 通知」层（不下载 / 不入库 / 不重打，入库人工触发）+ 本地定时 + 每周 + IM 通知。

### 8.1 增量 diff

按 slug 主键 diff 新旧 catalog，四态：

| 态 | 判定 | 动作 |
|---|---|---|
| `new` | slug 新增且有包 | 下载入库 |
| `updated` | **version 变** | 重下 + 追加 `skill_versions` |
| `removed` | slug 消失 | 软删 `is_active=false`（不物删） |
| `stats_only` | 仅 downloads/stars 变 | 只更新统计列（免下载） |

> selftest 内置 mock 四态回归（含「updated_at 变但 version 不变 + downloads 变 → 必判 stats_only」守坑 case）。

### 8.2 周度调度

```
抓新快照 → diff(baseline, new) → 对新增 slug 补 probe
→ 生成报告 → IM 通知（发给运维 owner）
→ 滚动基准（_baseline.jsonl 覆盖，旧的归档 snapshots/）
```

- **触发**：本地定时器，每周固定时刻。
- **双闸门防污染**：新快照 < 基准 95% → 中止、不滚基准、发告警（已两次回滚验证）。
- **运维自通知不走对外触达闸口**：对外触达（给客户发消息）必须经人工确认闸口；运维自通知发给 owner 本人，走对外闸口就不「自动」了。

### 8.3 sync → DB 自动回填（含内容层 enrich）

sync 报告后调 `sync_plan(plan, new, enrich=False)`：

- **元数据层（默认）**：new/updated → upsert(+versions)、removed → 软删、stats_only 默认不写（量大）。失败不阻塞报告 / 通知。
- **内容层（`enrich=True` 时）**：upsert 完对 new/updated 中**本地已存在**的 slug 回填 body/向量/Storage 包；**缺本地包的安全跳过**（内容层依赖本地包，须先人工「入库」下载解压）。整段 try/except 包住，回填失败只记日志不阻塞。

> 每周自动 sync 仍只发 `enrich=False`（新增 slug 此刻无本地包、enrich 无意义）。内容层回填走人工「入库」路径：下载 zip → 解压 → `sync_plan(plan, new, enrich=True)`。

---

## 9. 运维 · 成本 · 安全

**成本**（参考量级）：托管 Postgres Pro 档约 $25/mo + embedding 首灌一次性约几十到一百多元（batch 折扣）+ 每周增量约 ¥10/mo。

**安全**：

- 凭证从环境变量 / `.env` 读，**绝不硬编码进仓库**；上线前轮换。
- **RLS 只读**：`skills` + `skill_versions` 各 `enable row level security` + `revoke insert/update/delete from anon,authenticated` + `grant select` + 只读 select policy（`using(is_active)` → 软删技能对公开读不可见）。**写仅限 service_role / postgres**（二者 BYPASSRLS，sync/enrich/storage 管线不受影响）。
- bucket 私有，访问走 service key 或签名 URL。

**必备凭证**（填本地 `.env`，占位）：

```
DB_URL=<postgres-connection-string>
STORAGE_URL=<object-storage-endpoint>
SERVICE_KEY=<service-role-key>
EMBED_API_KEY=<embedding-api-key>
EMBED_MODEL=<embedding-endpoint-id>
```

---

## 附录：字段字典

抓取 → 清洗 → 落库的三套字段口径见 [field-dictionary.zh-CN.md](field-dictionary.zh-CN.md)。

## 附录：工程结晶

把全链路踩过的坑整理成的复盘见 [engineering-notes.zh-CN.md](engineering-notes.zh-CN.md) ——本方案最高价值的部分。
