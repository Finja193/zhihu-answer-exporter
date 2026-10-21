# -*- coding: utf-8 -*-
"""
Zhihu Topic Top-Answers Exporter

按话题抓取「精选」回答并导出 CSV（UTF-8）。
示例：https://www.zhihu.com/topic/19553148/top-answers

用法:
    python topic_scraper.py --topic 19553148 --target 2000
    python topic_scraper.py --topic 19553148 --target 2000 --out ./output

输出字段:
    博主名称、博主认证类型、答主名称、答主认证类型、问题标题、发布时间、
    正文、话题标签、赞同数、评论数、收藏数、点赞数、答案链接

实现要点:
    话题 feed 接口（/api/v5.1/topics/{id}/feeds/top_activity/v2）带 x-zse-96
    签名校验，无法用 requests 直接构造。这里先用 Playwright 打开一次话题页，
    从真实请求里截取签名头，再用该签名头驱动 requests 翻页，避免整场滚动页面。
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from zhihu_scraper import (  # 复用已有登录 / Cookie / 清洗逻辑
    OUTPUT_DIR,
    ensure_login,
    load_cookies,
    strip_html,
)

# ==================== 接口 ====================
TOPIC_FEED_API = "https://www.zhihu.com/api/v5.1/topics/{topic_id}/feeds/top_activity/v2"
QUESTION_API = "https://www.zhihu.com/api/v4/questions/{qid}?include=author,topics"
ANSWER_API = "https://www.zhihu.com/api/v4/answers/{aid}?include=thanks_count,voteup_count,comment_count,favlists_count"
CHILDREN_API = "https://www.zhihu.com/api/v4/topics/{topic_id}/children"
TOPIC_DETAIL_API = "https://www.zhihu.com/api/v4/topics/{topic_id}"

TOPIC_PAGE = "https://www.zhihu.com/topic/{topic_id}/top-answers"

# 单个话题的 feed 只能翻约 50-60 页（offset 1000~1200），这是服务端深度上限。
# 要拿更多数据必须合并主话题与其子话题的结果。
MAX_FEED_PAGES = 80

FEED_LIMIT = 20           # 单次翻页条数（接口上限 20）
ENCODING = "utf-8-sig"    # UTF-8 带 BOM，Excel 直接打开不乱码

# 核心字段：用于统计缺失率
CORE_FIELDS = ["博主名称", "答主名称", "问题标题", "发布时间", "正文"]

COLUMNS = [
    "博主名称", "博主认证类型", "答主名称", "答主认证类型",
    "问题标题", "发布时间", "正文", "话题标签",
    "赞同数", "评论数", "收藏数", "点赞数", "答案链接",
]


# ==================== 签名 ====================
def capture_signature(topic_id, cookies_dict, timeout=90):
    """打开话题页，从真实请求里截取 x-zse-96 签名头。

    该签名可跨 URL 复用（实测对 feed 翻页与问题详情接口均有效），
    因此只需抓取一次，后续请求全部由 requests 发起。
    """
    ctx_cookies = [
        {"name": k, "value": v, "domain": ".zhihu.com", "path": "/"}
        for k, v in cookies_dict.items()
    ]
    result = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_cookies(ctx_cookies)
        page = context.new_page()

        def on_response(resp):
            if result or "top_activity/v2" not in resp.url:
                return
            headers = {k.lower(): v for k, v in resp.request.headers.items()}
            if headers.get("x-zse-96"):
                result.update({
                    "x-zse-96": headers["x-zse-96"],
                    "x-zse-93": headers.get("x-zse-93", ""),
                    "user-agent": headers.get("user-agent", ""),
                })

        page.on("response", on_response)
        try:
            page.goto(TOPIC_PAGE.format(topic_id=topic_id), wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass

        deadline = time.time() + timeout
        scrolled = False
        while time.time() < deadline and not result:
            page.wait_for_timeout(500)
            # 首屏是 SSR 渲染，滚一下才会发出带签名的 feed 请求
            if not scrolled and time.time() > deadline - timeout + 4:
                try:
                    page.mouse.wheel(0, 100000)
                except Exception:
                    pass
                scrolled = True
        browser.close()

    return result


def build_session(cookies_dict, sig, referer):
    session = requests.Session()
    session.headers.update({
        "User-Agent": sig.get("user-agent") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": referer,
        "x-requested-with": "fetch",
        "x-zse-96": sig.get("x-zse-96", ""),
        "x-zse-93": sig.get("x-zse-93", ""),
    })
    session.cookies.update(cookies_dict)
    return session


def _request_json(session, url, referer=None):
    """GET 一个接口，返回 (ok, data|None, status)"""
    try:
        if referer:
            session.headers["Referer"] = referer
        resp = session.get(url, timeout=20)
    except Exception as e:
        return False, None, str(e)

    if resp.status_code != 200:
        msg = resp.text[:120]
        try:
            msg = resp.json().get("error", {}).get("message", msg)
        except Exception:
            pass
        return False, None, f"HTTP {resp.status_code} {msg}"

    try:
        return True, resp.json(), 200
    except Exception:
        return False, None, "JSON parse failed"


# ==================== 字段解析 ====================
def drop_redundant(labels):
    """去掉被别的标签完整包含的短标签。

    知乎会把「医学话题下的优秀答主」和泛化的「优秀答主」一起返回，
    后者是前者的子串，属于重复信息。
    """
    return [a for a in labels if not any(a != b and a in b for b in labels)]


def parse_auth_type(author):
    """从作者对象解析认证类型。

    知乎认证信息分散在 badge / badge_v2.merged_badges / detail_badges 中；
    badge_v2.title 在未认证时会退化成个人签名（headline），需要排除。
    """
    if not author:
        return ""
    headline = (author.get("headline") or "").strip()
    badge_v2 = author.get("badge_v2") or {}

    def add(label):
        label = (label or "").strip()
        if label and label != headline and label not in labels:
            labels.append(label)

    labels = []
    # badge_v2.title 是最完整的认证描述；未认证时它会退化成个人签名，需排除
    add(badge_v2.get("title"))
    for b in badge_v2.get("detail_badges") or []:
        add(b.get("description"))
    for b in author.get("badge") or []:
        add(b.get("description"))
    # merged_badges.title 多为「认证」「优秀答主」这类泛化标签，仅作兜底
    if not labels:
        for b in badge_v2.get("merged_badges") or []:
            add(b.get("title"))

    labels = drop_redundant(labels)

    is_org = bool(author.get("is_org")) or author.get("user_type") == "org"
    kind = "机构" if is_org else "个人"

    if labels:
        return "已认证·" + "、".join(labels[:3])
    return kind + "·未认证"


def parse_topics(question):
    names = []
    for t in (question or {}).get("topics") or []:
        name = (t.get("name") or "").strip()
        if name:
            names.append(name)
    return "|".join(names)


def fmt_time(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _is_missing(value):
    return value is None or str(value).strip() == ""


# ==================== 主流程 ====================
def _prepare_session(topic_id, referer, emit):
    """登录 → 截取签名 → 构造带自动重试的 session"""
    emit("[登录] 检查 Cookie ...")
    cookies = load_cookies()
    if not cookies:
        cookies = ensure_login()
    if not cookies:
        raise RuntimeError("登录失败，未获取到 Cookie")

    emit("[签名] 打开话题页截取 x-zse-96 ...")
    sig = capture_signature(topic_id, cookies)
    if not sig.get("x-zse-96"):
        raise RuntimeError("未能获取接口签名，请检查网络或重新登录后重试")
    emit("[签名] 已获取，开始抓取")

    session = build_session(cookies, sig, referer)

    def refresh_sig():
        emit("[签名] 已失效，重新截取 ...")
        new_sig = capture_signature(topic_id, cookies)
        if not new_sig.get("x-zse-96"):
            return False
        session.headers["x-zse-96"] = new_sig["x-zse-96"]
        session.headers["x-zse-93"] = new_sig.get("x-zse-93", "")
        if new_sig.get("user-agent"):
            session.headers["User-Agent"] = new_sig["user-agent"]
        return True

    def get_json(url, retries=2):
        """带签名失效自动重试的 GET"""
        for attempt in range(retries + 1):
            ok, data, info = _request_json(session, url, referer)
            if ok:
                return data
            if "10003" in str(info) or "403" in str(info):
                if attempt < retries and refresh_sig():
                    continue
            if attempt >= retries:
                emit(f"  [warn] 请求失败 {url[:90]} -> {info}")
                return None
            time.sleep(1.5 * (attempt + 1))
        return None

    return session, get_json


def scrape_topic_answers(topic_id, target=2000, out_dir=None, delay_feed=0.4,
                         delay_detail=0.25, autosave_every=200, progress=None,
                         stop_fn=None):
    """抓取话题精选回答，返回 (csv_path, rows, report)"""
    emit = progress or (lambda msg: print(msg, flush=True))
    out_dir = out_dir or OUTPUT_DIR
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    referer = TOPIC_PAGE.format(topic_id=topic_id)

    # 1~2. 登录 + 取签名
    session, get_json = _prepare_session(topic_id, referer, emit)

    q_cache = {}
    rows = []
    seen = set()
    offset = 0
    written = 0
    feed_fail = 0
    empty_pages = 0
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(out_dir, f"zhihu_topic_{topic_id}_{timestamp}.csv")

    emit(f"[抓取] 目标 {target} 条，话题 {topic_id}")

    while len(rows) < target:
        if stop_fn and stop_fn():
            emit("[停止] 收到停止信号")
            break

        url = f"{TOPIC_FEED_API.format(topic_id=topic_id)}?offset={offset}&limit={FEED_LIMIT}&"
        data = get_json(url)
        offset += FEED_LIMIT

        if not data:
            feed_fail += 1
            emit(f"[抓取] 第 {offset - FEED_LIMIT} 页失败（连续 {feed_fail} 次）")
            if feed_fail >= 3:
                emit("[抓取] 接口连续失败，提前结束")
                break
            time.sleep(2)
            continue
        feed_fail = 0

        items = data.get("data") or []
        if not items:
            # 接口会过滤掉非回答内容，偶尔出现整页为空但后面还有数据，
            # 因此连续多页为空才判定到末页
            empty_pages += 1
            if empty_pages >= 3:
                emit("[抓取] 连续 3 页无内容，已到末页")
                break
            time.sleep(delay_feed)
            continue
        empty_pages = 0

        got = 0
        for item in items:
            tgt = (item or {}).get("target") or {}
            if tgt.get("type") != "answer":
                continue
            answer_id = str(tgt.get("id") or "")
            if not answer_id or answer_id in seen:
                continue
            seen.add(answer_id)

            row = build_row(session, tgt, q_cache, get_json, delay_detail)
            rows.append(row)
            got += 1
            emit(f"[抓取] 已获取 {len(rows)}/{target} | {row['答主名称']} | {row['问题标题'][:24]}")
            if len(rows) >= target:
                break

        if got == 0 and data.get("paging", {}).get("is_end"):
            emit("[抓取] 已到末页")
            break

        if autosave_every and len(rows) - written >= autosave_every:
            write_csv(csv_path, rows)
            written = len(rows)
            emit(f"[保存] 断点已写入 {written} 条 -> {csv_path}")

        time.sleep(delay_feed + random.uniform(0, 0.2))

    write_csv(csv_path, rows)
    report = build_report(rows, topic_id, csv_path)
    report_path = os.path.splitext(csv_path)[0] + "_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    emit(f"[完成] {len(rows)} 条 -> {csv_path}")
    emit(f"[完成] 字段缺失率报告 -> {report_path}")
    return csv_path, rows, report


def write_csv(path, rows):
    """按固定列序写出 CSV（UTF-8 BOM）"""
    with open(path, "w", encoding=ENCODING, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def build_row(session, tgt, q_cache, get_json, delay_detail=0.25, require_tag=None):
    """把一条 feed 里的 answer 对象补齐为一行记录。

    require_tag: 若指定，则话题标签不含该关键词时返回 None（提前丢弃，
    省掉一次答案详情请求）
    """
    answer_id = str(tgt.get("id") or "")
    question = tgt.get("question") or {}
    qid = str(question.get("id") or "")

    # 问题详情：博主 + 话题标签（按 qid 缓存）
    blogger_name = ""
    blogger_auth = ""
    topics = ""
    if qid:
        if qid not in q_cache:
            q_cache[qid] = get_json(QUESTION_API.format(qid=qid))
            time.sleep(delay_detail + random.uniform(0, 0.15))
        qdata = q_cache.get(qid) or {}
        author = qdata.get("author") or {}
        blogger_name = (author.get("name") or "").strip()
        blogger_auth = parse_auth_type(author)
        topics = parse_topics(qdata)

    if require_tag and require_tag not in topics:
        return None

    # 点赞数（感谢数）需单独取答案详情
    like_count = 0
    if answer_id:
        adata = get_json(ANSWER_API.format(aid=answer_id))
        if isinstance(adata, dict):
            like_count = adata.get("thanks_count") or 0
            # 若 feed 未返回计数，用详情兜底
            for key, src in (("voteup_count", "voteup_count"),
                             ("comment_count", "comment_count"),
                             ("favlists_count", "favlists_count")):
                if not tgt.get(key) and adata.get(src):
                    tgt[key] = adata[src]
        time.sleep(delay_detail + random.uniform(0, 0.15))

    answer_url = f"https://www.zhihu.com/question/{qid}/answer/{answer_id}" if qid else ""

    return {
        "博主名称": blogger_name,
        "博主认证类型": blogger_auth,
        "答主名称": (tgt.get("author") or {}).get("name", ""),
        "答主认证类型": parse_auth_type(tgt.get("author")),
        "问题标题": question.get("title", ""),
        "发布时间": fmt_time(tgt.get("created_time")),
        "正文": strip_html(tgt.get("content", "")),
        "话题标签": topics,
        "赞同数": tgt.get("voteup_count", 0),
        "评论数": tgt.get("comment_count", 0),
        "收藏数": tgt.get("favlists_count", 0),
        "点赞数": like_count,
        "答案链接": answer_url,
    }


def fetch_children(get_json, topic_id, limit=20, max_pages=3):
    """取子话题列表 [(id, name)]"""
    out = []
    for page in range(max_pages):
        data = get_json(f"{CHILDREN_API.format(topic_id=topic_id)}?limit={limit}&offset={page * limit}")
        if not data:
            break
        items = data.get("data") or []
        out += [(str(i.get("id")), i.get("name") or "") for i in items if i.get("id")]
        if len(items) < limit:
            break
    return out


def collect_feed(get_json, topic_id, seen, emit, delay_feed, max_pages=MAX_FEED_PAGES):
    """遍历一个话题的精选 feed，返回新增的 answer target 列表"""
    picked = []
    offset = 0
    empty = 0
    fail = 0
    for _ in range(max_pages):
        url = f"{TOPIC_FEED_API.format(topic_id=topic_id)}?offset={offset}&limit={FEED_LIMIT}&"
        offset += FEED_LIMIT
        data = get_json(url)
        if not data:
            fail += 1
            if fail >= 3:
                break
            continue
        fail = 0
        items = data.get("data") or []
        if not items:
            empty += 1
            if empty >= 3:
                break
            time.sleep(delay_feed)
            continue
        empty = 0
        for item in items:
            tgt = (item or {}).get("target") or {}
            if tgt.get("type") != "answer":
                continue
            aid = str(tgt.get("id") or "")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            picked.append(tgt)
        time.sleep(delay_feed + random.uniform(0, 0.2))
    return picked


def scrape_topic_tree(topic_id, target=2000, out_dir=None, keyword=None,
                      collect_ratio=1.8, delay_feed=0.4, delay_detail=0.25,
                      autosave_every=200, progress=None, stop_fn=None):
    """主话题 + 子话题合并抓取，去重后导出 CSV。

    单个话题 feed 受服务端分页深度限制（约 50-60 页），只能取到约 650 条；
    合并主话题与子话题的精选流即可突破该上限。
    """
    emit = progress or (lambda msg: print(msg, flush=True))
    out_dir = out_dir or OUTPUT_DIR
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    referer = TOPIC_PAGE.format(topic_id=topic_id)

    session, get_json = _prepare_session(topic_id, referer, emit)

    if keyword is None:
        detail = get_json(TOPIC_DETAIL_API.format(topic_id=topic_id)) or {}
        keyword = (detail.get("name") or "").strip()
    emit(f"[话题] 主话题关键词: {keyword or '(不过滤)'}")

    children = fetch_children(get_json, topic_id)
    emit(f"[话题] 子话题 {len(children)} 个: " + "、".join(n for _, n in children))

    topic_list = [(str(topic_id), keyword or str(topic_id))] + children
    collect_target = int(target * collect_ratio)

    emit(f"[采集] 目标 {target} 条，先收集约 {collect_target} 条候选")
    seen = set()
    collected = []      # [(来源话题名, answer target)]
    stats = {}
    for tid, name in topic_list:
        if len(collected) >= collect_target or (stop_fn and stop_fn()):
            break
        picked = collect_feed(get_json, tid, seen, emit, delay_feed)
        stats[name] = len(picked)
        collected += [(name, t) for t in picked]
        emit(f"[采集] {name}({tid}) 新增 {len(picked)} 条，候选累计 {len(collected)}")

    emit(f"[采集] 候选 {len(collected)} 条，开始补齐字段")

    q_cache = {}
    rows = []
    written = 0
    skipped = 0
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(out_dir, f"zhihu_topic_{topic_id}_{timestamp}.csv")

    for idx, (tname, tgt) in enumerate(collected, 1):
        if stop_fn and stop_fn():
            emit("[停止] 收到停止信号")
            break
        if len(rows) >= target:
            break
        row = build_row(session, tgt, q_cache, get_json, delay_detail,
                        require_tag=keyword or None)
        if row is None:
            skipped += 1
            continue
        rows.append(row)
        emit(f"[补齐] {len(rows)}/{target} | {row['答主名称']} | {row['问题标题'][:22]}")
        if autosave_every and len(rows) - written >= autosave_every:
            write_csv(csv_path, rows)
            written = len(rows)
            emit(f"[保存] 断点已写入 {written} 条 -> {csv_path}")

    write_csv(csv_path, rows)
    report = build_report(rows, topic_id, csv_path)
    report["候选条数"] = len(collected)
    report["相关性过滤丢弃"] = skipped
    report["相关性关键词"] = keyword
    report["各话题贡献"] = stats
    report_path = os.path.splitext(csv_path)[0] + "_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    emit(f"[完成] {len(rows)} 条 -> {csv_path}")
    emit(f"[完成] 字段缺失率报告 -> {report_path}")
    return csv_path, rows, report


def build_report(rows, topic_id, csv_path):
    total = len(rows)
    fields = {}
    for field in COLUMNS:
        missing = sum(1 for r in rows if _is_missing(r.get(field)))
        rate = (missing / total) if total else 1.0
        fields[field] = {
            "缺失数": missing,
            "缺失率": round(rate, 4),
            "核心字段": field in CORE_FIELDS,
        }
    core_rates = [fields[f]["缺失率"] for f in CORE_FIELDS]
    return {
        "话题ID": str(topic_id),
        "导出文件": csv_path,
        "导出时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "总条数": total,
        "核心字段最大缺失率": round(max(core_rates), 4) if core_rates else 1.0,
        "核心字段缺失率达标(<=5%)": (max(core_rates) <= 0.05) if core_rates else False,
        "字段明细": fields,
    }


def print_report(report):
    print("\n" + "=" * 56)
    print("字段缺失率报告")
    print("=" * 56)
    print(f"总条数: {report['总条数']}")
    for field, info in report["字段明细"].items():
        flag = "*" if info["核心字段"] else " "
        print(f"{flag} {field:<8} 缺失 {info['缺失数']:>5}  {info['缺失率'] * 100:6.2f}%")
    print(f"\n核心字段最大缺失率: {report['核心字段最大缺失率'] * 100:.2f}% (目标 <= 5%)")
    print("=" * 56)


# ==================== CLI ====================
def main():
    parser = argparse.ArgumentParser(description="知乎话题精选回答导出 CSV")
    parser.add_argument("--topic", default="19553148", help="话题 ID，默认 19553148（中医）")
    parser.add_argument("--target", type=int, default=2000, help="抓取条数，默认 2000")
    parser.add_argument("--out", default=None, help=f"输出目录，默认 {OUTPUT_DIR}")
    parser.add_argument("--delay-feed", type=float, default=0.4, help="翻页间隔秒")
    parser.add_argument("--delay-detail", type=float, default=0.25, help="详情请求间隔秒")
    parser.add_argument("--no-autosave", action="store_true", help="关闭断点自动保存")
    parser.add_argument("--no-expand", action="store_true",
                        help="只抓主话题；默认会合并子话题以突破单话题分页深度上限")
    parser.add_argument("--keyword", default=None,
                        help="相关性过滤关键词，默认取主话题名称（如「中医」）")
    args = parser.parse_args()

    common = dict(
        topic_id=args.topic,
        target=args.target,
        out_dir=args.out,
        delay_feed=args.delay_feed,
        delay_detail=args.delay_detail,
        autosave_every=0 if args.no_autosave else 200,
    )
    try:
        if args.no_expand:
            csv_path, rows, report = scrape_topic_answers(**common)
        else:
            csv_path, rows, report = scrape_topic_tree(keyword=args.keyword, **common)
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(1)

    print_report(report)
    if report.get("各话题贡献"):
        print("\n各话题贡献（去重后）:")
        for name, n in report["各话题贡献"].items():
            print(f"  {name}: {n}")
    print(f"CSV: {csv_path}")


if __name__ == "__main__":
    main()
