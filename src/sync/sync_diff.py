#!/usr/bin/env python3
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
sync_diff.py — 与数据源增量同步（方案 B）+ 全量对齐 plan。

子命令：
  diff <old_catalog.jsonl> <new_catalog.jsonl>
      按 slug 主键 diff，分四态 → 写 _scan/sync-plan.json + _scan/to_fetch.txt
        new        slug 新增且有包 → 下载入库
        updated    updated_at 或 version 变 → 重下 + 追加 SkillVersion
        removed    slug 消失 → 软删 is_active=false（不物理删）
        stats_only 仅 downloads/stars 变 → 只更新统计列（免下载）
  plan <catalog.jsonl> [all_skills_dir]
      对比本地 all-skills 已有目录，输出 catalog 里「有包但本地缺」的 slug → _scan/to_fetch.txt
      （拉需 key / 补全 / 全量对齐都用它）
  selftest
      内置 mock 断言四态分类正确（无需网络/数据）

变更信号 = version（来自 _full_raw.jsonl 抓取字段）。
  ⚠️ updated_at 不可用作变更信号：实测它跟随 downloads 每次同步都刷新（全量条目全变），
     区分不了「内容更新」vs「仅下载量涨」。只认 version 变 = updated。
本脚本只做 diff/plan；下载用 fetch/fetch_bodies.py，解压用 fetch/extract.py。
幂等、无随机/时间依赖，可重入。
"""
import os, sys, json

SK = os.path.dirname(os.path.abspath(__file__))
SCAN = os.path.abspath(os.path.join(SK, "..", "normalize", "fixtures", "skills", "_scan"))
_PKG = (True, "true", 302, "302")


def load_catalog(path):
    d = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        s = r.get("slug")
        if s:
            d[s] = r
    return d


def diff_catalogs(old, new):
    olds, news = set(old), set(new)
    new_slugs = sorted(news - olds)
    removed = sorted(olds - news)
    updated, stats_only, unchanged = [], [], []
    for s in sorted(olds & news):
        o, n = old[s], new[s]
        # 变更信号只认 version：updated_at 跟随 downloads 每次同步都刷新（实测全量条目全变），
        # 无法区分「内容更新」vs「仅下载量涨」，故不用作 updated 信号。
        if n.get("version") != o.get("version"):
            updated.append(s)
        elif n.get("downloads") != o.get("downloads") or n.get("stars") != o.get("stars"):
            stats_only.append(s)
        else:
            unchanged.append(s)
    return {
        "new": [s for s in new_slugs if new[s].get("has_package") in _PKG],
        "new_no_package": [s for s in new_slugs if new[s].get("has_package") not in _PKG],
        "updated": updated,
        "removed": removed,
        "stats_only": stats_only,
        "unchanged_count": len(unchanged),
    }


def cmd_diff(oldp, newp):
    old, new = load_catalog(oldp), load_catalog(newp)
    plan = diff_catalogs(old, new)
    os.makedirs(SCAN, exist_ok=True)
    out = os.path.join(SCAN, "sync-plan.json")
    json.dump(plan, open(out, "w"), ensure_ascii=False, indent=1)
    fetch = plan["new"] + plan["updated"]
    open(os.path.join(SCAN, "to_fetch.txt"), "w").write("\n".join(fetch) + "\n")
    print("=== sync diff（方案 B）===")
    print(f"  新增(有包)   {len(plan['new']):>6}  → 下载入库")
    print(f"  新增(无包)   {len(plan['new_no_package']):>6}  → 跳过")
    print(f"  更新         {len(plan['updated']):>6}  → 重下 + 追加版本")
    print(f"  下架         {len(plan['removed']):>6}  → 软删 is_active=false")
    print(f"  仅统计变     {len(plan['stats_only']):>6}  → 只更新 downloads/stars")
    print(f"  未变         {plan['unchanged_count']:>6}")
    print(f"  待下载(new+updated) {len(fetch)} → {SCAN}/to_fetch.txt")
    print(f"  完整计划(含下架清单) → {out}")
    print("  下一步: 把 to_fetch.txt 喂下载器 → extract → 重跑 scan/validate/import/package")


def cmd_plan(catalogp, all_dir=None):
    all_dir = all_dir or os.path.abspath(os.path.join(
        SK, "..", "normalize", "fixtures", "skills", "all-skills"))
    cat = load_catalog(catalogp)
    have = set(os.listdir(all_dir)) if os.path.isdir(all_dir) else set()
    have_pkg = [s for s, r in cat.items() if r.get("has_package") in _PKG]
    missing = sorted(s for s in have_pkg if s not in have)
    os.makedirs(SCAN, exist_ok=True)
    out = os.path.join(SCAN, "to_fetch.txt")
    open(out, "w").write("\n".join(missing) + "\n")
    print("=== plan 全量对齐 ===")
    print(f"  catalog 有包技能   {len(have_pkg)}")
    print(f"  本地已有目录       {len(have)}")
    print(f"  待下载(缺失)       {len(missing)} → {out}")


def cmd_selftest():
    base = lambda **kw: {"updated_at": 1, "version": "1.0", "downloads": 10, "stars": 1, "has_package": True, **kw}
    old = {s: dict(slug=s, **base()) for s in ("a", "b", "c", "e")}
    new = {
        # 仅统计变：downloads 变 + updated_at 也变（模拟真实，updated_at 跟随 downloads 刷新）
        # → 必须判 stats_only，不能因 updated_at 变误判 updated（回归本次真跑暴露的 bug）
        "a": dict(slug="a", **base(downloads=99, updated_at=2)),
        "b": dict(slug="b", **base(updated_at=2, version="1.1")),  # 更新（version 变）
        # c 消失 → removed
        "d": dict(slug="d", **base(downloads=5)),              # 新增有包
        "f": dict(slug="f", **base(downloads=5, has_package=False)),  # 新增无包
        "e": dict(slug="e", **base()),                         # 未变
    }
    p = diff_catalogs(old, new)
    assert p["new"] == ["d"], p
    assert p["new_no_package"] == ["f"], p
    assert p["updated"] == ["b"], p
    assert p["removed"] == ["c"], p
    assert p["stats_only"] == ["a"], p
    assert p["unchanged_count"] == 1, p
    print("selftest PASS — 四态分类正确：new=d / no_pkg=f / updated=b / removed=c / stats=a / unchanged=1")


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__); return
    cmd = a[0]
    if cmd == "diff" and len(a) >= 3:
        cmd_diff(a[1], a[2])
    elif cmd == "plan" and len(a) >= 2:
        cmd_plan(a[1], a[2] if len(a) > 2 else None)
    elif cmd == "selftest":
        cmd_selftest()
    else:
        print("用法: diff <old.jsonl> <new.jsonl> | plan <catalog.jsonl> [all_dir] | selftest")


if __name__ == "__main__":
    main()
