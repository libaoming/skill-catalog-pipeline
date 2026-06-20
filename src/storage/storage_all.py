#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
全量对象存储上传加速版（首传/补传用；增量走 import_to_db.sync_plan → enrich_slugs）。
DB 只查 storage_path=null 的 slug → 本地 make_zip 整目录打内存 zip → 64 并发 upload_zip
（对象存储 PUT，x-upsert 幂等）→ 批量 update storage_path。
幂等续跑：只处理 storage_path is null 且本地有目录（make_zip 返回 None 的无目录 slug 自动跳过）。

前置 env（项目根 .env 或环境变量）：
  DB_URL、SUPABASE_URL、SERVICE_KEY（service_role，写权限）
跑（需带 psycopg2 的 venv）：
  python -u storage_all.py
"""
import os, importlib.util as u
from concurrent.futures import ThreadPoolExecutor

SKDIR = os.path.dirname(os.path.abspath(__file__))
se = u.spec_from_file_location("e", os.path.join(SKDIR, "enrich_skills.py"))
e = u.module_from_spec(se); se.loader.exec_module(e)
assert e.imp._env("SUPABASE_URL") and e.imp._env("SERVICE_KEY"), "缺 SUPABASE_URL / SERVICE_KEY"

import time
import psycopg2
from psycopg2.extras import execute_values

UPD = ("update public.skills s set storage_path=v.path, enriched_at=now() "
       "from (values %s) as v(slug, path) where s.slug=v.slug")


def db_once(fn, retries=4):
    """开临时连接跑 fn(cur)→commit→关。对连接死亡(SSL EOF/InterfaceError)/超时重连重试。
    关键：连接只活几秒（DB 操作期间），绝不在上传网络间隙空闲 → 本地网络代理砍不到。"""
    last = None
    for a in range(retries):
        conn = None
        try:
            conn = e.imp.connect(psycopg2); cur = conn.cursor()
            r = fn(cur); conn.commit(); cur.close(); conn.close()
            return r
        except psycopg2.Error as ex_db:
            last = ex_db
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(2 * (a + 1))
    raise last


slugs = db_once(lambda c: (c.execute("select slug from public.skills where storage_path is null") or c.fetchall()))
slugs = [r[0] for r in slugs]
print(f"待上传（storage_path=null）: {len(slugs)}", flush=True)


def up(slug):
    try:
        z = e.make_zip(slug)
        if not z:
            return slug, None  # 本地无目录，跳过
        return slug, e.upload_zip(slug, z)
    except Exception:
        return slug, None


CHUNK = 256
done = ok = skipped = 0
for i in range(0, len(slugs), CHUNK):
    chunk = slugs[i:i + CHUNK]
    with ThreadPoolExecutor(max_workers=64) as ex:   # 上传阶段：无 DB 连接持有
        res = list(ex.map(up, chunk))
    vals = [(s, p) for s, p in res if p is not None]
    if vals:
        try:
            db_once(lambda c, v=vals: execute_values(c, UPD, v, template="(%s,%s)"))  # 临时连接写
            ok += len(vals)
        except psycopg2.Error as ex_db:
            skipped += len(vals)   # 重试仍失败（残留锁/持续断连）→ storage_path 留 null，下轮幂等补记
            print(f"  ! chunk skip {len(vals)}（{type(ex_db).__name__}），下轮补记", flush=True)
    done += len(chunk)
    print(f"  {done}/{len(slugs)}  (上传 {ok}  跳过 {skipped})", flush=True)

total = db_once(lambda c: (c.execute("select count(*) from public.skills where storage_path is not null") or c.fetchone()))
print(f">>> 完成；storage_path 非空总数 = {total[0]}", flush=True)
