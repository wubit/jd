"""
1988ck.cc 全分类视频爬虫 - 按分类爬取m3u8链接，生成TVBox格式列表
用法: python3 scrape_1988ck.py
依赖: pip install aiohttp
输出: tvbox.m3u (TVBox格式，按分类+日期降序分页)
"""

import asyncio
import aiohttp
import re
import json
import math
import time
import sys

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ==================== 配置 ====================
BASE = "http://1988ck.cc"

# 视频分类 (ID, 名称, 总页数)
# 如需只爬部分分类，注释掉不需要的行即可
CATEGORIES = [
    (8,  "无码中文字幕", 113),
    (9,  "有码中文字幕", 846),
    (10, "日本无码",     319),
    (7,  "日本有码",     885),
    (26, "骑兵破解",      22),
    (15, "国产视频",     971),
    (21, "欧美高清",     131),
    (22, "动漫剧情",       7),
]

CONCURRENT_LIST = 30           # 列表页并发数
CONCURRENT_PLAY = 50           # 播放页并发数
PER_PAGE = 100                 # TVBox每页条数
OUTPUT_FILE = "tvbox.m3u"      # 输出文件名
TIMEOUT = aiohttp.ClientTimeout(total=30)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}
# ================================================


async def fetch(session, url, sem, retries=3):
    """带重试的异步HTTP请求"""
    for attempt in range(retries):
        try:
            async with sem:
                async with session.get(url, timeout=TIMEOUT, headers=HEADERS) as resp:
                    return await resp.text(errors='replace')
        except Exception as e:
            if attempt == retries - 1:
                return ""
            await asyncio.sleep(1)


def extract_title(html):
    """从播放页HTML提取视频标题"""
    h3s = re.findall(r'<h3 class="title">(.*?)</h3>', html)
    for h in h3s:
        h = h.strip()
        if h and h != "目录" and h != "为你推荐":
            return h.replace(",", "，").replace("\n", " ").strip()
    m = re.search(r'<title>(.*?)(?:详情|在线|迅雷|-)', html)
    if m:
        return m.group(1).replace(",", "，").strip()
    return ""


def extract_date(entry_url):
    """从m3u8链接提取日期用于排序"""
    m = re.search(r'/video/m3u8/(\d{4})/(\d{2})/(\d{2})/', entry_url)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    return "00000000"


async def scrape_listing_pages(session, cat_id, total_pages):
    """爬取某分类的所有列表页，获取播放页URL"""
    sem = asyncio.Semaphore(CONCURRENT_LIST)
    all_vodplay = set()

    for batch_start in range(1, total_pages + 1, 100):
        batch_end = min(batch_start + 100, total_pages + 1)
        urls = [f"{BASE}/vodtype/{cat_id}-{i}.html"
                for i in range(batch_start, batch_end)]
        tasks = [fetch(session, url, sem) for url in urls]
        results = await asyncio.gather(*tasks)

        for html in results:
            if html:
                links = re.findall(
                    r'href=["\'](?:http://1988ck\.cc)?(/vodplay/\d+-1-1\.html)["\']',
                    html
                )
                for link in links:
                    all_vodplay.add(link)

        print(f"    列表页 {batch_start}-{batch_end - 1}: "
              f"累计 {len(all_vodplay)} 个链接")

    return sorted(all_vodplay)


async def scrape_play_pages(session, vodplay_urls, label=""):
    """爬取播放页，提取标题和m3u8链接"""
    sem = asyncio.Semaphore(CONCURRENT_PLAY)
    results_list = []
    failed = []

    for batch_start in range(0, len(vodplay_urls), 200):
        batch = vodplay_urls[batch_start:batch_start + 200]
        full_urls = [f"{BASE}{path}" for path in batch]
        tasks = [fetch(session, url, sem) for url in full_urls]
        results = await asyncio.gather(*tasks)

        for i, html in enumerate(results):
            if not html:
                failed.append(batch[i])
                continue

            m = re.search(r'player_aaaa\s*=\s*(\{[^}]+\})', html)
            if not m:
                failed.append(batch[i])
                continue
            try:
                data = json.loads(m.group(1))
                url = data.get("url", "")
                if not url or ".m3u8" not in url:
                    failed.append(batch[i])
                    continue
            except json.JSONDecodeError:
                failed.append(batch[i])
                continue

            title = extract_title(html)
            if not title:
                title = batch[i].split("/")[-1].replace(".html", "")
            results_list.append((title, url))

        done = min(batch_start + 200, len(vodplay_urls))
        print(f"    {label}{done}/{len(vodplay_urls)}，"
              f"已获取 {len(results_list)} 条")

    return results_list, failed


async def scrape_category(session, cat_id, cat_name, total_pages):
    """爬取单个分类的所有视频"""
    print(f"\n  [列表页] 爬取 {total_pages} 页...")
    vodplay_urls = await scrape_listing_pages(session, cat_id, total_pages)
    print(f"  共 {len(vodplay_urls)} 个播放页链接")

    print(f"  [播放页] 爬取 {len(vodplay_urls)} 个...")
    results, failed = await scrape_play_pages(session, vodplay_urls)
    print(f"  成功 {len(results)} 条，失败 {len(failed)} 条")

    if failed:
        print(f"  [重试] {len(failed)} 个失败页面...")
        retry_results, _ = await scrape_play_pages(session, failed, label="重试 ")
        results.extend(retry_results)
        print(f"  补回 {len(retry_results)} 条")

    # 按日期降序排序
    results.sort(key=lambda x: extract_date(x[1]), reverse=True)
    return results


async def main():
    t0 = time.time()
    connector = aiohttp.TCPConnector(limit=60, force_close=True)
    all_categories_data = {}  # {cat_name: [(title, url), ...]}

    async with aiohttp.ClientSession(connector=connector) as session:
        for idx, (cat_id, cat_name, total_pages) in enumerate(CATEGORIES, 1):
            print(f"\n{'='*60}")
            print(f"[{idx}/{len(CATEGORIES)}] 分类: {cat_name} "
                  f"(ID={cat_id}, {total_pages}页)")
            print(f"{'='*60}")

            results = await scrape_category(
                session, cat_id, cat_name, total_pages
            )
            all_categories_data[cat_name] = results
            print(f"  => {cat_name}: {len(results)} 条视频")

    # 生成TVBox格式：按分类分组，每分类内按日期降序分页
    print(f"\n{'='*60}")
    print(f"[输出] 生成TVBox格式...")
    total_entries = 0

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for cat_name, entries in all_categories_data.items():
            if not entries:
                continue
            pages = math.ceil(len(entries) / PER_PAGE)
            for page in range(pages):
                start = page * PER_PAGE
                end = min(start + PER_PAGE, len(entries))
                f.write(f"{cat_name}第{page + 1}页,#genre#\n")
                for i in range(start, end):
                    title, url = entries[i]
                    f.write(f"{title},{url}\n")
            total_entries += len(entries)
            print(f"  {cat_name}: {len(entries)} 条, {pages} 页")

    elapsed = time.time() - t0
    print(f"\n[完成] 共 {total_entries} 条视频")
    print(f"  输出文件: {OUTPUT_FILE}")
    print(f"  总耗时: {elapsed:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
