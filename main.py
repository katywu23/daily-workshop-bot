import os
import re
import sys
import html
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timezone, timedelta

import requests
import urllib3
from bs4 import BeautifulSoup

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

# AI 相關關鍵字
AI_KEYWORDS = [
    "AI", "人工智慧", "生成式", "ChatGPT", "Gemini", "NotebookLM",
    "Copilot", "Claude", "Canva", "智慧學習", "大語言模型", "AIGC"
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
    r"(?:meet\.google\.com|zoom\.us|teams\.microsoft\.com|webex\.com)",
    re.IGNORECASE,
)


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
    text_upper = text.upper()
    return any(kw.upper() in text_upper for kw in AI_KEYWORDS)


def is_strictly_online(text: str) -> bool:
    """檢查是否為線上研習或附有線上會議連結"""
    text_upper = text.upper()
    return any(kw in text_upper for kw in ONLINE_KEYWORDS) or bool(MEETING_LINK_PATTERN.search(text))


def has_physical_address(location_text: str) -> bool:
    """檢查地點欄位或內文地點描述是否包含實體上課地址或實體場地"""
    if not location_text:
        return False
    # 排除電話/分機/日期等數字干擾
    cleaned = re.sub(r"電話[:：]?\s*[\d\-()#]+", "", location_text)
    cleaned = re.sub(r"20\d{2}[/-]\d{1,2}[/-]\d{1,2}", "", cleaned)
    cleaned = re.sub(r"1\d{2}[/-]\d{1,2}[/-]\d{1,2}", "", cleaned)
    # 若明確寫著「線上Google Meet會議室」且沒有路街號，不算實體地址
    cleaned_no_virtual_room = re.sub(r"(?:線上|Google\s*Meet|Teams|Zoom)\s*會議室", "", cleaned, flags=re.IGNORECASE)
    return bool(PHYSICAL_ADDRESS_PATTERN.search(cleaned_no_virtual_room))


def is_paid(text: str) -> bool:
    """檢查是否需要收費（排除收費課程）"""
    if not text:
        return False
    # 若明確標示 0 元或免費，且沒有收費字樣則放行
    for kw in PAID_KEYWORDS:
        if kw in text:
            if f"不{kw}" in text or f"免{kw}" in text or "免費" in text:
                continue
            return True
    # 檢查是否有金額如 500元、1,200元（排除 0元、0 元）
    fee_nums = re.findall(r"(\d[\d,]*)\s*元", text)
    for n in fee_nums:
        val = int(n.replace(",", ""))
        if val > 0:
            return True
    return False


def is_internal_school_only(text: str) -> bool:
    """檢查是否為校內研習"""
    return any(kw in text for kw in INTERNAL_KEYWORDS)


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


def extract_all_dates(text: str):
    """從文字中擷取所有日期（西元或民國）"""
    dates = []
    for m in re.finditer(r"(20\d{2})[-/.年](0?[1-9]|1[0-2])[-/.月](0?[1-9]|[12]\d|3[01])", text):
        try:
            dates.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    for m in re.finditer(r"(1\d{2})[-/.年](0?[1-9]|1[0-2])[-/.月](0?[1-9]|[12]\d|3[01])", text):
        try:
            dates.append(date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))))
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
# 1. 全國教師在職進修資訊網 (全臺各縣市 + 嚴格條件 1~5 + 零亂碼)
# ==============================================================================
def _query_inservice_city(city_code: str, city_name: str):
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
            for kw in ["AI", "線上"]:
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
                if is_internal_school_only(course_type) or is_internal_school_only(title):
                    continue
                if not is_ai_related(title):
                    continue
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


def _verify_inservice_detail(item: dict, today: date):
    """進入 CourseView.aspx 逐一驗證：無實體上課地址、線上研習、免費、非校內、尚在報名或推播當日"""
    cid = item["cid"]
    detail_url = f"https://www1.inservice.edu.tw/NAPP/CourseView.aspx?cid={cid}"
    try:
        resp = requests.get(detail_url, headers=HEADERS, timeout=12)
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

        # 條件 5: 排除校內研習
        if any(is_internal_school_only(t) for t in [item["title"], target_text, intro_text]):
            return None

        # 條件 4: 排除收費課程
        if fee_text:
            fee_num = re.sub(r"[^\d]", "", fee_text)
            if fee_num and int(fee_num) > 0:
                return None

        # 條件 2: 若有出現實體上課地址，就不要蒐集！
        if has_physical_address(location_text):
            return None

        # 條件 2 & 3: 必須為線上研習（課程時段為數位遠距教學，或地點/簡介/注意事項有明確線上研習或會議連結）
        combined_online_check = f"{item['title']} {time_mode_text} {location_text} {intro_text} {notes_text}"
        if "數位遠距教學" not in time_mode_text and not is_strictly_online(combined_online_check):
            return None

        # 條件 1 & 3: 檢查課程日期與報名區間（包含推播當日研習）
        c_dates = extract_all_dates(course_date_text or f"{item['start_date']} {item['end_date']}")
        c_start = c_dates[0] if c_dates else parse_date_safely(item["start_date"])
        c_end = c_dates[-1] if c_dates else parse_date_safely(item["end_date"])

        is_today_course = bool(c_start and c_end and c_start <= today <= c_end)
        if c_end and c_end < today:
            return None

        reg_dates = extract_all_dates(reg_date_text)
        reg_end = reg_dates[-1] if reg_dates else None
        is_in_reg_period = (reg_end is None or reg_end >= today)

        # 只要「尚在報名區間內」或「研習時間就是推播當日」皆保留
        if not (is_in_reg_period or is_today_course):
            return None

        # 若未開放線上報名且無其他表單連結且非今日研習，則略過
        if "未開放線上報名" in online_reg_text and not is_today_course:
            has_external_reg = any(k in f"{intro_text} {notes_text}" for k in ["forms.gle", "報名網址", "報名表單", "線上報名"])
            if not has_external_reg:
                return None

        # 嘗試擷取直接線上會議連結
        meet_match = re.search(r"(?:https?://)?(?:meet\.google\.com|zoom\.us|teams\.microsoft\.com|webex\.com)/[^\s<>\"'。，；)]+", f"{intro_text} {notes_text}", re.IGNORECASE)
        if meet_match:
            meet_url = meet_match.group(0)
            if not meet_url.startswith("http"):
                meet_url = f"https://{meet_url}"
            meet_line = f"\n  💻 會議連結: {meet_url}"
        else:
            meet_line = ""
        badge = "🔥[今日研習]" if is_today_course else "🟢[線上研習]"

        return (
            is_today_course,
            c_start or today,
            f"• {badge} {item['title']}\n"
            f"  📅 研習日期: {item['start_date']}｜🏫 {item['unit']}\n"
            f"  📍 上課地點: {location_text or '線上遠距'}{meet_line}\n"
            f"  🔗 {detail_url}"
        )
    except Exception:
        return None


def fetch_inservice_all_cities(today: date):
    """平行搜尋全臺各縣市的全國教師在職進修網 AI 線上研習"""
    all_candidates = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_query_inservice_city, code, name) for code, name in ALL_CITIES]
        for f in as_completed(futures):
            for item in f.result():
                all_candidates[item["cid"]] = item

    verified = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_verify_inservice_detail, item, today) for item in all_candidates.values()]
        for f in as_completed(futures):
            res = f.result()
            if res:
                verified.append(res)

    # 今日研習優先顯示，其餘依日期排序
    verified.sort(key=lambda x: (not x[0], x[1]))
    return [x[2] for x in verified[:8]]


# ==============================================================================
# 2. 臺南市政府教育局學習護照 (e-learning.tn.edu.tw)
# ==============================================================================
def fetch_tainan_passport_online(today: date):
    """抓取臺南市學習護照：AI 相關、線上研習、無實體地址、非校內、報名中或今日研習"""
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

                    # 條件 5: 排除校內研習
                    if is_internal_school_only(attr_text) or is_internal_school_only(title):
                        continue

                    # 條件 1 & 3: 研習日期 >= 今日（含推播當日）
                    c_date = parse_date_safely(course_date_str)
                    if c_date and c_date < today:
                        continue

                    # 條件 2: 若有出現實體上課地址或實體教室，就不要蒐集
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
                        c_date or today,
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
def search_yahoo_links(query: str, max_items: int = 8):
    """透過 Yahoo 奇摩搜尋取得標題與網址（過濾廣告與無效連結）"""
    encoded_q = urllib.parse.quote(query)
    url = f"https://tw.search.yahoo.com/search?p={encoded_q}&btf=m"
    found = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return found
        page_text = decode_response(resp)
        pattern = re.compile(
            r'RU=([^/"]+)/RK=.*?<h3[^>]*><span[^>]*>(.*?)</span></h3>',
            re.DOTALL,
        )
        for match in pattern.finditer(page_text):
            raw_url = urllib.parse.unquote(match.group(1))
            raw_title = re.sub(r"<[^>]+>", "", match.group(2))
            clean_title = html.unescape(raw_title).strip()

            if any(skip in raw_url.lower() for skip in [
                "yahoo.com", "bing.com", "doubleclick.net", "googleadservices",
                "uservoice.com", "inservice.edu.tw", "e-learning.tn.edu.tw"
            ]):
                continue
            if clean_title and raw_url not in [u for _, u in found]:
                found.append((clean_title, raw_url))
                if len(found) >= max_items:
                    break
    except Exception as e:
        print(f"[警告] 搜尋 '{query}' 失敗: {e}")
    return found


def _verify_school_webpage(title: str, url: str, today: date):
    """讀取學校或教育單位公告頁內容，驗證免費、無實體上課地址、非校內、有效日期"""
    if is_internal_school_only(title) or is_paid(title) or STRICT_STREET_ADDRESS_PATTERN.search(title):
        return None
    if not is_ai_related(title):
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(decode_response(resp), "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        body_text = soup.get_text(" ", strip=True)

        if is_internal_school_only(body_text) or is_paid(body_text):
            return None

        # 檢查內文是否有明確的「上課地點/研習地點」地址
        loc_matches = re.findall(r"(?:上課地點|研習地點|活動地點|地點)[:：]\s*([^\n。；]{2,40})", body_text)
        for loc_val in loc_matches:
            if has_physical_address(loc_val):
                return None

        if not is_strictly_online(f"{title} {body_text[:2000]}"):
            return None

        # 檢查活動日期或報名日期（有明確日期且全部早於 today 則排除；含推播當日）
        date_lines = re.findall(r"(?:研習時間|活動時間|研習日期|活動日期|報名日期|報名時間|日期)[:：]?\s*[^\n]{0,50}", body_text)
        extracted_dates = extract_all_dates(" ".join(date_lines))
        if extracted_dates:
            max_date = max(extracted_dates)
            if max_date < today:
                return None
            is_today = today in extracted_dates
        else:
            is_today = False

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

                if is_internal_school_only(title) or STRICT_STREET_ADDRESS_PATTERN.search(title):
                    continue
                p_dates = extract_all_dates(period_str)
                if p_dates and max(p_dates) < today:
                    continue

                full_url = f"https://bulletin.tn.edu.tw/{href.lstrip('/')}"
                b_resp = session.get(full_url, headers=HEADERS, timeout=10)
                b_text = BeautifulSoup(decode_response(b_resp), "html.parser").get_text(" ", strip=True)

                if is_paid(b_text) or is_internal_school_only(b_text):
                    continue
                loc_matches = re.findall(r"(?:研習地點|上課地點|地點)[:：]\s*([^\n。；]{2,40})", b_text)
                if any(has_physical_address(loc) for loc in loc_matches):
                    continue
                if not is_strictly_online(f"{title} {b_text}"):
                    continue

                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    results.append(f"• 🟢[南市教育局公告] {title}\n  🔗 {full_url}")
                    if len(results) >= 3:
                        break
    except Exception as e:
        print(f"[警告] 臺南市教育局公告系統抓取失敗: {e}")

    # (B) 全國各級學校與教育單位最新消息 / 公佈欄 / 研習訊息 (site:edu.tw)
    queries = [
        '"線上研習" AI (最新消息 OR 公佈欄 OR 研習訊息) site:edu.tw',
        '("線上研習" OR "Google Meet") (AI OR Gemini OR ChatGPT) site:edu.tw',
    ]
    candidates = []
    for q in queries:
        for title, url in search_yahoo_links(q, max_items=6):
            if url not in seen_urls:
                seen_urls.add(url)
                candidates.append((title, url))

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_verify_school_webpage, t, u, today) for t, u in candidates]
        for f in as_completed(futures):
            res = f.result()
            if res:
                results.append(res)
                if len(results) >= 6:
                    break

    return results[:6]


# ==============================================================================
# 4. 各社群平台 (FB、IG、Threads、Accupass，各縣市線上 AI 研習)
# ==============================================================================
def fetch_social_platforms():
    """抓取 FB、IG、Threads、Accupass 上關於「線上研習 + AI + 免費 + 非校內 + 無實體地址」資訊"""
    results = []
    seen_urls = set()

    social_queries = [
        ("FB", '("線上研習" OR "線上工作坊" OR "Google Meet") AI 研習 免費 site:facebook.com'),
        ("IG", '("線上研習" OR "線上講座") AI 免費 (site:instagram.com OR site:threads.net)'),
    ]

    for platform, q in social_queries:
        for title, url in search_yahoo_links(q, max_items=5):
            if url in seen_urls:
                continue
            if is_internal_school_only(title) or is_paid(title) or STRICT_STREET_ADDRESS_PATTERN.search(title):
                continue
            if not is_ai_related(title) or not is_strictly_online(title):
                continue
            seen_urls.add(url)
            tag = "IG/Threads" if ("instagram.com" in url or "threads.net" in url) else platform
            results.append(f"• 🟢[{tag}] {title}\n  🔗 {url}")
            if len(results) >= 5:
                break

    # Accupass 線上免費 AI 研習講座
    try:
        acc_url = f"https://www.accupass.com/search?q={urllib.parse.quote('AI 線上研習 免費')}"
        resp = requests.get(acc_url, headers=HEADERS, timeout=12)
        if resp.status_code == 200:
            soup = BeautifulSoup(decode_response(resp), "html.parser")
            count = 0
            for link in soup.select("a[href*='/event/']"):
                title = link.get_text(strip=True)
                href = link["href"]
                if not title or len(title) <= 4:
                    continue
                if is_internal_school_only(title) or is_paid(title) or STRICT_STREET_ADDRESS_PATTERN.search(title):
                    continue
                if is_ai_related(title) and is_strictly_online(title):
                    full_url = href if href.startswith("http") else f"https://www.accupass.com{href}"
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        results.append(f"• 🟢[Accupass] {title}\n  🔗 {full_url}")
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

    today = datetime.now(TW_TZ).date()
    today_str = today.strftime("%Y-%m-%d")
    print(f"=== 啟動【各縣市 × 純線上研習 × AI × 免費 × 報名中/今日 × 非校內 × 無實體地址】爬蟲 ({today_str}) ===")

    inservice_items = fetch_inservice_all_cities(today)
    passport_items = fetch_tainan_passport_online(today)
    school_items = fetch_schools_and_edu_units(today)
    social_items = fetch_social_platforms()

    sections = [
        "📘【1. 全國教師在職進修資訊網（各縣市線上AI）】\n"
        + ("\n".join(inservice_items) if inservice_items else "• 目前暫無符合所有嚴選條件之課程"),
        "📗【2. 臺南市政府教育局學習護照（線上AI）】\n"
        + ("\n".join(passport_items) if passport_items else "• 目前暫無符合所有嚴選條件之課程"),
        "🏫【3. 各級學校與教育單位（最新消息/公佈欄）】\n"
        + ("\n".join(school_items) if school_items else "• 目前暫無符合所有嚴選條件之公告"),
        "📱【4. 各社群平台（FB / IG / 活動平台）】\n"
        + ("\n".join(social_items) if social_items else "• 目前暫無符合所有嚴選條件之貼文"),
    ]

    message = (
        f"☀️ 早安！{today_str}\n"
        f"🎯 今日嚴選：【各縣市 ＋ 純線上AI研習 ＋ 免費 ＋ 報名中/今日舉辦 ＋ 非校內 ＋ 無實體地址】\n\n"
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
