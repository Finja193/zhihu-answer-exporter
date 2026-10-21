# -*- coding: utf-8 -*-
"""
Zhihu Answer Scraper - Web App
Flask backend with SSE progress streaming
"""

import json
import time
import re
import os
import sys
import queue
import threading
from datetime import datetime
from pathlib import Path
from html import unescape

import requests
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

import topic_scraper


def _get_base_path():
    """Get the base path for bundled exe or script"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _get_template_path():
    """Get templates directory path, handling PyInstaller bundling"""
    base = _get_base_path()
    templates_dir = os.path.join(base, 'templates')
    if os.path.isdir(templates_dir):
        return templates_dir
    # Fallback: search nearby
    for root, dirs, files in os.walk(base):
        if 'templates' in dirs:
            return os.path.join(root, 'templates')
    return 'templates'


app = Flask(__name__, template_folder=_get_template_path())

# ==================== Config ====================
PHONE = "15550687638"
PASSWORD = "hzy170510"
COOKIES_FILE = "zhihu_cookies.json"
OUTPUT_DIR = str(Path.home() / "Documents" / "ZhihuAnswers")
CUTOFF_TIMESTAMP = int(datetime(2024, 1, 1).timestamp())
DEFAULT_TOPIC_ID = "19553148"   # 中医
DEFAULT_TARGET = 2000           # 话题模式默认抓取条数
MAX_TARGET = 20000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.zhihu.com/",
    "x-requested-with": "fetch",
}

TEXT_PASSWORD_LOGIN = "\u5bc6\u7801\u767b\u5f55"
TEXT_LOGIN = "\u767b\u5f55"

# Global progress queue for SSE
progress_queues = {}  # task_id -> queue


def emit_progress(task_id, event_type, data):
    """Send progress event to the SSE stream"""
    if task_id in progress_queues:
        progress_queues[task_id].put({"event": event_type, "data": data})


# ==================== Login ====================
def login_zhihu(task_id):
    emit_progress(task_id, "login_status", {"phase": "login", "message": "Starting browser..."})

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

            emit_progress(task_id, "login_status", {"phase": "login", "message": "Navigating to login page..."})

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

            phone_input = page.locator('input[name="username"], input[type="text"]').first
            phone_input.click()
            phone_input.fill(PHONE)
            time.sleep(0.5)

            password_input = page.locator('input[name="password"], input[type="password"]').first
            password_input.click()
            password_input.fill(PASSWORD)
            time.sleep(0.5)

            submit_btn = page.locator(f'button[type="submit"], button:has-text("{TEXT_LOGIN}")').first
            submit_btn.click()

            emit_progress(task_id, "login_status", {
                "phase": "login",
                "message": "Credentials submitted. Please complete captcha in browser if needed...",
                "waiting_captcha": True
            })

            try:
                page.wait_for_url("https://www.zhihu.com/", timeout=60000)
            except PlaywrightTimeout:
                if page.locator('[class*="Avatar"]').first.is_visible():
                    pass
                else:
                    emit_progress(task_id, "login_status", {
                        "phase": "login",
                        "message": "Please confirm login in the browser...",
                        "waiting_confirm": True
                    })
                    # Wait additional time for manual login
                    time.sleep(30)

            time.sleep(2)

            cookies = context.cookies()
            cookies_dict = {}
            for cookie in cookies:
                cookies_dict[cookie["name"]] = cookie["value"]

            with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies_dict, f, ensure_ascii=False, indent=2)

            emit_progress(task_id, "login_status", {
                "phase": "login",
                "message": "Login successful!",
                "done": True
            })

        finally:
            browser.close()

    return cookies_dict


def load_cookies():
    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def is_cookies_valid(cookies_dict):
    session = requests.Session()
    session.cookies.update(cookies_dict)
    session.headers.update(HEADERS)
    try:
        resp = session.get("https://www.zhihu.com/api/v4/me", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "id" in data:
                return True, data.get("name", "unknown")
    except Exception:
        pass
    return False, None


def ensure_login(task_id):
    cookies = load_cookies()
    if cookies:
        valid, username = is_cookies_valid(cookies)
        if valid:
            emit_progress(task_id, "login_status", {
                "phase": "login",
                "message": f"Using cached login: {username}",
                "done": True,
                "cached": True
            })
            return cookies
    return login_zhihu(task_id)


# ==================== Fetch Answers ====================
def extract_question_id(url_or_id):
    match = re.search(r"question/(\d+)", str(url_or_id))
    if match:
        return match.group(1)
    return str(url_or_id).strip().strip("/")


def strip_html(html_text):
    if not html_text:
        return ""
    text = re.sub(r"<[^>]+>", "", html_text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_answer_content(session, answer_id):
    url = f"https://www.zhihu.com/api/v4/answers/{answer_id}"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("content", "")
    except Exception:
        pass
    return ""


def run_scrape(task_id, question_id):
    """Main scraping logic, runs in a background thread"""
    try:
        # Step 0: Login
        emit_progress(task_id, "phase", {"phase": "login", "message": "Checking login..."})
        cookies = ensure_login(task_id)

        # Step 1: Fetch answers with content
        emit_progress(task_id, "phase", {
            "phase": "scanning",
            "message": "Fetching answers with content...",
            "scanned": 0,
            "qualified": 0
        })

        session = requests.Session()
        session.cookies.update(cookies)
        session.headers.update(HEADERS)

        answers = []
        offset = 0
        limit = 20
        stop_fetching = False

        while not stop_fetching:
            url = (
                f"https://www.zhihu.com/api/v4/questions/{question_id}/answers"
                f"?sort_by=created&limit={limit}&offset={offset}"
                f"&include=data[*].content,excerpt"
            )

            try:
                resp = session.get(url, timeout=15)
            except Exception as e:
                emit_progress(task_id, "error", {"message": f"Request failed: {e}"})
                break

            if resp.status_code == 403:
                emit_progress(task_id, "error", {"message": "403 Forbidden - Login expired"})
                break

            if resp.status_code != 200:
                time.sleep(3)
                continue

            try:
                data = resp.json()
            except Exception:
                break

            items = data.get("data", [])
            paging = data.get("paging", {})
            is_end = paging.get("is_end", True)

            if not items:
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
                    "content": strip_html(content_html),
                    "created_date": datetime.fromtimestamp(created_time).strftime("%Y-%m-%d %H:%M:%S"),
                    "voteup_count": item.get("voteup_count", 0),
                    "comment_count": item.get("comment_count", 0),
                    "url": f"https://www.zhihu.com/question/{question_id}/answer/{item.get('id')}",
                }
                answers.append(answer_info)

            emit_progress(task_id, "fetch_progress", {
                "total": len(answers),
                "current": len(answers),
                "author": answers[-1]["author"] if answers else ""
            })

            if is_end:
                stop_fetching = True
            if stop_fetching:
                break

            offset += limit
            time.sleep(1.0)

        # Save results
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(OUTPUT_DIR, f"zhihu_{question_id}_{timestamp}.json")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(answers, f, ensure_ascii=False, indent=2)

        emit_progress(task_id, "done", {
            "total": len(answers),
            "json_path": json_path,
            "message": f"Done! {len(answers)} answers saved."
        })

    except Exception as e:
        emit_progress(task_id, "error", {"message": str(e)})
    finally:
        def cleanup():
            time.sleep(5)
            progress_queues.pop(task_id, None)
        threading.Thread(target=cleanup, daemon=True).start()


# ==================== Topic Scrape ====================
def extract_topic_id(url_or_id):
    """Extract topic ID from URL or raw ID string"""
    match = re.search(r"topic/(\d+)", str(url_or_id))
    if match:
        return match.group(1)
    return str(url_or_id).strip().strip("/")


def run_topic_scrape(task_id, topic_id, target):
    """Topic top-answers scraping, runs in a background thread"""
    def progress(message):
        m = re.search(r"(?:已获取|补齐)\s+(\d+)\s*/\s*(\d+)", message)
        if m:
            emit_progress(task_id, "fetch_progress", {
                "total": int(m.group(2)),
                "current": int(m.group(1)),
            })
        emit_progress(task_id, "log", {"message": message})

    try:
        emit_progress(task_id, "phase", {
            "phase": "login",
            "message": "Checking login and API signature..."
        })

        csv_path, rows, report = topic_scraper.scrape_topic_tree(
            topic_id=topic_id,
            target=target,
            progress=progress,
        )

        core_rate = report.get("核心字段最大缺失率", 0)
        emit_progress(task_id, "done", {
            "total": report.get("总条数", len(rows)),
            "json_path": csv_path,
            "message": f"Done! {report.get('总条数', len(rows))} answers saved. "
                       f"Core field missing rate: {core_rate * 100:.2f}%",
            "core_missing_rate": core_rate,
            "qualifies": report.get("核心字段缺失率达标(<=5%)", False),
        })

    except Exception as e:
        emit_progress(task_id, "error", {"message": str(e)})
    finally:
        def cleanup():
            time.sleep(5)
            progress_queues.pop(task_id, None)
        threading.Thread(target=cleanup, daemon=True).start()


# ==================== Flask Routes ====================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/check_login", methods=["GET"])
def check_login():
    cookies = load_cookies()
    if cookies:
        valid, username = is_cookies_valid(cookies)
        return jsonify({"logged_in": valid, "username": username})
    return jsonify({"logged_in": False, "username": None})


@app.route("/api/start", methods=["POST"])
def start_scrape():
    data = request.get_json() or {}
    mode = (data.get("mode") or "question").strip()
    user_input = (data.get("question_url") or "").strip()
    task_id = f"task_{int(time.time() * 1000)}"
    progress_queues[task_id] = queue.Queue()

    # 话题模式：抓取话题下的精选回答并导出 CSV
    if mode == "topic":
        topic_id = extract_topic_id(user_input or DEFAULT_TOPIC_ID)
        try:
            target = int(data.get("target") or DEFAULT_TARGET)
        except (TypeError, ValueError):
            target = DEFAULT_TARGET
        target = max(1, min(target, MAX_TARGET))

        thread = threading.Thread(
            target=run_topic_scrape, args=(task_id, topic_id, target), daemon=True)
        thread.start()
        return jsonify({"task_id": task_id, "topic_id": topic_id, "target": target, "mode": "topic"})

    if not user_input:
        progress_queues.pop(task_id, None)
        return jsonify({"error": "Please provide a question URL or ID"}), 400

    question_id = extract_question_id(user_input)
    thread = threading.Thread(target=run_scrape, args=(task_id, question_id), daemon=True)
    thread.start()

    return jsonify({"task_id": task_id, "question_id": question_id, "mode": "question"})


@app.route("/api/progress/<task_id>")
def progress_stream(task_id):
    """SSE endpoint for real-time progress"""
    if task_id not in progress_queues:
        return jsonify({"error": "Invalid task_id"}), 404

    def generate():
        q = progress_queues[task_id]
        while True:
            try:
                msg = q.get(timeout=30)
                event = msg["event"]
                data = json.dumps(msg["data"], ensure_ascii=False)
                yield f"event: {event}\ndata: {data}\n\n"
                if event == "done" or event == "error":
                    break
            except queue.Empty:
                yield f"event: heartbeat\ndata: {{}}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


if __name__ == "__main__":
    import webbrowser
    import threading as _thr

    Path("templates").mkdir(exist_ok=True)
    url = "http://127.0.0.1:5000"

    def open_browser():
        time.sleep(0.8)
        webbrowser.open(url)

    _thr.Thread(target=open_browser, daemon=True).start()

    print(f"Zhihu Scraper Web App running at {url}")
    print("Press Ctrl+C to stop")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
