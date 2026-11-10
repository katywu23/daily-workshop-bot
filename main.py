import os
import re
import sys
import html
import urllib.parse
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date

# 停用 SSL 警告（部分政府/學校憑證鏈不完整時確保連線正常）
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

# 明確判定為「線上形式」之關鍵字
ONLINE_KEYWORDS = [
    "線上", "遠距", "視訊", "MEET", "GOOGLE MEET", "TEAMS", "ZOOM", "WEBEX", "YOUTUBE直播"
]

# 明確判定為「收費 / 商業課程」之關鍵字
PAID_KEYWORDS = [
    "收費", "費用", "報名費", "學費", "自費", "NT$", "NTD", "元整", "定價"
]

# 明確判定為「校內研習」之關鍵字
INTERNAL_KEYWORDS = [
    "校內研習", "校內進修", "校本研習", "僅限本校", "限校內", "僅限校內"
]


def is_ai_related(text: str) -> bool:
    text_upper = text.upper()
    return any(kw.upper() in text_upper for kw in AI_KEYWORDS)


def is_strictly_online(location_or_desc: str) -> bool:
    """檢查地點或說明是否符合：純線上研習 / 提供線上會議連結"""
    text_upper = location_or_desc.upper()
    # 符合任一線上特徵
    return any(kw in text_upper for kw in ONLINE_KEYWORDS)


def is_paid(text: str) -> bool:
    """檢查是否需要收費（0元或免費則排除）"""
    if "免費" in text or "0元" in text or "公費" in text or "無" in text:
        # 如果有明確寫免費，不視為收費
        for kw in ["收費", "報名費", "自費"]:
            if kw in text and "不" + kw not in text and "免" + kw not in text:
                return True
        return False
    return any(kw in text for kw in PAID_KEYWORDS)


def is_internal_school_only(text: str) -> bool:
    """檢查是否為校內專屬研習"""
    return any(kw in text for kw in INTERNAL_KEYWORDS)


def parse_date_safely(date_str: str) -> date:
    """解析民國或西元日期格式"""
    # 支援: 2026/11/15, 2026-11-15, 115/11/15, 115.11.15
    match_ad = re.search(r'(20\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])', date_str)
    if match_ad:
        return date(int(match_ad.group(1)), int(match_ad.group(2)), int(match_ad.group(3)))

    match_roc = re.search(r'([12]?\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])', date_str)
    if match_roc:
        ad_year = int(match_roc.group(1)) + 1911
        return date(ad_year, int(match_roc.group(2)), int(match_roc.group(3)))

    return None


# ==============================================================================
# 1. 全國教師在職進修資訊網 (臺南市專區 + 條件 1~5 嚴格檢驗)
# ==============================================================================
def fetch_inservice_strictly_online(today: date):
    """
    抓取全國教師在職進修網臺南市研習：
    1. 尚在報名區間內
    2. 線上研習
    3. 上課地點為線上/提供線上會議連結
    4. 免費（排除收費）
    5. 排除校內研習
    """
    url = "https://www1.inservice.edu.tw/script/IndexQuery.aspx?city=18"
    results = []
    seen_cids = set()
    session = requests.Session()

    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        resp.encoding = "big5"
        pages_to_parse = [resp.text]

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
                title = cells[2].get_text(strip=True)
                course_type = cells[3].get_text(strip=True)
                start_date = cells[4].get_text(strip=True)
                unit = cells[6].get_text(strip=True)

                if not cid or cid in seen_cids:
                    continue
                seen_cids.add(cid)

                # 條件 5: 排除校內研習
                if is_internal_school_only(course_type) or is_internal_school_only(title):
                    continue

                if not is_ai_related(title):
                    continue

                # 進入課程詳細頁面檢查報名期限、費用與實際地點
                detail_url = f"https://www1.inservice.edu.tw/NAPP/CourseView.aspx?cid={cid}"
                try:
                    c_resp = session.get(detail_url, headers=HEADERS, timeout=12)
                    c_resp.encoding = "utf-8"
                    c_text = c_resp.text
                except Exception:
                    continue

                # 條件 4: 免費（排除收費，檢查繳費金額非 0）
                fee_match = re.search(r'．繳費金額：.*?<td[^>]*>([^<]+)</td>', c_text, re.DOTALL)
                if fee_match:
                    fee_val = fee_match.group(1).replace("元", "").strip()
                    if fee_val.isdigit() and int(fee_val) > 0:
                        continue

                # 條件 1: 檢查報名區間（報名日期截止日需 >= today）
                reg_match = re.search(r'．報名日期：.*?<td[^>]*>(.*?)</td>', c_text, re.DOTALL)
                if reg_match:
                    reg_text = reg_match.group(1)
                    end_part = reg_text.split("至")[-1] if "至" in reg_text else reg_text
                    reg_end_date = parse_date_safely(end_part)
                    if reg_end_date and reg_end_date < today:
                        # 報名已經截止
                        continue

                # 條件 2 & 3: 研習地點/上課地點必須是線上或附會議連結
                loc_match = re.search(r'．上課地點：.*?<td[^>]*>(.*?)</td>', c_text, re.DOTALL)
                loc_text = loc_match.group(1) if loc_match else ""
                full_desc = f"{title} {loc_text}"

                if not is_strictly_online(full_desc):
                    continue

                results.append(
                    f"• 🟢[線上研習] {title}\n"
                    f"  📅 課程日期: {start_date}｜🏫 {unit}\n"
                    f"  🔗 {detail_url}"
                )
                if len(results) >= 4:
                    break
            if len(results) >= 4:
                break
    except Exception as e:
        print(f"[警告] 全國教師在職進修網抓取失敗: {e}")

    return results


# ==============================================================================
# 2. 臺南市政府教育局學習護照 (e-learning.tn.edu.tw 條件嚴格過濾)
# ==============================================================================
def fetch_tainan_passport_strictly_online(today: date):
    """
    抓取臺南市學習護照：
    1. 尚在報名區間內（未超過研習日期）
    2. 線上研習
    3. 地點寫線上或給會議連結
    4. 免費（學習護照公辦皆免費）
    5. 排除校內研習（排除屬性為「校內研習」或「校內進修」）
    """
    url = "https://e-learning.tn.edu.tw/User/OnlineListQuickly.aspx"
    results = []
    seen_ocids = set()
    session = requests.Session()
    roc_year = str(today.year - 1911)

    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        resp.encoding = "utf-8"

        vs = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', resp.text)
        vsg = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]+)"', resp.text)
        ev = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', resp.text)

        if vs and ev:
            for kw in ["AI", "生成式", "線上", "數位"]:
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
                    course_date_str = date_code_text[0] if date_code_text else ""
                    ocid = date_code_text[1] if len(date_code_text) > 1 else ""

                    link_tag = cols[1].find("a")
                    if not link_tag:
                        continue
                    title = link_tag.get_text(strip=True)
                    place_text = cols[1].get_text(" ", strip=True).replace(title, "").strip()
                    school_text = cols[2].get_text(" ", strip=True).split()[0] if cols[2].get_text(strip=True) else ""
                    attr_text = cols[5].get_text(" ", strip=True)  # 屬性：校內研習 / 全市性 / 市府委辦
                    note_text = cols[7].get_text(" ", strip=True)

                    if not ocid or ocid in seen_ocids:
                        continue

                    # 條件 5: 排除校內研習
                    if is_internal_school_only(attr_text) or is_internal_school_only(title):
                        continue

                    # 條件 1: 尚在報名/研習區間內
                    c_date = parse_date_safely(course_date_str)
                    if c_date and c_date < today:
                        continue

                    full_content = f"{title} {place_text} {note_text}"

                    # 條件: 必須為 AI 相關
                    if not is_ai_related(full_content):
                        continue

                    # 條件 2 & 3: 研習地點/形式必須是線上
                    if not is_strictly_online(full_content):
                        continue

                    seen_ocids.add(ocid)
                    detail_url = f"https://e-learning.tn.edu.tw/User/CourseDetail.aspx?OCID={ocid}"
                    results.append(
                        f"• 🟢[線上研習] {title}\n"
                        f"  📅 研習日期: {course_date_str}｜🏫 {school_text}（代號:{ocid}）\n"
                        f"  🔗 {detail_url}"
                    )
                    if len(results) >= 4:
                        break
                if len(results) >= 4:
                    break
    except Exception as e:
        print(f"[警告] 臺南市學習護照抓取失敗: {e}")

    return results


# ==============================================================================
# 3. 各級學校與教育單位 (教育局公告系統 + 學校公開線上研習公告)
# ==============================================================================
def fetch_schools_strictly_online(today: date):
    """抓取各級學校與教育局公告：嚴格檢查線上研習、有效報名區間、非校內限定、免費"""
    results = []
    seen_urls = set()
    session = requests.Session()

    bulletin_url = "https://bulletin.tn.edu.tw/SearchBulletin.aspx"
    try:
        r1 = session.get(bulletin_url, headers=HEADERS, timeout=15)
        r1.encoding = "utf-8"
        vs = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', r1.text)
        vsg = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]+)"', r1.text)
        ev = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', r1.text)

        if vs and ev:
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
                "ctl00$ContentPlaceHolder1$rbl_BulletinType": "1",  # 教育局公告（具全市公開性質）
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
                period_str = cols[3].get_text(" ", strip=True)  # 公告期間 2026/11/06 2026/11/11
                href = a_tag.get("href", "")

                # 條件 5: 排除校內
                if is_internal_school_only(title):
                    continue

                # 條件 1: 檢查公告期間截止日 >= today
                dates_in_period = re.findall(r'20\d{2}[/-]\d{2}[/-]\d{2}', period_str)
                if dates_in_period:
                    end_d = parse_date_safely(dates_in_period[-1])
                    if end_d and end_d < today:
                        continue

                # 檢查公告詳情
                full_url = f"https://bulletin.tn.edu.tw/{href.lstrip('/')}"
                try:
                    b_resp = session.get(full_url, headers=HEADERS, timeout=10)
                    b_resp.encoding = "utf-8"
                    content_text = b_resp.text
                except Exception:
                    continue

                # 條件 4: 排除收費
                if is_paid(content_text):
                    continue

                # 條件 2 & 3: 必須為線上研習（包含 Meet 連結或線上地點）
                if is_strictly_online(title) or is_strictly_online(content_text):
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        results.append(f"• 🟢[教育局公告/線上] {title}\n  🔗 {full_url}")
                        if len(results) >= 3:
                            break
    except Exception as e:
        print(f"[警告] 學校/教育局公告檢索失敗: {e}")

    # (B) 檢索各校與教育單位官網公開線上 AI 研習
    school_web_items = search_strictly_online_links(
        '臺南 ("線上研習" OR "Google Meet") AI (site:edu.tw OR site:tn.edu.tw)',
        max_items=3
    )
    for title, url in school_web_items:
        if url not in seen_urls and "inservice.edu.tw" not in url and "e-learning.tn.edu.tw" not in url:
            seen_urls.add(url)
            results.append(f"• 🟢[學校/教育單位] {title}\n  🔗 {url}")

    return results[:5]


# ==============================================================================
# 4. 各社群平台 (FB、IG、Accupass) 條件嚴格過濾
# ==============================================================================
def search_strictly_online_links(query: str, max_items: int = 3):
    """搜尋引擎檢索，確保結果為免費、非校內、線上研習"""
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

            # 條件 5: 排除校內
            if is_internal_school_only(clean_title):
                continue

            # 條件 4: 排除收費
            if is_paid(clean_title):
                continue

            # 條件 2 & 3: 必須為線上研習
            if not is_strictly_online(clean_title):
                continue

            if clean_title and raw_url not in [u for _, u in found]:
                found.append((clean_title, raw_url))
                if len(found) >= max_items:
                    break
    except Exception as e:
        print(f"[警告] 搜尋 '{query}' 失敗: {e}")
    return found


def fetch_social_strictly_online():
    """抓取 FB、IG、Accupass 符合條件的線上研習"""
    results = []
    seen_urls = set()

    # 1. FB 公開社團/粉絲頁線上研習貼文
    fb_items = search_strictly_online_links(
        '臺南 ("線上研習" OR "Google Meet") AI (site:facebook.com)',
        max_items=3
    )
    for title, url in fb_items:
        if url not in seen_urls:
            seen_urls.add(url)
            results.append(f"• 🟢[FB社群] {title}\n  🔗 {url}")

    # 2. Instagram 線上研習
    ig_items = search_strictly_online_links(
        '臺南 ("線上研習" OR "線上工作坊") AI (site:instagram.com OR site:threads.net)',
        max_items=2
    )
    for title, url in ig_items:
        if url not in seen_urls:
            seen_urls.add(url)
            platform = "IG" if "instagram.com" in url else "Threads"
            results.append(f"• 🟢[{platform}社群] {title}\n  🔗 {url}")

    # 3. Accupass 活動通（免費線上講座/研習）
    try:
        acc_url = f"https://www.accupass.com/search?q={urllib.parse.quote('AI 線上研習')}"
        resp = requests.get(acc_url, headers=HEADERS, timeout=12)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            count = 0
            for link in soup.select("a[href*='/event/']"):
                title = link.get_text(strip=True)
                href = link["href"]
                if not title or len(title) <= 4:
                    continue
                if is_internal_school_only(title) or is_paid(title):
                    continue
                if is_ai_related(title) and is_strictly_online(title):
                    full_url = href if href.startswith("http") else f"https://www.accupass.com{href}"
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        results.append(f"• 🟢[Accupass線上] {title}\n  🔗 {full_url}")
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
                "text": text[:4950],
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

    today = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")
    print(f"=== 啟動【嚴格條件：臺南市 × 線上研習 × AI × 免費 × 開放報名 × 非校內】爬蟲 ({today_str}) ===")

    inservice_items = fetch_inservice_strictly_online(today)
    passport_items = fetch_tainan_passport_strictly_online(today)
    school_items = fetch_schools_strictly_online(today)
    social_items = fetch_social_strictly_online()

    sections = []

    sections.append(
        "📘【全國教師在職進修網（線上/免費）】\n"
        + ("\n".join(inservice_items) if inservice_items else "• 目前暫無開放報名中之免費線上課程")
    )
    sections.append(
        "📗【臺南市學習護照（線上/非校內）】\n"
        + ("\n".join(passport_items) if passport_items else "• 目前暫無開放報名中之線上課程")
    )
    sections.append(
        "🏫【各級學校與教育局公告（線上）】\n"
        + ("\n".join(school_items) if school_items else "• 目前暫無開放報名中之線上研習公告")
    )
    sections.append(
        "📱【社群與活動平台（FB / IG / 線上）】\n"
        + ("\n".join(social_items) if social_items else "• 目前暫無符合條件之公開線上活動貼文")
    )

    message = (
        f"☀️ 早安！{today_str}\n"
        f"🎯 嚴選篩選：【線上研習 ＋ AI相關 ＋ 免費 ＋ 報名中 ＋ 非校內】\n\n"
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
