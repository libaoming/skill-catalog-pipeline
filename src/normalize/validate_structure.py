#!/usr/bin/env python3
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
按「skill 安装标准结构」校验，过滤不合规（dry-run，不写库）。

依赖 scan.py 产出的 _scan/manifest.json（拿到每个 skill 的 chosen SKILL.md）。

标准结构（能否被 Claude Code 安装/加载的硬条件）：
  1. metadata.json 存在且可解析，含 slug（平台主键）          —— 缺=过滤
  2. 有且能消歧出唯一 SKILL.md                                —— 无=过滤
  3. SKILL.md 含合法 YAML frontmatter                         —— 无=过滤
  4. frontmatter 有非空 name                                  —— 无=过滤
  5. frontmatter 有非空 description                           —— 无=过滤
软警告（仍可装，但标记待修，不过滤）：
  - name 不匹配 ^[a-z0-9][a-z0-9-]*$（如含中文/大写/空格）
  - len(name) > 64
  - len(description) > 1024
  - name 与目录名不一致
产出：
  _scan/installable.tsv   合规可安装（含 warnings 列）
  _scan/filtered.tsv      被过滤（含原因）
  stdout                  标准结构统计
"""
import os, sys, json, re

SK = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(SK, "..", "fixtures", "skills", "all-skills")
OUT = os.path.join(SK, "..", "fixtures", "skills", "_scan")
ROOT = os.path.abspath(ROOT)
OUT = os.path.abspath(OUT)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

try:
    import yaml
    HAVE_YAML = True
except Exception:
    HAVE_YAML = False


def extract_frontmatter(path):
    """读 SKILL.md 头部，返回 frontmatter 文本块（不含 --- 围栏）或 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(16384)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    # 第一行 --- 之后，到下一行 --- 之间
    rest = head[3:]
    nl = rest.find("\n")
    if nl == -1:
        return None
    rest = rest[nl + 1:]
    end = rest.find("\n---")
    if end == -1:
        return None
    return rest[:end]


def parse_fm(block):
    """返回 dict（至少尝试取 name/description）。yaml 优先，降级正则。"""
    if HAVE_YAML:
        try:
            d = yaml.safe_load(block)
            return d if isinstance(d, dict) else {}
        except Exception:
            pass
    # 降级：逐行取顶层标量 name/description 是否存在
    out = {}
    for line in block.splitlines():
        m = re.match(r"^(name|description)\s*:\s*(.*)$", line)
        if m:
            out.setdefault(m.group(1), m.group(2).strip().strip('"\''))
    return out


def main():
    manifest = json.load(open(os.path.join(OUT, "manifest.json")))
    stats = {
        "total": 0, "installable": 0, "filtered": 0,
        "f_no_metadata": 0, "f_no_skill_md": 0, "f_no_frontmatter": 0,
        "f_no_name": 0, "f_no_description": 0,
        "w_name_invalid": 0, "w_name_too_long": 0,
        "w_desc_too_long": 0, "w_name_dir_mismatch": 0,
    }
    installable, filtered = [], []

    for m in manifest:
        stats["total"] += 1
        d = m["dir"]
        reasons, warns = [], []

        # 标准1 / 2（沿用 scan 的判定）
        if not m["has_metadata"] or not m["metadata_parseable"]:
            reasons.append("no_metadata"); stats["f_no_metadata"] += 1
        if m["skill_md_count"] == 0 or not m["skill_md_chosen"]:
            reasons.append("no_skill_md"); stats["f_no_skill_md"] += 1

        fm = {}
        if m["skill_md_chosen"]:
            block = extract_frontmatter(os.path.join(ROOT, d, m["skill_md_chosen"]))
            if block is None:
                reasons.append("no_frontmatter"); stats["f_no_frontmatter"] += 1
            else:
                fm = parse_fm(block)
                name = (fm.get("name") or "").strip() if fm else ""
                desc = fm.get("description")
                desc = str(desc).strip() if desc is not None else ""
                if not name:
                    reasons.append("no_name"); stats["f_no_name"] += 1
                if not desc:
                    reasons.append("no_description"); stats["f_no_description"] += 1
                # 软警告
                if name:
                    if not NAME_RE.match(name):
                        warns.append("name_invalid"); stats["w_name_invalid"] += 1
                    if len(name) > 64:
                        warns.append("name_too_long"); stats["w_name_too_long"] += 1
                    if name != m["slug"] and name != d:
                        warns.append("name_dir_mismatch"); stats["w_name_dir_mismatch"] += 1
                if len(desc) > 1024:
                    warns.append("desc_too_long"); stats["w_desc_too_long"] += 1

        if m["skill_md_count"] > 1:
            warns.append("multi_skill_md")

        if reasons:
            stats["filtered"] += 1
            filtered.append((m["slug"], d, ",".join(reasons)))
        else:
            stats["installable"] += 1
            installable.append((m["slug"], d, m["skill_md_chosen"], ",".join(warns) or "-"))

    with open(os.path.join(OUT, "installable.tsv"), "w") as f:
        f.write("slug\tdir\tskill_md\twarnings\n")
        for r in installable:
            f.write("\t".join(r) + "\n")
    with open(os.path.join(OUT, "filtered.tsv"), "w") as f:
        f.write("slug\tdir\tfilter_reasons\n")
        for r in filtered:
            f.write("\t".join(r) + "\n")

    print(f"===== 标准结构校验（yaml={'on' if HAVE_YAML else 'fallback'}，dry-run）=====")
    print(f"  总计 skill            {stats['total']}")
    print(f"  ✅ 合规可安装         {stats['installable']}")
    print(f"  ❌ 过滤(不合规)       {stats['filtered']}")
    print("  --- 过滤原因（硬条件，可重叠）---")
    print(f"  无 metadata.json      {stats['f_no_metadata']}")
    print(f"  无 SKILL.md           {stats['f_no_skill_md']}")
    print(f"  无 frontmatter        {stats['f_no_frontmatter']}")
    print(f"  无 name               {stats['f_no_name']}")
    print(f"  无 description        {stats['f_no_description']}")
    print("  --- 软警告（仍可装，标记待修）---")
    print(f"  name 不规范           {stats['w_name_invalid']}")
    print(f"  name 超 64            {stats['w_name_too_long']}")
    print(f"  description 超 1024   {stats['w_desc_too_long']}")
    print(f"  name≠目录名           {stats['w_name_dir_mismatch']}")
    print(f"\n产出: {OUT}/installable.tsv , {OUT}/filtered.tsv")


if __name__ == "__main__":
    main()
