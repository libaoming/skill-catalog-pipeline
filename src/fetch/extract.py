#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
extract.py — 把 full-bodies/*.zip 解开成可直接用的文件夹结构。

输入:
  fixtures/skills/full-bodies/{slug}.zip   (正文包)
  fixtures/skills/_full_raw.jsonl          (元数据,事实源)
输出:
  fixtures/skills/all-skills/{slug}/
      SKILL.md + metadata.json + (references/ scripts/ assets/ 等原有内容)
  fixtures/skills/all-skills/_index.json   (全部技能精简索引,导库用)
幂等:已解压且有 metadata.json 的跳过。
"""
import os, json, zipfile, urllib.parse, time

SRC_ZIPS = "fixtures/skills/full-bodies"
SRC_META = "fixtures/skills/_full_raw.jsonl"
OUT = "fixtures/skills/all-skills"

t0 = time.time()

# 1) 读元数据 slug -> 精简 meta
meta = {}
for line in open(SRC_META):
    line = line.strip()
    if not line:
        continue
    d = json.loads(line)
    slug = d.get("slug")
    if not slug:
        continue
    meta[slug] = {
        "slug": slug,
        "name": d.get("name") or slug,
        "description_zh": d.get("description_zh") or "",
        "description": d.get("description") or "",
        "category": d.get("category") or "",
        "tags": d.get("tags") or [],
        "downloads": d.get("downloads") or 0,
        "installs": d.get("installs") or 0,
        "stars": d.get("stars") or 0,
        "version": d.get("version") or "",
        "source": d.get("source") or "",
        "author": d.get("ownerName") or "",
        "requires_api_key": (d.get("labels") or {}).get("requires_api_key") in (True, "true"),
        "icon_url": d.get("iconUrl") or "",
        "homepage": d.get("homepage") or "",
    }
print(f"元数据 {len(meta)} 条", flush=True)

os.makedirs(OUT, exist_ok=True)

def safe_extract(zf, dest):
    """防 zip-slip:逐成员校验路径后解压。"""
    for m in zf.infolist():
        p = os.path.realpath(os.path.join(dest, m.filename))
        if not p.startswith(os.path.realpath(dest) + os.sep) and p != os.path.realpath(dest):
            continue
        zf.extract(m, dest)

zips = sorted(f for f in os.listdir(SRC_ZIPS) if f.endswith(".zip"))
total = len(zips)
ok = skip = bad = 0
index = []

for i, fn in enumerate(zips, 1):
    base = fn[:-4]                       # quoted slug
    slug = urllib.parse.unquote(base)
    dest = os.path.join(OUT, base)
    mfile = os.path.join(dest, "metadata.json")
    m = meta.get(slug) or {"slug": slug, "name": slug}
    if os.path.exists(mfile):
        skip += 1
    else:
        try:
            with zipfile.ZipFile(os.path.join(SRC_ZIPS, fn)) as zf:
                os.makedirs(dest, exist_ok=True)
                safe_extract(zf, dest)
            with open(mfile, "w") as f:
                json.dump(m, f, ensure_ascii=False, indent=1)
            ok += 1
        except Exception as e:
            bad += 1
            with open(os.path.join(OUT, "_extract_failed.tsv"), "a") as f:
                f.write(f"{slug}\t{type(e).__name__}\n")
            continue
    index.append({
        "slug": slug, "dir": base,
        "name": m.get("name", slug),
        "desc": (m.get("description_zh") or m.get("description") or "")[:120],
        "category": m.get("category", ""),
        "downloads": m.get("downloads", 0),
        "requires_api_key": m.get("requires_api_key", False),
        "icon_url": m.get("icon_url", ""),
    })
    if i % 5000 == 0 or i == total:
        print(f"  {i}/{total}  解压={ok} 跳过={skip} 失败={bad}  {time.time()-t0:.0f}s", flush=True)

index.sort(key=lambda x: -x["downloads"])
with open(os.path.join(OUT, "_index.json"), "w") as f:
    json.dump({"total": len(index), "skills": index}, f, ensure_ascii=False, separators=(",", ":"))

# 终验:有 SKILL.md 的文件夹数
have_body = 0
for e in os.scandir(OUT):
    if e.is_dir() and os.path.isfile(os.path.join(e.path, "SKILL.md")):
        have_body += 1
print(f"完成: 解压={ok} 跳过={skip} 失败={bad} / {total}", flush=True)
print(f"终验: 文件夹含 SKILL.md = {have_body}; _index.json = {len(index)} 条; 用时 {(time.time()-t0)/60:.1f}min", flush=True)
