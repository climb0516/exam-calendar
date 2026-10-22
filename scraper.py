#!/usr/bin/env python3
# Created by 青云 on 2026/10/21.
"""考试报名日历 scraper — 抓各考试官网的最新公告（官网抓不到的用新闻补充），输出 data.json。
考试日程本身在人工维护的 exams.json 里，页面同时读取两个文件。仅依赖 Python 标准库。"""
import html, json, re, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT  = Path(__file__).parent
OUT   = ROOT / "data.json"
CST   = timezone(timedelta(hours=8))
NOW   = datetime.now(CST)
TODAY = NOW.date()
KEEP_DAYS = 120        # 公告保留多少天
NEWS_DAYS = 30

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "zh-CN,zh;q=0.9"}

# 官网公告列表：标题要命中 include，且不能命中 exclude
SITES = [
    {"key": "yz",    "name": "研招网",           "url": "https://yz.chsi.com.cn/",     "include": r"研考|硕士研究生.*(?:考试|招生工作)|研究生(?:招生)?考试", "exclude": r"复试|调剂|院校|大学|学院|学位|培养|导师|博士|推免|推荐免试|拟录取|名单|章程|拟招生"},
    {"key": "cet",   "name": "四六级官网",        "url": "https://cet.neea.edu.cn/",    "include": r"四、六级|四六级|CET", "exclude": r""},
    {"key": "ntce",  "name": "教资官网",          "url": "https://ntce.neea.edu.cn/",   "include": r"教师资格", "exclude": r""},
    {"key": "ncre",  "name": "计算机等级考试官网", "url": "https://ncre.neea.edu.cn/",   "include": r"计算机等级|NCRE", "exclude": r""},
    {"key": "cpta",  "name": "中国人事考试网",     "url": "http://www.cpta.com.cn/",     "include": r"资格考试|职业资格|成绩|报名|考试", "exclude": r"招聘|声明|遴选|选调|事业单位"},
    {"key": "kzp",   "name": "会计资格评价网",     "url": "http://kzp.mof.gov.cn/",      "include": r"会计.*资格|会计.*考试", "exclude": r"准则"},
    {"key": "rk",    "name": "软考官网",          "url": "https://www.ruankao.org.cn/", "include": r"软件资格|软考|计算机技术与软件|计算机化考试", "exclude": r"会议|座谈|调研|召开"},
]
# 官网抓不到的考试：Google / Bing 新闻
# （人事考试网、会计资格评价网、软考官网对 GitHub Actions 的海外 IP 返回 405 / 超时，也用新闻兜底）
NEWS = {
    "guokao": ["国考 报名", "国家公务员考试 公告"],
    "cpa":    ["注册会计师 全国统一考试", "注会 考试 成绩", "CPA 考试 报名"],
    "fakao":  ["法律职业资格考试 报名", "法考 成绩公布"],
    "kuaiji": ["会计职称 考试 成绩", "会计专业技术资格考试 报名"],
    "zhiye":  ["职业资格考试 成绩公布", "一级建造师 考试", "执业药师 考试", "经济师 考试"],
    "ruankao": ["软考 成绩", "软考 报名", "计算机软件资格考试"],
}
NEWS_MUST = {"guokao": r"国考|国家公务员|中央机关", "cpa": r"注册会计师|CPA|注会", "fakao": r"法律职业资格|法考",
             "kuaiji": r"会计.{0,4}(?:职称|资格)", "ruankao": r"软考|软件资格",
             "zhiye": r"职业资格|建造师|造价工程师|执业药师|经济师|经济专业技术|安全工程师|消防工程师|社会工作者|监理工程师|勘察设计"}
NEWS_EXCLUDE = re.compile(r"培训|课程|网课|押题|真题|答案|机构|班|广告|怎么备考|备考攻略|攻略|经验|教育提醒|教育网")
NEWS_SKIP = {"zhiye": re.compile(r"法律职业资格|法考")}     # 法考单独一组，别混进「职业资格」

# ── 工具 ──────────────────────────────────────────────────────────────────────

def fetch(url, timeout=20):
    b = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()
    for enc in ("utf-8", "gb18030"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", "ignore")

def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()

def run(label, fn):
    try:
        res = fn()
        print(f"  ✓ {label:<14} {len(res):>4}")
        return res, ""
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
        print(f"  ✗ {label:<14}    0 {err}")
        return None, err

def find_date(*texts):
    """在链接后面的文字或 URL 里找发布日期：2026-09-27 / 2026.09.27 / 2026年9月27日 / URL 里的 20260927。"""
    for t in texts:
        m = re.search(r"(20\d\d)[-./年](\d{1,2})[-./月](\d{1,2})", t) or re.search(r"(?<!\d)(20[2-3]\d)(\d\d)(\d\d)(?!\d)", t)
        if m:
            try:
                d = date(int(m[1]), int(m[2]), int(m[3]))
                if d <= TODAY + timedelta(days=1):
                    return d.isoformat()
            except ValueError:
                pass
    return ""

# ── 官网公告 ──────────────────────────────────────────────────────────────────

def site_notices(site):
    s = fetch(site["url"])
    links = list(re.finditer(r"<a\b[^>]*?href\s*=\s*['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", s, re.S | re.I))
    out, seen = [], set()
    for i, m in enumerate(links):
        title = clean(m[2])
        attr = re.search(r"title\s*=\s*['\"]([^'\"]+)['\"]", m[0])
        if attr and len(clean(attr[1])) > len(title):
            title = clean(attr[1])
        title = re.sub(r'^.*?["\']>\s*', "", title)         # 个别站点 title 属性里断行，带出了 HTML 碎片
        if len(title) < 10 or not re.search(site["include"], title):
            continue
        if site["exclude"] and re.search(site["exclude"], title):
            continue
        url = urllib.parse.urljoin(site["url"], m[1])
        if url in seen or url.startswith("javascript"):
            continue
        seen.add(url)
        nxt = links[i + 1].start() if i + 1 < len(links) else m.end() + 300
        out.append({"site": site["key"], "source": site["name"], "title": title, "url": url,
                    "date": find_date(s[m.end():nxt][:300], m[1])})
    if not out:
        raise ValueError("no notices matched")
    return out

# ── 新闻补充 ──────────────────────────────────────────────────────────────────

def google_news(q):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"})
    x = fetch(url)
    out = []
    for it in re.findall(r"<item>(.*?)</item>", x, re.S):
        t = clean(re.search(r"<title>(.*?)</title>", it, re.S)[1])
        src = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
        source = clean(src[1]) if src else ""
        if source and t.endswith(" - " + source):
            t = t[: -len(source) - 3]
        pub = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        out.append({"title": t, "url": clean(re.search(r"<link>(.*?)</link>", it, re.S)[1]), "source": source, "published": pub and pub[1]})
    return out

def bing_news(q):
    url = "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": q, "format": "rss", "setlang": "zh-cn"})
    x = fetch(url)
    out = []
    for it in re.findall(r"<item>(.*?)</item>", x, re.S):
        link = clean(re.search(r"<link>(.*?)</link>", it, re.S)[1])
        real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url")
        pub = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        src = re.search(r"<News:Source>(.*?)</News:Source>", it, re.S)
        desc = re.search(r"<description>(.*?)</description>", it, re.S)    # Bing 带一段正文摘要（Google 的只是标题链接，不取）
        out.append({"title": clean(re.search(r"<title>(.*?)</title>", it, re.S)[1]), "url": real[0] if real else link,
                    "source": clean(src[1]) if src else "", "published": pub and pub[1], "desc": clean(desc[1])[:300] if desc else ""})
    return out

def news(exam):
    raw = []
    for q in NEWS[exam]:
        for fn in (google_news, bing_news):
            try:
                raw += fn(q)
            except Exception as e:
                print(f"    {fn.__name__}({q}) 失败：{e}")
    if not raw:
        raise ValueError("all queries failed")
    cutoff = NOW - timedelta(days=NEWS_DAYS)
    out, seen = [], {}
    for it in raw:
        try:
            pub = parsedate_to_datetime(it["published"]).astimezone(CST)
        except Exception:
            continue
        t = it["title"]
        key = re.sub(r"[^一-龥A-Za-z0-9]", "", t)[:18]
        if key in seen:
            if it.get("desc") and not seen[key].get("desc"):      # 同一条 Google 先到没摘要，用 Bing 的补上
                seen[key]["desc"] = it["desc"]
            continue
        if pub < cutoff or not re.search(NEWS_MUST[exam], t) or NEWS_EXCLUDE.search(t) \
                or (exam in NEWS_SKIP and NEWS_SKIP[exam].search(t)):
            continue
        n = {"site": exam, "source": it["source"] or "新闻", "title": t, "url": it["url"],
             "date": pub.date().isoformat(), "news": True}
        if it.get("desc"):
            n["desc"] = it["desc"]
        seen[key] = n
        out.append(n)
    out.sort(key=lambda n: n["date"], reverse=True)
    return out[:12]

# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text("utf-8"))
        except Exception:
            prev = {}
    old = {n["url"]: n for n in prev.get("notices") or []}
    first_run = not old

    print(f"考试报名日历 · {TODAY}")
    jobs = {s["key"]: (lambda s=s: site_notices(s)) for s in SITES}
    jobs.update({k: (lambda k=k: news(k)) for k in NEWS})
    with ThreadPoolExecutor(6) as ex:
        futs = {k: ex.submit(run, k, fn) for k, fn in jobs.items()}
    res = {k: f.result() for k, f in futs.items()}

    sources, notices = {}, []
    for k, (data, err) in res.items():
        prev_k = [n for n in old.values() if n["site"] == k]
        stale = data is None
        items = prev_k if stale else data
        sources[k] = {"ok": not stale, "stale": stale and bool(prev_k), "error": err, "count": len(items),
                      "name": next((s["name"] for s in SITES if s["key"] == k), "新闻")}
        for n in items:
            o = old.get(n["url"])
            # 首次出现的日期：第一次运行时不算「新」，之后新出现的记今天
            n["first_seen"] = o["first_seen"] if o else ("" if first_run else TODAY.isoformat())
            notices.append(n)

    cut = (TODAY - timedelta(days=KEEP_DAYS)).isoformat()
    notices = [n for n in notices if (n["date"] or n["first_seen"] or TODAY.isoformat()) >= cut]
    notices.sort(key=lambda n: n["date"] or n["first_seen"] or "", reverse=True)

    data = {
        "updated": NOW.isoformat(timespec="seconds"),
        "today": TODAY.isoformat(),
        "sources": sources,
        "notices": notices,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"→ data.json {OUT.stat().st_size / 1024:.0f} KB，{len(notices)} 条公告")

if __name__ == "__main__":
    main()
