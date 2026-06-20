#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
语义检索验证：query -> multimodal embedding -> pgvector cosine 最近邻。
复用 storage/enrich_skills._embed_one（同一融合口径，保证 query 向量与库内 body 向量同空间）。

前置 env（项目根 .env 或环境变量）：DB_URL、EMBED_API_KEY、EMBED_MODEL
跑（需带 psycopg2 的 venv）：
  python -u search.py "帮我打电话做电话销售" [topk]
不带 query 则跑一组内置中文场景查询做冒烟验证。
"""
import os, sys, importlib.util as u

SKDIR = os.path.dirname(os.path.abspath(__file__))
# enrich_skills 在 storage 层
ENRICH = os.path.join(SKDIR, "..", "storage", "enrich_skills.py")
se = u.spec_from_file_location("e", os.path.abspath(ENRICH))
e = u.module_from_spec(se); se.loader.exec_module(e)
key = e.imp._env("EMBED_API_KEY"); model = e.imp._env("EMBED_MODEL")
assert key and model, "缺 EMBED_API_KEY / EMBED_MODEL"

import psycopg2
conn = e.imp.connect(psycopg2); cur = conn.cursor()

SQL = ("select slug, coalesce(display_name, slug), category, downloads, "
       "1 - (embedding <=> %s::vector) as sim "
       "from public.skills where embedding is not null "
       "order by embedding <=> %s::vector limit %s")


def search(query, k=8):
    qvec = str(e._embed_one(query, key, model))
    cur.execute(SQL, (qvec, qvec, k))
    rows = cur.fetchall()
    print(f"\n🔎 query: {query!r}  (top {k})")
    for slug, name, cat, dl, sim in rows:
        print(f"  {sim:.3f}  {slug:<40} [{cat or '-'}] dl={dl}  {name}")
    return rows


if __name__ == "__main__":
    if len(sys.argv) > 1:
        q = sys.argv[1]
        k = int(sys.argv[2]) if len(sys.argv) > 2 else 8
        search(q, k)
    else:
        for q in ["帮我给客户打电话做电话销售跟进",
                  "把会议录音转成文字纪要",
                  "分析一批用户反馈提炼主题",
                  "写一份社交媒体种草文案",
                  "查一家公司的工商信息和风险"]:
            search(q, 6)
    conn.close()
