# -*- coding: utf-8 -*-
import json, requests, re, time, os
from html import unescape
from datetime import datetime
from pathlib import Path

CUTOFF = 1704038400  # 2024-01-01
QID = "3677852539"
OUT = str(Path.home() / "Documents" / "ZhihuAnswers")

with open("zhihu_cookies.json", "r", encoding="utf-8") as f:
    cookies = json.load(f)

s = requests.Session()
s.cookies.update(cookies)
s.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.zhihu.com/"
})

def strip(html):
    if not html: return ""
    t = re.sub(r"<[^>]+>", "", html)
    t = unescape(t)
    return re.sub(r"\s+", " ", t).strip()

answers = []
offset = 0
stop = False

while not stop:
    url = (f"https://www.zhihu.com/api/v4/questions/{QID}/answers"
           f"?sort_by=created&limit=20&offset={offset}"
           f"&include=data[*].content,excerpt")
    resp = s.get(url, timeout=15)
    print(f"Page offset={offset} status={resp.status_code}")
    if resp.status_code != 200:
        break

    data = resp.json()
    items = data.get("data", [])
    if not items:
        break

    for item in items:
        ct = item.get("created_time")
        if not ct or ct < CUTOFF:
            stop = True
            break
        c = strip(item.get("content", ""))
        answers.append({
            "id": item.get("id"),
            "author": item.get("author", {}).get("name", "anonymous"),
            "content": c,
            "created_date": datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M:%S"),
            "voteup_count": item.get("voteup_count", 0),
            "url": f"https://www.zhihu.com/question/{QID}/answer/{item.get('id')}",
        })
        preview = c[:60].replace("\n", " ")
        print(f"  [{len(answers)}] {answers[-1]['author']}: {preview}...")

    if data.get("paging", {}).get("is_end"):
        stop = True
    if stop:
        break
    offset += 20
    time.sleep(1)

Path(OUT).mkdir(parents=True, exist_ok=True)
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
jp = os.path.join(OUT, f"zhihu_{QID}_{ts}.json")
with open(jp, "w", encoding="utf-8") as f:
    json.dump(answers, f, ensure_ascii=False, indent=2)
print(f"\nDone! {len(answers)} answers -> {jp}")
