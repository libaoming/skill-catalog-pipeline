-- 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
-- 技能库只读 RLS（上线前启用）。
-- 目标：公开角色（anon / authenticated）只能 SELECT，不能写；写入仅限 service_role / 超级用户（二者 BYPASSRLS，sync/enrich/storage 脚本不受影响）。
-- 应用：必须在「对象存储全量上传等批量 UPDATE 跑完后」执行——ALTER TABLE ENABLE RLS 取表级 ACCESS EXCLUSIVE 锁，会和批量写冲突。
-- 跑法：经 import_to_db.connect() 直连执行，或在托管控制台 SQL Editor 执行本文件。

-- ───── skills ─────
alter table public.skills enable row level security;
revoke insert, update, delete on public.skills from anon, authenticated;
grant select on public.skills to anon, authenticated;
drop policy if exists skills_public_read on public.skills;
create policy skills_public_read on public.skills
  for select to anon, authenticated
  using (is_active);   -- 软删（is_active=false）的技能对公开读不可见

-- ───── skill_versions ─────
alter table public.skill_versions enable row level security;
revoke insert, update, delete on public.skill_versions from anon, authenticated;
grant select on public.skill_versions to anon, authenticated;
drop policy if exists skill_versions_public_read on public.skill_versions;
create policy skill_versions_public_read on public.skill_versions
  for select to anon, authenticated
  using (true);
