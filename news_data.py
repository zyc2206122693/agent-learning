#!/usr/bin/env python3
"""Real-time financial news (7x24 快讯) data layer for the fund dashboard.

Fetches fast news from Eastmoney's 7x24 API, tags each item with a
category (宏观/行业/公司/市场) and sentiment (利好/利空/中性), dedupes by
news code + title hash, and caches the recent items locally so the
dashboard keeps working when the upstream is unreachable.
"""

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "news_cache.json")

NEWS_API = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
PAGE_SIZE = 50
MAX_PAGES = 2          # fetch up to 100 items per refresh
CACHE_LIMIT = 200      # keep the most recent 200 items locally
CACHE_TTL = 60         # seconds; serve cached data within TTL, fetch beyond it

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# category keyword rules, first match wins (more specific categories first)
CATEGORY_RULES = [
    ("宏观", r"央行|美联储|加息|降息|CPI|PPI|PMI|GDP|经济数据|通胀|就业数据|失业|利率决议|汇率|关税|货币政策|财政部|国务院|发改委|统计局|欧央行|日央行"),
    ("行业", r"芯片|半导体|AI|人工智能|机器人|新能源|光伏|锂电|储能|医药|创新药|白酒|消费|银行|地产|军工|券商|汽车|算力|英伟达|纳斯达克|纳指|游戏|传媒|黄金|原油|稀土|数据要素|6G|固态电池|低空经济"),
    ("公司", r"公告|签约|收购|增持|减持|业绩|财报|中标|定增|回购|停牌|重组|立案|处罚|招股书|IPO|融资|发行债券"),
    ("市场", r"A股|美股|港股|上证|深证|创业板|科创|指数|涨停|跌停|成交|北向|板块|概念|尾盘|开盘"),
]

POSITIVE = r"涨停|大涨|上涨|涨超|净买入|超预期|增持|中标|获批|上调|新高|创纪录|增长|扭亏|预增|回购|分红|签约|合作|收购|获批"
NEGATIVE = r"跌停|大跌|下跌|跌超|减持|亏损|下调|警示|风险提示|暴跌|违约|处罚|调查|诉讼|裁员|预亏|爆雷|承压|下滑"

# keywords linking a news item to funds in this portfolio
RELATED_RULES = [
    ("纳指", r"纳斯达克|纳指"),
    ("AI 主题", r"人工智能|AI|算力"),
    ("机器人", r"机器人"),
    ("制造业", r"半导体|制造|工业"),
    ("资源", r"资源|有色|沪港深|港股"),
    ("债券", r"债券|债市|利率债|信用债|纯债"),
]

COLORS = {
    "利好": "#ef4444",  # red = up in CN convention
    "利空": "#10b981",  # green = down
    "中性": "#64748b",
}


def _http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_time(s):
    """'2026-08-06 22:40:15' -> datetime (naive); None if unparseable."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def tag_news(item):
    """Add category / sentiment / related tags to a raw news item."""
    text = item["title"] + " " + item.get("summary", "")
    category = "市场"
    for name, pat in CATEGORY_RULES:
        if re.search(pat, text):
            category = name
            break
    sentiment = "中性"
    if re.search(NEGATIVE, text):
        sentiment = "利空"
    if re.search(POSITIVE, text):
        sentiment = "利好"
    related = []
    for label, pat in RELATED_RULES:
        if re.search(pat, text):
            related.append(label)
    return category, sentiment, related


def _news_llm_enabled():
    """是否在看板实时刷新时对每条新闻做 LLM 分类。

    默认关闭：每次刷新上百条新闻若逐条调 LLM 会很慢且烧 token，
    实时看板仍用正则打标。用 config.json 的 news_llm_classify 显式开启
    （教学/批量场景用 news_classifier 里的批量接口更划算）。
    """
    try:
        cfg = json.load(open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8"))
        return bool(cfg.get("news_llm_classify", False))
    except Exception:
        return False


def normalize(item):
    """Map a raw Eastmoney item to the dashboard's news schema."""
    code = item.get("code", "")
    title = re.sub(r"^【[^】]*】", "", item.get("title", ""))
    summary = item.get("summary", "")
    category, sentiment, related = tag_news({"title": title, "summary": summary})
    if _news_llm_enabled():
        try:
            from news_classifier import classify_news  # 懒加载，避免耦合
            r = classify_news(title, summary)
            category, sentiment, related = r["category"], r["sentiment"], r["related"]
        except Exception:
            pass  # LLM 失败则保留正则结果
    stock = []
    for s in item.get("stockList", []):
        if isinstance(s, dict) and s.get("stockName"):
            stock.append(s["stockName"])
    return {
        "id": code or hashlib.md5(title.encode("utf-8")).hexdigest()[:16],
        "time": item.get("showTime", ""),
        "title": title,
        "summary": summary,
        "category": category,
        "sentiment": sentiment,
        "related": related,
        "stock": stock[:5],
        "source": "新浪财经快讯" if "新浪" in item.get("source", "") else "东方财富",
    }


def fetch_news():
    """Fetch fresh news from Eastmoney (multi-page), tagged and deduped."""
    items, seen = [], set()
    sort_end = ""
    for _ in range(MAX_PAGES):
        params = {
            "client": "web",
            "biz": "web_724",
            "fastColumn": "102",
            "sortEnd": sort_end,
            "pageSize": str(PAGE_SIZE),
            "req_trace": "1",
        }
        url = NEWS_API + "?" + urllib.parse.urlencode(params)
        try:
            data = json.loads(_http_get(url))
        except Exception:
            break
        page_list = (data.get("data") or {}).get("fastNewsList") or []
        if not page_list:
            break
        for raw in page_list:
            item = normalize(raw)
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            items.append(item)
        sort_end = (data.get("data") or {}).get("sortEnd", "")
        if not sort_end:
            break
    return items


def load_cache():
    """Cache format: {"fetched_at": "YYYY-MM-DD HH:MM:SS", "items": [...]}."""
    if not os.path.exists(CACHE_PATH):
        return {}, []
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):  # legacy list format
            return {}, data
        return data.get("fetched_at", ""), data.get("items", [])
    except Exception:
        return {}, []


def save_cache(fetched_at, items):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": fetched_at, "items": items}, f, ensure_ascii=False, indent=1)


def merge_dedupe(new_items, old_items):
    """Merge new + cached items, dedupe by id (title-hash fallback), keep newest."""
    merged, seen = [], set()
    for item in new_items + old_items:
        key = item.get("id") or hashlib.md5(item.get("title", "").encode("utf-8")).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    merged.sort(key=lambda x: _parse_time(x.get("time", "")) or datetime.min, reverse=True)
    return merged[:CACHE_LIMIT]


def get_news(query="", category="", sentiment="", refresh=False, now=None):
    """Return news matching the filters.

    Serves from cache when fresh (within TTL); fetches upstream otherwise.
    Returns {"total": n, "deduped": m, "items": [...]}.
    """
    now = now or datetime.now()
    cached_ts, cached = load_cache()
    raw_count = len(cached)
    ts = _parse_time(cached_ts)
    fresh = ts is not None and (now - ts).total_seconds() < CACHE_TTL

    items = cached
    if refresh or not fresh:
        try:
            new_items = fetch_news()
            if new_items:
                items = merge_dedupe(new_items, cached)
                save_cache(now.strftime("%Y-%m-%d %H:%M:%S"), items)
        except Exception:
            pass  # fall back to cache on upstream failure

    query = (query or "").strip().lower()
    matched = []
    for it in items:
        if query and query not in (it.get("title", "") + it.get("summary", "")).lower():
            continue
        if category and category != "全部" and it.get("category") != category:
            continue
        if sentiment and sentiment != "全部" and it.get("sentiment") != sentiment:
            continue
        matched.append(it)

    # Recommendation-first: news matching held funds rank on top (more
    # matches = higher), then newest within the same relevance tier.
    def _sort_key(it):
        rel = it.get("related") or []
        return (len(rel), _parse_time(it.get("time", "")) or datetime.min)
    matched.sort(key=_sort_key, reverse=True)

    return {
        "total": len(matched),
        "deduped": raw_count - len(items),
        "items": matched[:50],
    }


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else ""
    res = get_news(query=q, refresh=True)
    print(f"共 {res['total']} 条 (去重后)")
    for it in res["items"][:5]:
        print(f"[{it['time']}] ({it['category']}/{it['sentiment']}) {it['title']}")
