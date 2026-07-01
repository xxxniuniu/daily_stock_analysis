#!/usr/bin/env python3
"""
舟山公安 & 嵊泗公安 要闻推送脚本

每天早上 07:30 通过 GitHub Actions 自动抓取最新要闻，推送到微信（Server酱3）。

环境变量：
    SERVERCHAN3_SENDKEY  - 必填，Server酱3 推送密钥
    MAX_NEWS_PER_SOURCE  - 可选，每个来源最多条数（默认 6）
    NEWSOURCES_JSON      - 可选，自定义新闻源 JSON（覆盖默认源）
"""

import os
import re
import sys
import json
import logging
from datetime import date
from typing import Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("police_news")


# 默认新闻源配置（如果实际网站结构不同可自行调整）
DEFAULT_SOURCES = [
    {
        "name": "舟山公安",
        "icon": "🏛️",
        "urls": [
            "http://gaj.zhoushan.gov.cn/xwzx/gzdt/",
            "http://gaj.zhoushan.gov.cn/xwzx/",
            "http://gaj.zhoushan.gov.cn/zwgk/",
        ],
        "link_base": "http://gaj.zhoushan.gov.cn",
        "selectors": {
            "items": [
                "ul.list li", "ul.news-list li",
                ".news-list li", ".right-list li", "ul li",
            ],
            "title": "a",
            "date": ["span.date", "span.time", "em", ".date", "span"],
        },
    },
    {
        "name": "嵊泗公安",
        "icon": "🏛️",
        "urls": [
            "http://ssgaj.zhoushan.gov.cn/",
            "http://gaj.zhoushan.gov.cn/",
        ],
        "link_base": "http://ssgaj.zhoushan.gov.cn",
        "selectors": {
            "items": [
                "ul.list li", "ul.news-list li",
                ".news-list li", ".right-list li", "ul li",
            ],
            "title": "a",
            "date": ["span.date", "span.time", "em", ".date", "span"],
        },
        "keyword_filter": ["嵊泗"],
    },
]


def fetch_page(url: str, timeout: int = 15) -> Optional[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text if resp.status_code == 200 else None
    except requests.RequestException as e:
        logger.warning("  请求失败: %s", e)
        return None


def extract_news_items(html: str, source: dict, max_items: int = 6) -> list:
    soup = BeautifulSoup(html, "html.parser")
    selectors = source.get("selectors", {})
    item_patterns = selectors.get("items", ["ul li"])
    link_base = source.get("link_base", "")
    keyword_filter = source.get("keyword_filter", [])

    items, seen_links = [], set()
    for pattern in item_patterns:
        if len(items) >= max_items:
            break
        for elem in soup.select(pattern):
            if len(items) >= max_items:
                break
            link_el = elem.find("a")
            if not link_el:
                continue
            title = link_el.get_text(strip=True)
            if not title or len(title) < 2:
                continue
            href = link_el.get("href", "")
            if href and not href.startswith("http"):
                href = link_base.rstrip("/") + "/" + href.lstrip("/")
            if not href or href in seen_links:
                continue
            seen_links.add(href)
            date_el = elem.find("span")
            pub_date = date_el.get_text(strip=True) if date_el else ""
            item = {"title": title, "url": href, "date": pub_date}
            if keyword_filter and not any(kw in title for kw in keyword_filter):
                continue
            items.append(item)
    return items[:max_items]


def fetch_source_news(source: dict, max_items: int = 6) -> list:
    name = source.get("name", "未知源")
    logger.info("正在抓取 [%s]...", name)
    all_items = []
    for url in source.get("urls", []):
        if len(all_items) >= max_items:
            break
        html = fetch_page(url)
        if not html:
            continue
        all_items.extend(extract_news_items(html, source, max_items))
    seen, unique = set(), []
    for item in all_items:
        if item["title"] not in seen:
            seen.add(item["title"])
            unique.append(item)
    return unique[:max_items]


def build_report(all_news: dict) -> str:
    today = date.today().strftime("%Y年%m月%d日")
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    lines = [f"📋 **舟山警事早报**", f"📅 {today} 星期{weekdays[date.today().weekday()]}", ""]

    total = sum(len(v) for v in all_news.values())
    if total == 0:
        lines.append("⚠️ 暂时没有获取到最新要闻，请检查网络或新闻源配置。\n")
        return "\n".join(lines)

    for src_name, items in all_news.items():
        if not items:
            continue
        lines.append(f"━━━ {src_name} ━━━")
        for i, item in enumerate(items[:6], 1):
            ds = f" ({item['date']})" if item.get("date") else ""
            title = item["title"][:40] + "..." if len(item["title"]) > 40 else item["title"]
            lines.append(f"{i}. {title}{ds}")
        lines.append("")
    lines.append("---\n🤖 每日自动推送 · daily_stock_analysis")
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
            logger.info("推送成功！")
            return True
        logger.error("推送失败: %s", res.get("message", "未知错误"))
        return False
    except Exception as e:
        logger.error("推送异常: %s", e)
        return False


def main():
    sendkey = os.environ.get("SERVERCHAN3_SENDKEY", "").strip()
    if not sendkey:
        logger.error("缺少 SERVERCHAN3_SENDKEY 环境变量")
        sys.exit(1)

    sources_json = os.environ.get("NEWSOURCES_JSON", "")
    sources = json.loads(sources_json) if sources_json else DEFAULT_SOURCES
    max_per = int(os.environ.get("MAX_NEWS_PER_SOURCE", "6"))

    all_news, total = {}, 0
    for src in sources:
        items = fetch_source_news(src, max_per)
        name = f"{src.get('icon', '📰')} {src.get('name', '未知')}"
        all_news[name] = items
        total += len(items)

    title = f"舟山警事早报 {date.today().strftime('%m/%d')}"
    content = build_report(all_news)
    print("\n" + content + "\n")

    if total > 0:
        send_serverchan3(sendkey, title, content)
    else:
        logger.warning("没有获取到新闻，跳过推送")


if __name__ == "__main__":
    main()
