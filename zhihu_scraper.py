# -*- coding: utf-8 -*-
"""
Zhihu Answer Scraper
Get all answers posted after 2024-01-01 for a given question.
"""

import json
import time
import re
import os
from datetime import datetime
from pathlib import Path
from html import unescape

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ==================== Config ====================
PHONE = "15550687638"
PASSWORD = "hzy170510"
COOKIES_FILE = "zhihu_cookies.json"
OUTPUT_DIR = str(Path.home() / "Documents" / "ZhihuAnswers")
CUTOFF_TIMESTAMP = int(datetime(2024, 1, 1).timestamp())  # 1704038400

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.zhihu.com/",
    "x-requested-with": "fetch",
}

# Chinese text used in page locators (as unicode escapes to avoid encoding issues)
TEXT_PASSWORD_LOGIN = "\u5bc6\u7801\u767b\u5f55"  # ??????
TEXT_LOGIN = "\u767b\u5f55"  # ???


# ==================== Login ====================
def login_zhihu():
    """Login to Zhihu using Playwright browser, return cookies dict"""
    print("\n[Login] Starting browser...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=HEADERS["User-Agent"],
        )
        page = context.new_page()

        try:
            page.goto("https://www.zhihu.com/signin", wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

            # Switch to password login
            try:
                switch_btn = page.locator(f"text={TEXT_PASSWORD_LOGIN}").first
                if switch_btn.is_visible():
                    switch_btn.click()
                    time.sleep(1)
                else:
                    for text in [TEXT_LOGIN, TEXT_PASSWORD_LOGIN]:
                        try:
                            btn = page.locator(f"text={text}").first
                            if btn.is_visible():
                                btn.click()
                                time.sleep(1)
                                break
                        except Exception:
                            pass
            except Exception:
                pass

            # Fill phone number
            phone_input = page.locator('input[name="username"], input[type="text"]').first
            phone_input.click()
            phone_input.fill(PHONE)
            time.sleep(0.5)

            # Fill password
            password_input = page.locator('input[name="password"], input[type="password"]').first
            password_input.click()
            password_input.fill(PASSWORD)
            time.sleep(0.5)

            # Click login button
            submit_btn = page.locator(f'button[type="submit"], button:has-text("{TEXT_LOGIN}")').first
            submit_btn.click()

            print("[Login] Credentials submitted. Please solve captcha manually if needed...")
            print("[Login] Waiting for login to complete (max 60s)...")

            try:
                page.wait_for_url("https://www.zhihu.com/", timeout=60000)
            except PlaywrightTimeout:
                if page.locator('[class*="Avatar"]').first.is_visible():
                    pass
                else:
                    print("[Login] Cannot confirm login status, please check browser manually")
                    input("[Login] Press Enter if logged in...")

            time.sleep(2)

            cookies = context.cookies()
            cookies_dict = {}
            for cookie in cookies:
                cookies_dict[cookie["name"]] = cookie["value"]

            with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies_dict, f, ensure_ascii=False, indent=2)

            print(f"[Login] Success! Cookies saved to {COOKIES_FILE}")

        finally:
            browser.close()

    return cookies_dict


def load_cookies():
    """Load cookies from file"""
    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        print(f"[Cookies] Loaded from {COOKIES_FILE}")
        return cookies
    return None


def is_cookies_valid(cookies_dict):
    """Check if cookies are still valid"""
    session = requests.Session()
    session.cookies.update(cookies_dict)
    session.headers.update(HEADERS)

    try:
        resp = session.get("https://www.zhihu.com/api/v4/me", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "id" in data:
                print(f"[Verify] Cookies valid, user: {data.get('name', 'unknown')}")
                return True
    except Exception:
        pass

    print("[Verify] Cookies expired, need re-login")
    return False


def ensure_login():
    """Ensure valid login state"""
    cookies = load_cookies()
    if cookies and is_cookies_valid(cookies):
        return cookies
    return login_zhihu()


# ==================== Fetch Answers ====================
def extract_question_id(url_or_id):
    """Extract question ID from URL or raw ID string"""
    match = re.search(r"question/(\d+)", str(url_or_id))
    if match:
        return match.group(1)
    return str(url_or_id).strip().strip("/")


def strip_html(html_text):
    """Remove HTML tags and decode entities, return plain text"""
    if not html_text:
        return ""
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", html_text)
    # Decode HTML entities (&amp; &lt; &gt; &#xxxx; etc.)
    text = unescape(text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_answer_content(session, answer_id):
    """Fetch full content of a single answer"""
    url = f"https://www.zhihu.com/api/v4/answers/{answer_id}"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("content", "")
    except Exception:
        pass
    return ""


def fetch_answers(cookies_dict, question_id):
    """Fetch all answers after 2024-01-01 for the given question"""
    session = requests.Session()
    session.cookies.update(cookies_dict)
    session.headers.update(HEADERS)

    answers = []
    offset = 0
    limit = 20
    stop_fetching = False
    page_num = 0

    print(f"\n[Fetch] Getting answers for question {question_id}...")
    print(f"[Filter] Only answers after {datetime.fromtimestamp(CUTOFF_TIMESTAMP).strftime('%Y-%m-%d %H:%M:%S')}")

    while not stop_fetching:
        page_num += 1
        url = (
            f"https://www.zhihu.com/api/v4/questions/{question_id}/answers"
            f"?sort_by=created"
            f"&limit={limit}"
            f"&offset={offset}"
            f"&include=data[*].content,excerpt"
        )

        try:
            resp = session.get(url, timeout=15)
        except Exception as e:
            print(f"[Error] Request failed (page {page_num}): {e}")
            time.sleep(3)
            continue

        if resp.status_code == 403:
            print("[Error] 403 Forbidden - Cookies may be expired, delete cookies file and retry")
            break

        if resp.status_code != 200:
            print(f"[Error] HTTP {resp.status_code}, retrying...")
            time.sleep(3)
            continue

        try:
            data = resp.json()
        except Exception as e:
            print(f"[Error] JSON parse failed: {e}")
            break

        items = data.get("data", [])
        paging = data.get("paging", {})
        is_end = paging.get("is_end", True)

        if not items:
            print(f"  [Page {page_num}] No data, stopping")
            break

        for item in items:
            created_time = item.get("created_time")
            if created_time is None:
                continue

            if created_time < CUTOFF_TIMESTAMP:
                stop_fetching = True
                break

            content_html = item.get("content", "")
            answer_info = {
                "id": item.get("id"),
                "author": item.get("author", {}).get("name", "anonymous"),
                "author_url": item.get("author", {}).get("url", ""),
                "content": strip_html(content_html),
                "created_time": created_time,
                "created_date": datetime.fromtimestamp(created_time).strftime("%Y-%m-%d %H:%M:%S"),
                "voteup_count": item.get("voteup_count", 0),
                "comment_count": item.get("comment_count", 0),
                "url": f"https://www.zhihu.com/question/{question_id}/answer/{item.get('id')}",
            }
            answers.append(answer_info)

        print(f"  [Page {page_num}] scanned {len(items)} | qualified total: {len(answers)}")

        if is_end:
            stop_fetching = True

        if stop_fetching:
            break

        offset += limit
        time.sleep(1.0)

    print(f"\n[Done] Total qualified answers: {len(answers)}")
    return answers


# ==================== Save Results ====================
def save_results(answers, question_id):
    """Save results to JSON and TXT files"""
    Path(OUTPUT_DIR).mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(OUTPUT_DIR, f"zhihu_{question_id}_{timestamp}.json")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(answers, f, ensure_ascii=False, indent=2)

    print(f"\n[Save] JSON saved to: {json_path}")

    txt_path = os.path.join(OUTPUT_DIR, f"zhihu_{question_id}_{timestamp}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"Question ID: {question_id}\n")
        f.write(f"Fetch time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Qualified answers: {len(answers)}\n")
        f.write("=" * 60 + "\n\n")

        for i, ans in enumerate(answers, 1):
            f.write(f"--- Answer {i} ---\n")
            f.write(f"Author: {ans['author']}\n")
            f.write(f"Time: {ans['created_date']}\n")
            f.write(f"Upvotes: {ans['voteup_count']} | Comments: {ans['comment_count']}\n")
            f.write(f"URL: {ans['url']}\n")
            f.write(f"Content:\n{ans['content']}\n")
            f.write("\n" + "=" * 60 + "\n\n")

    print(f"[Save] TXT summary saved to: {txt_path}")


# ==================== Main ====================
def main():
    print("=" * 55)
    print("  Zhihu Answer Scraper - Filter: after 2024-01-01")
    print("=" * 55)

    user_input = input("\nEnter Zhihu question URL or ID: ").strip()
    question_id = extract_question_id(user_input)
    print(f"Question ID: {question_id}")

    cookies = ensure_login()

    answers = fetch_answers(cookies, question_id)

    if answers:
        save_results(answers, question_id)
    else:
        print("\n[Result] No qualified answers found")

    print("\nDone!")


if __name__ == "__main__":
    main()
