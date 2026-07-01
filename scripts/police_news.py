#!/usr/bin/env python3
"""
舟山警事早报 — 微信公众号要闻推送

每天早上 07:30 自动抓取前一天四个公众号的要闻，
AI 分析总结后推送微信。

公众号源：
  - 嵊泗列岛先锋
  - 嵊泗公安
  - 舟山公安
  - 浙江公安

数据来源：搜狗微信搜索（无需登录）

环境变量：
    SERVERCHAN3_SENDKEY  - 必填，推送密钥
    DEEPSEEK_API_KEY     - 可选，开启 AI 分析
"""

import os
import re
import sys
import json
import logging
import hashlib
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

# 要追踪的公众号
WECHAT_ACCOUNTS = [
    {"name": "嵊泗列岛先锋", "icon": "🚩"},
    {"name": "嵊泗公安", "icon": "🏛️"},
    {"name": "舟山公安", "icon": "🏛️"},
    {"name": "浙江公安", "icon": "🏛️"},
]

YESTERDAY = date.today() - timedelta(days=1)
YESTERDAY_STR = YESTERDAY.strftime("%Y-%m-%d")


# ============================================================
# 搜狗微信搜索
# ============================================================

def sogou_wechat_search(account_name: str, max_items: int = 5) -> list:
    """
    在搜狗微信搜索中查找公众号最新文章

    策略：
      1. 先按公众号名称搜索(type=2)，从结果页提取最新文章
      2. 如果搜不到，按文章关键词搜索(type=1)兜底
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://weixin.sogou.com/",
    }

    session = requests.Session()
    session.headers.update(headers)

    # 先访问首页拿 cookie
    session.get("https://weixin.sogou.com/", timeout=10)

    articles = []

    # 方法 1: 按公众号搜索 (type=2)
    logger.info("  搜狗搜索公众号 [%s]...", account_name)
    try:
        params = {"type": "2", "query": account_name, "ie": "utf8"}
        resp = session.get(
            "https://weixin.sogou.com/weixin",
            params=params,
            timeout=15,
        )
        if resp.status_code == 200:
            items = parse_account_page(resp.text, account_name, max_items)
            if items:
                logger.info("  找到 %d 篇公众号文章", len(items))
                articles.extend(items)
    except Exception as e:
        logger.warning("  搜狗公众号搜索异常: %s", e)

    # 方法 2: 文章搜索兜底 (type=1)
    if len(articles) < max_items:
        logger.info("  搜狗文章搜索 [%s]...", account_name)
        try:
            params = {"type": "1", "query": account_name, "ie": "utf8"}
            resp = session.get(
                "https://weixin.sogou.com/weixin",
                params=params,
                timeout=15,
            )
            if resp.status_code == 200:
                items = parse_article_page(resp.text, account_name, max_items - len(articles))
                if items:
                    logger.info("  文章搜索找到 %d 篇", len(items))
                    # 只取该公众号发布的
                    articles.extend(items)
        except Exception as e:
            logger.warning("  搜狗文章搜索异常: %s", e)

    # 去重
    seen = set()
    unique = []
    for a in articles:
        key = a["title"][:30]
        if key not in seen:
            seen.add(key)
            unique.append(a)
    return unique[:max_items]


def parse_account_page(html: str, account_name: str, max_items: int) -> list:
    """解析公众号搜索结果的 HTML（type=2）"""
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 尝试多种可能的选择器
    for container_sel in [".news-box2", ".news-box", ".main"]:
        container = soup.select_one(container_sel)
        if container:
            break

    if not container:
        container = soup

    # 每个公众号结果卡片
    cards = container.select(".news-list2 li, .wx-rb-item, .rb-item")
    for card in cards[:3]:  # 只检查前 3 个结果
        # 确认是这个公众号
        name_el = card.select_one(
            ".account, .wx-rb__name, .rb-name, .news-title2 a, .tit a"
        )
        card_name = ""
        if name_el:
            card_name = name_el.get_text(strip=True)
        # 如果卡片里有公众号名称匹配
        if account_name not in card_name and card_name:
            continue

        # 提取文章列表
        article_els = card.select(
            ".news-list2 .news-item, .wx-rb__list li, "
            ".rb-list li, .article-list li, ul li"
        )
        if not article_els:
            # 可能直接就是文章条目
            article_els = [card]

        for art in article_els[:max_items]:
            item = extract_sogou_article_item(art)
            if item and len(items) < max_items:
                items.append(item)

    return items


def parse_article_page(html: str, account_name: str, max_items: int) -> list:
    """解析文章搜索结果的 HTML（type=1）"""
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for art_sel in [
        ".news-list li",
        ".results .result",
        ".news-box .news-item",
    ]:
        articles = soup.select(art_sel)
        if articles:
            break

    for art in articles[:max_items * 2]:
        # 检查公众号名称
        account_el = art.select_one(
            ".account, .s-p, .news-from, .source, .info .name, em"
        )
        if account_el:
            text = account_el.get_text(strip=True)
            if account_name not in text:
                continue

        item = extract_sogou_article_item(art)
        if item and len(items) < max_items:
            items.append(item)

    return items


def extract_sogou_article_item(art_elem) -> Optional[dict]:
    """从搜狗搜索结果中提取单篇文章信息"""
    # 标题
    title_el = art_elem.select_one(
        "h3 a, .tit a, .wx-rb__title a, "
        ".rb-title a, .article-title a, .news-title a, .txt-box h3 a"
    )
    if not title_el:
        return None
    title = title_el.get_text(strip=True)
    if not title or len(title) < 3:
        return None

    href = title_el.get("href", "")
    if href and not href.startswith("http"):
        href = "https://weixin.sogou.com" + href

    # 摘要
    summary = ""
    for sel in [
        ".txt-box p", ".s-p", ".summary", ".desc, .wx-rb__summary",
        ".rb-summary", ".article-summary", ".news-des",
    ]:
        el = art_elem.select_one(sel)
        if el:
            summary = el.get_text(strip=True)
            break

    # 日期
    pub_date = ""
    for sel in [
        ".time", ".s-t", ".date", ".wx-rb__time",
        ".rb-time", ".article-date",
    ]:
        el = art_elem.select_one(sel)
        if el:
            pub_date = el.get_text(strip=True)
            # 可能是相对时间 "3天前" "昨天"
            pub_date = normalize_date(pub_date)
            break

    return {
        "title": re.sub(r"\s+", " ", title).strip(),
        "url": href,
        "summary": re.sub(r"\s+", " ", summary).strip()[:200] if summary else "",
        "date": pub_date,
    }


def normalize_date(date_str: str) -> str:
    """把模糊日期转为标准格式"""
    date_str = date_str.strip()
    if not date_str:
        return ""
    if "分钟前" in date_str or "小时前" in date_str:
        return YESTERDAY_STR
    if "昨天" in date_str:
        return YESTERDAY_STR
    if "前天" in date_str:
        return (YESTERDAY - timedelta(days=1)).strftime("%Y-%m-%d")
    # 格式如 "2026-06-30" 或 "06-30"
    match = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", date_str)
    if match:
        return match.group(1).replace("/", "-")
    match = re.search(r"(\d{1,2}[-/]\d{1,2})", date_str)
    if match:
        return f"{date.today().year}-{match.group(1).replace('/', '-')}"
    return date_str


# ============================================================
# 搜索引引擎兜底（Bing/Baidu）
# ============================================================

def search_wechat_articles(account_name: str, max_items: int = 5) -> list:
    """通过搜索引擎查找公众号文章"""
    items = []
    query = f"site:mp.weixin.qq.com {account_name}"

    # 尝试 Bing
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
            ),
        }
        resp = requests.get(
            "https://www.bing.com/search",
            params={"q": query, "setlang": "zh-cn"},
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for result in soup.select("#b_results li.b_algo")[:max_items]:
                title_el = result.select_one("h2 a")
                summary_el = result.select_one(".b_caption p")
                if title_el:
                    title = title_el.get_text(strip=True)
                    url = title_el.get("href", "")
                    summary = summary_el.get_text(strip=True) if summary_el else ""
                    # 只保留微信文章
                    if "mp.weixin.qq.com" in url:
                        items.append({
                            "title": title,
                            "url": url,
                            "summary": summary[:200] if summary else "",
                            "date": "",
                        })
    except Exception as e:
        logger.warning("  Bing 搜索异常: %s", e)

    return items[:max_items]


# ============================================================
# AI 分析（DeepSeek）
# ============================================================

def analyze_with_deepseek(api_key: str, source_data: dict) -> Optional[str]:
    """调用 DeepSeek 对新闻进行 AI 分析总结"""
    if not api_key:
        return None

    # 整理新闻文本
    lines = [f"昨日（{YESTERDAY_STR}）公众号要闻汇总：", ""]
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

    prompt = f"""你是一个政务要闻分析助手。请对以下微信公众号昨日发布的警务/政务新闻进行分析总结：

{news_text}

按这个格式输出（每项尽量简短，适合手机阅读）：

📌 **昨日要闻概述**
（2-3句话概括整体主题和重要信息）

🔍 **分类解读**
• 类别1：具体内容
• 类别2：具体内容

⚠️ **重点关注**
• 需要留意的事项

各公众号的重要文章也单独提一下：
📰 **各号要点**
• @公众号名：要点
"""

    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是政务要闻分析助手，用简洁中文输出分析。"},
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
            logger.warning("DeepSeek 返回 %d: %s", resp.status_code, resp.text[:100])
            return None
    except Exception as e:
        logger.warning("AI 分析失败: %s", e)
        return None


# ============================================================
# 报告构建 + 推送
# ============================================================

def build_report(source_data: dict, ai_analysis: Optional[str] = None) -> str:
    """构建完整的推送报告"""
    today_cn = date.today().strftime("%Y年%m月%d日")
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]

    lines = []
    lines.append(f"📋 **舟山警事早报**")
    lines.append(f"📅 {today_cn} 星期{weekdays[date.today().weekday()]}")
    lines.append(f"📊 汇总 {YESTERDAY_STR} 四个公众号要闻")

    # 统计
    total = sum(len(v) for v in source_data.values())
    lines.append("")

    # AI 分析
    if ai_analysis:
        lines.append("━━━ 🤖 AI 分析解读 ━━━")
        lines.append(ai_analysis)
        lines.append("")

    if total == 0:
        lines.append("⚠️ 暂未获取到昨日要闻。")
        return "\n".join(lines)

    # 各公众号文章
    for acc_name, articles in source_data.items():
        if not articles:
            lines.append(f"━━━ {acc_name} ━━━")
            lines.append("（暂未获取到文章）")
            lines.append("")
            continue
        lines.append(f"━━━ {acc_name} ━━━")
        for i, a in enumerate(articles[:5], 1):
            title = a["title"]
            if len(title) > 45:
                title = title[:45] + "…"
            date_info = f" [{a['date']}]" if a.get("date") else ""
            lines.append(f"{i}. {title}{date_info}")
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
            logger.info("推送成功！")
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
    logger.info("微信公众号要闻抓取")
    logger.info(f"分析日期: {YESTERDAY_STR}")
    logger.info("=" * 50)

    source_data = {}
    total = 0

    for acc in WECHAT_ACCOUNTS:
        name = acc["name"]
        icon = acc.get("icon", "📰")
        logger.info("正在搜索 [%s]...", name)

        # 先走搜狗
        articles = sogou_wechat_search(name, max_per)

        # 搜狗没抓到，试搜索引擎
        if not articles:
            logger.info("  搜狗无结果，尝试搜索引擎兜底...")
            articles = search_wechat_articles(name, max_per)

        key = f"{icon} {name}"
        source_data[key] = articles
        total += len(articles)
        if articles:
            logger.info(f"  ✓ 获取到 {len(articles)} 篇文章")
        else:
            logger.warning(f"  ✗ 未获取到文章")

    logger.info("共获取 %d 篇文章", total)

    # AI 分析
    ai = None
    if deepseek_key and total > 0:
        logger.info("正在 AI 分析...")
        ai = analyze_with_deepseek(deepseek_key, source_data)

    # 构建并推送
    title = f"舟山警事早报 {YESTERDAY_STR}"
    content = build_report(source_data, ai)
    print("\n" + content + "\n")

    if total > 0:
        send_serverchan3(sendkey, title, content)
    else:
        logger.warning("所有公众号均未获取到文章，跳过推送")


if __name__ == "__main__":
    main()
