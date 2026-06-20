#!/usr/bin/env python3
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
导入层（产出规范化 NDJSON 中间产物，不直连 DB）。

输入：_scan/installable.tsv（合规 skill，由 validate_structure.py 产出）
对每条：读 all-skills/<dir>/metadata.json（主源）+ 消歧 SKILL.md（正文）→
字段映射 + fallback 链 → 产出：
  _scan/import/skills.ndjson    每行一个 Skill 记录（含 body 全文，供检索）
  _scan/import/versions.ndjson  每行 slug+version（喂 SkillVersion）

只读 metadata.json + SKILL.md（不碰弃用的 sidecar meta，故无 BOM 问题）。
**默认排除 requires_api_key 的技能**，--include-need-key 可纳入。
幂等：无随机/时间依赖，同快照重跑逐字节一致。
"""
import os, sys, json, re

# 默认排除需 key 技能；--include-need-key 可纳入
INCLUDE_NEED_KEY = "--include-need-key" in sys.argv
# --limit N：仅处理前 N 行（调试用）
limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None

SK = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(SK, "..", "fixtures", "skills", "all-skills")
SCAN = os.path.join(SK, "..", "fixtures", "skills", "_scan")
ROOT = os.path.abspath(ROOT)
SCAN = os.path.abspath(SCAN)
OUT = os.path.join(SCAN, "import")
os.makedirs(OUT, exist_ok=True)


def read_json(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def split_md(text):
    """分离 YAML frontmatter 文本块与正文 body。返回 (fm_block|None, body)。"""
    if not text.startswith("---"):
        return None, text.strip()
    rest = text[3:]
    nl = rest.find("\n")
    if nl == -1:
        return None, text.strip()
    rest = rest[nl + 1:]
    end = rest.find("\n---")
    if end == -1:
        return None, text.strip()
    fm = rest[:end]
    after = rest[end + 4:]            # 跳过 "\n---"
    bnl = after.find("\n")
    body = after[bnl + 1:] if bnl != -1 else ""
    return fm, body.strip()


def fm_field(block, key):
    """从 frontmatter 文本块取顶层标量字段（降级正则，无 pyyaml 依赖）。"""
    if not block:
        return ""
    m = re.search(r"(?m)^%s\s*:\s*(.*)$" % key, block)
    if not m:
        return ""
    return m.group(1).strip().strip('"\'')


def main():
    tsv = os.path.join(SCAN, "installable.tsv")
    with open(tsv, encoding="utf-8") as f:
        rows = [ln.rstrip("\n").split("\t") for ln in f][1:]  # 跳表头
    if limit:
        rows = rows[:limit]

    n = 0
    seen = set()
    dup = 0
    skipped = 0
    fout = open(os.path.join(OUT, "skills.ndjson"), "w", encoding="utf-8")
    vout = open(os.path.join(OUT, "versions.ndjson"), "w", encoding="utf-8")
    body_chars = 0

    for row in rows:
        if len(row) < 3:
            continue
        slug_tsv, d, skill_md = row[0], row[1], row[2]
        warnings = row[3] if len(row) > 3 else "-"

        md = read_json(os.path.join(ROOT, d, "metadata.json")) or {}
        slug = md.get("slug") or slug_tsv
        if md.get("requires_api_key") and not INCLUDE_NEED_KEY:
            skipped += 1
            continue
        if slug in seen:
            dup += 1
        seen.add(slug)

        try:
            with open(os.path.join(ROOT, d, skill_md), encoding="utf-8",
                      errors="replace") as sf:
                text = sf.read()
        except OSError:
            text = ""
        fm_block, body = split_md(text)
        fm_name = fm_field(fm_block, "name")
        fm_desc = fm_field(fm_block, "description")
        body_chars += len(body)

        desc_zh = (md.get("description_zh") or "").strip()
        desc_en = (md.get("description") or "").strip()
        description = desc_zh or desc_en or fm_desc or ""
        version = (md.get("version") or "").strip()

        skill = {
            "slug": slug,
            "title": md.get("name") or fm_name or slug,
            "description": description,
            "description_zh": desc_zh,
            "description_en": desc_en,
            "category": md.get("category") or "",
            "tags": md.get("tags") or [],
            "downloads": md.get("downloads") or 0,
            "installs": md.get("installs") or 0,
            "stars": md.get("stars") or 0,
            "version": version,
            "source": md.get("source") or "",
            "author": md.get("author") or "",
            "requires_api_key": bool(md.get("requires_api_key")),
            "icon_url": md.get("icon_url") or "",
            "homepage": md.get("homepage") or "",
            "fm_name": fm_name,
            "fm_description": fm_desc,
            "skill_md_path": skill_md,
            "warnings": "" if warnings == "-" else warnings,
            "body": body,
        }
        fout.write(json.dumps(skill, ensure_ascii=False) + "\n")
        vout.write(json.dumps(
            {"slug": slug, "version": version}, ensure_ascii=False) + "\n")
        n += 1

    fout.close()
    vout.close()
    print("===== 导入产物（NDJSON，未写库）=====")
    print(f"  installable 输入行     {len(rows)}")
    print(f"  跳过需 key             {skipped}")
    print(f"  写出 skill 记录        {n}")
    print(f"  唯一 slug              {len(seen)}")
    print(f"  重复 slug              {dup}")
    print(f"  body 总字符            {body_chars:,}")
    print(f"\n产出: {OUT}/skills.ndjson , {OUT}/versions.ndjson")


if __name__ == "__main__":
    main()
