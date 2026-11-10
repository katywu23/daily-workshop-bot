import os
import sys
import requests
from bs4 import BeautifulSoup
from datetime import datetime


def fetch_inservice_courses():
    """抓取全國教師在職進修網最新研習資訊"""
    url = "https://www1.inservice.edu.tw/index2-3.aspx"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    courses = []
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, "html.parser")

        # 解析表格或連結中包含課程資訊的項目
        for link in soup.select("a[href]"):
            title = link.get_text(strip=True)
            href = link["href"]
            if len(title) > 6 and any(kw in title for kw in ["研習", "工作坊", "線上", "數位", "AI", "課程", "講座"]):
                full_url = href if href.startswith("http") else f"https://www1.inservice.edu.tw/{href.lstrip('/')}"
                courses.append(f"📘 [教網] {title}\n🔗 {full_url}")
                if len(courses) >= 5:
                    break
    except Exception as e:
        print(f"[警告] 抓取全國教師在職進修網失敗: {e}")

    return courses


def fetch_accupass_events(keyword="線上研習"):
    """抓取 Accupass 活動通的線上研習/講座活動"""
    url = f"https://www.accupass.com/search?q={requests.utils.quote(keyword)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    events = []
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        for link in soup.select("a[href*='/event/']"):
            title = link.get_text(strip=True)
            href = link["href"]
            if title and len(title) > 4:
                full_url = href if href.startswith("http") else f"https://www.accupass.com{href}"
                item = f"🎫 [Accupass] {title}\n🔗 {full_url}"
                if item not in events:
                    events.append(item)
                if len(events) >= 5:
                    break
    except Exception as e:
        print(f"[警告] 抓取 Accupass 失敗: {e}")

    return events


def send_line_push_message(token: str, user_id: str, text: str):
    """透過 LINE Messaging API 發送 Push Message"""
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "text",
                "text": text[:5000],  # LINE 單則訊息上限為 5000 字元
            }
        ],
    }
    response = requests.post(url, headers=headers, json=payload, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"LINE API 發送失敗 ({response.status_code}): {response.text}")
    print("✅ LINE 推播訊息發送成功！")


def main():
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")

    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"=== 開始執行每日線上研習爬蟲 ({today_str}) ===")

    inservice_list = fetch_inservice_courses()
    accupass_list = fetch_accupass_events()

    all_items = inservice_list + accupass_list

    if all_items:
        body = "\n\n".join(all_items)
        message = f"☀️ 早安！為您整理 {today_str} 最新線上研習與活動資訊：\n\n{body}"
    else:
        message = f"☀️ 早安！({today_str}) 目前暫無抓到符合條件的最新線上研習資訊，請稍後至官網查看。"

    print("\n--- 預覽推播內容 ---")
    print(message)
    print("--------------------\n")

    if not token or not user_id:
        print("[提示] 未設定 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID 環境變數，略過實際發送。")
        sys.exit(0)

    send_line_push_message(token, user_id, message)


if __name__ == "__main__":
    main()
