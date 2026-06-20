#!/usr/bin/env python3
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
按「每技能一个独立包」分发结构打包（zip）。

输入：_scan/installable.tsv（合规技能，列: slug<TAB>dir<TAB>skill_md<TAB>warnings）
产出到 handoff/skills-packages/：
  catalog.ndjson       ★ 全量 metadata 索引：每行一个技能的 metadata.json + 三字段口径 + 包指针
  packages/<dir>.zip   ★ 每个技能一个独立 zip 包，解压得 <dir>/ 完整目录(可独立安装使用)

「三字段各司其职」口径（对治研发 name==slug 误报）:
  - slug          平台主键 / 去重 / 同步 diff（数据源全局唯一）
  - display_name  展示给用户（= metadata.name；空或 == slug 时按 description_zh 首句 → slug 兜底）
  - _skill_name   装 Claude Code 当 skill 用的标识符（取自 SKILL.md frontmatter name，与 slug 本就不同，不校验相等）

每个独立包内容 = 该技能原始目录（metadata.json + SKILL.md + scripts/ + references/ + assets/），
剔除弃用的 sidecar meta（_meta.json / _legacy_meta.json，按你数据源实际命名调整）。
**默认排除 requires_api_key 的技能**，--include-need-key 可纳入。
--only-need-key 反向：**仅打需 key 的技能**（与默认包互补）。
--out DIR 指定输出目录，避免覆盖既有包。
幂等：固定 zip 内时间戳/权限 + 排序遍历，同输入重跑字节级一致；全量重打前清空 packages/。
用法：python3 package_skills.py [--limit N] [--include-need-key | --only-need-key] [--out DIR]
"""
import os, sys, json, re, zipfile, shutil

SK = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(SK, "..", "fixtures", "skills", "all-skills"))
OUT = os.path.abspath(os.path.join(
    SK, "..", "fixtures", "skills", "handoff", "skills-packages"))
DROP = {"_meta.json", "_legacy_meta.json"}  # 弃用 sidecar，按你数据源实际命名调整
FIXED_DT = (1980, 1, 1, 0, 0, 0)   # zip 最小合法时间，固定以保幂等

limit = None
if "--limit" in sys.argv:
    limit = int(sys.argv[sys.argv.index("--limit") + 1])
# --out DIR：覆盖输出目录（绝对或相对当前工作目录均可）
if "--out" in sys.argv:
    OUT = os.path.abspath(sys.argv[sys.argv.index("--out") + 1])
PKG = os.path.join(OUT, "packages")
# 需 key 三态：默认排除；--include-need-key 全纳入；--only-need-key 仅需 key（互斥，only 优先）
ONLY_NEED_KEY = "--only-need-key" in sys.argv
INCLUDE_NEED_KEY = "--include-need-key" in sys.argv or ONLY_NEED_KEY

try:
    import yaml
    HAVE_YAML = True
except Exception:
    HAVE_YAML = False


def read_metadata(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def extract_frontmatter(path):
    """读 SKILL.md 头部，返回 frontmatter 文本块（不含 --- 围栏）或 None。复用 validate_structure 逻辑。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(16384)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
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
    if HAVE_YAML:
        try:
            d = yaml.safe_load(block)
            return d if isinstance(d, dict) else {}
        except Exception:
            pass
    out = {}
    for line in block.splitlines():
        m = re.match(r"^(name|description)\s*:\s*(.*)$", line)
        if m:
            out.setdefault(m.group(1), m.group(2).strip().strip('"\''))
    return out


def skill_md_name(src, skill_md):
    """从该技能 chosen SKILL.md 提取 frontmatter name（装载标识符）。"""
    if not skill_md or skill_md == "-":
        return ""
    p = os.path.join(src, skill_md)
    if not os.path.isfile(p):
        return ""
    block = extract_frontmatter(p)
    if not block:
        return ""
    return str((parse_fm(block).get("name") or "")).strip()


def first_sentence(s):
    s = (s or "").strip()
    for sep in ("。", "\n", "！", "!", ". "):
        i = s.find(sep)
        if i > 0:
            return s[:i].strip()
    return s[:80].strip()


def display_name_of(md, slug):
    """展示名口径：metadata.name 优先；空或 == slug 时按 description_zh 首句 → slug 兜底。"""
    name = (md.get("name") or "").strip()
    if name and name != slug:
        return name
    dz = first_sentence(md.get("description_zh") or md.get("description"))
    return dz or slug


def write_zip(zip_path, src, d):
    """把 src 目录打成 zip，顶层为 <d>/，剔除 DROP，固定时间戳/权限保幂等。"""
    entries = []
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for fn in sorted(files):
            if fn in DROP:
                continue
            abs_p = os.path.join(root, fn)
            arcname = os.path.relpath(abs_p, os.path.dirname(src))  # 顶层 = d/
            entries.append((arcname, abs_p))
    entries.sort()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, abs_p in entries:
            zi = zipfile.ZipInfo(arcname, date_time=FIXED_DT)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            with open(abs_p, "rb") as fp:
                zf.writestr(zi, fp.read())


def main():
    # 全量重打前清空 packages/，保证与 catalog 严格一致、无旧格式/需 key 残留
    if not limit and os.path.isdir(PKG):
        shutil.rmtree(PKG)
    os.makedirs(PKG, exist_ok=True)
    rows = [l.split("\t") for l in
            open(os.path.join(SK, "..", "fixtures", "skills", "_scan", "installable.tsv"),
                 encoding="utf-8").read().splitlines()[1:]]
    if limit:
        rows = rows[:limit]

    cat = open(os.path.join(OUT, "catalog.ndjson"), "w", encoding="utf-8")
    n = 0
    n_scripts = 0
    n_name_fallback = 0
    n_skill_name = 0
    n_skip_key = 0
    for row in rows:
        if len(row) < 3:
            continue
        slug, d, skill_md = row[0], row[1], row[2]
        src = os.path.join(ROOT, d)
        if not os.path.isdir(src):
            continue

        md = read_metadata(os.path.join(src, "metadata.json"))
        need_key = bool(md.get("requires_api_key"))
        if ONLY_NEED_KEY:
            if not need_key:        # 仅需 key 模式：跳过不需 key 的
                n_skip_key += 1
                continue
        elif need_key and not INCLUDE_NEED_KEY:
            n_skip_key += 1
            continue

        write_zip(os.path.join(PKG, d + ".zip"), src, d)

        has_scripts = os.path.isdir(os.path.join(src, "scripts"))
        if has_scripts:
            n_scripts += 1

        disp = display_name_of(md, slug)
        if disp != (md.get("name") or "").strip():
            n_name_fallback += 1
        sname = skill_md_name(src, skill_md)
        if sname:
            n_skill_name += 1

        md["display_name"] = disp        # 展示名（已兜底）
        md["_skill_name"] = sname        # SKILL.md frontmatter name（装载标识）
        md["_package"] = f"packages/{d}.zip"
        md["_dir"] = d
        md["_skill_md"] = skill_md
        md["_has_scripts"] = has_scripts
        cat.write(json.dumps(md, ensure_ascii=False) + "\n")
        n += 1
        if n % 5000 == 0:
            print(f"  ...{n} packed")

    cat.close()
    mode = ("仅需 key" if ONLY_NEED_KEY else
            "含需 key" if INCLUDE_NEED_KEY else "不含需 key")
    print(f"===== 打包完成（zip · {mode}） =====")
    print(f"  独立技能包    {n}  → {PKG}/<dir>.zip")
    skip_label = "跳过不需 key" if ONLY_NEED_KEY else "跳过需 key  "
    print(f"  {skip_label}   {n_skip_key}")
    print(f"  其中脚本类    {n_scripts}")
    print(f"  display_name 走兜底 {n_name_fallback}（metadata.name 空或==slug）")
    print(f"  _skill_name 提取到  {n_skill_name}")
    print(f"  全量 metadata 索引 catalog.ndjson  ({n} 行)")
    print(f"  输出目录      {OUT}")


if __name__ == "__main__":
    main()
