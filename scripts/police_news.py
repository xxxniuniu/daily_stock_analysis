#!/usr/bin/env python3
"""
舟山警事早报 — 微信公众号要闻推送

每天 07:30 从 DuckDuckGo 搜索引擎抓取四个公众号前一天的文章，
AI 分析总结后推送到微信。

公众号：嵊泗列岛先锋、嵊泗公安、舟山公安、浙江公安

数据来源：
  - DuckDuckGo（主力，不封爬虫）
  - SearXNG 公共实例（兜底）
  - Bing（最后兜底）

环境变量：
    SERVERCHAN3_SENDKEY  - 必填，推送密钥
    DEEPSEEK_API_KEY     - 必填（已配置），开启 AI 分析
    MAX_NEWS_PER_SOURCE  - 可选，每个号最多条数（默认 5）
"""

import os
import re
import sys
import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import quote, urljoin

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

WECHAT_ACCOUNTS = [
    {"name": "嵊泗列岛先锋", "icon": "🚩"},
    {"name": "嵊泗公安", "icon": "🏛️"},
    {"name": "舟山公安", "icon": "🏛️"},
    {"name": "浙江公安", "icon": "🏛️"},
]


def make_session() -> requests.Session:
    """创建带有浏览器特征的请求会话"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return s


def clean_title(text: str) -> str:
    """清洗标题"""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[\d.、\s]+", "", text)  # 去掉序号
    return text


def is_wechat_url(url: str) -> bool:
    """判断是否为微信文章链接"""
    return "mp.weixin.qq.com" in url


# ============================================================
# 数据源 1: DuckDuckGo HTML 搜索（最可靠）
# ============================================================

def search_duckduckgo(query: str, max_items: int = 5) -> list:
    """使用 DuckDuckGo HTML 搜索（不爬虫友好，可自动运行）"""
    session = make_session()
    url = "https://html.duckduckgo.com/html/"
    params = {"q": query, "kl": "cn-zh"}
    items = []

    try:
        resp = session.post(url, data=params, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            logger.warning("  DuckDuckGo 返回 %d", resp.status_code)
            return items

        soup = BeautifulSoup(resp.text, "html.parser")
        results = soup.select(".result, .web-result, .result__body, .results_links")

        if not results:
            results = soup.select("div[data-nosnippet] li, .nrn-react-div, .result-title")

        for res in results[:max_items * 3]:
            if len(items) >= max_items:
                break

            # DuckDuckGo 的各种结果结构
            title_el = (
                res.select_one(".result__title a, .result-title a, a.result__a, .result-title")
            )
            link_el = (
                res.select_one(".result__url, .result__body a, .result-url, a[href]")
            )

            # 优先找标题链接
            if title_el:
                href = title_el.get("href", "")
                title = title_el.get_text(strip=True)
            elif link_el:
                href = link_el.get("href", "")
                title = link_el.get_text(strip=True) or ""
            else:
                continue

            # DuckDuckGo 的链接是重定向格式
            # 从重定向 URL 中提取真实链接
            real_url = extract_ddg_url(href)
            if not real_url or not is_wechat_url(real_url):
                continue

            title = clean_title(title)
            if not title or len(title) < 5:
                continue

            # 提取摘要
            summary = ""
            for sel in [".result__snippet", ".result-snippet", ".snippet", "p"]:
                el = res.select_one(sel)
                if el:
                    summary = el.get_text(strip=True)
                    break

            items.append({
                "title": title,
                "url": real_url,
                "summary": summary[:200] if summary else "",
            })

        logger.info("  DuckDuckGo: %d 篇", len(items))
    except Exception as e:
        logger.warning("  DuckDuckGo 异常: %s", e)

    return items


def extract_ddg_url(url: str) -> str:
    """从 DuckDuckGo 的重定向 URL 中提取真实地址"""
    # 格式: //duckduckgo.com/l/?uddg=https%3A%2F%2F...&rut=...
    match = re.search(r"uddg=([^&]+)", url)
    if match:
        from urllib.parse import unquote
        return unquote(match.group(1))
    # 或者是直接的 http/https
    if url.startswith("http"):
        return url
    return ""


# ============================================================
# 数据源 2: SearXNG 公共实例搜索
# ============================================================

def search_searxng(query: str, max_items: int = 5) -> list:
    """使用 SearXNG 公共实例搜索"""
    instances = [
        "https://search.sapti.me",
        "https://searx.be",
        "https://searx.work",
        "https://searx.thegpm.org",
    ]

    session = make_session()
    items = []

    for instance in instances:
        if items:
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
            for result in data.get("results", []):
                if len(items) >= max_items:
                    break
                url = result.get("url", "")
                if not is_wechat_url(url):
                    continue
                title = clean_title(result.get("title", ""))
                if not title or len(title) < 5:
                    continue
                items.append({
                    "title": title,
                    "url": url,
                    "summary": (result.get("content", "") or "")[:200],
                })
        except Exception as e:
            logger.debug("  SearXNG %s 失败: %s", instance, e)
            continue

    if items:
        logger.info("  SearXNG: %d 篇", len(items))
    return items


# ============================================================
# 数据源 3: Bing 搜索（备胎）
# ============================================================

def search_bing(query: str, max_items: int = 5) -> list:
    """使用 Bing 搜索"""
    session = make_session()
    items = []
    try:
        resp = session.get(
            "https://www.bing.com/search",
            params={"q": query, "setlang": "zh-cn", "count": "10"},
            timeout=15,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for result in soup.select("#b_results li.b_algo, .b_algo"):
                if len(items) >= max_items:
                    break
                title_el = result.select_one("h2 a")
                if not title_el:
                    continue
                url = title_el.get("href", "")
                if not is_wechat_url(url):
                    continue
                title = clean_title(title_el.get_text(strip=True))
                if not title or len(title) < 5:
                    continue
                summary_el = result.select_one(".b_caption p, .sn_b_desc")
                summary = summary_el.get_text(strip=True)[:200] if summary_el else ""
                items.append({"title": title, "url": url, "summary": summary})
    except Exception as e:
        logger.warning("  Bing 异常: %s", e)
    if items:
        logger.info("  Bing: %d 篇", len(items))
    return items


# ============================================================
# 聚合抓取
# ============================================================

def fetch_account_articles(account: dict, max_items: int = 5) -> list:
    """从多个搜索引擎抓取单个公众号的文章"""
    name = account["name"]
    logger.info("正在搜索 [%s]...", name)

    query = f"site:mp.weixin.qq.com {name}"
    all_items = []

    # 1. DuckDuckGo（主力）
    items = search_duckduckgo(query, max_items)
    all_items.extend(items)

    # 2. SearXNG（兜底 1）
    if len(all_items) < max_items:
        items2 = search_searxng(query, max_items - len(all_items))
        all_items.extend(items2)

    # 3. Bing（兜底 2）
    if len(all_items) < max_items:
        items3 = search_bing(query, max_items - len(all_items))
        all_items.extend(items3)

    # 去重
    seen = set()
    unique = []
    for a in all_items:
        key = a["title"][:30]
        if key not in seen:
            seen.add(key)
            unique.append(a)

    result = unique[:max_items]
    if result:
        logger.info("  ✓ 共 %d 篇", len(result))
    else:
        logger.warning("  ✗ 未找到文章")
    return result


# ============================================================
# AI 分析（DeepSeek）
# ============================================================

def analyze_with_deepseek(api_key: str, source_data: dict) -> Optional[str]:
    if not api_key:
        return None

    lines = [f"昨日（{YESTERDAY_STR}）微信公众号文章汇总：", ""]
    for acc_name, articles in source_data.items():
        if not articles:
            continue
        lines.append(f"--- {acc_name} ---")
        for a in articles:
            t = a["title"]
            s = a.get("summary", "")
            lines.append(f"- {t}" + (f"：{s[:100]}" if s else ""))
        lines.append("")

    news_text = "\n".join(lines)
    if len(news_text) < 50:
        return None

    prompt = f"""你是一个政务要闻分析助手。请分析以下昨日发布的政务/警务公众号文章：

{news_text}

按以下格式输出（每条不超过 50 字）：

📌 **昨日要闻概述**
（2-3句话概括）

🔍 **分类解读**
• 类别：内容

📰 **各号要点**
• @公众号：要点

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
    lines.append(f"📊 汇总 {YESTERDAY_STR} 四个公众号要闻")
    lines.append("")

    if ai_analysis:
        lines.append("━━━ 🤖 AI 分析解读 ━━━")
        lines.append(ai_analysis)
        lines.append("")

    total = sum(len(v) for v in source_data.values())
    if total == 0:
        lines.append("⚠️ 暂未获取到昨日要闻。")
        return "\n".join(lines)

    for acc_name, articles in source_data.items():
        if not articles:
            continue
        lines.append(f"━━━ {acc_name} ━━━")
        for i, a in enumerate(articles[:5], 1):
            title = a["title"]
            lines.append(f"{i}. {title[:45] + '…' if len(title) > 45 else title}")
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
    max_per = int(os.environ.get("MAX_NEWS_PER_SOURCE", "5"))

    logger.info("=" * 50)
    logger.info("舟山警事早报")
    logger.info(f"分析日期: {YESTERDAY_STR}")
    logger.info(f"公众号: {', '.join(a['name'] for a in WECHAT_ACCOUNTS)}")
    logger.info(f"搜索引擎: DuckDuckGo → SearXNG → Bing")
    logger.info("=" * 50)

    source_data = {}
    total = 0

    for acc in WECHAT_ACCOUNTS:
        articles = fetch_account_articles(acc, max_per)
        source_data[f"{acc['icon']} {acc['name']}"] = articles
        total += len(articles)

    logger.info(f"\n共获取 {total} 篇文章")

    ai = None
    if deepseek_key and total >= 3:
        logger.info("正在 AI 分析...")
        ai = analyze_with_deepseek(deepseek_key, source_data)

    title = f"舟山警事早报 {YESTERDAY_STR}"
    content = build_report(source_data, ai)
    print("\n" + content + "\n")

    if total > 0:
        send_serverchan3(sendkey, title, content)
    else:
        logger.warning("无文章，跳过推送")


if __name__ == "__main__":
    main()
