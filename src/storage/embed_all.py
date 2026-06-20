#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
全量 embedding 加速版（首灌/重灌用；增量走 import_to_db.sync_plan → enrich_slugs）。
DB 只查 embedding=null 的 slug（不拉大 body，避免 statement 超时）→ body 本地 read_body → 64 并发
multimodal embed（每条单请求，一次融合成 1 向量）→ 批量 update embedding。
幂等续跑：只处理 embedding is null 且 body is not null；失败 slug 跳过不阻塞。

前置 env（项目根 .env 或环境变量）：
  DB_URL、EMBED_API_KEY、EMBED_MODEL（=推理接入点 id 或模型名）
跑（需带 psycopg2 的 venv）：
  python -u embed_all.py
灌完别忘建索引：
  create index if not exists idx_skills_embedding on public.skills using hnsw (embedding vector_cosine_ops);
"""
import os, importlib.util as u
from concurrent.futures import ThreadPoolExecutor

SKDIR = os.path.dirname(os.path.abspath(__file__))
se = u.spec_from_file_location("e", os.path.join(SKDIR, "enrich_skills.py"))
e = u.module_from_spec(se); se.loader.exec_module(e)
key = e.imp._env("EMBED_API_KEY"); model = e.imp._env("EMBED_MODEL")
assert key and model, "缺 EMBED_API_KEY / EMBED_MODEL"

import psycopg2
from psycopg2.extras import execute_values
conn = e.imp.connect(psycopg2); cur = conn.cursor()
cur.execute("select slug from public.skills where embedding is null and body is not null")
slugs = [r[0] for r in cur.fetchall()]
print(f"待 embed（embedding=null 且有 body）: {len(slugs)}")


def emb(slug):
    body = e.read_body(slug)
    if not body:
        return slug, None
    try:
        return slug, e._embed_one(body, key, model)
    except Exception:
        return slug, None


UPD = ("update public.skills s set embedding=v.emb::vector, enriched_at=now() "
       "from (values %s) as v(slug, emb) where s.slug=v.slug")
CHUNK = 256
done = ok = 0
for i in range(0, len(slugs), CHUNK):
    chunk = slugs[i:i + CHUNK]
    with ThreadPoolExecutor(max_workers=64) as ex:
        res = list(ex.map(emb, chunk))
    vals = [(s, str(v)) for s, v in res if v is not None]
    if vals:
        execute_values(cur, UPD, vals, template="(%s,%s)")
        conn.commit()
        ok += len(vals)
    done += len(chunk)
    print(f"  {done}/{len(slugs)}  (成功 {ok})")
cur.execute("select count(*) from public.skills where embedding is not null")
print(f">>> 完成；embedding 非空总数 = {cur.fetchone()[0]}")
conn.close()
