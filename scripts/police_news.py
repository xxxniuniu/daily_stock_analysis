#!/usr/bin/env python3
"""
舟山公安 & 嵊泗公安 要闻推送 + AI 分析总结

数据来源：微博官方账号（移动端 API，GitHub Actions 可访问）
+ 政府网站兜底

环境变量：
    SERVERCHAN3_SENDKEY  - 必填，Server酱3 推送密钥
    DEEPSEEK_API_KEY     - 可选，DeepSeek API Key
    MAX_NEWS_PER_SOURCE  - 可选，每个来源最多条数（默认 6）
"""

import os
import re
import sys
import json
import logging
from datetime import date, datetime
from typing import Optional

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

# ============================================================
# 微博新闻源（UID 已预填，搜不到时会自动搜索补充）
# ============================================================
# 常用公安微博 UID（优先使用，搜不到会走搜索流程）
KNOWN_UIDS = {
    "舟山公安": "1987654321",       # 待确认，会自动搜索纠正
    "嵊泗公安": "2987654321",       # 待确认，会自动搜索纠正
}

WEIBO_SOURCES = [
    {"name": "舟山公安", "icon": "🏛️", "search_keywords": ["舟山公安", "舟山市公安局"]},
    {"name": "嵊泗公安", "icon": "🏛️", "search_keywords": ["嵊泗公安", "嵊泗县公安局"]},
]

# 政府网站兜底源
FALLBACK_SOURCES = [
    {
        "name": "舟山公安(站)",
        "icon": "🌐",
        "urls": ["http://gaj.zhoushan.gov.cn/xwzx/gzdt/", "http://gaj.zhoushan.gov.cn/xwzx/"],
        "link_base": "http://gaj.zhoushan.gov.cn",
        "selectors": {
            "items": ["ul.list li", "ul.news-list li", ".news-list li", ".right-list li", "ul li"],
            "title": "a",
            "date": ["span.date", "span.time", "em", ".date", "span"],
        },
    },
    {
        "name": "嵊泗公安(站)",
        "icon": "🌐",
        "urls": ["http://ssgaj.zhoushan.gov.cn/", "http://gaj.zhoushan.gov.cn/"],
        "link_base": "http://ssgaj.zhoushan.gov.cn",
        "selectors": {
            "items": ["ul.list li", "ul.news-list li", ".news-list li", ".right-list li", "ul li"],
            "title": "a", "date": ["span.date", "span.time", "em", ".date", "span"],
        },
        "keyword_filter": ["嵊泗"],
    },
]


# ============================================================
# 微博抓取
# ============================================================

def search_weibo_user(keyword: str) -> Optional[str]:
    """在微博上搜索用户，返回 UID"""
    url = "https://m.weibo.cn/api/container/getIndex"
    params = {"type": "search", "q": keyword}
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": "https://m.weibo.cn/",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        cards = data.get("data", {}).get("cards", [])
        for card in cards:
            card_group = card.get("card_group", []) if card.get("card_group") else [card]
            for c in card_group:
                user = c.get("user") or {}
                if user.get("screen_name") and keyword in user.get("screen_name", ""):
                    uid = str(user.get("id", ""))
                    logger.info("  找到微博用户 [%s] UID=%s", user["screen_name"], uid)
                    return uid
                # 也检查一下 mblog 的作者
                mblog = c.get("mblog") or {}
                if mblog:
                    u = mblog.get("user") or {}
                    if u.get("screen_name") and keyword in u.get("screen_name", ""):
                        uid = str(u.get("id", ""))
                        logger.info("  找到微博用户 [%s] UID=%s", u["screen_name"], uid)
                        return uid
        return None
    except Exception as e:
        logger.warning("  微博搜索失败: %s", e)
        return None


def fetch_weibo_posts(uid: str, max_items: int = 6) -> list:
    """获取用户最新微博"""
    url = f"https://m.weibo.cn/api/container/getIndex"
    params = {"type": "uid", "value": uid, "containerid": f"107603{uid}"}
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": f"https://m.weibo.cn/u/{uid}",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning("  Weibo API 返回 %d", resp.status_code)
            return []
        data = resp.json()
        cards = data.get("data", {}).get("cards", [])
        items = []
        for card in cards:
            if len(items) >= max_items:
                break
            mblog = card.get("mblog")
            if not mblog:
                continue
            text = mblog.get("text", "")
            # 去除 HTML 标签
            text = re.sub(r"<[^>]+>", "", text).strip()
            if not text or len(text) < 3:
                continue
            created = mblog.get("created_at", "")
            # 解析微博时间
            pub_date = parse_weibo_time(created)
            items.append({
                "title": text[:80].replace("\n", " "),
                "url": f"https://m.weibo.cn/detail/{mblog.get('id', '')}",
                "date": pub_date,
                "content": text[:500],
            })
        logger.info("  获取到 %d 条微博", len(items))
        return items
    except Exception as e:
        logger.warning("  微博抓取失败: %s", e)
        return []


def parse_weibo_time(created_at: str) -> str:
    """将微博时间转为 YYYY-MM-DD 格式"""
    try:
        # 微博格式: Tue Jun 30 18:00:00 +0800 2026
        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def fetch_weibo_source(source: dict, max_items: int = 6) -> list:
    """从微博抓取指定源的新闻"""
    name = source.get("name", "")
    keywords = source.get("search_keywords", [name]) or [name]
    logger.info("正在从微博抓取 [%s]...", name)

    # 尝试预填 UID 或搜索
    uid = KNOWN_UIDS.get(name)
    if not uid or True:  # 每次搜索确保准确
        uid = None
        for kw in keywords:
            uid = search_weibo_user(kw)
            if uid:
                break

    if not uid:
        logger.warning("  未找到微博账号")
        return []

    return fetch_weibo_posts(uid, max_items)


# ============================================================
# 政府网站兜底抓取
# ============================================================

def fetch_page(url: str, timeout: int = 10) -> Optional[str]:
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
     
