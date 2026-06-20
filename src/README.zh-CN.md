# skill-catalog-pipeline（脱敏参考实现）

> 🌏 [English](README.md) | **中文**

> ## ⚠️ 这是脱敏参考实现，**非开箱即跑**
>
> 本仓库是一条「技能目录抓取 → 清洗 → 入库 → 语义检索 → 增量同步」管线的**脱敏代码参考**，
> 用于理解管线设计与改造借鉴，**不能直接运行**：
> - 所有凭证（DB 密码、对象存储 service key、embedding API key）和所有第三方 host
>   **已抽成环境变量**，仓库内不含任何真实值；
> - 依赖一组你必须自行准备的**外部服务**：数据源 API、Postgres+pgvector、对象存储、embedding 服务；
> - 仓库内**不含任何真实数据**（无技能元数据、无正文、无向量、无快照）。

## 数据源声明

原始技能数据来自**某第三方公开技能市场服务**，其数据归该服务及各技能作者所有。
本仓库**不包含**任何抓取到的真实数据，也**不公开**该第三方的真实域名——
抓取 host 必须由你自己通过环境变量 `SKILL_MARKET_API_BASE` 配置后才能运行抓取层。
是否抓取、如何抓取须自行确认目标服务的条款与合规边界。

## 环境变量占位表

| 变量 | 说明 |
|------|------|
| `DB_URL` | Postgres 标准连接串（含 host/user/password），如 `postgresql://<user>:<pwd>@<pooler-host>:5432/postgres`。入库/检索/回填统一从这里读，代码不硬编码任何 host/账号 |
| `SUPABASE_URL` | 对象存储 / REST host（用于上传完整 zip 包），形如 `https://<project>.<host>` |
| `SUPABASE_PROJECT_REF` | 托管 Postgres 项目标识（仅在需要单独引用 project ref 时用；连接本身走 `DB_URL`） |
| `SERVICE_KEY` | 对象存储写权限密钥（service_role 级；公开 anon key 无写权限，不能用） |
| `EMBED_API_KEY` | embedding 服务的 API key（以 `Bearer` 方式带在请求头） |
| `EMBED_API_BASE` | embedding 服务 endpoint base（不含则代码用占位 `<embedding-api-base>`，请求会失败） |
| `EMBED_MODEL` | embedding 模型标识（推理接入点 id 或模型名）；**向量维度须与 `enrich_schema.sql` 里 `vector(N)` 对齐** |
| `SKILL_MARKET_API_BASE` | 数据源技能市场 API host（抓取层用，不含则用占位 `<skill-market-api>`，抓取会失败） |

可把以上变量写进仓库根 `.env`（脚本会相对化读取），或直接 export 到环境。

## 目录导航（五层管线）

```
src/
  fetch/        抓取层：从数据源 API 拉全量元数据 + 正文包
    fetch_all.py      list/probe/build 三阶段：抓列表→探测有无包→产「能直接用」交付清单
    fetch_bodies.py   按「不需 key + 有包」并发下载每条正文 zip（断点续传、失败重试）
    extract.py        把正文 zip 解成 all-skills/<slug>/ 目录结构 + 元数据合并

  normalize/    清洗层：扫描、按安装标准校验、规范化、打分发包
    scan.py                 dry-run 扫描，按 metadata/SKILL.md 出 import|skip|review 决策
    validate_structure.py   按「可被安装/加载」硬条件过滤，产 installable.tsv / filtered.tsv
    import_skills.py        产规范化 NDJSON 中间产物（含 body，供入库/检索）
    package_skills.py       每技能打一个独立 zip 分发包 + catalog.ndjson 索引

  storage/      存储层：建表、入库、向量化、对象存储、回填
    schema.sql          skills / skill_versions 表结构（仅元数据）
    enrich_schema.sql   pgvector 升级：body + embedding 向量 + storage_path 列 + HNSW 索引
    rls.sql             只读 RLS（公开角色只能 SELECT，写入仅 service_role）
    import_to_db.py     元数据 → Postgres（全量灌入 + 供同步层增量 upsert，四态↔DB）
    enrich_skills.py    内容层回填：body 正文 + embedding 向量 + 完整 zip 传对象存储
    embed_all.py        全量 embedding 加速版（64 并发；只处理 embedding=null）
    storage_all.py      全量对象存储上传加速版（64 并发；db_once 临时连接抗断连）

  retrieval/    检索层
    search.py     query → embedding → pgvector cosine 最近邻（halfvec/vector 检索 SQL）

  sync/         同步层
    sync_diff.py  按 slug 主键 diff 出 new/updated/removed/stats_only 四态增量计划
```

## 运行顺序提示

```
fetch（list → probe → build → fetch_bodies → extract）
  → normalize（scan → validate_structure → import_skills / package_skills）
  → storage（schema.sql → import_to_db → enrich_schema.sql → embed_all / storage_all / enrich_skills）
  → 建向量索引（数据量大时走托管控制台 SQL Editor 执行 enrich_schema.sql 里的 HNSW 索引，
                先灌完向量再建索引更快）
  → 上线前执行 rls.sql 收紧写权限
  → retrieval（search.py 冒烟验证语义检索）

sync（每周一次）：
  fetch 出新 catalog → sync_diff diff 旧/新 → 四态增量计划
  → 喂下载器/extract 补 new+updated → import_to_db.sync_plan 写库（可带 enrich 回填）
```
