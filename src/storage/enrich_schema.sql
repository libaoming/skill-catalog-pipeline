-- 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
-- 彻底版升级：pgvector + body 正文 + embedding 向量 + 对象存储指针
-- 跑法：psql "$DB_URL" -f enrich_schema.sql（或托管控制台 SQL Editor）
-- 前置：托管实例须支持 pgvector，且存储容量放得下 body+向量（免费层通常不够）

create extension if not exists vector;

alter table public.skills
  add column if not exists body         text,          -- SKILL.md 正文（去 frontmatter）
  add column if not exists embedding    vector(2048),  -- ⚠️ 维度按 embedding 模型改：常见 2048/2560，建列前必须确认
  add column if not exists storage_path text,          -- 对象存储里完整 zip 的路径，如 skills/<slug>.zip
  add column if not exists enriched_at  timestamptz;   -- 上次回填 body/向量/包的时间

-- 向量近邻索引（HNSW + 余弦）。数据量大时建议「先灌完向量再建索引」更快。
create index if not exists idx_skills_embedding
  on public.skills using hnsw (embedding vector_cosine_ops);

-- 语义检索示例（查与 query 向量最近的在架技能）：
--   select slug, title, 1 - (embedding <=> :qvec) as score
--   from public.skills
--   where is_active and embedding is not null
--   order by embedding <=> :qvec
--   limit 20;
