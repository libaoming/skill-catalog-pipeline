#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
技能元数据 → Postgres（仅元数据，不含 body）。两种用法：
  1) CLI 全量灌入：python3 import_to_db.py [--src _scan/_baseline.jsonl] [--batch 5000] [--only-free]
  2) 被同步层调用 sync_plan(plan, new)：把 diff 四态增量写库

连接（统一走 DB_URL；标准 PG 连接串，含 host/user/password 全部在串里，免代码硬编码）：
  export DB_URL="postgresql://<user>:<pwd>@<pooler-host>:5432/postgres"
  （也可放项目根 .env；上线请用轮换后的新密码）
依赖：pip install psycopg2-binary

四态↔DB：new/updated → upsert(+skill_versions)；removed → is_active=false（软删）；
  stats_only → 默认不写（只 downloads/stars 变，量大；sync_stats=True 才一起 upsert）。
"""
import os, sys, json

SK = os.path.dirname(os.path.abspath(__file__))
# .env 路径相对仓库根（脱敏：原绝对路径已相对化）
ENV = os.path.join(SK, "..", "..", ".env")
DEFAULT_SRC = os.path.join(SK, "..", "normalize", "fixtures", "skills", "_scan", "_baseline.jsonl")
SCHEMA = os.path.join(SK, "schema.sql")

COLS = ["slug", "title", "description", "description_zh", "category", "tags",
        "downloads", "installs", "stars", "version", "source", "author",
        "requires_api_key", "has_package", "icon_url", "homepage"]

UPSERT = f"""insert into public.skills ({",".join(COLS)}) values %s
on conflict (slug) do update set
  title=excluded.title, description=excluded.description, description_zh=excluded.description_zh,
  category=excluded.category, tags=excluded.tags, downloads=excluded.downloads,
  installs=excluded.installs, stars=excluded.stars, version=excluded.version,
  source=excluded.source, author=excluded.author, requires_api_key=excluded.requires_api_key,
  has_package=excluded.has_package, icon_url=excluded.icon_url, homepage=excluded.homepage,
  is_active=true, updated_at=now();"""


def _env(key):
    if os.environ.get(key):
        return os.environ[key]
    if os.path.exists(ENV):
        for line in open(ENV, encoding="utf-8"):
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def have_creds():
    return bool(_env("DB_URL"))


# TCP keepalive：撑过 embed/对象存储上传这种几十秒网络密集间隙里 DB 连接的空闲期，
# 防本地网络代理按空闲时长砍长连接 → 后续 UPDATE 挂在死 socket。
# （多分钟级索引构建救不了；此处间隙仅数十秒，10s 探活有效。）
_KEEPALIVE = dict(keepalives=1, keepalives_idle=10, keepalives_interval=5, keepalives_count=5)


def connect(psycopg2):
    url = _env("DB_URL")
    if not url:
        sys.exit("!! 缺 DB_URL（标准 PG 连接串，含 host/user/password）")
    conn = psycopg2.connect(url, **_KEEPALIVE)
    # statement_timeout 调宽（防批量 UPDATE/upsert 在负载下被 DB 默认超时掐断），
    # 但不设 0（会让撞锁的 UPDATE 无限挂）；lock_timeout 让撞残留行锁的语句 30s 内快速失败。
    # Session pooler(5432) 下 SET 在会话内持续；索引构建走服务端 SQL Editor，不经此路径。
    try:
        c = conn.cursor()
        c.execute("set statement_timeout = '10min'")
        c.execute("set lock_timeout = '30s'")
        c.close(); conn.commit()
    except Exception:
        pass
    return conn


def needs_key(r):
    return str((r.get("labels") or {}).get("requires_api_key")).lower() == "true"


def _clean(v):
    """去 NUL（0x00）——PG text/jsonb 不接受；递归 str/list/dict。"""
    if isinstance(v, str):
        return v.replace("\x00", "")
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    return v


def _row(r, Json):
    return (r.get("slug"), _clean(r.get("name")), _clean(r.get("description")), _clean(r.get("description_zh")),
            _clean(r.get("category")), Json(_clean(r.get("tags") or [])),
            r.get("downloads") or 0, r.get("installs") or 0, r.get("stars") or 0,
            _clean(r.get("version")), _clean(r.get("source")), _clean(r.get("author")),
            needs_key(r), r.get("has_package"), _clean(r.get("iconUrl")), _clean(r.get("homepage")))


def ensure_schema(cur, conn):
    if os.path.exists(SCHEMA):
        cur.execute(open(SCHEMA, encoding="utf-8").read())
        conn.commit()


def _upsert(cur, execute_values, rows, batch=5000, verbose=False):
    tmpl = "(" + ",".join(["%s"] * len(COLS)) + ")"
    for i in range(0, len(rows), batch):
        execute_values(cur, UPSERT, rows[i:i + batch], template=tmpl)
        if verbose:
            print(f"  upsert {min(i + batch, len(rows))}/{len(rows)}")


def _insert_versions(cur, execute_values, vrows, batch=5000):
    for i in range(0, len(vrows), batch):
        execute_values(cur,
                        "insert into public.skill_versions(slug,version) values %s on conflict (slug,version) do nothing",
                        vrows[i:i + batch])


def enrich_changed(plan):
    """对 new/updated 中本地 all-skills/<slug>/ 已存在的 slug 回填 body/向量/对象存储包。
    缺本地目录的 slug（如刚检测到、尚未下载入库）安全跳过——内容层回填依赖本地包，
    须先经人工『入库』(下载+解压到 all-skills) 才能补。返回回填条数（失败抛错由调用方兜底）。"""
    import importlib.util as _u
    _spec = _u.spec_from_file_location("enr", os.path.join(SK, "enrich_skills.py"))
    enr = _u.module_from_spec(_spec); _spec.loader.exec_module(enr)
    alldir = os.path.join(SK, "..", "normalize", "fixtures", "skills", "all-skills")
    changed = list(plan["new"]) + list(plan["updated"])
    with_dir = [s for s in changed if os.path.isdir(os.path.join(alldir, s))]
    if not with_dir:
        return 0, len(changed)
    return enr.enrich_slugs(with_dir), len(changed) - len(with_dir)


def sync_plan(plan, new, sync_stats=False, enrich=False):
    """供同步层调用：把 diff 四态增量写库。无凭证/无 psycopg2 → 返回 skip 原因（不抛错）。
    enrich=True：元数据写完后，对本地已有包的 new/updated slug 补 body/向量/对象存储（缺包的跳过、失败不阻塞）。"""
    if not have_creds():
        return "skip(无 DB 凭证，未写库)"
    try:
        import psycopg2
        from psycopg2.extras import execute_values, Json
    except ImportError:
        return "skip(psycopg2 未装)"
    conn = connect(psycopg2)
    cur = conn.cursor()
    ensure_schema(cur, conn)
    changed = list(plan["new"]) + list(plan["updated"])
    if sync_stats:
        changed += list(plan["stats_only"])
    rows = [_row(new[s], Json) for s in changed if s in new]
    _upsert(cur, execute_values, rows)
    vrows = [(s, _clean(new[s].get("version"))) for s in (list(plan["new"]) + list(plan["updated"]))
             if s in new and new[s].get("version")]
    _insert_versions(cur, execute_values, vrows)
    removed = list(plan.get("removed") or [])
    if removed:
        cur.execute("update public.skills set is_active=false, updated_at=now() where slug = any(%s)", (removed,))
    conn.commit()
    cur.close()
    conn.close()
    msg = (f"upsert {len(rows)}（new {len(plan['new'])}+updated {len(plan['updated'])}"
           f"{'+stats '+str(len(plan['stats_only'])) if sync_stats else ''}）/ 软删 {len(removed)} / 版本+{len(vrows)}")
    if enrich:
        try:
            n_enr, n_skip = enrich_changed(plan)
            msg += f" / 内容回填 {n_enr}（跳过无本地包 {n_skip}）"
        except Exception as e:
            msg += f" / 内容回填失败(不阻塞): {e}"
    return msg


def main(argv):
    src = argv[argv.index("--src") + 1] if "--src" in argv else DEFAULT_SRC
    batch = int(argv[argv.index("--batch") + 1]) if "--batch" in argv else 5000
    only_free = "--only-free" in argv

    import psycopg2
    from psycopg2.extras import execute_values, Json

    rows, vrows, n_key = [], [], 0
    for line in open(src, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if not r.get("slug"):
            continue
        nk = needs_key(r)
        if nk:
            n_key += 1
        if only_free and nk:
            continue
        rows.append(_row(r, Json))
        if r.get("version"):
            vrows.append((r["slug"], _clean(r.get("version"))))

    print(f"待入库 {len(rows)} 行（需 key {n_key}，{'已排除' if only_free else '含'}）；源 {src}")
    conn = connect(psycopg2)
    cur = conn.cursor()
    ensure_schema(cur, conn)
    print("✓ 表结构已确保（skills / skill_versions）")
    _upsert(cur, execute_values, rows, batch=batch, verbose=True)
    conn.commit()
    _insert_versions(cur, execute_values, vrows, batch=batch)
    conn.commit()
    cur.execute("select count(*), count(*) filter (where is_active), count(*) filter (where requires_api_key) from public.skills")
    total, active, needk = cur.fetchone()
    print(f">>> skills 表：总 {total} / 在架 {active} / 需 key {needk}")
    cur.close(); conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
