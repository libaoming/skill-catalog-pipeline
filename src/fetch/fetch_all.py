#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
技能市场全量抓取 + 分布统计 + 「能直接用」交付包。

源：某第三方公开技能市场服务（公开无鉴权）。host 通过环境变量
SKILL_MARKET_API_BASE 配置，本仓库不含真实域名，也不含任何真实数据。
本脚本是全量扫描（数万条规模），输出分布统计 + 可直接用的交付清单。

三阶段（可分开跑、幂等、断点续抓）：
  list   抓全量列表元数据 → fixtures/skills/_full_raw.jsonl   + 出分布统计
  probe  对每条探测 download 是否有包(302=有 / 404=无) → 写回 _full_raw.jsonl 的 has_package
  build  按「能直接用」(不需key + 有包) 筛选 → fixtures/skills/full-handoff.json

用法：
  python3 fetch_all.py list
  python3 fetch_all.py probe          # 全量探测
  python3 fetch_all.py probe --free    # 仅探测不需key的（省请求）
  python3 fetch_all.py build [--min-downloads N]
  python3 fetch_all.py stats           # 仅根据已抓 jsonl 重算分布

API 备忘（实测）：
  列表  GET /api/skills?page=&pageSize=100   单条含 category/description_zh/labels.requires_api_key/downloads/source/slug...
  下载  GET /api/v1/download?slug={slug} → 302→对象存储 zip(有包) / 404(无包)
"""
import json, os, sys, time, urllib.parse, urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SK = os.path.join(ROOT, "fixtures", "skills")
RAW = os.path.join(SK, "_full_raw.jsonl")
STATS = os.path.join(SK, "_full_stats.json")
OUT = os.path.join(SK, "full-handoff.json")
# 数据源 host：通过环境变量配置，代码内不出现真实第三方域名
API = os.environ.get("SKILL_MARKET_API_BASE", "https://<skill-market-api>")
UA = {"User-Agent": "curl/8"}
PAGE_SIZE = 100


# ── 不跟随重定向的 opener：302=有包 / 404=无包，不下载对象存储 zip body ──
class _NoRedirect(urllib.request.HTTPErrorProcessor):
    def http_response(self, req, resp):
        return resp
    https_response = http_response


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _get(url, timeout=30, retries=4):
    last = None
    for i in range(retries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()
        except Exception as e:
            last = e; time.sleep(0.8 * (i + 1))
    raise last


def http_json(url):
    return json.loads(_get(url, timeout=25))


def probe_status(slug, timeout=15, retries=2):
    """只看 download 端点首响应状态码，不跟随 302、不下载 zip。返回 int 状态码或 -1。"""
    url = f"{API}/api/v1/download?slug={urllib.parse.quote(slug)}"
    last = -1
    for i in range(retries):
        try:
            r = _NO_REDIRECT_OPENER.open(urllib.request.Request(url, headers=UA), timeout=timeout)
            code = r.status
            r.close()
            return code
        except Exception:
            last = -1; time.sleep(0.5 * (i + 1))
    return last


# ── 阶段 1：全量列表抓取 ──
def fetch_list(out=None):
    target = out or RAW   # --out 时写到独立路径，不覆盖既有 _full_raw.jsonl（保护 diff 旧基准）
    first = http_json(f"{API}/api/skills?page=1&pageSize={PAGE_SIZE}")["data"]
    total = first.get("total") or 0
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"total={total}  pages={pages}  pageSize={PAGE_SIZE}")

    def grab(page):
        try:
            d = http_json(f"{API}/api/skills?page={page}&pageSize={PAGE_SIZE}")["data"]
            return page, (d.get("skills") or []), None
        except Exception as e:
            return page, [], str(e)

    seen, rows = set(), []

    def run_pages(page_list):
        """跑一批页，收集本批失败页（网络异常）。"""
        failed, done = [], 0
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(grab, p) for p in page_list]
            for f in as_completed(futs):
                page, skills, err = f.result()
                if err:                       # 仅网络异常算失败（空页不重试，靠完整性兜底）
                    failed.append(page); continue
                for s in skills:
                    sl = s.get("slug")
                    if sl and sl not in seen:
                        seen.add(sl); rows.append(s)
                done += 1
                if done % 50 == 0:
                    print(f"  ...{done}/{len(page_list)} 页, 累计 {len(rows)} 条")
        return failed

    all_pages = list(range(1, pages + 1))
    failed = run_pages(all_pages)
    for attempt in range(3):                  # 失败页重试最多 3 轮（防网络丢页）
        if not failed:
            break
        print(f"  重试失败页 {len(failed)} 个（第 {attempt + 1} 轮）")
        time.sleep(2)
        failed = run_pages(failed)

    # 并集补抓：数据源分页本身不稳定（单轮波动 ±1000+），整轮重抓取 slug 并集逼近全集
    # （seen 去重，只补漏掉的 slug）；最多 3 轮，达 99.5% 即停。防误报巨量「下架」。
    for extra in range(3):
        if total and len(rows) >= total * 0.995:
            break
        print(f"  并集补抓第 {extra + 2} 轮（当前 {len(rows)}/{total}）")
        time.sleep(2)
        run_pages(all_pages)

    # 完整性校验：抓到的去重数须接近 API 报告 total（容忍 2%），否则中止不写残缺快照
    if total and len(rows) < total * 0.98:
        raise SystemExit(
            f"!! 抓取不完整: 去重后 {len(rows)} < total {total} 的 98%"
            f"（残留失败页 {failed[:20]}{'...' if len(failed) > 20 else ''}），中止不写残缺快照")

    rows.sort(key=lambda r: -(r.get("downloads") or 0))
    with open(target, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f">>> 写 {target}  共 {len(rows)} 条 (去重后)")
    compute_stats(rows)


def load_raw():
    if not os.path.exists(RAW):
        sys.exit(f"!! 未找到 {RAW}，先跑 list")
    rows = []
    with open(RAW, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _needs_key(r):
    return str((r.get("labels") or {}).get("requires_api_key")).lower() == "true"


# ── 分布统计 ──
def compute_stats(rows=None):
    if rows is None:
        rows = load_raw()
    n = len(rows)
    by_cat = Counter(r.get("category") or "(空)" for r in rows)
    by_src = Counter(r.get("source") or "(空)" for r in rows)
    need_key = sum(1 for r in rows if _needs_key(r))
    verified = sum(1 for r in rows if r.get("verified"))
    has_zh = sum(1 for r in rows if (r.get("description_zh") or "").strip())
    # 下载量分段
    dl_buckets = Counter()
    for r in rows:
        d = r.get("downloads") or 0
        if d == 0: dl_buckets["0"] += 1
        elif d < 100: dl_buckets["1-99"] += 1
        elif d < 1000: dl_buckets["100-999"] += 1
        elif d < 10000: dl_buckets["1k-9.9k"] += 1
        else: dl_buckets["10k+"] += 1
    # 探测结果（若已 probe）
    probed = [r for r in rows if "has_package" in r]
    has_pkg = sum(1 for r in probed if r.get("has_package"))
    no_pkg = sum(1 for r in probed if r.get("has_package") is False)

    stats = {
        "total": n,
        "requires_api_key": {"true": need_key, "false_or_none": n - need_key},
        "verified": verified,
        "has_description_zh": has_zh,
        "by_category": dict(by_cat.most_common()),
        "by_source": dict(by_src.most_common()),
        "downloads_buckets": dict(dl_buckets),
        "probe": {"probed": len(probed), "has_package": has_pkg, "no_package(404)": no_pkg} if probed else "未探测，跑 probe",
    }
    with open(STATS, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=1)
    print(f"\n===== 分布统计 =====")
    print(f"总数: {n}")
    print(f"需 API key: {need_key}  |  不需(可直连): {n-need_key}")
    print(f"verified: {verified}  |  有中文描述: {has_zh}")
    print(f"下载量分段: {dict(dl_buckets)}")
    print(f"分类 Top15:")
    for c, k in by_cat.most_common(15):
        print(f"   {c:24s} {k}")
    print(f"来源 source: {dict(by_src.most_common())}")
    if probed:
        print(f"探测: 已探 {len(probed)}  有包 {has_pkg}  无包404 {no_pkg}")
    print(f">>> {STATS}")


# ── 阶段 2：探测有包 vs 404 ──
def probe(only_free=False):
    rows = load_raw()
    targets = [r for r in rows if (not only_free or not _needs_key(r)) and "has_package" not in r]
    print(f"探测目标 {len(targets)} 条 ({'仅不需key' if only_free else '全量'}), 已探 {len(rows)-len(targets)}")
    idx = {r["slug"]: r for r in rows}
    done = 0
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(probe_status, r["slug"]): r["slug"] for r in targets}
        for f in as_completed(futs):
            slug = futs[f]
            code = f.result()
            idx[slug]["has_package"] = (code == 302 or code == 200)
            idx[slug]["_probe_code"] = code
            done += 1
            if done % 500 == 0:
                print(f"  ...{done}/{len(targets)} 探测中")
    with open(RAW, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f">>> 回写 {RAW}")
    compute_stats(rows)


# ── skillsets：官方策展场景包（每包数条精选）→ slug 标注 ──
def fetch_skillsets():
    cache = os.path.join(SK, "_skillsets.json")
    try:
        sets = http_json(f"{API}/api/v1/skillsets")["skillSets"]
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(sets, fh, ensure_ascii=False, indent=1)
    except Exception:
        sets = json.load(open(cache, encoding="utf-8")) if os.path.exists(cache) else []
    return sets


def _slim(r, slug2set):
    slug = r["slug"]
    return {
        "slug": slug,
        "name": r.get("name"),
        "category": r.get("category") or "other",
        "featured_in_skillset": slug2set.get(slug),
        "description_zh": (r.get("description_zh") or r.get("description") or "")[:500],
        "downloads": r.get("downloads"),
        "installs": r.get("installs"),
        "stars": r.get("stars"),
        "source": r.get("source"),
        "version": r.get("version"),
        "homepage": r.get("homepage"),
        "iconUrl": r.get("iconUrl"),
        "requires_api_key": False,
        "install_cmd": f"skill install {slug}",
        "download_url": f"{API}/api/v1/download?slug={slug}",
    }


# ── 阶段 3：产出「能直接用」交付（分层：全量镜像 jsonl + 精选 json）──
def build(min_downloads=1000):
    rows = load_raw()
    sets = fetch_skillsets()
    slug2set = {}
    for s in sets:
        for sl in (s.get("skillSlugs") or []):
            slug2set.setdefault(sl, s.get("displayName"))
    probed = any("has_package" in r for r in rows)

    def usable(r):
        if _needs_key(r):
            return False
        if "has_package" in r and not r.get("has_package"):
            return False
        return True

    base = [r for r in rows if usable(r)]
    base.sort(key=lambda r: -(r.get("downloads") or 0))

    # ① 全量镜像 jsonl（不需key + 有包，不限下载量；导库自筛）
    CAT = os.path.join(SK, "full-catalog.jsonl")
    with open(CAT, "w", encoding="utf-8") as fh:
        for r in base:
            fh.write(json.dumps(_slim(r, slug2set), ensure_ascii=False) + "\n")

    # ② 精选 json：downloads >= 门槛，按平台 category 分组
    picked = [r for r in base if (r.get("downloads") or 0) >= min_downloads]
    by_cat = defaultdict(list)
    for r in picked:
        by_cat[r.get("category") or "other"].append(_slim(r, slug2set))
    for v in by_cat.values():
        v.sort(key=lambda x: -(x.get("downloads") or 0))

    # ③ 官方 skillset 场景包（人工策展精选）
    rowidx = {r["slug"]: r for r in rows}
    skillset_packs = []
    for s in sorted(sets, key=lambda x: x.get("id") or 0):
        items = [_slim(rowidx[sl], slug2set) for sl in (s.get("skillSlugs") or []) if sl in rowidx]
        skillset_packs.append({
            "skillset": s.get("displayName"),
            "slug": s.get("slug"),
            "scene": s.get("scene"),
            "summary": s.get("summary"),
            "skill_count": len(items),
            "skills": items,
        })

    payload = {
        "_doc": ("全量「能直接用」交付（分层）。能直接用=不需API key + 有可下载包(probe 302/200)。"
                 "本文件=精选层(downloads>=%d,按平台category分组) + 官方skillset场景包(人工策展)。"
                 "完整镜像见同目录 full-catalog.jsonl(全量不需key+有包,每行一条,导库自筛)。"
                 "SKILL.md正文按需用 download_url 抓(避免单文件爆炸)。"
                 "⚠️ skillsets 只是若干官方策展场景包,非全量分类体系;主分类用平台 category。"
                 "源=某第三方公开技能市场服务。") % min_downloads,
        "_generated_by": "fetch_all.py build",
        "_api": API,
        "_probed": probed,
        "_filter": {"requires_api_key": False, "has_package": True, "min_downloads": min_downloads},
        "_summary": {
            "mirror_total(full-catalog.jsonl)": len(base),
            "selection_total": len(picked),
            "selection_by_category": {c: len(v) for c, v in sorted(by_cat.items(), key=lambda kv: -len(kv[1]))},
            "official_skillset_packs": len(skillset_packs),
        },
        "selection_by_category": {c: v for c, v in sorted(by_cat.items(), key=lambda kv: -len(kv[1]))},
        "official_skillset_packs": skillset_packs,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print(f">>> 全量镜像 {CAT}  {len(base)} 条")
    print(f">>> 精选交付 {OUT}  {len(picked)} 条 (dl>={min_downloads}) + {len(skillset_packs)} 个官方场景包")
    print(f">>> 精选分类: {payload['_summary']['selection_by_category']}")
    if not probed:
        print("⚠️ 尚未 probe，has_package 未过滤（可能含少量404无包，probe 后重跑 build 即精确）")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    args = sys.argv[2:]
    if cmd == "list":
        out = args[args.index("--out") + 1] if "--out" in args else None
        fetch_list(out=out)
    elif cmd == "probe":
        probe(only_free="--free" in args)
    elif cmd == "stats":
        compute_stats()
    elif cmd == "build":
        md = 1000
        if "--min-downloads" in args:
            md = int(args[args.index("--min-downloads") + 1])
        build(min_downloads=md)
    else:
        sys.exit(f"未知命令: {cmd}")
