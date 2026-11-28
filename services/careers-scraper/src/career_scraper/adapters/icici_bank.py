"""ICICI Bank's own careers site (careers.icici.bank.in "iUniverse" applicant system).

The official entry ``https://www.icicicareers.com/`` redirects to the bank's own
single-page applicant system at ``careers.icici.bank.in``.  That SPA renders the
"All Jobs" listing from an ASP.NET JSON API (``CareerApplicantApi``).  Listing
search bodies and their responses are AES-128-CBC encrypted client side; the
job detail endpoint used by the desktop job page is encrypted the same way, and
a plain-text mobile-JD endpoint exposes the same record's mobilised variant.
"""
from __future__ import annotations

import base64
import json
import random
import re
import secrets
import string
import time
from hashlib import sha256
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Comment, Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.icicicareers.com/"
SITE_ORIGIN = "https://careers.icici.bank.in"
LISTING_PATH = "/CareerApplicant/Career/job-listing/"
DETAIL_PATH = "/CareerApplicant/Career/job-details/{job_id}"
API_ORIGIN = SITE_ORIGIN + "/CareerApplicantApi"
SEARCH_API = API_ORIGIN + "/Career/Search/1"
SINGLE_JOB_API = API_ORIGIN + "/Career/getSingleJob/1/{job_id}"
MOBILE_JD_API = API_ORIGIN + "/Career/getMobileJd/{job_id}"
GROUPS_API = API_ORIGIN + "/Career/Groups"

ITEMS_PER_PAGE = 12
SEARCH_TEMPLATE = {
    "userId": 1,
    "ApplicantId": "",
    "keyword": "",
    "maingroup": "",
    "experience": "",
    "PageNo": 0,
    "limit": ITEMS_PER_PAGE,
    "isAllIndia": 3,
}
# Encrypt/decrypt pair taken from the site's own request transform
# (REACT_APP bundle module 0x51).  The trailing 16 characters of both request
# and response payloads are the UTF-8 IV; the remainder is base64 ciphertext.
SITE_AES_KEY = b"72LK9osTzVTH09BC"
IV_ALPHABET = string.ascii_letters + string.digits
DECRYPT_ATTEMPTS = 15
JOB_ID = re.compile(r"^\d{3,12}$")
SEARCH_REQUEST_HEADERS = {
    "Authorization": "Bearer token",
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": SITE_ORIGIN,
    "Referer": SITE_ORIGIN + LISTING_PATH,
}
JSON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": SITE_ORIGIN + LISTING_PATH,
}


# ---------------------------------------------------------------------------
# Minimal AES-128 implementation (the runtime has no crypto dependency)
# ---------------------------------------------------------------------------

def _rotl(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (8 - shift))) & 0xFF


def _build_sbox() -> list[int]:
    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        sbox[p] = (q ^ _rotl(q, 1) ^ _rotl(q, 2) ^ _rotl(q, 3) ^ _rotl(q, 4) ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


SBOX = _build_sbox()
INV_SBOX = [0] * 256
for _index, _value in enumerate(SBOX):
    INV_SBOX[_value] = _index


def _xtime(value: int) -> int:
    value <<= 1
    return (value ^ 0x1B) & 0xFF if value & 0x100 else value


def _build_mul() -> dict[int, list[int]]:
    table = {}
    for constant in (2, 3, 9, 11, 13, 14):
        row = []
        for byte in range(256):
            if constant == 2:
                result = _xtime(byte)
            elif constant == 3:
                result = _xtime(byte) ^ byte
            elif constant == 9:
                result = _xtime(_xtime(_xtime(byte))) ^ byte
            elif constant == 11:
                result = _xtime(_xtime(_xtime(byte))) ^ _xtime(byte) ^ byte
            elif constant == 13:
                result = _xtime(_xtime(_xtime(byte))) ^ _xtime(_xtime(byte)) ^ byte
            else:  # 14 == 8 ^ 4 ^ 2
                result = _xtime(_xtime(_xtime(byte))) ^ _xtime(_xtime(byte)) ^ _xtime(byte)
            row.append(result)
        table[constant] = row
    return table


MUL = _build_mul()


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) != 16:
        raise ValueError("ICICI Bank site AES key must be 16 bytes")
    words = [list(key[index:index + 4]) for index in range(0, 16, 4)]
    rcon = 1
    for index in range(4, 44):
        temp = list(words[index - 1])
        if index % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[byte] for byte in temp]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        words.append([words[index - 4][position] ^ temp[position] for position in range(4)])
    return [sum(words[row * 4:row * 4 + 4], []) for row in range(11)]


def _shift_rows(state: list[int]) -> list[int]:
    out = [0] * 16
    for column in range(4):
        for row in range(4):
            out[column * 4 + row] = state[((column + row) % 4) * 4 + row]
    return out


def _inv_shift_rows(state: list[int]) -> list[int]:
    out = [0] * 16
    for column in range(4):
        for row in range(4):
            out[column * 4 + row] = state[((column - row) % 4) * 4 + row]
    return out


def _mix_columns(state: list[int]) -> list[int]:
    out = [0] * 16
    for column in range(4):
        a = state[column * 4:column * 4 + 4]
        out[column * 4 + 0] = MUL[2][a[0]] ^ MUL[3][a[1]] ^ a[2] ^ a[3]
        out[column * 4 + 1] = a[0] ^ MUL[2][a[1]] ^ MUL[3][a[2]] ^ a[3]
        out[column * 4 + 2] = a[0] ^ a[1] ^ MUL[2][a[2]] ^ MUL[3][a[3]]
        out[column * 4 + 3] = MUL[3][a[0]] ^ a[1] ^ a[2] ^ MUL[2][a[3]]
    return out


def _inv_mix_columns(state: list[int]) -> list[int]:
    out = [0] * 16
    for column in range(4):
        a = state[column * 4:column * 4 + 4]
        out[column * 4 + 0] = MUL[14][a[0]] ^ MUL[11][a[1]] ^ MUL[13][a[2]] ^ MUL[9][a[3]]
        out[column * 4 + 1] = MUL[9][a[0]] ^ MUL[14][a[1]] ^ MUL[11][a[2]] ^ MUL[13][a[3]]
        out[column * 4 + 2] = MUL[13][a[0]] ^ MUL[9][a[1]] ^ MUL[14][a[2]] ^ MUL[11][a[3]]
        out[column * 4 + 3] = MUL[11][a[0]] ^ MUL[13][a[1]] ^ MUL[9][a[2]] ^ MUL[14][a[3]]
    return out


def aes_encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = [block[index] ^ round_keys[0][index] for index in range(16)]
    for rnd in range(1, 11):
        state = [SBOX[byte] for byte in state]
        state = _shift_rows(state)
        if rnd != 10:
            state = _mix_columns(state)
        state = [state[index] ^ round_keys[rnd][index] for index in range(16)]
    return bytes(state)


def aes_decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = [block[index] ^ round_keys[10][index] for index in range(16)]
    for rnd in range(9, -1, -1):
        state = _inv_shift_rows(state)
        state = [INV_SBOX[byte] for byte in state]
        state = [state[index] ^ round_keys[rnd][index] for index in range(16)]
        if rnd:
            state = _inv_mix_columns(state)
    return bytes(state)


def aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    if not ciphertext or len(ciphertext) % 16:
        raise ValueError("AES-CBC ciphertext must be a non-empty multiple of 16 bytes")
    if len(iv) != 16:
        raise ValueError("AES-CBC IV must be 16 bytes")
    round_keys = _expand_key(key)
    out, previous = bytearray(), list(iv)
    for offset in range(0, len(ciphertext), 16):
        block = list(ciphertext[offset:offset + 16])
        plain = aes_decrypt_block(bytes(block), round_keys)
        out.extend(left ^ right for left, right in zip(plain, previous))
        previous = block
    padding = out[-1]
    if not 1 <= padding <= 16 or out[-padding:] != bytes([padding]) * padding:
        raise ValueError("AES-CBC plaintext has invalid PKCS#7 padding")
    return bytes(out[:-padding])


def aes_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    if len(iv) != 16:
        raise ValueError("AES-CBC IV must be 16 bytes")
    padding = 16 - len(plaintext) % 16
    padded = plaintext + bytes([padding]) * padding
    round_keys = _expand_key(key)
    out, previous = bytearray(), list(iv)
    for offset in range(0, len(padded), 16):
        block = bytes(left ^ right for left, right in zip(padded[offset:offset + 16], previous))
        previous = list(aes_encrypt_block(block, round_keys))
        out.extend(previous)
    return bytes(out)


def decrypt_api_payload(payload, key: bytes = SITE_AES_KEY):
    """Decrypt one ``<base64 ciphertext><16-char IV>`` API payload.

    Returns the decoded JSON value, or ``None`` when this particular response
    was not produced by the matched key (the site's load balancer answers with
    a non-decodable variant on part of the traffic).  Undecodable payloads are
    retried, never guessed at.
    """
    if not isinstance(payload, str) or len(payload) < 40:
        return None
    try:
        iv = payload[-16:].encode()
        ciphertext = base64.b64decode(payload[:-16])
        plaintext = aes_cbc_decrypt(ciphertext, key, iv)
        return json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def encrypt_api_body(value: dict, key: bytes = SITE_AES_KEY) -> str:
    iv = "".join(secrets.choice(IV_ALPHABET) for _ in range(16))
    plaintext = json.dumps(value, separators=(",", ":")).encode()
    cipher = aes_cbc_encrypt(plaintext, key, iv.encode())
    return base64.b64encode(cipher).decode() + iv


def search_body(page: int, *, limit: int = ITEMS_PER_PAGE) -> dict:
    return {**SEARCH_TEMPLATE, "PageNo": page, "limit": limit}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def clean_visible_text(value: str) -> str:
    """Collapse whitespace the way the rendered DOM exposes it."""
    value = re.sub(r"\s+", " ", value or "")
    value = re.sub(r"\s+([,.;!?])", r"\1", value)
    return value.strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    for node in soup.find_all(string=lambda value: isinstance(value, Comment)):
        node.extract()
    return clean_visible_text(soup.get_text(" ", strip=True))


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "careers.icici.bank.in"
            or not parsed.path.lower().startswith("/careerapplicant/career/")):
        raise ValueError("ICICI Bank official careers entry no longer redirects to its own careers site")
    soup = BeautifulSoup(html, "lxml")
    scripts = [script.get("src") for script in soup.select("script[src]")]
    if not any("/CareerApplicant/static/js/main." in (src or "") for src in scripts):
        raise ValueError("ICICI Bank careers entry is no longer the CareerApplicant single-page app")
    return {
        "url": response_url,
        "script_sources": scripts,
        "title": (soup.title.get_text(" ", strip=True) if soup.title else None),
    }


def parse_groups_payload(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("ICICI Bank groups payload is not a list")
    groups = []
    for row in value:
        if not isinstance(row, dict) or "g_title" not in row or "g_value" not in row:
            raise ValueError("ICICI Bank groups payload row is missing title/value")
        groups.append({
            "group_id": row.get("g_id"),
            "title": str(row.get("g_title") or "").strip(),
            "value": str(row.get("g_value") or "").strip(),
            "description": row.get("g_description"),
        })
    return groups


def parse_search_payload(value, requested_page: int) -> dict:
    if not isinstance(value, list):
        raise ValueError("ICICI Bank search payload is not a list")
    rows, seen, issues = [], set(), []
    total = None
    for row in value:
        if not isinstance(row, dict):
            issues.append("search_row_not_object")
            continue
        identity = str(row.get("f_jobId") or "")
        if not JOB_ID.fullmatch(identity):
            issues.append(f"search_row_missing_numeric_job_id:{identity or 'blank'}")
            continue
        if identity in seen:
            issues.append(f"search_duplicate_job_id:{identity}")
            continue
        if not str(row.get("f_title") or "").strip():
            issues.append(f"search_row_missing_title:{identity}")
            continue
        row_total = row.get("Total_Rows")
        if isinstance(row_total, int):
            total = row_total if total is None else total
        seen.add(identity)
        rows.append(dict(row))
    if len(value) > ITEMS_PER_PAGE and not issues:
        issues.append("search_page_larger_than_requested_limit")
    return {
        "rows": rows,
        "job_ids": [row["f_jobId"] for row in rows],
        "total": total,
        "page": requested_page,
        "issues": issues,
    }


def decode_base64_html(value) -> str:
    """Decode the site's description field into the HTML document it renders.

    Most records ship ``base64;<base64 of URL-encoded HTML>``; a few postings
    ship the URL-encoded document directly.  Both are unquoted until the value
    is real HTML, never left as encoded text.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if text.startswith("base64;"):
        payload = text.split(";", 1)[1]
        try:
            text = base64.b64decode(payload).decode("utf-8", "replace")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"ICICI Bank detail description is not decodable base64: {exc}") from exc
    for _ in range(3):
        head = text.lstrip()[:40].upper()
        if "%3C" not in head and not head.startswith("%"):
            break
        text = unquote(text)
    return text


CATEGORY_LABELS = ("category", "job level", "industry", "experience", "location", "job type")


DECORATIVE_DOTS = re.compile(r"^[\s.·]+$")

# Tailwind utilities that decide an element's display at a breakpoint.  The
# employer HTML ships responsive pairs such as
# ``div.lg:hidden.md:hidden.block`` (mobile copy) next to
# ``div.lg:block.md:block.hidden`` (desktop copy); only the latter is painted in a
# desktop browser, so the former must not leak into the stored description.
DISPLAY_UTILITIES = {
    "block", "contents", "flex", "flow-root", "grid", "hidden", "inline",
    "inline-block", "inline-flex", "inline-grid", "inline-table", "list-item",
    "none", "table", "table-cell", "table-column", "table-row", "visible",
}
# Site stylesheet rules that paint these nodes nowhere (``.sm-step-hidden`` and
# ``.sm-hidden`` are declared as ``display: none``), plus the mobile show/hide
# controls that pair with the responsive copies.
ALWAYS_HIDDEN_CLASSES = {"sm-step-hidden", "sm-hidden"}
MOBILE_CONTROL_CLASSES = {"showbtn", "hidebtn"}


def _display_token(class_name: str, prefix: str = "") -> str | None:
    """Return the display utility a Tailwind class selects, if any."""
    if not class_name.startswith(prefix):
        return None
    if prefix:
        class_name = class_name[len(prefix):]
    class_name = class_name.lstrip("!")
    return class_name if class_name in DISPLAY_UTILITIES else None


def _hidden_at_desktop(classes: set[str]) -> bool:
    """True when the site's responsive classes hide the node in a >=1024px viewport."""
    display = None
    for token in classes:
        value = _display_token(token)
        if value is not None:
            display = value
    for token in classes:
        value = _display_token(token, "md:")
        if value is not None:
            display = value
    for token in classes:
        value = _display_token(token, "lg:")
        if value is not None:
            display = value
    return display in {"hidden", "none"}


def _drop_responsive_layout(soup: BeautifulSoup) -> None:
    """Drop desktop-hidden responsive duplicates and their show/hide controls."""
    for element in list(soup.find_all(True)):
        if element.parent is None:
            continue
        classes = set(element.get("class") or [])
        if not classes:
            continue
        if classes & MOBILE_CONTROL_CLASSES:
            element.decompose()
            continue
        if classes & ALWAYS_HIDDEN_CLASSES:
            element.decompose()
            continue
        if _hidden_at_desktop(classes):
            element.decompose()


def _drop_decorative_dots(soup: BeautifulSoup) -> None:
    """Remove layout-only dot runs that CSS hides from the rendered page.

    The employer ships carousel dots as ``<label class="slidersad">.</label>``
    (or ``sliderbranch`` / ``slideBranchsad``).  Only those layout elements are
    removed: a lone ``.`` text node can also be a sentence-ending period after
    an inline ``<strong>`` and is real published prose, so raw text nodes are
    never stripped.
    """
    for element in list(soup.find_all(True)):
        if not DECORATIVE_DOTS.match(element.get_text(" ", strip=True) or ""):
            continue
        classes = " ".join(element.get("class") or [])
        if element.name == "label" or re.search(r"slider|slide|carousel|indicator|dot", classes, re.I):
            element.decompose()
    for element in list(soup.find_all(attrs={"class": re.compile(r"(?:^|\s)(?:hide|hidemore)(?:\s|$)")})):
        element.decompose()
    for element in list(soup.select('[style*="display:none"], [style*="display: none"]')):
        element.decompose()


def _category_card(soup: BeautifulSoup) -> Tag | None:
    """The site renders the category block as a custom ``<card>`` element."""
    card = soup.find("card")
    if card is not None:
        return card
    candidates = [element for element in soup.find_all(True)
                  if "job level" in element.get_text(" ", strip=True).lower()
                  and "category" in element.get_text(" ", strip=True).lower()]
    candidates.sort(key=lambda element: len(element.get_text(strip=True)))
    return candidates[0] if candidates else None


def parse_description_html(html: str) -> dict:
    """Split the rendered detail document into its category card and job body."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    for node in soup.find_all(string=lambda value: isinstance(value, Comment)):
        node.extract()
    card = _category_card(soup)
    if card is not None:
        root = card.find_parent("div", class_="container") or card.parent
    else:
        roots = soup.select("div.container")
        root = max(roots, key=lambda node: len(node.get_text(strip=True))) if roots else (soup.body or soup)
    categories = {}
    if card is not None:
        labels = [re.sub(r"\s+", " ", node.get_text(" ", strip=True).replace("\xa0", " ")).strip()
                  for node in card.select("p")]
        for index, label in enumerate(labels):
            key = label.lower().rstrip(":")
            if key in CATEGORY_LABELS and key not in categories and index + 1 < len(labels):
                categories[key] = labels[index + 1]
        card.decompose()
    body_html = "".join(str(child) for child in root.contents).strip()
    sections = []
    body_soup = BeautifulSoup(body_html, "lxml")
    _drop_responsive_layout(body_soup)
    _drop_decorative_dots(body_soup)
    cleaned_root = body_soup.body or body_soup
    body_html = "".join(str(child) for child in cleaned_root.contents).strip()
    for heading in body_soup.select("h2"):
        section_html = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and sibling.name == "h2":
                break
            section_html.append(str(sibling) if isinstance(sibling, Tag) else str(sibling))
        sections.append({
            "heading": re.sub(r"\s+", " ", heading.get_text(" ", strip=True)),
            "html": "".join(section_html).strip(),
            "text": clean_visible_text(text_html("".join(section_html))),
        })
    faq = []
    for panel in body_soup.select("div.faqpanel"):
        for item in panel.select("li"):
            question = item.find("h4")
            summary = item.select_one("details summary")
            if not question:
                continue
            faq.append({
                "question": re.sub(r"\s+", " ", question.get_text(" ", strip=True)),
                "answer": re.sub(r"\s+", " ", (summary.get_text(" ", strip=True) if summary else "")),
            })
    return {
        "categories": categories,
        "sections": sections,
        "faq": faq,
        "description_html": body_html,
        "description_text": clean_visible_text(text_html(body_html)),
        "description_normalized": normalized_body_text(body_html),
    }


def _detail_record_rows(value) -> list[dict]:
    if isinstance(value, dict):
        for key in ("Data", "data"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    if isinstance(value, list):
        return value
    raise ValueError("ICICI Bank job detail payload is not an object or list")


def parse_detail_row(row: dict) -> dict:
    if not isinstance(row, dict):
        raise ValueError("ICICI Bank job detail row is not an object")
    identity = str(row.get("JobID") or row.get("f_jobId") or "")
    if not JOB_ID.fullmatch(identity):
        raise ValueError(f"ICICI Bank job detail row has no numeric JobID: {identity!r}")
    title = str(row.get("f_title") or row.get("JobTitle") or "").strip()
    if not title:
        raise ValueError(f"ICICI Bank job detail {identity} has no title")
    html = decode_base64_html(row.get("f_description"))
    if html and "<" not in html:
        raise ValueError(f"ICICI Bank job detail {identity} description field is not HTML")
    parsed = parse_description_html(html)
    jd_html = row.get("JD") or ""
    parsed.update({
        "job_id": identity,
        "title": title,
        "job_number": row.get("JobNumber"),
        "job_title": row.get("JobTitle"),
        "job_type": row.get("JobType"),
        "location_field": row.get("Location"),
        "main_group": row.get("MainGroup"),
        "sub_group": row.get("SubGroup"),
        "department": row.get("Department"),
        "function": row.get("Function"),
        "grade": row.get("Grade"),
        "experience": row.get("Experience"),
        "created_date": row.get("f_createdDate"),
        "end_date": row.get("EndDate"),
        "short_description": row.get("f_short_description"),
        "group_tagged": row.get("f_groupTagged"),
        "related_jobs": row.get("f_relatedJobs"),
        "related_documents": row.get("f_relatedDocuments"),
        "is_featured": row.get("f_isFeaturedJob"),
        "posted_to": row.get("postedto"),
        "short_url": row.get("ShortUrl"),
        "cover_image": row.get("f_cover_img_path"),
        "seo_image": row.get("seo_image"),
        "jd_field_html": jd_html,
        "jd_field_text": text_html(jd_html),
        "rendered_html_sha256": sha256(html.encode()).hexdigest() if html else None,
    })
    if not parsed["description_text"]:
        raise ValueError(f"ICICI Bank job detail {identity} rendered an empty description")
    return parsed


def parse_mobile_row(row: dict) -> dict:
    """Trim the plain-text mobile-JD record to cross-check the desktop record."""
    keep = ("JobID", "JobNumber", "JobTitle", "Location", "MainGroup", "SubGroup", "Department",
            "Experience", "Function", "Grade", "f_createdDate", "EndDate", "f_isDelete",
            "IsPostedToCareer", "postedto", "ShortUrl")
    mobile = {key: row.get(key) for key in keep}
    jd = row.get("JD") or ""
    mobile["jd_sha256"] = sha256(jd.encode()).hexdigest() if jd else None
    mobile["jd_chars"] = len(text_html(jd))
    return mobile


def iso_date(value) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value.strip())
    return match.group(1) if match else None


def location_list(value) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    parts = [part.strip(" .") for part in re.split(r",|/|\band\b", value) if part.strip(" .")]
    return [part for part in parts if part]


def detail_url(job_id: str) -> str:
    return SITE_ORIGIN + DETAIL_PATH.format(job_id=job_id)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class IciciBank(Adapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.metadata = self.result.metadata
        self.metadata.update({
            "decrypt_attempts": 0,
            "decrypt_failures": 0,
            "search_attempts_per_page": {},
            "detail_attempts_per_job": {},
            "mobile_cross_checks": {},
        })
        self.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": SITE_ORIGIN + LISTING_PATH,
            "search_api": SEARCH_API,
            "detail_api": SINGLE_JOB_API.format(job_id="{jobId}"),
            "mobile_detail_api": MOBILE_JD_API.format(job_id="{jobId}"),
            "items_per_page": ITEMS_PER_PAGE,
            "encrypted_listing_and_detail_payloads": True,
            "search_filter_used": "unfiltered All Jobs listing (keyword='', maingroup='', experience='', isAllIndia=3)",
        })

    async def _decrypting_request(self, method: str, url: str, payload_key: str, *,
                                  body: dict | None = None, headers: dict | None = None,
                                  label: str | None = None, attempts: int = DECRYPT_ATTEMPTS):
        last_error = "no_attempts"
        for attempt in range(1, attempts + 1):
            self.metadata["decrypt_attempts"] += 1
            request_headers = {**(headers or {})}
            kwargs = {"headers": request_headers}
            if body is not None:
                value = body() if callable(body) else body
                kwargs["json"] = {"data": encrypt_api_body(value)}
            response = await self.http.request(method, url, **kwargs)
            try:
                value = response.json()
            except ValueError as exc:
                last_error = f"non_json_response:{exc}"
            else:
                decoded = decrypt_api_payload(value.get(payload_key))
                if decoded is not None:
                    if label:
                        self.metadata.setdefault("decrypt_attempt_log", {})[label] = attempt
                    return decoded, value, response
                last_error = f"undecryptable_payload:ResponseCode={value.get('ResponseCode')}"
            self.metadata["decrypt_failures"] += 1
            if attempt < attempts:
                await _sleep(min(2.0, 0.25 * attempt) + random.uniform(0.0, 0.2))
        raise ValueError(f"ICICI Bank API payload could not be decoded after {attempts} attempts "
                         f"({url}): {last_error}")

    async def run(self):
        entry_started = time.perf_counter()
        entry = await self.http.request("GET", OFFICIAL_ENTRY)
        entry_meta = parse_official_entry(entry.text, str(entry.url))
        self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - entry_started)
        self.result.metadata["official_entry"] = entry_meta

        groups_started = time.perf_counter()
        groups_value, _, _ = await self._decrypting_request(
            "GET", GROUPS_API, "Data", headers=JSON_HEADERS, label="groups")
        groups = parse_groups_payload(groups_value)
        self.result.timings["groups_seconds"] = max(0.0, time.perf_counter() - groups_started)
        self.result.metadata["published_groups"] = groups

        listing_started = time.perf_counter()
        rows, pages, totals, page_attempts = [], 0, [], {}
        while True:
            if not self.page_guard():
                break
            body = search_body(pages)
            value, envelope, _ = await self._decrypting_request(
                "POST", SEARCH_API, "Data", body=body,
                headers=SEARCH_REQUEST_HEADERS, label=f"search_page_{pages}")
            parsed = parse_search_payload(value, pages)
            page_attempts[pages] = self.metadata.get("decrypt_attempt_log", {}).get(f"search_page_{pages}")
            if parsed["issues"]:
                self.result.errors.extend(f"listing_page_{pages}:{issue}" for issue in parsed["issues"])
            if parsed["total"] is not None:
                totals.append(parsed["total"])
            page_rows = parsed["rows"]
            if pages == 0 and not page_rows:
                self.result.errors.append("listing_page_0_empty")
            rows.extend(page_rows)
            pages += 1
            if len(page_rows) < ITEMS_PER_PAGE:
                break
            if pages > 200:
                self.result.errors.append("page_budget_exhausted")
                break
        self.result.timings["listing_seconds"] = max(0.0, time.perf_counter() - listing_started)
        self.metadata["search_attempts_per_page"] = page_attempts
        self.metadata["listing_pages"] = pages

        unique: dict[str, dict] = {}
        duplicates = []
        for row in rows:
            identity = str(row["f_jobId"])
            if identity in unique:
                duplicates.append(identity)
                continue
            unique[identity] = row
        advertised = totals[0] if totals else None
        self.result.expected_total = advertised
        self.metadata["listing_totals_observed"] = totals
        if duplicates:
            self.result.warnings.append(f"listing_repeated_ids:{len(duplicates)}")
        if advertised is not None and len(unique) != advertised:
            self.result.errors.append(f"listing_count_mismatch:collected={len(unique)}:advertised={advertised}")
        if not unique:
            self.result.errors.append("listing_returned_no_jobs")
            self.result.listing_complete = False
            self.result.finish()
            return self.result

        self.result.metadata["listing_ids"] = list(unique)
        for row in unique.values():
            identity = str(row["f_jobId"])
            job = self.job(
                raw={"listing": row},
                title=str(row.get("f_title") or "").strip(),
                url=detail_url(identity),
                job_id=identity,
                department=row.get("hc_Department") or row.get("hc_MainGroup"),
                locations=location_list(row.get("hc_Location")),
                employment_type=row.get("hc_JobType") or None,
                experience=row.get("hc_Experience"),
                description="",
            )
            job.raw["listing_url"] = SITE_ORIGIN + LISTING_PATH
            self.add(job)
            if self.limit():
                break

        detail_started = time.perf_counter()
        jobs = list(self.result.jobs)
        await self.hydrate(jobs, self._hydrate_job)
        for _ in range(2):
            failed = [job for job in self.result.jobs if "detail_fetch_failed" in job.warnings]
            if not failed:
                break
            self.result.metadata["detail_retry_batches"] = self.result.metadata.get("detail_retry_batches", 0) + 1
            await self.hydrate(list(failed), self._hydrate_job)
        self.result.timings["detail_hydration_seconds"] = round(max(0.0, time.perf_counter() - detail_started), 6)
        self.result.timings["source_collection_seconds"] = self.result.timings.get("listing_seconds")

        verified_ids = {job.job_id for job in self.result.jobs if job.description_verified}
        expected_ids = {str(row["f_jobId"]) for row in unique.values()}
        self.result.listing_complete = (
            not self.result.errors
            and len(self.result.jobs) == len(unique)
            and expected_ids == {job.job_id for job in self.result.jobs}
            and len(verified_ids) == len(self.result.jobs)
        )
        self.result.metadata["membership_reconciliation"] = {
            "advertised_total": advertised,
            "listing_pages": pages,
            "rows_seen": len(rows),
            "unique_listing_ids": len(unique),
            "jobs_collected": len(self.result.jobs),
            "verified_descriptions": len(verified_ids),
            "missing_ids": sorted(expected_ids - {job.job_id for job in self.result.jobs}),
            "unverified_ids": sorted({job.job_id for job in self.result.jobs} - verified_ids),
            "complete": self.result.listing_complete,
        }
        self.result.finish()
        return self.result

    async def _hydrate_job(self, job):
        identity = job.job_id
        detail, envelope, response = await self._decrypting_request(
            "GET", SINGLE_JOB_API.format(job_id=identity), "Data",
            headers={**JSON_HEADERS, "Referer": job.url}, label=f"detail_{identity}")
        rows = _detail_record_rows(detail)
        if len(rows) != 1:
            raise ValueError(f"ICICI Bank job detail {identity} returned {len(rows)} records")
        parsed = parse_detail_row(rows[0])
        if parsed["job_id"] != identity:
            raise ValueError(f"ICICI Bank job detail returned {parsed['job_id']} for {identity}")
        detail_attempts = self.metadata.get("decrypt_attempt_log", {}).get(f"detail_{identity}")
        self.metadata["detail_attempts_per_job"][identity] = detail_attempts

        mobile = None
        try:
            mobile_response = await self.http.request(
                "GET", MOBILE_JD_API.format(job_id=identity),
                headers={**JSON_HEADERS, "Referer": job.url})
            mobile_rows = _detail_record_rows(mobile_response.json())
            if len(mobile_rows) == 1:
                mobile = parse_mobile_row(mobile_rows[0])
        except Exception as exc:  # cross-check evidence only; never a substitute detail
            job.warnings.append("mobile_detail_cross_check_failed")
            self.result.warnings.append(f"mobile_detail_cross_check_failed:{identity}:{type(exc).__name__}")
        if mobile:
            mobile["job_number_matches"] = mobile.get("JobNumber") == parsed["job_number"]
            mobile["created_date_matches"] = mobile.get("f_createdDate") == parsed["created_date"]
            mobile["jd_text_matches"] = mobile.get("jd_sha256") == (
                sha256(parsed["jd_field_html"].encode()).hexdigest() if parsed["jd_field_html"] else None)
            self.metadata["mobile_cross_checks"][identity] = mobile
            if not mobile["job_number_matches"]:
                job.warnings.append("mobile_requisition_mismatch")

        categories = parsed["categories"]
        locations = location_list(categories.get("location")) or location_list(parsed.get("location_field"))
        job.title = parsed["title"]
        job.requisition_id = parsed["job_number"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "company_api_rendered_description_field"
        job.description_source_url = SINGLE_JOB_API.format(job_id=identity)
        job.locations = locations
        job.department = parsed.get("department") or parsed.get("function")
        job.team = parsed.get("sub_group") or parsed.get("main_group")
        job.employment_type = categories.get("job type") or None
        job.experience = categories.get("experience") or parsed.get("experience")
        # The job page publishes no posting date.  f_createdDate is the ATS
        # record's creation timestamp, not a posting date, so date_posted stays
        # null and the raw value is retained under an explicit label.
        job.date_posted = None
        job.date_posted_raw = None
        job.valid_through = iso_date(parsed.get("end_date"))
        job.raw["record_created_date"] = iso_date(parsed.get("created_date"))
        job.raw["record_created_date_raw"] = parsed.get("created_date")
        job.raw.update({
            "detail": {key: value for key, value in parsed.items()
                       if key not in ("description_html", "description_text", "jd_field_html")},
            "description_source_sha256": parsed["rendered_html_sha256"],
            "detail_api_response_sha256": sha256(response.content).hexdigest(),
            "apply_control": {
                "label": "Apply Now",
                "kind": "button",
                "requires_sign_in": True,
                "url": None,
                "observed_on": detail_url(identity),
            },
            "detail_attempts": detail_attempts,
            "mobile_cross_check": mobile,
        })
        if parsed.get("faq"):
            job.raw["detail"]["faq"] = parsed["faq"]
        job.warnings = [warning for warning in job.warnings if warning != "detail_fetch_failed"]
        if not job.locations:
            job.warnings.append("no_location_published")
        if not job.description_verified and parsed["description_text"]:
            job.description_unavailable = False
        return job


async def _sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


def detail_page_reference(html: str) -> dict:
    """Normalised visible description body from a saved/rendered detail page."""
    parsed = parse_description_html(html)
    return {
        "normalized_chars": len(parsed["description_normalized"]),
        "fnv1a64": fnv1a64(parsed["description_normalized"]),
        "sha256": sha256(parsed["description_normalized"].encode()).hexdigest(),
        "categories": parsed["categories"],
        "headings": [section["heading"] for section in parsed["sections"]],
    }


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"
