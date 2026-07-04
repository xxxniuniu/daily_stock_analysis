#!/usr/bin/env python3
"""
舟山警事早报 — 官方多源推送

每天 07:30 从多个可靠官方渠道获取浙江/舟山/嵊泗公安政务要闻，
AI 分析总结后推送微信。

数据来源（按优先级）：
  1. RSS 订阅 — 新华网/人民网/中国政府网/浙江新闻/舟山网等（最稳）
  2. SearXNG 公共搜索引擎 — 股票分析系统同款（已实测可用）
  3. DuckDuckGo 搜索兜底

环境变量：
    SERVERCHAN3_SENDKEY  - 必填，推送密钥
    DEEPSEEK_API_KEY     - 必填，AI 分析
    MAX_NEWS_PER_SOURCE  - 可选，每个源最多条数（默认 3）
"""

import os
import re
import sys
import json
import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("police_news")

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = "deepseek-chat"

YESTERDAY = date.today() - timedelta(days=1)
YESTERDAY_STR = YESTERDAY.strftime("%Y-%m-%d")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/125.0.0.0 Safari/537.36")

# ============================================================
# RSS 源配置（新华社/人民网/政府网/浙江本地 — 全球可访问）
# ============================================================
# feedparser 可直接解析，URL 尽量选稳定的频道
RSS_FEEDS = [
    # 浙江本地新闻
    {"name": "浙江新闻", "icon": "📰", "url": "https://zjnews.zjol.com.cn/xwzx/jrtt.xml", "keywords": ["公安", "警察", "消防", "安全", "治安", "交通", "政治", "党建", "舟山", "嵊泗", "反诈"]},
    {"name": "舟山网", "icon": "🏝️", "url": "https://www.zhoushan.cn/xwwzx/zuixin.xml", "keywords": ["公安", "安全", "警察", "消防", "政治", "党建", "政法", "警"]},

    # 全国性媒体（用关键词过滤出浙江/公安相关内容）
    {"name": "新华网", "icon": "🇨🇳", "url": "http://www.xinhuanet.com/politics/news_politics.xml", "keywords": ["浙江", "舟山", "嵊泗", "公安", "消防", "安全", "政法", "警察", "政治工作"]},
    {"name": "人民网", "icon": "🏛️", "url": "http://www.people.com.cn/rss/politics.xml", "keywords": ["浙江", "舟山", "嵊泗", "公安", "消防", "安全", "政法", "警察"]},
    {"name": "央视新闻", "icon": "📺", "url": "https://news.cctv.com/rss/chin.xml", "keywords": ["浙江", "舟山", "嵊泗", "公安", "消防", "安全", "政法"]},
    {"name": "中国政府网", "icon": "🏛️", "url": "http://www.gov.cn/xinwen/xinwen.xml", "keywords": ["浙江", "舟山", "嵊泗", "公安", "消防", "安全", "政法", "警察"]},

    # 法治公安
    {"name": "中国警察网", "icon": "👮", "url": "https://www.cpd.com.cn/rss/cpd.xml", "keywords": ["浙江", "舟山", "嵊泗", "政治", "党建"]},
    {"name": "中国新闻网", "icon": "📡", "url": "https://www.chinanews.com.cn/rss/gn.xml", "keywords": ["浙江", "舟山", "嵊泗", "公安", "消防", "安全", "政法", "警察"]},
]

# SearXNG 公共实例池（从股票分析系统中提取的可用实例）
SEARXNG_INSTANCES = [
    "https://search.sapti.me",
    "https://searx.be",
    "https://searx.work",
    "https://searx.thegpm.org",
    "https://searx.namejeff.xyz",
    "https://searx.rhscz.eu",
    "https://searx.linxx.net",
    "https://searx.tiekoetter.com",
    "https://searxng.wuemeli.com",
    "https://searx.ro",
    "https://ooglester.com",
    "https://searx.party",
]

# 公安政务关键词（用于过滤匹配）
POLICE_KEYWORDS = [
    "公安", "警察", "警情", "警事", "警务", "警队", "交警", "刑警", "治安",
    "消防", "安全", "交通", "反诈", "禁毒", "扫黑", "防范",
    "政治工作", "党建", "政法", "法治", "执法",
    "舟山", "嵊泗", "定海", "普陀", "岱山", "浙江",
    "通知", "公告", "公示", "通报",
]


def clean_title(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_relevant(title: str, summary: str = "") -> bool:
    """判断是否与公安政务相关"""
    text = title + " " + summary
    return any(kw in text for kw in POLICE_KEYWORDS)


# ============================================================
# 数据源 1: RSS 订阅（最可靠）
# ============================================================

def fetch_rss_feed(src: dict, max_items: int = 3) -> list:
    """抓取 RSS feed，过滤出相关条目"""
    url = src["url"]
    name = src.get("name", "")
    keywords = src.get("keywords", POLICE_KEYWORDS)
    items = []

    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if resp.status_code != 200:
            logger.warning("  RSS %s: HTTP %d", name, resp.status_code)
            return items

        # 解析 RSS/Atom
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            logger.warning("  RSS %s: 解析失败", name)
            return items

        # 兼容 RSS 2.0 和 Atom
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = []

        # RSS 2.0: <rss><channel><item>
        channel = root.find("channel")
        if channel is not None:
            entries = channel.findall("item")
        else:
            # Atom: <feed><entry>
            entries = root.findall("atom:entry", ns)
            if not entries:
                entries = root.findall("entry")

        for entry in entries[:max_items * 10]:
            if len(items) >= max_items:
                break

            # 标题
            title_el = entry.find("title")
            if title_el is None:
                title_el = entry.find("atom:title", ns)
            if title_el is None:
                continue
            title = clean_title(title_el.text or "")

            # 链接
            link_el = entry.find("link")
            link_url = ""
            if link_el is not None:
                link_url = link_el.text or link_el.get("href", "")

            # 摘要
            desc_el = entry.find("description")
            if desc_el is None:
                desc_el = entry.find("summary")
            summary = clean_title(desc_el.text or "") if desc_el is not None else ""

            # 日期
            pub_date = ""
            for tag in ["pubDate", "dc:date", "published", "updated"]:
                date_el = entry.find(tag)
                if date_el is not None:
                    pub_date = clean_title(date_el.text or "")
                    break

            if not title or len(title) < 5:
                continue

            # 关键词过滤
            if not any(kw in (title + summary) for kw in keywords):
                continue

            items.append({
                "title": title,
                "url": link_url,
                "summary": summary[:200],
                "date": pub_date,
                "source": f"RSS-{name}",
            })

    except Exception as e:
        logger.warning("  RSS %s 异常: %s", name, e)

    if items:
        logger.info("  RSS[%s]: %d 篇相关", name, len(items))
    return items


# ============================================================
# 数据源 2: SearXNG 搜索（股票分析同款，实测可用）
# ============================================================

def search_searxng(keywords: str, max_items: int = 5) -> list:
    """使用 SearXNG 搜索浙江/舟山公安新闻"""
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    items = []

    queries = [
        f"site:mp.weixin.qq.com {keywords} 公安",
        f"{keywords} 公安 新闻",
        f"{keywords} 警务 动态",
    ]

    for instance in SEARXNG_INSTANCES:
        if items:
            break
        for query in queries:
            if len(items) >= max_items:
                break
            try:
                resp = session.get(
                    f"{instance}/search",
                    params={"q": query, "format": "json", "language": "zh-CN"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                results = data.get("results", [])
                for result in results:
                    if len(items) >= max_items:
                        break
                    url = result.get("url", "")
                    title = clean_title(result.get("title", ""))
                    if not title or len(title) < 5:
                        continue
                    summary = (result.get("content", "") or "")[:200]
                    # 关键词过滤
                    if not is_relevant(title, summary):
                        continue
                    items.append({
                        "title": title,
                        "url": url,
                        "summary": summary,
                        "date": "",
                        "source": f"SearXNG-{instance.split('//')[1].split('.')[0]}",
                    })
            except Exception:
                continue

    if items:
        logger.info("  SearXNG: %d 篇", len(items))
    return items[:max_items]


# ============================================================
# 数据源 3: DuckDuckGo 搜索（兜底）
# ============================================================

def search_duckduckgo(query: str, max_items: int = 3) -> list:
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    items = []

    queries = [
        f"site:gov.cn {query} 公安",
        f"site:people.com.cn {query} 公安",
        f"site:xinhuanet.com {query} 公安",
    ]

    for q in queries:
        if len(items) >= max_items:
            break
        try:
            resp = session.post(
                "https://html.duckduckgo.com/html/",
                data={"q": q, "kl": "cn-zh"},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for res in soup.select(".result, .result__body, a.result__a"):
                if len(items) >= max_items:
                    break
                text = res.get_text(strip=True)
                href = res.get("href", "")
                # 提取真实 URL
                m = re.search(r"uddg=([^&]+)", href)
                real_url = ""
                if m:
                    from urllib.parse import unquote
                    real_url = unquote(m.group(1))
                if not text or len(text) < 5:
                    continue
                if not is_relevant(text):
                    continue
                items.append({
                    "title": clean_title(text[:80]),
                    "url": real_url or "",
                    "summary": "",
                    "date": "",
                    "source": "DuckDuckGo",
                })
        except Exception:
            continue

    if items:
        logger.info("  DuckDuckGo: %d 篇", len(items))
    return items[:max_items]


# ============================================================
# 聚合抓取
# ============================================================

def fetch_all(max_per_source: int = 3) -> Dict[str, list]:
    """从所有数据源抓取并汇总"""
    collection = {}  # 按主题分组

    # 1. RSS 源（最稳定）
    logger.info("\n=== RSS 源 ===")
    for feed in RSS_FEEDS:
        items = fetch_rss_feed(feed, max_per_source)
        name = f"{feed['icon']} {feed['name']}"
        if name not in collection:
            collection[name] = []
        collection[name].extend(items)

    # 2. SearXNG 搜索
    logger.info("\n=== SearXNG 搜索 ===")
    for region_name, keywords in [
        ("舟山公安", "舟山"),
        ("嵊泗公安", "嵊泗"),
        ("浙江公安", "浙江"),
    ]:
        items = search_searxng(keywords, max_per_source)
        if items:
            collection[f"🔍 {region_name}"] = items

    # 3. DuckDuckGo 兜底
    logger.info("\n=== DuckDuckGo 兜底 ===")
    for region_name, keywords in [
        ("舟山警事", "舟山"),
        ("嵊泗警事", "嵊泗"),
        ("浙江警事", "浙江"),
    ]:
        items = search_duckduckgo(keywords, max_per_source)
        if items:
            collection[f"🔍 {region_name}"] = items

    # 去重 + 裁剪
    result = {}
    for name, items in collection.items():
        seen = set()
        unique = []
        for a in items:
            key = a["title"][:30]
            if key not in seen:
                seen.add(key)
                unique.append(a)
        if unique:
            result[name] = unique[:max_per_source]

    return result


# ============================================================
# AI 分析
# ============================================================

def analyze_with_deepseek(api_key: str, source_data: dict) -> Optional[str]:
    if not api_key:
        return None

    lines = [f"昨日（{YESTERDAY_STR}）要闻汇总：", ""]
    for src_name, articles in source_data.items():
        if not articles:
            continue
        lines.append(f"--- {src_name} ---")
        for a in articles:
            t = a["title"]
            s = a.get("summary", "")
            lines.append(f"- {t}" + (f"：{s[:80]}" if s else ""))
        lines.append("")

    news_text = "\n".join(lines)
    if len(news_text) < 80:
        return None

    prompt = f"""你是一个政务要闻分析助手。请分析以下昨日发布的浙江/舟山/嵊泗公安政务新闻：

{news_text}

按以下格式输出（每条不超过 50 字）：

📌 **昨日要闻概述**
（2-3句话概括主题）

🔍 **分类解读**
• 类别：内容

📰 **各号要点**
• @来源：要点

⚠️ **重点关注**
• 需要留意的事"""

    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是政务要闻分析助手，用简洁中文输出摘要。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 1200,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            analysis = result["choices"][0]["message"]["content"].strip()
            logger.info("AI 分析完成")
            return analysis
        logger.warning("DeepSeek 返回 %d", resp.status_code)
        return None
    except Exception as e:
        logger.warning("AI 分析失败: %s", e)
        return None


# ============================================================
# 报告构建 + 推送
# ============================================================

def build_report(source_data: dict, ai_analysis: Optional[str] = None) -> str:
    today_cn = date.today().strftime("%Y年%m月%d日")
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]

    lines = []
    lines.append(f"📋 **舟山警事早报**")
    lines.append(f"📅 {today_cn} 星期{weekdays[date.today().weekday()]}")
    lines.append(f"📊 汇总 {YESTERDAY_STR} 多源要闻")
    lines.append("")

    if ai_analysis:
        lines.append("━━━ 🤖 AI 分析解读 ━━━")
        lines.append(ai_analysis)
        lines.append("")

    total = sum(len(v) for v in source_data.values())
    if total == 0:
        lines.append("⚠️ 暂未获取到昨日要闻。")
        return "\n".join(lines)

    for src_name, articles in source_data.items():
        if not articles:
            continue
        lines.append(f"━━━ {src_name} ━━━")
        for i, a in enumerate(articles[:3], 1):
            title = a["title"]
            lines.append(f"{i}. {title[:50] + '…' if len(title) > 50 else title}")
        lines.append("")

    lines.append("---")
    lines.append("🤖 每日自动推送 · daily_stock_analysis")
    return "\n".join(lines)


def send_serverchan3(sendkey: str, title: str, content: str) -> bool:
    if sendkey.startswith("sctp"):
        m = re.match(r"sctp(\d+)t", sendkey)
        if not m:
            return False
        url = f"https://{m.group(1)}.push.ft07.com/send/{sendkey}.send"
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        r = requests.post(url, json={"title": title, "desp": content}, timeout=15)
        res = r.json()
        if res.get("code") == 0:
            logger.info("✓ 推送成功！")
            return True
        logger.error("推送失败: %s", res.get("error", res.get("message", "未知错误")))
        return False
    except Exception as e:
        logger.error("推送异常: %s", e)
        return False


# ============================================================
# 主入口
# ============================================================

def main():
    sendkey = os.environ.get("SERVERCHAN3_SENDKEY", "").strip()
    if not sendkey:
        logger.error("缺少 SERVERCHAN3_SENDKEY")
        sys.exit(1)
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    max_per = int(os.environ.get("MAX_NEWS_PER_SOURCE", "3"))

    logger.info("=" * 50)
    logger.info("舟山警事早报")
    logger.info(f"分析日期: {YESTERDAY_STR}")
    logger.info(f"数据源: RSS({len(RSS_FEEDS)}个) + SearXNG + DuckDuckGo")
    logger.info("=" * 50)

    # 抓取所有源
    source_data = fetch_all(max_per)
    total = sum(len(v) for v in source_data.values())

    logger.info(f"\n共获取 {total} 篇")

    # AI 分析
    ai = None
    if deepseek_key and total >= 3:
        logger.info("正在 AI 分析...")
        ai = analyze_with_deepseek(deepseek_key, source_data)

    # 构建并推送
    if total == 0:
        logger.warning("无内容，跳过推送")
        # 不要退出报错，只是通知用户
        send_serverchan3(sendkey, f"舟山警事早报 {YESTERDAY_STR}",
                         f"📋 **舟山警事早报**\n📅 {date.today().strftime('%Y年%m月%d日')}\n\n⚠️ 暂未获取到昨日要闻，请检查数据源配置。\n\n---\n🤖 daily_stock_analysis")
        return

    title = f"舟山警事早报 {YESTERDAY_STR}"
    content = build_report(source_data, ai)
    print("\n" + content + "\n")

    send_serverchan3(sendkey, title, content)


if __name__ == "__main__":
    main()
