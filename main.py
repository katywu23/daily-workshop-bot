import os
import re
import sys
import html
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timezone, timedelta

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

# 停用 SSL 警告（部分學校/政府網站憑證鏈不完整時確保穩定連線）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TW_TZ = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

# 全國 22 縣市於全國教師在職進修網之代碼與名稱
ALL_CITIES = [
    ("1", "臺北市"), ("2", "新北市"), ("9", "桃園市"), ("11", "臺中市"),
    ("18", "臺南市"), ("3", "高雄市"), ("5", "基隆市"), ("7", "新竹市"),
    ("8", "新竹縣"), ("10", "苗栗縣"), ("13", "彰化縣"), ("14", "南投縣"),
    ("17", "雲林縣"), ("15", "嘉義市"), ("16", "嘉義縣"), ("21", "屏東縣"),
    ("6", "宜蘭縣"), ("23", "花蓮縣"), ("22", "臺東縣"), ("20", "澎湖縣"),
    ("24", "金門縣"), ("25", "連江縣"),
]

# AI 與常見 AI 工具相關關鍵字
AI_KEYWORDS = [
    "AI", "人工智慧", "生成式", "AIGC", "AI工具", "智慧學習", "大語言模型", "LLM",
    "GPT", "ChatGPT", "Gemini", "NotebookLM", "Copilot", "Claude", "Canva",
    "Perplexity", "Gamma", "Suno", "Padlet", "Midjourney", "Grok", "Felo", "Poe",
    "MagicSchool", "Diffit", "Eduaide", "Brisk", "DALL-E", "Prompt", "提示詞", "Vibe Coding"
]

# 明確判定為「線上形式」之關鍵字
ONLINE_KEYWORDS = [
    "線上", "遠距", "視訊", "GOOGLE MEET", "MEET.GOOGLE", "TEAMS", "ZOOM",
    "WEBEX", "YOUTUBE直播", "線上直播", "會議室連結", "會議連結"
]

# 明確判定為「收費」之關鍵字
PAID_KEYWORDS = [
    "收費", "報名費", "學費", "自費", "NT$", "NTD", "元整", "定價", "早鳥價", "原價"
]

# 明確判定為「校內研習」之關鍵字
INTERNAL_KEYWORDS = [
    "校內研習", "校內進修", "校本研習", "僅限本校", "限校內", "僅限校內", "本校教師"
]

# 實體地址/實體場地判斷（用於檢查「上課地點 / 研習地點 / 地點」欄位，出現實體地址或校舍即排除）
PHYSICAL_ADDRESS_PATTERN = re.compile(
    r"(?:[路街巷弄]|大道|\d+之?\d*號|\d+樓|\d+F\b|[一二三四五六七八九十]+樓|"
    r"教室|會議室|禮堂|演講廳|視聽室|活動中心|圖書館|行政大樓|教學大樓|綜合大樓|"
    r"國小|國中|高中|高職|實小|實中|大學|學院|校區)"
)

# 用於標題或純文字的嚴格門牌地址判斷（避免誤殺「智慧教室」、「AI之路」等標題）
STRICT_STREET_ADDRESS_PATTERN = re.compile(
    r"(?:[縣市區鄉鎮].*?(?:[路街巷弄]|大道).*?\d*號|(?:[路街]|大道)[一二三四五六七八九十\d]*段?\d+號)"
)

MEETING_LINK_PATTERN = re.compile(
    r"(?:https?://)?(?:meet\.google\.com|zoom\.us|teams\.microsoft\.com|webex\.com|youtu\.be|youtube\.com/live)/[^\s<>\"'。，；)）]+",
    re.IGNORECASE,
)

LABELED_ONLINE_URL_PATTERN = re.compile(
    r"(?:會議連結|線上連結|視訊連結|直播連結|直播網址|會議網址|線上會議|Google\s*Meet|Teams|Zoom)[^\n]{0,25}?(https?://[^\s<>\"'。，；)）]+)",
    re.IGNORECASE,
)


def extract_meeting_url(text: str) -> str:
    """從課程簡介或注意事項擷取會議/線上連結網址"""
    if not text:
        return ""
    m = MEETING_LINK_PATTERN.search(text)
    if m:
        url = m.group(0).rstrip(".,;。，；)）")
        return url if url.startswith("http") else f"https://{url}"
    m2 = LABELED_ONLINE_URL_PATTERN.search(text)
    if m2:
        return m2.group(1).rstrip(".,;。，；)）")
    return ""


def decode_response(resp: requests.Response) -> str:
    """安全解碼網頁內容，徹底避免 UTF-8 被誤判為 Big5 導致的亂碼"""
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "utf-8" in content_type or "utf8" in content_type:
        return resp.content.decode("utf-8", errors="replace")
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ["cp950", "big5", resp.apparent_encoding or "utf-8"]:
        try:
            return resp.content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return resp.content.decode("utf-8", errors="replace")


def is_ai_related(text: str) -> bool:
    if re.search(r"(?<![A-Za-z])AI(?![A-Za-z])", text, re.IGNORECASE):
        return True
    text_upper = text.upper()
    return any(kw.upper() in text_upper for kw in AI_KEYWORDS if kw != "AI")


def is_strictly_online(text: str) -> bool:
    """檢查是否為線上研習或附有線上會議連結"""
    text_upper = text.upper()
    return any(kw in text_upper for kw in ONLINE_KEYWORDS) or bool(MEETING_LINK_PATTERN.search(text))


def has_physical_address(location_text: str) -> bool:
    """檢查地點欄位或內文地點描述是否包含實體上課地址或實體場地"""
    if not location_text:
        return False
    cleaned = re.sub(r"電話[:：]?\s*[\d\-()#]+", "", location_text)
    cleaned = re.sub(r"20\d{2}[/-]\d{1,2}[/-]\d{1,2}", "", cleaned)
    cleaned = re.sub(r"1\d{2}[/-]\d{1,2}[/-]\d{1,2}", "", cleaned)
    cleaned_no_virtual_room = re.sub(r"(?:線上|Google\s*Meet|Teams|Zoom)\s*會議室", "", cleaned, flags=re.IGNORECASE)
    return bool(PHYSICAL_ADDRESS_PATTERN.search(cleaned_no_virtual_room))


def is_paid(text: str) -> bool:
    """檢查是否需要收費（排除收費課程）"""
    if not text:
        return False
    for kw in PAID_KEYWORDS:
        if kw in text:
            if f"不{kw}" in text or f"免{kw}" in text or "免費" in text:
                continue
            return True
    fee_nums = re.findall(r"(\d[\d,]*)\s*元", text)
    for n in fee_nums:
        val = int(n.replace(",", ""))
        if val > 0:
            return True
    return False


def is_internal_school_only(text: str) -> bool:
    """檢查是否為校內研習"""
    return any(kw in text for kw in INTERNAL_KEYWORDS)


def has_past_year(text: str, today: date) -> bool:
    """檢查文字是否明確提到已過去的年份（例如 114年、114學年度、2025年）"""
    if not text:
        return False
    current_roc = today.year - 1911
    for m in re.finditer(r"(1\d{2})\s*(?:年|學年度)", text):
        yr = int(m.group(1))
        if yr < current_roc:
            return True
    for m in re.finditer(r"(20\d{2})\s*(?:年|[-/.](?:0?[1-9]|1[0-2]))", text):
        yr = int(m.group(1))
        if yr < today.year:
            return True
    return False


def parse_date_safely(date_str: str):
    """解析西元或民國日期格式"""
    if not date_str:
        return None
    match_ad = re.search(r"(20\d{2})[-/.年](0?[1-9]|1[0-2])[-/.月](0?[1-9]|[12]\d|3[01])", date_str)
    if match_ad:
        try:
            return date(int(match_ad.group(1)), int(match_ad.group(2)), int(match_ad.group(3)))
        except ValueError:
            return None

    match_roc = re.search(r"(1\d{2})[-/.年](0?[1-9]|1[0-2])[-/.月](0?[1-9]|[12]\d|3[01])", date_str)
    if match_roc:
        try:
            return date(int(match_roc.group(1)) + 1911, int(match_roc.group(2)), int(match_roc.group(3)))
        except ValueError:
            return None
    return None


def extract_all_dates(text: str, include_month_day_year: int = None):
    """從文字中擷取所有日期（西元或民國，可選補充僅有 M/D 或 M月D日 的短日期）"""
    dates = []
    for m in re.finditer(r"(20\d{2})\s*[-/.年]\s*(0?[1-9]|1[0-2])\s*[-/.月]\s*(0?[1-9]|[12]\d|3[01])", text):
        try:
            dates.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    for m in re.finditer(r"(1\d{2})\s*[-/.年]\s*(0?[1-9]|1[0-2])\s*[-/.月]\s*(0?[1-9]|[12]\d|3[01])", text):
        try:
            dates.append(date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    if include_month_day_year:
        stripped = re.sub(r"(?:20\d{2}|1\d{2})\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}日?", "", text)
        for m in re.finditer(r"(?<![\d/:])(1[0-2]|0?[1-9])\s*[/月]\s*(3[01]|[12]\d|0?[1-9])(?![\d/:])", stripped):
            try:
                dates.append(date(include_month_day_year, int(m.group(1)), int(m.group(2))))
            except ValueError:
                pass
    return dates


def extract_field_value(soup: BeautifulSoup, label_prefix: str) -> str:
    """從全國教師在職進修網 CourseView.aspx 擷取指定欄位文字"""
    for td in soup.select("td.cw_table_sch"):
        if label_prefix in td.get_text(strip=True):
            next_td = td.find_next_sibling("td")
            if next_td:
                return next_td.get_text(" ", strip=True)
    return ""


# ==============================================================================
# 1. 全國教師在職進修資訊網 (全臺各縣市 + 嚴格條件 + 零亂碼)
# ==============================================================================
def _query_inservice_city(city_code: str, city_name: str, today: date):
    """查詢單一縣市的全國教師在職進修網課程列表"""
    url = f"https://www1.inservice.edu.tw/script/IndexQuery.aspx?city={city_code}"
    candidates = {}
    session = requests.Session()
    try:
        resp = session.get(url, headers=HEADERS, timeout=12)
        html_init = decode_response(resp)
        pages = [html_init]

        vs_match = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', html_init)
        ev_match = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', html_init)
        if vs_match and ev_match:
            for kw in ["AI", "線上", "Gemini", "NotebookLM", "ChatGPT", "Canva"]:
                payload = {
                    "__EVENTTARGET": "",
                    "__EVENTARGUMENT": "",
                    "__LASTFOCUS": "",
                    "__VIEWSTATE": vs_match.group(1),
                    "__EVENTVALIDATION": ev_match.group(1),
                    "tbCourseName": kw,
                    "tbSchoolName": "",
                    "tbStartDateFrom": "",
                    "tbStartDateTo": "",
                    "ddlSubDist": "",
                    "ddlSchoolLevelByCity": "",
                    "ddlCourseClass": "",
                    "ddlCourseTag": "",
                    "Button1": "查詢",
                }
                r_post = session.post(url, data=payload, headers=HEADERS, timeout=12)
                pages.append(decode_response(r_post))

        for page_html in pages:
            soup = BeautifulSoup(page_html, "html.parser")
            for row in soup.select("tr.table_bottom"):
                cells = row.select("td.wide-only")
                if len(cells) < 8:
                    continue
                cid = cells[1].get_text(strip=True)
                title = cells[2].get_text(strip=True)
                course_type = cells[3].get_text(strip=True)
                start_date = cells[4].get_text(strip=True)
                end_date = cells[5].get_text(strip=True)
                unit = cells[6].get_text(strip=True)

                if not cid or cid in candidates:
                    continue
                if has_past_year(title, today):
                    continue
                if is_internal_school_only(course_type) or is_internal_school_only(title):
                    continue
                c_end = parse_date_safely(end_date)
                if c_end and c_end < today:
                    continue
                # 不先依標題過濾，保留非校內且未過期之候選課程，於詳細頁檢查「課程簡介」與「課程表及注意事項」
                candidates[cid] = {
                    "cid": cid,
                    "city": city_name,
                    "title": title,
                    "start_date": start_date,
                    "end_date": end_date,
                    "unit": unit,
                }
    except Exception as e:
        print(f"[警告] 查詢 {city_name} 在職進修網失敗: {e}")
    return list(candidates.values())


def _verify_inservice_detail(item: dict, today: date, session: requests.Session):
    """進入 CourseView.aspx 逐一驗證：
    - 若『課程簡介』或『課程表及注意事項』有會議/線上連結網址 -> 直接視為線上研習並納入（不因主辦單位預設填寫地址而排除）
    - 若無會議連結 -> 維持檢查『上課地點』不能出現實體地址且須為線上/數位遠距，並符合 AI/AI工具相關條件（標題、課程簡介或課程表及注意事項）
    - 全數皆須免費、非校內研習、且尚未過期（尚在報名中或推播當日）
    """
    cid = item["cid"]
    detail_url = f"https://www1.inservice.edu.tw/NAPP/CourseView.aspx?cid={cid}"
    try:
        resp = session.get(detail_url, headers=HEADERS, timeout=12)
        page_html = decode_response(resp)
        soup = BeautifulSoup(page_html, "html.parser")

        course_date_text = extract_field_value(soup, "上課日期")
        time_mode_text = extract_field_value(soup, "課程時段")
        location_text = extract_field_value(soup, "上課地點")
        intro_text = extract_field_value(soup, "課程簡介")
        notes_text = extract_field_value(soup, "課程表")
        reg_date_text = extract_field_value(soup, "報名日期")
        online_reg_text = extract_field_value(soup, "線上報名")
        target_text = extract_field_value(soup, "參加對象")
        fee_text = extract_field_value(soup, "繳費金額")

        # 排除校內研習
        if any(is_internal_school_only(t) for t in [item["title"], target_text, intro_text]):
            return None

        # 排除收費課程
        if fee_text:
            fee_num = re.sub(r"[^\d]", "", fee_text)
            if fee_num and int(fee_num) > 0:
                return None

        # 檢查「課程簡介」或「課程表及注意事項」是否有會議/線上連結網址
        intro_and_notes = f"{intro_text}\n{notes_text}"
        meet_url = extract_meeting_url(intro_and_notes)
        has_meeting_link = bool(meet_url)

        ai_matched = is_ai_related(f"{item['title']} {intro_text} {notes_text}")

        if has_meeting_link:
            # 只要簡介或注意事項明確給出線上會議連結，即視為線上研習納入蒐集
            pass
        else:
            # 無直接會議連結時：須為 AI 相關、不可有實體上課地址，且須為線上研習
            if not ai_matched:
                return None
            if has_physical_address(location_text):
                return None
            combined_online_check = f"{item['title']} {time_mode_text} {location_text} {intro_text} {notes_text}"
            if "數位遠距教學" not in time_mode_text and not is_strictly_online(combined_online_check):
                return None

        # 嚴格過濾研習時間：必須能明確解析日期，且結束日 >= today（含推播當日）
        c_dates = extract_all_dates(course_date_text or f"{item['start_date']} {item['end_date']}")
        c_start = c_dates[0] if c_dates else parse_date_safely(item["start_date"])
        c_end = c_dates[-1] if c_dates else parse_date_safely(item["end_date"])

        if not c_end or c_end < today:
            return None

        is_today_course = bool(c_start and c_start <= today <= c_end)
        reg_dates = extract_all_dates(reg_date_text)
        reg_end = reg_dates[-1] if reg_dates else None
        is_in_reg_period = (reg_end is not None and reg_end >= today)

        # 只要「尚在報名區間內」或「研習時間為推播當日（或已附會議連結免事先報名）」皆保留
        no_reg_needed = any(k in intro_and_notes for k in ["無需事先報名", "免事先報名", "無需報名", "免報名", "直接點選", "自由參加"])
        if not (is_in_reg_period or is_today_course or (has_meeting_link and no_reg_needed)):
            return None

        # 若未開放線上報名且無其他外部報名連結且非今日研習/免報名，則略過
        if "未開放線上報名" in online_reg_text and not is_today_course and not no_reg_needed:
            has_external_reg = any(k in intro_and_notes for k in ["forms.gle", "報名網址", "報名表單", "線上報名"])
            if not has_external_reg:
                return None

        meet_line = f"\n  💻 會議連結: {meet_url}" if meet_url else ""
        loc_display = "線上會議（詳見會議連結）" if (has_meeting_link and has_physical_address(location_text)) else (location_text or "線上遠距")
        badge = "🔥[今日研習]" if is_today_course else "🟢[線上研習]"

        return (
            is_today_course,
            ai_matched,
            has_meeting_link,
            c_start or today,
            f"• {badge} {item['title']}\n"
            f"  📅 研習日期: {item['start_date']}｜🏫 {item['unit']}\n"
            f"  📍 上課地點: {loc_display}{meet_line}\n"
            f"  🔗 {detail_url}"
        )
    except Exception:
        return None


def fetch_inservice_all_cities(today: date):
    """平行搜尋全臺各縣市的全國教師在職進修網 AI/AI工具 線上研習"""
    all_candidates = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_query_inservice_city, code, name, today) for code, name in ALL_CITIES]
        for f in as_completed(futures):
            for item in f.result():
                all_candidates[item["cid"]] = item

    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)

    verified = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(_verify_inservice_detail, item, today, session) for item in all_candidates.values()]
        for f in as_completed(futures):
            res = f.result()
            if res:
                verified.append(res)

    # 排序：今日研習優先 -> AI/AI工具相關優先 -> 附有會議連結優先 -> 日期由近到遠
    verified.sort(key=lambda x: (not x[0], not x[1], not x[2], x[3]))
    return [x[4] for x in verified[:15]]


# ==============================================================================
# 2. 臺南市政府教育局學習護照 (e-learning.tn.edu.tw)
# ==============================================================================
def fetch_tainan_passport_online(today: date):
    """抓取臺南市學習護照：AI 相關、線上研習、無實體地址、非校內、研習日期 >= 今日"""
    url = "https://e-learning.tn.edu.tw/User/OnlineListQuickly.aspx"
    results = []
    seen_ocids = set()
    session = requests.Session()
    roc_year = str(today.year - 1911)

    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        html_init = decode_response(resp)

        vs = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', html_init)
        vsg = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]+)"', html_init)
        ev = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', html_init)

        if vs and ev:
            for kw in ["AI", "生成式", "線上", "數位", "Gemini"]:
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
                soup = BeautifulSoup(decode_response(r_post), "html.parser")

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
                    attr_text = cols[5].get_text(" ", strip=True)
                    note_text = cols[7].get_text(" ", strip=True)

                    if not ocid or ocid in seen_ocids:
                        continue
                    if has_past_year(title, today):
                        continue
                    if is_internal_school_only(attr_text) or is_internal_school_only(title):
                        continue

                    # 必須有明確日期且 >= 今日（含推播當日）
                    c_date = parse_date_safely(course_date_str)
                    if not c_date or c_date < today:
                        continue

                    # 若有出現實體上課地址或實體教室，就不要蒐集
                    if has_physical_address(place_text):
                        continue

                    full_content = f"{title} {place_text} {note_text}"
                    if not is_ai_related(full_content) or not is_strictly_online(full_content):
                        continue

                    seen_ocids.add(ocid)
                    detail_url = f"https://e-learning.tn.edu.tw/User/CourseDetail.aspx?OCID={ocid}"
                    is_today = (c_date == today)
                    badge = "🔥[今日研習]" if is_today else "🟢[線上研習]"
                    results.append((
                        is_today,
                        c_date,
                        f"• {badge} {title}\n"
                        f"  📅 研習日期: {course_date_str}｜🏫 {school_text}（代號:{ocid}）\n"
                        f"  🔗 {detail_url}"
                    ))
    except Exception as e:
        print(f"[警告] 臺南市學習護照抓取失敗: {e}")

    results.sort(key=lambda x: (not x[0], x[1]))
    return [x[2] for x in results[:5]]


# ==============================================================================
# 3. 各級學校和教育單位（最新消息 / 公佈欄 / 研習訊息，跨各縣市）
# ==============================================================================
def search_yahoo_results(query: str, max_items: int = 8, use_month_filter: bool = True):
    """透過 Yahoo 奇摩搜尋取得 (title, url, snippet) 清單（含 3 次重試，避免暫時性 500 錯誤造成空結果）"""
    encoded_q = urllib.parse.quote(query)
    url = f"https://tw.search.yahoo.com/search?p={encoded_q}" + ("&btf=m" if use_month_filter else "")
    found = []
    seen_urls = set()

    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code != 200:
                print(f"[警告] Yahoo 搜尋 '{query}' 狀態碼 {resp.status_code} (第 {attempt + 1} 次)")
                time.sleep(1.2)
                continue
            soup = BeautifulSoup(decode_response(resp), "html.parser")
            for card in soup.select("div.algo"):
                a_tag = card.select_one("a[href*='RU='], h3 a, a:has(h3)")
                if not a_tag or not a_tag.has_attr("href"):
                    continue
                href = a_tag["href"]
                ru_match = re.search(r"RU=([^/]+)/RK=", href)
                raw_url = urllib.parse.unquote(ru_match.group(1)) if ru_match else href
                if any(skip in raw_url.lower() for skip in [
                    "yahoo.com", "bing.com", "doubleclick.net", "googleadservices",
                    "uservoice.com", "inservice.edu.tw", "e-learning.tn.edu.tw"
                ]):
                    continue
                h3_tag = card.select_one("h3")
                title = html.unescape(h3_tag.get_text(" ", strip=True)) if h3_tag else html.unescape(a_tag.get_text(" ", strip=True))
                comp_text = card.select_one("div.compText")
                snippet = html.unescape(comp_text.get_text(" ", strip=True)) if comp_text else ""

                if title and raw_url not in seen_urls:
                    seen_urls.add(raw_url)
                    found.append((title, raw_url, snippet))
                    if len(found) >= max_items:
                        return found
            if found:
                return found
            time.sleep(0.8)
        except Exception as e:
            print(f"[警告] Yahoo 搜尋 '{query}' 失敗 (第 {attempt + 1} 次): {e}")
            time.sleep(1.2)

    return found


def _verify_school_webpage(title: str, url: str, snippet: str, today: date):
    """讀取學校或教育單位公告頁內容，嚴格驗證：當日或未來研習、無過去年份(如114年)、免費、無地址、非校內"""
    if has_past_year(title, today) or has_past_year(snippet, today):
        return None
    if is_internal_school_only(title) or is_paid(title) or STRICT_STREET_ADDRESS_PATTERN.search(title):
        return None
    if not is_ai_related(f"{title} {snippet}"):
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(decode_response(resp), "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        # 優先取主要文章區塊，避免抓到側邊欄或全站頁尾年份
        main_node = soup.select_one("main, article, .entry-content, .mcont, #main") or soup.body or soup
        lines = [line.strip() for line in main_node.get_text("\n", strip=True).splitlines() if line.strip()]
        body_text = "\n".join(lines)

        if is_internal_school_only(body_text) or is_paid(body_text):
            return None

        # 檢查內文是否有明確的「上課地點/研習地點」地址
        loc_matches = re.findall(r"(?:上課地點|研習地點|活動地點|地點)[:：]\s*([^\n。；]{2,40})", body_text)
        for loc_val in loc_matches:
            if has_physical_address(loc_val):
                return None

        if not is_strictly_online(f"{title}\n{body_text[:2500]}"):
            return None

        # 過濾掉網站系統的發佈/更新/今日日期雜訊，只保留與研習/活動/報名相關的日期行
        filtered_lines = [
            ln for ln in lines
            if not any(noise in ln for noise in ["更新日期", "最後更新", "發佈日期", "發布日期", "刊登日期", "今日日期", "瀏覽", "Copyright", "版權"])
        ]
        event_lines = [
            ln for ln in filtered_lines
            if any(k in ln for k in ["研習時間", "研習日期", "活動時間", "活動日期", "辦理時間", "課程時間", "報名日期", "報名期限", "報名時間", "場次", "時間：", "日期："])
        ]

        # 若無標籤行，從主文章前段尋找活動日期
        target_date_text = "\n".join(event_lines) if event_lines else "\n".join(filtered_lines[:35])

        # 若明確寫著過去年份（如 114年、114學年度、2025年），直接剔除
        if has_past_year(target_date_text, today):
            return None

        extracted_dates = extract_all_dates(target_date_text)
        # 嚴格要求：必須有明確可驗證的日期，且最大日期 >= today（含推播當日）
        if not extracted_dates:
            return None
        if max(extracted_dates) < today:
            return None

        is_today = today in extracted_dates
        badge = "🔥[今日研習]" if is_today else "🟢[教育單位]"
        return f"• {badge} {title}\n  🔗 {url}"
    except Exception:
        return None


def fetch_schools_and_edu_units(today: date):
    """抓取全國各級學校、教育單位之最新消息 / 公佈欄 / 研習訊息"""
    results = []
    seen_urls = set()

    # (A) 臺南市教育局資訊中心公告系統 (bulletin.tn.edu.tw)
    bulletin_url = "https://bulletin.tn.edu.tw/SearchBulletin.aspx"
    session = requests.Session()
    try:
        r1 = session.get(bulletin_url, headers=HEADERS, timeout=12)
        html1 = decode_response(r1)
        vs = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', html1)
        vsg = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]+)"', html1)
        ev = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', html1)

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
                "ctl00$ContentPlaceHolder1$rbl_BulletinType": "1",
                "ctl00$ContentPlaceHolder1$ddl_FilterString": "BulletinSubject",
                "ctl00$ContentPlaceHolder1$tb_KeyWord": "AI",
                "ctl00$ContentPlaceHolder1$rbl_Status": "1",
                "ctl00$ContentPlaceHolder1$cb_Poster": "on",
                "ctl00$ContentPlaceHolder1$btn_Query": "查詢",
            }
            r2 = session.post(bulletin_url, data=payload, headers=HEADERS, timeout=12)
            soup = BeautifulSoup(decode_response(r2), "html.parser")
            for row in soup.select("#ctl00_ContentPlaceHolder1_gv_BList tr")[1:]:
                cols = row.find_all("td")
                if len(cols) < 5:
                    continue
                a_tag = cols[1].find("a")
                if not a_tag:
                    continue
                title = a_tag.get_text(strip=True)
                period_str = cols[3].get_text(" ", strip=True)
                href = a_tag.get("href", "")

                if has_past_year(title, today) or is_internal_school_only(title) or STRICT_STREET_ADDRESS_PATTERN.search(title):
                    continue
                p_dates = extract_all_dates(period_str)
                if not p_dates or max(p_dates) < today:
                    continue

                full_url = f"https://bulletin.tn.edu.tw/{href.lstrip('/')}"
                b_resp = session.get(full_url, headers=HEADERS, timeout=10)
                b_text = BeautifulSoup(decode_response(b_resp), "html.parser").get_text("\n", strip=True)

                if is_paid(b_text) or is_internal_school_only(b_text):
                    continue
                loc_matches = re.findall(r"(?:研習地點|上課地點|地點)[:：]\s*([^\n。；]{2,40})", b_text)
                if any(has_physical_address(loc) for loc in loc_matches):
                    continue
                if not is_strictly_online(f"{title}\n{b_text}"):
                    continue

                event_lines = re.findall(r"(?:研習時間|研習日期|活動時間|活動日期)[:：][^\n]{0,60}", b_text)
                if event_lines:
                    ev_text = "\n".join(event_lines)
                    if has_past_year(ev_text, today):
                        continue
                    ev_dates = extract_all_dates(ev_text)
                    if ev_dates and max(ev_dates) < today:
                        continue

                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    results.append(f"• 🟢[南市教育局公告] {title}\n  🔗 {full_url}")
                    if len(results) >= 3:
                        break
    except Exception as e:
        print(f"[警告] 臺南市教育局公告系統抓取失敗: {e}")

    # (B) 全國各級學校與教育單位最新消息 / 公佈欄 / 研習訊息 (site:edu.tw)
    roc_year = today.year - 1911
    queries = [
        f'"線上研習" AI {roc_year}年 (最新消息 OR 公佈欄 OR 研習訊息) site:edu.tw',
        f'("線上研習" OR "Google Meet") (AI OR Gemini OR ChatGPT) {roc_year}年 site:edu.tw',
    ]
    candidates = []
    for q in queries:
        for title, url, snippet in search_yahoo_results(q, max_items=6):
            if url not in seen_urls:
                seen_urls.add(url)
                candidates.append((title, url, snippet))

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_verify_school_webpage, t, u, s, today) for t, u, s in candidates]
        for f in as_completed(futures):
            res = f.result()
            if res:
                results.append(res)
                if len(results) >= 6:
                    break

    return results[:6]


# ==============================================================================
# 4. 各社群平台 (FB、IG、Threads，各縣市線上 AI 研習，已移除 Accupass)
# ==============================================================================
def is_specific_social_post_url(url: str) -> bool:
    """只保留具體的單篇貼文、活動或影片連結，排除粉絲專頁首頁與社團首頁"""
    lower = url.lower()
    if "facebook.com" in lower:
        return any(seg in lower for seg in [
            "/posts/", "/permalink/", "/events/", "/photos/", "/videos/",
            "story_fbid=", "fbid=", "/reel/"
        ])
    if "instagram.com" in lower:
        return any(seg in lower for seg in ["/p/", "/reel/"])
    if "threads.net" in lower or "threads.com" in lower:
        return "/post/" in lower
    return False


def fetch_social_platforms(today: date):
    """抓取 FB、IG、Threads 上關於「線上研習 + AI + 免費 + 非校內 + 無地址 + 未來/當日日期」資訊"""
    results = []
    seen_urls = set()
    roc_year = today.year - 1911

    social_queries = [
        ("FB", f'("線上研習" OR "線上工作坊" OR "Google Meet") AI 研習 ({roc_year}年 OR {today.year}) site:facebook.com', True),
        ("FB", f'"線上研習" AI 報名 {today.year} site:facebook.com', True),
        ("IG/Threads", f'("線上研習" OR "線上講座" OR "線上工作坊" OR "線上課程") AI (site:instagram.com OR site:threads.com OR site:threads.net)', False),
    ]

    for platform, q, use_month in social_queries:
        for title, url, snippet in search_yahoo_results(q, max_items=10, use_month_filter=use_month):
            if url in seen_urls:
                continue
            if not is_specific_social_post_url(url):
                continue

            combined = f"{title} {snippet}"

            if has_past_year(combined, today):
                continue
            if is_internal_school_only(combined) or is_paid(combined) or STRICT_STREET_ADDRESS_PATTERN.search(combined):
                continue
            if not is_ai_related(combined) or not is_strictly_online(combined):
                continue

            # 若摘要內有明確活動/研習日期，確認未過期（排除摘要開頭的貼文發佈日期 e.g. "2026年10月20日 ·"）
            post_date_match = re.match(r"^(\d{4}年\d{1,2}月\d{1,2}日|\d+\s*(?:小時|天)前)\s*·\s*", snippet)
            post_prefix = post_date_match.group(1) if post_date_match else ""
            snippet_body = snippet[post_date_match.end():] if post_date_match else snippet

            event_dates = extract_all_dates(f"{title} {snippet_body}", include_month_day_year=today.year)
            if event_dates:
                if max(event_dates) < today:
                    continue
                date_hint = f"（{max(event_dates).strftime('%Y/%m/%d')}）"
            else:
                # 摘要未截到具體活動日期時，僅保留本月或幾天內的近期貼文
                is_recent_post = ("前" in post_prefix) or (f"{today.year}年{today.month}月" in post_prefix)
                if not is_recent_post:
                    continue
                date_hint = ""

            # 若地點欄明確寫了實體地址則略過
            loc_matches = re.findall(r"(?:地點|上課地點|研習地點)[:：]\s*([^\n。；]{2,40})", combined)
            if any(has_physical_address(loc) for loc in loc_matches):
                continue

            seen_urls.add(url)
            tag = "IG/Threads" if ("instagram.com" in url or "threads.net" in url or "threads.com" in url) else platform
            is_today = today in event_dates
            badge = "🔥[今日研習]" if is_today else f"🟢[{tag}]"
            results.append(f"• {badge} {title}{date_hint}\n  🔗 {url}")
            if len(results) >= 6:
                break

    return results


# ==============================================================================
# LINE Messaging API 推播
# ==============================================================================
def send_line_push_message(token: str, user_id: str, messages_list: list):
    """透過 LINE Messaging API 發送 Push Message（支援單次最多 5 則訊息氣泡，避免超過 5000 字元截斷）"""
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {
        "to": user_id,
        "messages": [
            {"type": "text", "text": msg[:4950]}
            for msg in messages_list[:5]
            if msg.strip()
        ],
    }
    response = requests.post(url, headers=headers, json=payload, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"LINE API 發送失敗 ({response.status_code}): {response.text}")
    print("✅ LINE 推播訊息發送成功！")


def main():
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")

    today = datetime.now(TW_TZ).date()
    today_str = today.strftime("%Y-%m-%d")
    print(f"=== 啟動【各縣市 × 純線上研習 × AI × 免費 × 報名中/今日 × 非校內 × 無實體地址】爬蟲 ({today_str}) ===")

    inservice_items = fetch_inservice_all_cities(today)
    passport_items = fetch_tainan_passport_online(today)
    school_items = fetch_schools_and_edu_units(today)
    social_items = fetch_social_platforms(today)

    sections = [
        "📘【1. 全國教師在職進修資訊網（各縣市線上/含會議連結/AI工具）】\n"
        + ("\n".join(inservice_items) if inservice_items else "• 目前暫無符合所有嚴選條件之課程"),
        "📗【2. 臺南市政府教育局學習護照（線上AI）】\n"
        + ("\n".join(passport_items) if passport_items else "• 目前暫無符合所有嚴選條件之課程"),
        "🏫【3. 各級學校與教育單位（最新消息/公佈欄）】\n"
        + ("\n".join(school_items) if school_items else "• 目前暫無符合所有嚴選條件之公告"),
        "📱【4. 各社群平台（FB / IG / Threads）】\n"
        + ("\n".join(social_items) if social_items else "• 目前暫無符合所有嚴選條件之貼文"),
    ]

    header = (
        f"☀️ 早安！{today_str}\n"
        f"🎯 今日嚴選：【各縣市 ＋ 線上研習/含會議連結 ＋ AI與AI工具 ＋ 免費 ＋ 推播當日及報名中 ＋ 非校內】"
    )
    full_message = f"{header}\n\n" + "\n\n".join(sections)

    print("\n--- 預覽推播內容 ---")
    print(full_message)
    print("--------------------\n")

    if not token or not user_id:
        print("[提示] 未設定 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID 環境變數，略過實際發送。")
        sys.exit(0)

    if len(full_message) <= 4800:
        messages_to_send = [full_message]
    else:
        messages_to_send = [f"{header}\n\n{sections[0]}"] + sections[1:]

    send_line_push_message(token, user_id, messages_to_send)


if __name__ == "__main__":
    main()
