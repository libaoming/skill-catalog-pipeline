#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 脱敏参考实现：凭证/数据源 host 全部走环境变量，详见 src/README.md。依赖外部服务，非开箱即跑。
"""
fetch_bodies.py — 全量技能正文下载器（数据源 → 本地交接）

从 fixtures/skills/_full_raw.jsonl 筛「不需 key + 有包」，并发拉每条的
download_url(302→对象存储 zip)，存为 OUTDIR/{slug}.zip。纯标准库、并发、断点续传、失败重试。

用法:
  python3 fetch_bodies.py            # 下载全量(不需key+有包)
  python3 fetch_bodies.py --min-downloads 1000   # 只下 dl>=N
  python3 fetch_bodies.py --workers 30           # 并发数(默认24)
  python3 fetch_bodies.py --retry-failed         # 只重试上次失败的

进度写 OUTDIR/_progress.log；失败写 OUTDIR/_failed.tsv（slug<TAB>reason）。
已存在且非空的 {slug}.zip 自动跳过 → 可随时 Ctrl-C 续传。
"""
import os, sys, json, time, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# 数据源 host：通过环境变量配置，代码内不出现真实第三方域名
API = os.environ.get("SKILL_MARKET_API_BASE", "https://<skill-market-api>")
SRC = "fixtures/skills/_full_raw.jsonl"
OUTDIR = "fixtures/skills/full-bodies"
UA = "skill-catalog-pipeline/1.0"
TIMEOUT = 40
RETRY = 2

_lock = Lock()
_done = 0
_ok = 0
_skip = 0
_fail = 0
_t0 = time.time()


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(os.path.join(OUTDIR, "_progress.log"), "a") as f:
        f.write(line + "\n")


def want(d, min_dl, need_key=False):
    rk = (d.get("labels") or {}).get("requires_api_key")
    hp = d.get("has_package")
    is_key = str(rk).lower() == "true"
    if need_key:
        if not is_key:          # --need-key 模式：只要需 key 的
            return False
    elif rk not in (False, "false", None, 0):  # 默认：只要不需 key 的
        return False
    if hp not in (True, "true", 302, "302"):
        return False
    if (d.get("downloads") or 0) < min_dl:
        return False
    return True


def fetch_one(slug):
    """下载单个 slug 的 zip → OUTDIR/{slug}.zip。返回 ('ok'|'skip'|'fail', reason)。"""
    safe = urllib.parse.quote(slug, safe="")
    dst = os.path.join(OUTDIR, safe + ".zip")
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return ("skip", "")
    url = f"{API}/api/v1/download?slug={urllib.parse.quote(slug)}"
    last = ""
    for attempt in range(RETRY + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # 自动跟 302→对象存储
                data = r.read()
            if not data:
                last = "empty"
                continue
            if data[:2] != b"PK":  # 非 zip(可能是错误页/json)
                last = "not-zip(%dB)" % len(data)
                continue
            tmp = dst + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)
            return ("ok", "")
        except urllib.error.HTTPError as e:
            last = f"http{e.code}"
            if e.code == 404:
                return ("fail", "404")  # 无包，不重试
        except Exception as e:
            last = type(e).__name__
        time.sleep(0.5 * (attempt + 1))
    return ("fail", last)


def main():
    args = sys.argv[1:]
    min_dl = 0
    workers = 24
    retry_failed = "--retry-failed" in args
    if "--min-downloads" in args:
        min_dl = int(args[args.index("--min-downloads") + 1])
    if "--workers" in args:
        workers = int(args[args.index("--workers") + 1])
    need_key = "--need-key" in args
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None

    os.makedirs(OUTDIR, exist_ok=True)

    if retry_failed:
        fpath = os.path.join(OUTDIR, "_failed.tsv")
        slugs = []
        if os.path.exists(fpath):
            for line in open(fpath):
                s = line.split("\t")[0].strip()
                if s:
                    slugs.append(s)
        slugs = sorted(set(slugs))
        log(f">>> 重试模式：{len(slugs)} 个曾失败的 slug")
        open(fpath, "w").close()  # 清空，本轮重写
    else:
        slugs = []
        for line in open(SRC):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if want(d, min_dl, need_key):
                slugs.append(d["slug"])
        if limit:
            slugs = slugs[:limit]
        log(f">>> 全量筛选：{len(slugs)} 条待下 (need_key={need_key}, min_downloads={min_dl}, workers={workers})")

    total = len(slugs)
    global _done, _ok, _skip, _fail
    fail_f = open(os.path.join(OUTDIR, "_failed.tsv"), "a")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, s): s for s in slugs}
        for fut in as_completed(futs):
            slug = futs[fut]
            try:
                status, reason = fut.result()
            except Exception as e:
                status, reason = "fail", type(e).__name__
            with _lock:
                _done += 1
                if status == "ok":
                    _ok += 1
                elif status == "skip":
                    _skip += 1
                else:
                    _fail += 1
                    fail_f.write(f"{slug}\t{reason}\n")
                    fail_f.flush()
                if _done % 500 == 0 or _done == total:
                    el = time.time() - _t0
                    rate = _done / el if el else 0
                    eta = (total - _done) / rate / 60 if rate else 0
                    log(f"  {_done}/{total}  ok={_ok} skip={_skip} fail={_fail}  "
                        f"{rate:.1f}/s  ETA {eta:.0f}min")

    fail_f.close()
    el = (time.time() - _t0) / 60
    log(f">>> 完成：ok={_ok} skip={_skip} fail={_fail} / {total}  用时 {el:.1f}min")
    log(f">>> 正文目录 {OUTDIR}/  失败清单 {OUTDIR}/_failed.tsv（可 --retry-failed 续）")


if __name__ == "__main__":
    main()
