#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
彻底版回填：body 正文 + embedding 向量 + 完整 zip 传对象存储 → 回填 skills 表。
（元数据已由 import_to_db.py 灌入；本脚本补「内容层」。）

前置 env（项目根 .env 或环境变量）：
  DB_URL                 DB 直连（复用 import_to_db.connect）
  EMBED_API_KEY          embedding 服务 API key
  EMBED_MODEL            embedding 模型（推理接入点 id 或模型名）
  EMBED_API_BASE         embedding 服务 endpoint base（不含则用占位）
  SUPABASE_URL           对象存储 REST host（如 https://<project>.<host>）
  SERVICE_KEY            service_role key（对象存储上传需写权限；anon 不行）
依赖：psycopg2-binary；仅用标准库 urllib 调 embedding/对象存储。

用法：
  python3 enrich_skills.py [--limit N] [--batch 32] [--only slugA,slugB]
                           [--skip-storage] [--skip-embedding] [--skip-body]
  增量（供 sync 调用）：enrich_slugs([...slug...])

注意：先跑 enrich_schema.sql 建列（向量维度对齐 EMBED_MODEL）。
"""
import os, sys, io, json, zipfile, time, urllib.request, importlib.util

SK = os.path.dirname(os.path.abspath(__file__))
ALL = os.path.join(SK, "..", "normalize", "fixtures", "skills", "all-skills")
# embedding 服务 endpoint：通过环境变量配置，代码内不出现真实第三方域名
EMBED_API_BASE = os.environ.get("EMBED_API_BASE", "<embedding-api-base>")
ARK_URL = f"{EMBED_API_BASE}/api/v3/embeddings/multimodal"  # 多模态 embedding endpoint
BUCKET = "skills"
EMBED_MAX_CHARS = 9000   # ~4096 token 上限的字符近似，超长截断再算 embedding

# 复用 import_to_db 的 connect/_env（懒加载，避免重复实现）
_spec = importlib.util.spec_from_file_location("imp", os.path.join(SK, "import_to_db.py"))
imp = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(imp)


def read_body(slug):
    """读 all-skills/<slug>/SKILL.md 去 frontmatter 的正文；找不到返回 None。"""
    p = os.path.join(ALL, slug, "SKILL.md")
    if not os.path.isfile(p):
        return None
    txt = open(p, encoding="utf-8", errors="replace").read()
    if txt.startswith("---"):
        end = txt.find("\n---", 3)
        if end != -1:
            txt = txt[end + 4:]
    return txt.replace("\x00", "").strip()


def make_zip(slug):
    """把 all-skills/<slug>/ 整目录打成内存 zip（含 scripts/references/assets）。"""
    d = os.path.join(ALL, slug)
    if not os.path.isdir(d):
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(d):
            dirs.sort()
            for fn in sorted(files):
                if fn in ("_meta.json", "_legacy_meta.json"):  # 弃用 sidecar，按你数据源实际命名调整
                    continue
                ab = os.path.join(root, fn)
                zf.write(ab, os.path.relpath(ab, os.path.dirname(d)))
    return buf.getvalue()


def _embed_one(text, key, model, retries=3):
    """multimodal 一次融合成 1 向量 → 每条单独请求。返回向量（不预 normalize，靠 cosine 索引）。"""
    payload = json.dumps({"model": model,
                          "input": [{"type": "text", "text": text[:EMBED_MAX_CHARS] or " "}]}).encode()
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(ARK_URL, data=payload, method="POST", headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=60).read())
            return r["data"]["embedding"]
        except Exception as e:
            last = e; time.sleep(1.5 * (i + 1))
    raise last


def embed_batch(texts, workers=20):
    """并发对每条文本单独请求 multimodal embedding，返回等序向量列表。"""
    key = imp._env("EMBED_API_KEY"); model = imp._env("EMBED_MODEL")
    if not key or not model:
        raise RuntimeError("缺 EMBED_API_KEY / EMBED_MODEL")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda t: _embed_one(t, key, model), texts))


def upload_zip(slug, data, retries=3):
    """PUT 到对象存储 bucket/skills/<slug>.zip（service_role key，x-upsert）。返回 storage_path。"""
    url = imp._env("SUPABASE_URL"); key = imp._env("SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("缺 SUPABASE_URL / SERVICE_KEY")
    path = f"{BUCKET}/{slug}.zip"
    req = urllib.request.Request(f"{url}/storage/v1/object/{path}", data=data, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "apikey": key,
                                          "Content-Type": "application/zip", "x-upsert": "true"})
    last = None
    for i in range(retries):
        try:
            urllib.request.urlopen(req, timeout=120).read()
            return path
        except Exception as e:
            last = e; time.sleep(1.5 * (i + 1))
    raise last


def enrich_slugs(slugs, batch=32, do_body=True, do_embed=True, do_storage=True, verbose=False):
    """回填一批 slug 的 body/embedding/storage_path。供全量 CLI 和 sync 增量复用。"""
    import psycopg2
    conn = imp.connect(psycopg2); cur = conn.cursor()
    done = 0
    for i in range(0, len(slugs), batch):
        chunk = slugs[i:i + batch]
        bodies = {s: read_body(s) for s in chunk}
        embs = {}
        if do_embed:
            valid = [s for s in chunk if bodies.get(s)]
            if valid:
                vecs = embed_batch([bodies[s] for s in valid])
                embs = dict(zip(valid, vecs))
        for s in chunk:
            sets, vals = [], []
            if do_body and bodies.get(s) is not None:
                sets.append("body=%s"); vals.append(bodies[s])
            if do_embed and s in embs:
                sets.append("embedding=%s"); vals.append(str(embs[s]))  # pgvector 接受 '[...]' 文本
            if do_storage:
                z = make_zip(s)
                if z:
                    sets.append("storage_path=%s"); vals.append(upload_zip(s, z))
            if sets:
                sets.append("enriched_at=now()")
                vals.append(s)
                cur.execute(f"update public.skills set {','.join(sets)} where slug=%s", vals)
        conn.commit()
        done += len(chunk)
        if verbose:
            print(f"  enriched {done}/{len(slugs)}")
    cur.close(); conn.close()
    return done


def main(argv):
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    batch = int(argv[argv.index("--batch") + 1]) if "--batch" in argv else 32
    only = argv[argv.index("--only") + 1].split(",") if "--only" in argv else None
    flags = dict(do_body="--skip-body" not in argv,
                 do_embed="--skip-embedding" not in argv,
                 do_storage="--skip-storage" not in argv)

    if only:
        slugs = only
    else:
        slugs = sorted(d for d in os.listdir(ALL) if os.path.isdir(os.path.join(ALL, d)))
        if limit:
            slugs = slugs[:limit]
    print(f"回填 {len(slugs)} 个技能；{flags}")
    n = enrich_slugs(slugs, batch=batch, verbose=True, **flags)
    print(f">>> 完成回填 {n} 个")


if __name__ == "__main__":
    main(sys.argv[1:])
