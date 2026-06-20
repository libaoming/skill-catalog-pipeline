# 工程结晶 — 把 7.7 万条技能做成可检索库踩过的坑

> 🌏 [English](engineering-notes.en.md) | **中文**

> 这是本仓库**最高价值**的一篇。架构图谁都画得出，真正贵的是下面这些「跑了真数据才现形」的坑。
> 每条：**现象 → 根因 → 结论**。带 ⭐ 的是最容易踩、代价最大的。

---

## 一、向量检索

### ⭐ 1. 2048 维超 pgvector 全精度 `vector` 上限 2000

- **现象**：embedding 维度 2048，建 `hnsw (embedding vector_cosine_ops)` 索引直接报错。
- **根因**：pgvector 的 HNSW / IVFFlat 对全精度 `vector` 类型有 **2000 维上限**。
- **结论**：用 **`halfvec` 半精度**（上限 4000，精度损失对排序基本无感）。索引建在表达式上，**查询两侧也要 cast** 才能命中：

```sql
-- 建索引
create index idx_skills_embedding_ivf on public.skills
  using ivfflat ((embedding::halfvec(2048)) halfvec_cosine_ops) with (lists = 200);

-- 查询（两侧都 cast，否则不走索引）
order by embedding::halfvec(2048) <=> :qvec::halfvec(2048)
```

> 选 IVFFlat 不选 HNSW：HNSW 在小实例上建索引耗内存更高、更易被资源墙掐。

### ⭐⭐ 2. 本地建向量索引必断连（最坑的一条）

- **现象**：本地脚本建索引，等几分钟后必报 `SSL SYSCALL error: EOF detected`；服务端日志**无 OOM / FATAL**（排除了服务端问题）。
- **根因**：本地代理（如 Clash，把 DB 连到 fake-IP `198.18.x.x`）会**按时长砍多分钟空闲长连接**，TCP keepalive 无效。灌数据 / 查询时连接上有数据流能活着，但建索引是「纯等待几分钟」，必被砍。
- **结论**：**建向量索引一律走数据库托管控制台的 SQL Editor（服务端执行）**，绕开本地连接，一次成功。备选 = 给代理对数据库 host 设直连。

### 3. 建索引的资源墙

- **现象**：服务端建索引被 `statement_timeout` 掐；或并行 worker 在 `/dev/shm` 开段导致 `DiskFull`。
- **结论**：建前两件套：

```sql
set statement_timeout = '30min';            -- 别让超时掐掉
set max_parallel_maintenance_workers = 0;   -- 串行，避开并行 worker 撑爆 /dev/shm
```

---

## 二、数据库连接与批量写

### ⭐ 4. 批量写库别持长命连接

- **现象**：storage 上传 / enrich 在负载下反复死。持一个长连接贯穿整个上传循环，每 chunk 几十秒的网络间隙里连接空闲被代理砍，随后 `rollback()` 在死连接上抛 `InterfaceError` 把整个 job 带崩。
- **结论**：两种活法二选一——
  1. **临时连接 `db_once`**：上传阶段完全不持 DB 连接；每 chunk 上传完才临时开连接做 UPDATE → commit → **立即关**，连接只活几秒永不空闲；对连接死亡重连重试 4 次（指数退避）。
  2. **keepalive + 有限 timeout**：连接建立后 `set statement_timeout='10min'`（不设 0，0 会让撞锁的 UPDATE 无限挂）、`set lock_timeout='30s'`、TCP `keepalives_idle=10s`。

### 5. 大字段查询会超时

- **现象**：`select` 带 `body` 大字段全表查，statement 超时。
- **结论**：DB 只查 `slug`（轻），`body` 从本地文件读。需要 embedding 的行只查 `embedding is null and body is not null`，不拉大字段。

### 6. 脏数据里的 NUL 字节

- **现象**：某技能字段含 `NUL`（`0x00`），整批 upsert 被 PG 拒收。
- **结论**：入库前递归 `_clean` 去 NUL（PG text/jsonb 不收 `0x00`）。

### ⭐⭐ 7. 残留 CREATE INDEX 进程持表锁拖垮全 DB

- **现象**：所有 UPDATE（批量写）全堵死、连接池占满 `ECHECKOUTTIMEOUT`、控制台报「exhausting multiple resources」、连 SQL Editor 自己都连接超时。
- **根因**：上个 session 一个 `create index ... hnsw` 进程 **active 卡死 13 小时**仍持 `skills` 表锁。
- **结论**：**任何「全 DB 写入都卡 / 连接超时」先查 `pg_stat_activity` 找 active 老查询持锁**（用走管理通道、不占 Session pool 的途径查）。`CREATE INDEX` 会卡数小时，普通 UPDATE 被 kill 后也会 IO-bound 残留把 DB 压住。

### 8. 别杀健康的批量任务

- **现象**：为提速去 `pkill` 正在跑的批量写库任务，结果更糟。
- **根因**：杀在写库的 ~3s 窗口会留 IO-bound 残留 UPDATE（服务端继续跑，会话池不随客户端死而停），把 DB 又压住、池又满。
- **结论**：要停先让它跑完一个 chunk 的 commit 间隙。批量任务本就幂等可断点续跑，**慢但稳**。杀生产 backend（`pg_terminate_backend`）应由 owner 在 SQL Editor 亲跑（共享生产 DB 杀进程要谨慎）。

---

## 三、数据抓取与同步

### ⭐ 9. 变更信号认 `version` 不认 `updated_at`

- **现象**：`updated_at` 跟随 downloads 每次同步刷新，每条技能天天「更新」。
- **根因**：平台的 `updated_at` 是脏信号，无法区分「内容更新」vs「下载量涨」。
- **结论**：diff 的变更信号**只认 `version`**。守坑 case：「updated_at 变但 version 不变 + downloads 变 → 必判 stats_only（只更统计、不重下）」。
  > 一句话总结：**没有验证机制的目标只是许愿**——拿真数据一跑，脏信号立刻现形。

### ⭐ 10. API 分页抖动 → 多轮并集 + 完整性闸门

- **现象**：同一时刻三次抓全量，返回数量波动 ±1000+。单轮抓必漏，漏掉的会被 diff 误判成「下架」，下架噪音上万条。
- **结论**：三层防御——失败页重试 ×3 轮；整轮抓完取 slug 并集，未达 total 99.5% 就整轮重抓并入并集直到收敛；最终 < total 98% 直接抛错，**残缺快照绝不滚成基准**。下架噪音从上万收敛到几十（≈ 0.06%，API 固有波动）。

### 11. 软删不物删

- **结论**：技能下架标 `is_active=false`，不物理删除。配合 RLS 的 `using(is_active)`，下架技能对公开读不可见，但版本史和 slug 仍在，便于回溯和复活。

---

## 四、Embedding 与对象存储

### 12. 多模态 embedding 的三个硬约束

- **model 名直调 404** → 必须先建推理接入点，用 endpoint-id 调。
- **多模态一次融合成 1 向量** → 每条单请求，不能批量塞多文本。
- 维度 **2048**（正是它触发了第 1 条的 pgvector 上限坑）。

### 13. Storage 上传缺 header 就 400

- **现象**：上传对象存储返 400。
- **结论**：header 必带 `apikey` + `Authorization: Bearer <service_key>` + `x-upsert:true`（缺 apikey 直接 400）；写权限必须 service_role / secret key，anon 不行。

### 14. 串行上传太慢 → 64 并发

- **现象**：7.5 万条串行 PUT 要数小时。
- **结论**：照 embed 范式做独立 64 并发加速版（`ThreadPoolExecutor`），配合第 4 条的 `db_once` 抗断连，一口气跑完。

---

## 五、平台与协作

### 15. 托管 DB 的 MCP 只读，写要直连

- **结论**：托管数据库的 MCP 工具多是只读模式，建表 / 写必须用 DB 直连凭证（MCP 的 migration / execute 写操作会被拒）。但**查 `pg_stat_activity` 这类诊断走 MCP 管理通道更好**——不占 Session pool（见第 7 条）。

### 16. Session pooler 长连接会被掐

- **结论**：免费 IPv4 Session pooler 下长任务连接可能被掐断 → 脚本每批 commit + 幂等续跑 + 自动重跑包装兜底。这是「为什么所有批量脚本都要幂等」的根因之一。

### 17. 凭证管理纪律

- **现象**：早期把凭证写 `.env` 被安全分类器拦。
- **结论**：凭证一律从环境变量 / `.env` 读（环境变量优先），脚本里只留占位符；上线前轮换；`.env` 进 `.gitignore`，**绝不进仓库**。

---

## 一句话收口

这套管线真正难的不是「画五层架构」，而是：

1. **维度超限**逼你从 `vector` 换 `halfvec` + 表达式索引；
2. **本地代理砍空闲连接**逼你把建索引挪到服务端、把批量写改成临时连接；
3. **脏的 `updated_at`** 逼你只认 `version` 做变更信号；
4. **API 分页抖动**逼你做多轮并集 + 完整性闸门；
5. **残留索引锁**逼你养成「先查 `pg_stat_activity`」的肌肉记忆。

每一条都是真数据跑出来的，不是设计阶段想得到的。
