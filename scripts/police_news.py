#!/usr/bin/env python3
"""
舟山警事早报 — 微信公众号要闻推送

每天早上 07:30 自动抓取前一天四个公众号的文章，
AI 分析总结后推送微信。

公众号源（通过搜索引擎抓取 mp.weixin.qq.com 索引）：
  - 嵊泗列岛先锋
  - 嵊泗公安
  - 舟山公安
  - 浙江公安

环境变量：
    SERVERCHAN3_SENDKEY  - 必填，推送密钥
    DEEPSEEK_API_KEY     - 可选，开启 AI 分析
    MAX_NEWS_PER_SOURCE  - 可选，每个号最多条数（默认 5）
"""

import os
import re
import sys
import json
import logging
from datetime import date, datetime, timedelta
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

YESTERDAY = date.today() - timedelta(days=1)
YESTERDAY_STR = YESTERDAY.strftime("%Y-%m-%d")

# 要追踪的公众号
WECHAT_ACCOUNTS = [
    {"name": "嵊泗列岛先锋", "icon": "🚩"},
    {"name": "嵊泗公安", "icon": "🏛️"},
    {"name": "舟山公安", "icon": "🏛️"},
    {"name": "浙江公安", "icon": "🏛️"},
]


# ============================================================
# 搜索引擎查找微信文章
# ============================================================

def search_google_wechat(account_name: str, max_items: int = 5) -> list:
    """通过 Google 搜索查找公众号文章（site:mp.weixin.qq.com）"""
    query = f"site:mp.weixin.qq.com {account_name}"
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/125.0.0.0 Safari/537.36")

    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.google.com/",
    })

    items = []

    # ===== Google =====
    try:
        params = {"q": query, "hl": "zh-CN", "num": str(min(max_items * 2, 10))}
        resp = session.get(
            "https://www.google.com/search",
            params=params,
            timeout=15,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Google 搜索结果容器
            for sel in [
                "div.g",                      # 经典布局
                "div[data-hveid]",             # 新版
                "div.kCrYT",                   # 移动版/旧版
                "div.yuRUbf",                  # 2024+
                ".MjjYud",                     # 2025+
            ]:
                results = soup.select(sel)
                if results:
                    break

            for result in results[:max_items * 2]:
                if len(items) >= max_items:
                    break
                title_el = result.select_one("h3 a, a[href*='mp.weixin.qq.com']")
                if not title_el:
                    title_el = result.select_one("a")
                if not title_el:
                    continue

                url = title_el.get("href", "")
                # Google 搜索结果 URL 有时是 /url?q=... 格式
                url = extract_google_url(url)
                if not url or "mp.weixin.qq.com" not in url:
                    continue

                title = title_el.get_text(strip=True)
                if not title or len(title) < 5:
                    continue

                # 摘要
                summary = ""
                for sel2 in [".st", ".lEBKkf", ".VwiC3b", "span.aCOpRe", ".fYyStc"]:
                    el = result.select_one(sel2)
                    if el:
                        summary = el.get_text(strip=True)
                        break

                items.append({
                    "title": re.sub(r"\s+", " ", title).strip(),
                    "url": url,
                    "summary": summary[:200] if summary else "",
                    "source": "Google",
                })
    except Exception as e:
        logger.warning("  Google 搜索异常: %s", e)

    return items[:max_items]


def search_bing_wechat(account_name: str, max_items: int = 5) -> list:
    """通过 Bing 搜索查找公众号文章（site:mp.weixin.qq.com）"""
    query = f"site:mp.weixin.qq.com {account_name}"
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/125.0.0.0 Safari/537.36")

    items = []
    try:
        resp = requests.get(
            "https://www.bing.com/search",
            params={"q": query, "setlang": "zh-cn", "count": "10"},
            headers={
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.bing.com/",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            results = soup.select("#b_results li.b_algo, .b_algo, .b_caption")
            if not results:
                # 尝试其他选择器
                results = soup.select("li.b_algo")

            for result in results[:max_items * 2]:
                if len(items) >= max_items:
                    break
                title_el = result.select_one("h2 a")
                if not title_el:
                    continue
                url = title_el.get("href", "")
                if "mp.weixin.qq.com" not in url:
                    continue
                title = title_el.get_text(strip=True)
                if not title or len(title) < 5:
                    continue
                summary_el = result.select_one(".b_caption p, .sn_b_desc")
                summary = summary_el.get_text(strip=True)[:200] if summary_el else ""
                items.append({
                    "title": re.sub(r"\s+", " ", title).strip(),
                    "url": url,
                    "summary": summary,
                    "source": "Bing",
                })
    except Exception as e:
        logger.warning("  Bing 搜索异常: %s", e)

    return items[:max_items]


def extract_google_url(url: str) -> str:
    """从 Google 搜索结果中提取真实 URL"""
    # 处理 /url?q=xxx&sa=... 格式
    match = re.search(r"/url\?q=([^&]+)", url)
    if match:
        from urllib.parse import unquote
        return unquote(match.group(1))
    return url


def fetch_account_articles(account: dict, max_items: int = 5) -> list:
    """抓取单个公众号的文章"""
    name = account["name"]
    icon = account.get("icon", "📰")
    logger.info("正在搜索 [%s]...", name)

    all_items = []

    # 1. Google 搜索
    items = search_google_wechat(name, max_items)
    logger.info("  Google: %d 篇", len(items))
    all_items.extend(items)

    # 2. Google 没结果 -> Bing 搜索
    if len(all_items) < max_items:
        items2 = search_bing_wechat(name, max_items)
        logger.info("  Bing: %d 篇", len(items2))
        all_items.extend(items2)

    # 去重
    seen = set()
    unique = []
    for a in all_items:
        key = a["title"][:30]
        if key not in seen:
            seen.add(key)
            unique.append(a)

    logger.info("  => 共 %d 篇", len(unique[:max_items]))
    return unique[:max_items]


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
            if s:
                lines.append(f"- {t}：{s[:100]}")
            else:
                lines.append(f"- {t}")
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
        else:
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
            if len(title) > 45:
                title = title[:45] + "…"
            lines.append(f"{i}. {title}")
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
        logger.error("推送失败: %s", res.get("message", "未知错误"))
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
    logger.info("舟山警事早报 — 微信公众号文章搜索")
    logger.info(f"分析日期: {YESTERDAY_STR}")
    logger.info(f"公众号: {', '.join(a['name'] for a in WECHAT_ACCOUNTS)}")
    logger.info("=" * 50)

    source_data = {}
    total = 0

    for acc in WECHAT_ACCOUNTS:
        articles = fetch_account_articles(acc, max_per)
        name = f"{acc['icon']} {acc['name']}"
        source_data[name] = articles
        total += len(articles)

    logger.info(f"\n共获取 {total} 篇文章")

    # AI 分析
    ai = None
    if deepseek_key and total >= 3:
        logger.info("正在 AI 分析...")
        ai = analyze_with_deepseek(deepseek_key, source_data)

    # 构建并推送
    title = f"舟山警事早报 {YESTERDAY_STR}"
    content = build_report(source_data, ai)
    print("\n" + content + "\n")

    if total > 0:
        send_serverchan3(sendkey, title, content)
    else:
        logger.warning("跳过推送")


if __name__ == "__main__":
    main()
