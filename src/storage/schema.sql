-- 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
-- 技能库 · Postgres 表结构（仅元数据，不含 body）
-- 跑法：psql "$DB_URL" -f schema.sql
--   或 托管控制台 → SQL Editor 粘贴执行
-- 字段对齐 _full_raw.jsonl / _baseline.jsonl 的 API 元数据

create table if not exists public.skills (
  slug             text primary key,        -- 主键（集合差定新增/下架）
  title            text,                     -- = API name（展示名）
  description      text,
  description_zh   text,
  category         text,
  tags             jsonb default '[]'::jsonb,
  downloads        integer default 0,
  installs         integer default 0,
  stars            integer default 0,
  version          text,                     -- 更新信号（diff 认 version 不认 updated_at）
  source           text,                     -- 来源渠道
  author           text,
  requires_api_key boolean default false,    -- 进哪个分发包
  has_package      boolean,                  -- probe 结果（302/200=有包）
  icon_url         text,
  homepage         text,
  is_active        boolean not null default true,   -- 软删：下架 = false（不物删）
  first_seen_at    timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index if not exists idx_skills_category  on public.skills(category);
create index if not exists idx_skills_req_key    on public.skills(requires_api_key);
create index if not exists idx_skills_active     on public.skills(is_active);
create index if not exists idx_skills_downloads  on public.skills(downloads desc);

create table if not exists public.skill_versions (
  id      bigserial primary key,
  slug    text not null references public.skills(slug) on delete cascade,
  version text not null,
  seen_at timestamptz not null default now(),
  unique (slug, version)                     -- updated 态追加新版本
);
create index if not exists idx_sv_slug on public.skill_versions(slug);
