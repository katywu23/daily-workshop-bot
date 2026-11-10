import os
import re
import sys
import html
import urllib.parse
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# 停用 SSL 警告（部分學校或政府舊版網站憑證可能不完整）
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

# AI 相關關鍵字
AI_KEYWORDS = [
    "AI", "人工智慧", "生成式", "ChatGPT", "Gemini", "NotebookLM",
    "Copilot", "Claude", "Canva", "智慧學習", "數位教學", "數位學習", "大語言模型"
]

# 研習/線上相關關鍵字
WORKSHOP_KEYWORDS = [
    "研習", "工作坊", "講座", "線上", "增能", "培訓", "共學", "課程", "Meet"
]


def is_ai_related(text: str) -> bool:
    text_upper = text.upper()
    return any(kw.upper() in text_upper for kw in AI_KEYWORDS)


def is_workshop_related(text: str) -> bool:
    return any(kw in text for kw in WORKSHOP_KEYWORDS)


# ==============================================================================
# 1. 全國教師在職進修資訊網 (臺南市 city=18 + AI/線上研習)
# ==============================================================================
def fetch_inservice_tainan_ai():
    """抓取全國教師在職進修資訊網（鎖定臺南市 city=18 及 AI/線上研習）"""
    url = "https://www1.inservice.edu.tw/script/IndexQuery.aspx?city=18"
    results = []
    seen_cids = set()
    session = requests.Session()

    try:
        # 先取得初始頁面（同時包含最新臺南市研習與 ViewState）
        resp = session.get(url, headers=HEADERS, timeout=15)
        resp.encoding = "big5"
        pages_to_parse = [resp.text]

        # 提取 __VIEWSTATE 與 __EVENTVALIDATION 以查詢 "AI" 關鍵字
        vs_match = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', resp.text)
        ev_match = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', resp.text)
        if vs_match and ev_match:
            payload = {
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                "__LASTFOCUS": "",
                "__VIEWSTATE": vs_match.group(1),
                "__EVENTVALIDATION": ev_match.group(1),
                "tbCourseName": "AI",
                "tbSchoolName": "",
                "tbStartDateFrom": "",
                "tbStartDateTo": "",
                "ddlSubDist": "",
                "ddlSchoolLevelByCity": "",
                "ddlCourseClass": "",
                "ddlCourseTag": "",
                "Button1": "查詢".encode("big5", errors="ignore"),
            }
            resp_ai = session.post(url, data=payload, headers=HEADERS, timeout=15)
            resp_ai.encoding = "big5"
            pages_to_parse.insert(0, resp_ai.text)

        for page_html in pages_to_parse:
            soup = BeautifulSoup(page_html, "html.parser")
            for row in soup.select("tr.table_bottom"):
                cells = row.select("td.wide-only")
                if len(cells) < 8:
                    continue
                cid = cells[1].get_text(strip=True)
                link_tag = cells[2].find("a")
                title = cells[2].get_text(strip=True)
                start_date = cells[4].get_text(strip=True)
                unit = cells[6].get_text(strip=True)
                location = cells[7].get_text(strip=True)

                if not cid or cid in seen_cids:
                    continue
                if is_ai_related(title):
                    seen_cids.add(cid)
                    href = link_tag["href"] if link_tag and link_tag.has_attr("href") else f"/NAPP/CourseView.aspx?cid={cid}"
                    full_url = f"https://www1.inservice.edu.tw{href}"
                    online_tag = "🟢[線上]" if any(k in title for k in ["線上", "遠距", "Meet", "混成"]) else f"📍[{location}]"
                    results.append(
                        f"• {online_tag} {title}\n"
                        f"  📅 {start_date}｜🏫 {unit}\n"
                        f"  🔗 {full_url}"
                    )
                    if len(results) >= 5:
                        break
            if len(results) >= 5:
                break
    except Exception as e:
        print(f"[警告] 全國教師在職進修網抓取失敗: {e}")

    return results


# ==============================================================================
# 2. 臺南市政府教育局學習護照 (e-learning.tn.edu.tw)
# ==============================================================================
def fetch_tainan_passport_ai():
    """抓取臺南市學習護照系統中與 AI / 線上相關的最新研習"""
    url = "https://e-learning.tn.edu.tw/User/OnlineListQuickly.aspx"
    results = []
    seen_ocids = set()
    session = requests.Session()
    roc_year = str(datetime.now().year - 1911)

    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        resp.encoding = "utf-8"

        vs = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', resp.text)
        vsg = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]+)"', resp.text)
        ev = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', resp.text)

        if vs and ev:
            for kw in ["AI", "生成式", "線上"]:
                payload = {
                    "__EVENTTARGET": "",
                    "__EVENTARGUMENT": "",
                    "__LASTFOCUS": "",
                    "__VIEWSTATE": vs.group(1),
                    "__VIEWSTATEGENERATOR": vsg.group(1) if vsg else "EAB9C523",
                    "__VIEWSTATEENCRYPTED": "",
                    "__EVENTVALIDATION": ev.group(1),
                    "ctl00$ContentPlaceHolder1$RadioButtonList1": "4",
                    "ctl00$ContentPlaceHolder1$ddlYear": roc_year,
                    "ctl00$ContentPlaceHolder1$ddlMonth": "全部月份",
                    "ctl00$ContentPlaceHolder1$ddlDay": "全部日期",
                    "ctl00$ContentPlaceHolder1$ucStageSchool1$ddlStage": "0",
                    "ctl00$ContentPlaceHolder1$ucStageSchool1$ddlSchool": "0",
                    "ctl00$ContentPlaceHolder1$ddlCourseCategory": "0",
                    "ctl00$ContentPlaceHolder1$ddlStatisticsByAlly": "0",
                    "ctl00$ContentPlaceHolder1$txbKeyword": kw,
                    "ctl00$ContentPlaceHolder1$txbOCID": "",
                    "ctl00$ContentPlaceHolder1$btSearch": "搜尋",
                }
                r_post = session.post(url, data=payload, headers=HEADERS, timeout=15)
                r_post.encoding = "utf-8"
                soup = BeautifulSoup(r_post.text, "html.parser")

                grid = soup.find("table", id="ctl00_ContentPlaceHolder1_GridView1")
                if not grid:
                    continue

                for row in grid.find_all("tr")[1:]:
                    cols = row.find_all("td")
                    if len(cols) < 8:
                        continue
                    date_code_text = cols[0].get_text(" ", strip=True).split()
                    course_date = date_code_text[0] if date_code_text else ""
                    ocid = date_code_text[1] if len(date_code_text) > 1 else ""

                    link_tag = cols[1].find("a")
                    if not link_tag:
                        continue
                    title = link_tag.get_text(strip=True)
                    place_text = cols[1].get_text(" ", strip=True).replace(title, "").strip()
                    school_text = cols[2].get_text(" ", strip=True).split()[0] if cols[2].get_text(strip=True) else ""
                    limit_text = cols[5].get_text(" ", strip=True)
                    note_text = cols[7].get_text(" ", strip=True)

                    full_info = f"{title} {place_text} {note_text}"
                    if not ocid or ocid in seen_ocids:
                        continue
                    if not is_ai_related(full_info):
                        continue

                    seen_ocids.add(ocid)
                    detail_url = f"https://e-learning.tn.edu.tw/User/CourseDetail.aspx?OCID={ocid}"
                    is_online = any(k in full_info for k in ["線上", "Meet", "meet.google", "遠距", "直播", "網路"])
                    tag = "🟢[線上]" if is_online else f"📍[{place_text or '詳見內文'}]"
                    results.append(
                        f"• {tag} {title}\n"
                        f"  📅 {course_date}｜🏫 {school_text}（代號:{ocid}｜{limit_text}）\n"
                        f"  🔗 {detail_url}"
                    )
                    if len(results) >= 5:
                        break
                if len(results) >= 5:
                    break
    except Exception as e:
        print(f"[警告] 臺南市學習護照抓取失敗: {e}")

    return results


# ==============================================================================
# 3. 各級學校和教育單位 (臺南市教育局公告系統 + 各校 .edu.tw 網站)
# ==============================================================================
def fetch_schools_and_edu_units():
    """抓取臺南市教育局資訊中心公告系統 (含教育局公告與各校校務資訊) 及各級學校教育單位網站"""
    results = []
    seen_urls = set()

    # (A) 臺南市政府教育局資訊中心公告系統 (bulletin.tn.edu.tw)
    bulletin_url = "https://bulletin.tn.edu.tw/SearchBulletin.aspx"
    session = requests.Session()
    try:
        r1 = session.get(bulletin_url, headers=HEADERS, timeout=15)
        r1.encoding = "utf-8"
        vs = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', r1.text)
        vsg = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]+)"', r1.text)
        ev = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', r1.text)

        if vs and ev:
            # rbl_BulletinType: "1"=教育局公告, "2"=學校校務資訊
            for b_type in ["1", "2"]:
                payload = {
                    "ctl00_ToolkitScriptManager1_HiddenField": "",
                    "__EVENTTARGET": "",
                    "__EVENTARGUMENT": "",
                    "__LASTFOCUS": "",
                    "__VIEWSTATE": vs.group(1),
                    "__VIEWSTATEGENERATOR": vsg.group(1) if vsg else "CA0B0334",
                    "__VIEWSTATEENCRYPTED": "",
                    "__EVENTVALIDATION": ev.group(1),
                    "ctl00$ContentPlaceHolder1$ddl_FilterDate": "PostedDate",
                    "ctl00$ContentPlaceHolder1$tb_BeginDate": "",
                    "ctl00$ContentPlaceHolder1$tb_EndDate": "",
                    "ctl00$ContentPlaceHolder1$rbl_BulletinType": b_type,
                    "ctl00$ContentPlaceHolder1$ddl_FilterString": "BulletinSubject",
                    "ctl00$ContentPlaceHolder1$tb_KeyWord": "AI",
                    "ctl00$ContentPlaceHolder1$rbl_Status": "1",
                    "ctl00$ContentPlaceHolder1$cb_Poster": "on",
                    "ctl00$ContentPlaceHolder1$btn_Query": "查詢",
                }
                r2 = session.post(bulletin_url, data=payload, headers=HEADERS, timeout=15)
                r2.encoding = "utf-8"
                soup = BeautifulSoup(r2.text, "html.parser")
                for row in soup.select("#ctl00_ContentPlaceHolder1_gv_BList tr")[1:]:
                    cols = row.find_all("td")
                    if len(cols) < 5:
                        continue
                    a_tag = cols[1].find("a")
                    if not a_tag:
                        continue
                    title = a_tag.get_text(strip=True)
                    post_date = cols[4].get_text(strip=True).split()[0]
                    href = a_tag.get("href", "")
                    if is_workshop_related(title):
                        full_url = f"https://bulletin.tn.edu.tw/{href.lstrip('/')}"
                        if full_url not in seen_urls:
                            seen_urls.add(full_url)
                            type_label = "教育局公告" if b_type == "1" else "學校公告"
                            results.append(f"• [{type_label}] {title} ({post_date})\n  🔗 {full_url}")
                            if len(results) >= 4:
                                break
    except Exception as e:
        print(f"[警告] 臺南市教育局公告系統抓取失敗: {e}")

    # (B) 各級學校與大專院校教育單位 (.edu.tw) 線上 AI 研習檢索
    edu_items = search_yahoo_links("臺南 AI 線上研習 site:edu.tw", max_items=3)
    for title, url in edu_items:
        if url not in seen_urls and "inservice.edu.tw" not in url and "e-learning.tn.edu.tw" not in url:
            seen_urls.add(url)
            results.append(f"• [教育單位] {title}\n  🔗 {url}")

    return results[:6]


# ==============================================================================
# 4. 各社群平台 (Facebook、Instagram、Threads、Accupass)
# ==============================================================================
def search_yahoo_links(query: str, max_items: int = 3):
    """透過 Yahoo 奇摩搜尋取得公開社群貼文或學校網頁之標題與真實網址"""
    encoded_q = urllib.parse.quote(query)
    url = f"https://tw.search.yahoo.com/search?p={encoded_q}&btf=m"
    found = []
    try:
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        pattern = re.compile(
            r'RU=([^/"]+)/RK=.*?<h3[^>]*><span[^>]*>(.*?)</span></h3>',
            re.DOTALL,
        )
        for match in pattern.finditer(resp.text):
            raw_url = urllib.parse.unquote(match.group(1))
            raw_title = re.sub(r"<[^>]+>", "", match.group(2))
            clean_title = html.unescape(raw_title).strip()
            if any(skip in raw_url for skip in ["yahoo.com", "bing.com", "uservoice.com"]):
                continue
            if clean_title and raw_url not in [u for _, u in found]:
                found.append((clean_title, raw_url))
                if len(found) >= max_items:
                    break
    except Exception as e:
        print(f"[警告] 搜尋 '{query}' 失敗: {e}")
    return found


def fetch_social_and_community_platforms():
    """抓取 FB、IG、Threads、Accupass 上關於「臺南 + 線上研習 + AI」的活動資訊"""
    results = []
    seen_urls = set()

    # 1. Facebook 公開貼文與社團 (如臺南市教育噗浪客、科技中心、教師社群)
    for title, url in search_yahoo_links("臺南 AI 線上研習 site:facebook.com", max_items=3):
        if url not in seen_urls:
            seen_urls.add(url)
            results.append(f"• [FB 社群] {title}\n  🔗 {url}")

    # 2. Instagram / Threads 貼文
    for title, url in search_yahoo_links("臺南 AI 研習 (site:instagram.com OR site:threads.net)", max_items=2):
        if url not in seen_urls:
            seen_urls.add(url)
            platform = "IG" if "instagram.com" in url else "Threads"
            results.append(f"• [{platform} 社群] {title}\n  🔗 {url}")

    # 3. Accupass 活動通 (臺南 / 線上 AI 研習講座)
    try:
        acc_url = f"https://www.accupass.com/search?q={urllib.parse.quote('AI 線上研習')}"
        resp = requests.get(acc_url, headers=HEADERS, timeout=12)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            count = 0
            for link in soup.select("a[href*='/event/']"):
                title = link.get_text(strip=True)
                href = link["href"]
                if title and len(title) > 4 and is_ai_related(title):
                    full_url = href if href.startswith("http") else f"https://www.accupass.com{href}"
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        results.append(f"• [Accupass] {title}\n  🔗 {full_url}")
                        count += 1
                        if count >= 2:
                            break
    except Exception as e:
        print(f"[警告] Accupass 抓取失敗: {e}")

    return results


# ==============================================================================
# LINE Messaging API 推播
# ==============================================================================
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
                "text": text[:4950],  # 確保不超過 LINE 單則 5000 字元上限
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
    print(f"=== 開始執行【臺南市 × AI × 線上研習】爬蟲 ({today_str}) ===")

    inservice_items = fetch_inservice_tainan_ai()
    passport_items = fetch_tainan_passport_ai()
    school_items = fetch_schools_and_edu_units()
    social_items = fetch_social_and_community_platforms()

    sections = []

    sections.append(
        "📘【1. 全國教師在職進修網（臺南/AI）】\n"
        + ("\n".join(inservice_items) if inservice_items else "• 今日暫無新增符合條件之課程")
    )
    sections.append(
        "📗【2. 臺南市教育局學習護照（AI/線上）】\n"
        + ("\n".join(passport_items) if passport_items else "• 今日暫無新增符合條件之課程")
    )
    sections.append(
        "🏫【3. 各級學校與教育單位公告】\n"
        + ("\n".join(school_items) if school_items else "• 今日暫無新增符合條件之公告")
    )
    sections.append(
        "📱【4. 社群平台（FB / IG / 活動平台）】\n"
        + ("\n".join(social_items) if social_items else "• 今日暫無新增符合條件之社群貼文")
    )

    message = (
        f"☀️ 早安！{today_str}\n"
        f"為您彙整【臺南市 ＋ 線上研習 ＋ AI 相關】最新研習資訊：\n\n"
        + "\n\n".join(sections)
    )

    print("\n--- 預覽推播內容 ---")
    print(message)
    print("--------------------\n")

    if not token or not user_id:
        print("[提示] 未設定 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID 環境變數，略過實際發送。")
        sys.exit(0)

    send_line_push_message(token, user_id, message)


if __name__ == "__main__":
    main()
