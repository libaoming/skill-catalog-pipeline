#!/usr/bin/env python3
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
all-skills 批量扫描器（dry-run，只产报告，绝不写库）。

方案1：扫描 → 归一化判定 → 跳过报告。产出：
  _scan/manifest.json     每个 skill 一条决策记录（import|skip|review）
  _scan/skip-report.tsv   仅 decision != import 的，供人工先核
  stdout                  汇总数字（与统计对账）

判定规则：
  metadata.json = 唯一可信主键源（slug）。缺失/不可解析 → skip。
  SKILL.md 计数（递归）：0 → skip(no_skill_md)；>1 → review(multi_skill_md, 带消歧 chosen)；1 → import。
  _meta.json / 数据源附带的 sidecar meta = 补充源，BOM/不可解析只记 flag，不降级决策。
消歧（多 SKILL.md）：优先根 ./SKILL.md，否则按 (depth, 字典序) 取第一个。
"""
import os, sys, json

# 默认扫描相对路径下的 all-skills；也可命令行传入绝对路径
ROOT = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "skills", "all-skills")
ROOT = os.path.abspath(ROOT)
OUT = os.path.join(ROOT, "..", "_scan")
OUT = os.path.abspath(OUT)
os.makedirs(OUT, exist_ok=True)


def read_json(path):
    """返回 (data, parseable, had_bom)。BOM 去除后再 parse。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None, False, False
    had_bom = raw[:3] == b"\xef\xbb\xbf"
    if had_bom:
        raw = raw[3:]
    try:
        return json.loads(raw.decode("utf-8")), True, had_bom
    except Exception:
        return None, False, had_bom


def find_skill_mds(skill_dir):
    """递归列出 skill_dir 下所有 SKILL.md 的相对路径。"""
    found = []
    for dirpath, _dirs, files in os.walk(skill_dir):
        if "SKILL.md" in files:
            rel = os.path.relpath(os.path.join(dirpath, "SKILL.md"), skill_dir)
            found.append(rel)
    return found


def choose_skill_md(rels):
    """消歧：优先根 SKILL.md，否则 (depth, 字典序) 最小。"""
    if "SKILL.md" in rels:
        return "SKILL.md"
    return sorted(rels, key=lambda p: (p.count(os.sep), p))[0]


def main():
    entries = sorted(
        e.name for e in os.scandir(ROOT)
        if e.is_dir() and not e.name.startswith("_scan")
    )
    manifest = []
    counts = {
        "total_dirs": 0,
        "import": 0, "skip": 0, "review": 0,
        "skill_md_0": 0, "skill_md_1": 0, "skill_md_multi": 0,
        "has_meta": 0, "meta_bom": 0, "meta_unparseable": 0,
        "has_extra_meta": 0,
        "no_metadata": 0, "metadata_unparseable": 0,
    }
    slugs_seen = {}
    dup_slugs = []

    for name in entries:
        d = os.path.join(ROOT, name)
        counts["total_dirs"] += 1
        flags = []

        meta_path = os.path.join(d, "metadata.json")
        md, md_ok, _ = read_json(meta_path)
        has_metadata = os.path.exists(meta_path)
        slug = (md or {}).get("slug") or name
        if not has_metadata:
            counts["no_metadata"] += 1
            flags.append("no_metadata")
        elif not md_ok:
            counts["metadata_unparseable"] += 1
            flags.append("metadata_unparseable")

        # slug 唯一性核查
        if slug in slugs_seen:
            dup_slugs.append((slug, name, slugs_seen[slug]))
            flags.append("dup_slug")
        else:
            slugs_seen[slug] = name

        # SKILL.md 计数 + 消歧
        skill_mds = find_skill_mds(d)
        n = len(skill_mds)
        chosen = choose_skill_md(skill_mds) if n else None
        if n == 0:
            counts["skill_md_0"] += 1
        elif n == 1:
            counts["skill_md_1"] += 1
        else:
            counts["skill_md_multi"] += 1

        # 补充源
        sm_path = os.path.join(d, "_meta.json")
        has_meta = os.path.exists(sm_path)
        meta_bom = meta_unparse = False
        if has_meta:
            counts["has_meta"] += 1
            _sm, sm_ok, meta_bom = read_json(sm_path)
            if meta_bom:
                counts["meta_bom"] += 1
                flags.append("meta_bom")
            if not sm_ok:
                meta_unparse = True
                counts["meta_unparseable"] += 1
                flags.append("meta_unparseable")
        has_extra = os.path.exists(os.path.join(d, "_legacy_meta.json"))  # 弃用 sidecar，按你数据源实际命名调整
        if has_extra:
            counts["has_extra_meta"] += 1

        # 决策
        if not has_metadata:
            decision = "skip"; flags.append("skip:no_metadata")
        elif not md_ok:
            decision = "skip"; flags.append("skip:metadata_unparseable")
        elif n == 0:
            decision = "skip"; flags.append("skip:no_skill_md")
        elif n > 1:
            decision = "review"; flags.append("review:multi_skill_md")
        else:
            decision = "import"
        counts[decision] += 1

        manifest.append({
            "slug": slug, "dir": name, "decision": decision,
            "skill_md_count": n, "skill_md_chosen": chosen,
            "has_metadata": has_metadata, "metadata_parseable": md_ok,
            "has_meta": has_meta, "meta_bom": meta_bom,
            "meta_unparseable": meta_unparse,
            "has_extra_meta": has_extra,
            "flags": flags,
        })

    # 写 manifest
    with open(os.path.join(OUT, "manifest.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=0)

    # 写 skip-report.tsv
    with open(os.path.join(OUT, "skip-report.tsv"), "w") as f:
        f.write("decision\tslug\tdir\tskill_md_count\tflags\n")
        for m in manifest:
            if m["decision"] != "import":
                f.write("\t".join([
                    m["decision"], m["slug"], m["dir"],
                    str(m["skill_md_count"]), ",".join(m["flags"]),
                ]) + "\n")

    # 汇总
    print("===== 扫描汇总（dry-run，未写库） =====")
    for k in ["total_dirs", "import", "skip", "review"]:
        print(f"  {k:22} {counts[k]}")
    print("  --- SKILL.md 计数对账 ---")
    print(f"  skill_md_1(唯一)        {counts['skill_md_1']}")
    print(f"  skill_md_0(无)          {counts['skill_md_0']}")
    print(f"  skill_md_multi(多个)    {counts['skill_md_multi']}")
    print("  --- 补充源 ---")
    print(f"  has_meta(_meta.json)    {counts['has_meta']}")
    print(f"  meta_bom(BOM 阻断)      {counts['meta_bom']}")
    print(f"  meta_unparseable        {counts['meta_unparseable']}")
    print(f"  has_extra_meta          {counts['has_extra_meta']}")
    print("  --- 主键源异常 ---")
    print(f"  no_metadata             {counts['no_metadata']}")
    print(f"  metadata_unparseable    {counts['metadata_unparseable']}")
    print(f"  dup_slug                {len(dup_slugs)}")
    if dup_slugs[:5]:
        print("    示例:", dup_slugs[:5])
    print(f"\n产出: {OUT}/manifest.json , {OUT}/skip-report.tsv")


if __name__ == "__main__":
    main()
