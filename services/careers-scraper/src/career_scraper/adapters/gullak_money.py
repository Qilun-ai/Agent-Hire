"""Gullak Money's official careers journey and its company-published Candid listing.

The official site links its "Careers" navigation item to
https://candid.hub.gullak.money/jobs/. That SPA calls its public listing endpoint
https://autohire.internal.svc.uat.glkmny.tech/public/jobs, which returns the 13
Gullak pipelines shown by the rendered UI. Each public pipeline may publish a
role-bearing Google Drive PDF or Google Docs document; those documents are the
full descriptions. There is no per-job detail page: the only exact per-job link
is the SPA's Apply Now route, and the JD attachment is the published detail.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
from html import escape
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..attachments import extract_attachment
from ..models import now


OFFICIAL_ENTRY = "https://www.gullak.money/"
LISTING_URL = "https://candid.hub.gullak.money/jobs/"
APP_ORIGIN = "https://candid.hub.gullak.money"
APP_SHELL_URL = APP_ORIGIN + "/"
API_BASE = "https://autohire.internal.svc.uat.glkmny.tech"
API_JOBS = API_BASE + "/public/jobs"
API_JOBS_META = API_BASE + "/public/jobs/{id}/meta"
EMPLOYER = "Gullak"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
DRIVE_FILE_RE = re.compile(r"^/file/d/([A-Za-z0-9_-]+)(?:/|$)")
DOCS_FILE_RE = re.compile(r"^/document/d/([A-Za-z0-9_-]+)(?:/|$)")
JD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PAGE_HEADER_RE = re.compile(r"^Gullak:\s*India['’]s\s+(?:Gold|Savings)\s+App\.?$", re.I)
BULLET_RE = re.compile(r"^\s*(?P<marker>●|•|▪|◦|\*|-|–|—|\d+\.)\s+(?P<text>.+)$")
META_PREFIXES = ("role:", "experience:", "location:", "role type:")

# The Product Designer PDF is vector outlines with no text layer. pypdf and
# pdftotext both return no text. The text below was transcribed from the two
# rendered pages of the exact published Drive file and checked against the Drive
# viewer; it is used only while that exact file SHA-256 is on the listing.
JD_TEXT_OVERRIDES = {
    "19c8g2UKvciVazdRYaESKHPg35aWB92as": {
        "sha256": "741e3dd1199afd2ff751d05c343a87d515b6fa0776955a2098717940c9e2b7ec",
        "pages": 2,
        "method": "gullak_money.rendered_pdf_manual_transcription",
        "note": "No text layer in the official PDF; text transcribed from its two rendered pages, including the Drive viewer check.",
        "text": """Role: Product Designer
Experience: 0–2 Years
Location: Work from Office – Koramangala, Bangalore
Role Type: Full Time

About Gullak

Gullak is on a mission to put a Gold Balance on the phone of every Indian. Built by the creators of the BHIM UPI app, Gullak is transforming how Indians save and grow their wealth through gold - making it simple, trusted, and rewarding.

Backed by marquee investors including Y Combinator, Chiratae Ventures, Better Capital and industry leaders such as the founders of Juspay and CRED, Gullak has already facilitated ₹1500Cr+ in gold investments and serves 4 lakh+ active users.

Role Overview

We're looking for a Product Designer who is obsessed with understanding users, identifying problems and crafting delightful experiences. This role is perfect for someone who combines strong visual design skills with structured problem-solving and an intense desire to learn and build. You'll thrive here if you're hungry, ambitious and excited to take ownership beyond your job description.

You'll work closely with Product, Engineering and Growth teams to design intuitive experiences across the Gullak ecosystem - from concept and wireframes to polished interfaces and prototypes.

Responsibilities

- Understand user needs, behaviours and pain points through research and feedback.
- Translate business and product requirements into intuitive user flows, wireframes and high-fidelity designs.
- Create visually compelling and user-friendly experiences across mobile and web products.
- Collaborate closely with Product Managers and Engineers to bring ideas to life.
- Build prototypes and iterate rapidly based on user and stakeholder feedback.
- Contribute to and maintain design systems for consistency across products.
- Participate in design reviews and continuously improve the overall user experience.
- Use data, user insights and design thinking to solve complex product problems.

What We're Looking For

- 0–2 years of experience in Product Design, UI/UX Design or related fields.
- Strong problem-solving and analytical thinking skills.
- Excellent visual design fundamentals including typography, color, hierarchy, spacing and layout.
- Hands-on experience with Figma and Claude Design (or similar AI design tools).
- Ability to create wireframes, mockups and interactive prototypes.
- Ability to work in extremely fast paced environments
- Strong communication skills and ability to collaborate across teams.
- A portfolio showcasing thoughtful design decisions and end-to-end design projects.
- High ownership and hunger to learn, build and create impact. We value people who are proactive, move fast, seek feedback and constantly push themselves to improve.
- Passion for building consumer-facing products and delivering exceptional user experiences.

Why Join Us?

- Work directly with founders who have built products used by millions of Indians.
- Design products that impact the financial lives of millions of users.
- High ownership, steep learning curve and rapid career growth.
- Opportunity to shape the future of digital gold and wealth creation in India.
""",
    }
}

KNOWN_HEADINGS = {
    "about gullak",
    "role overview",
    "responsibilities",
    "job responsibilities",
    "primary responsibilities",
    "what are we looking for",
    "what we're looking for",
    "what we’re looking for",
    "why join us",
    "why join us?",
    "what you'll own",
    "what you’ll own",
    "what you'll get",
    "what you’ll get",
    "added advantage",
    "before you apply",
    "minimum commitment",
    "hiring process",
    "we'd love you if you are someone who..",
    "we’d love you if you are someone who..",
    "bonus",
    "good to have",
    "our story",
    "founding team",
    "engineering @ gullak",
}
RESPONSIBILITY_HEADINGS = {
    "responsibilities", "job responsibilities", "primary responsibilities",
    "what you'll own", "what you’ll own",
}
QUALIFICATION_HEADINGS = {
    "what are we looking for", "what we're looking for", "what we’re looking for",
    "added advantage", "we'd love you if you are someone who..",
    "we’d love you if you are someone who..", "good to have",
}


def clean_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


def heading_key(value: str | None) -> str:
    value = clean_text(value).rstrip(":?").strip().lower()
    value = value.replace("’", "'")
    return re.sub(r"\s+", " ", value)


def _is_meta(value: str) -> bool:
    return any(value.lower().startswith(prefix) for prefix in META_PREFIXES)


def _is_heading(value: str) -> bool:
    key = heading_key(value)
    if key in KNOWN_HEADINGS:
        return True
    # A short line ending in a colon is a section heading ("About Gullak:").
    # Long sentences that happen to end in a colon are body text.
    return value.endswith(":") and len(value.split()) <= 6 and "." not in value


def _join_wrapped(left: str, right: str) -> str:
    if left.endswith("-") and right[:1].islower():
        return left + right
    return left + " " + right


def _bullet_match(value: str):
    match = BULLET_RE.match(value)
    if not match:
        return None
    marker, text = match.group("marker"), match.group("text").strip()
    if marker in {"–", "—"}:
        marker = "-"
    return marker, text


def _remove_page_headers(lines: list[str]) -> list[str]:
    return [line for line in lines if not PAGE_HEADER_RE.match(line.strip())]


def _mark_first_title(blocks: list[dict]) -> None:
    for block in blocks:
        if block["kind"] == "heading" and block["text"].strip().lower() in {"customer success representative", "gullak - frontend engineer", "gullak - backend engineer"}:
            block["kind"] = "title"
            return
        if block["kind"] == "paragraph" and len(block["text"]) <= 120:
            block["kind"] = "title"
            return
        if block["kind"] in {"meta", "bullet"}:
            return
        if block["kind"] == "heading":
            return


def pdf_blocks(text: str) -> list[dict]:
    """Turn pypdf layout text into title/meta/heading/paragraph/bullet blocks."""
    blocks: list[dict] = []
    blank = True
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    for raw in _remove_page_headers(normalized.splitlines()):
        if not raw.strip():
            blank = True
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        value = clean_text(raw)
        bullet = _bullet_match(value)
        if bullet:
            marker, body = bullet
            blocks.append({"kind": "bullet", "text": body, "marker": marker})
            blank = False
            continue
        if _is_meta(value):
            blocks.append({"kind": "meta", "text": value})
            blank = False
            continue
        if _is_heading(value):
            blocks.append({"kind": "heading", "text": value})
            blank = False
            continue
        previous = blocks[-1] if blocks else None
        if previous and previous["kind"] == "bullet" and not blank and indent >= 4:
            previous["text"] = _join_wrapped(previous["text"], value)
            continue
        if previous and previous["kind"] == "paragraph" and not blank:
            previous["text"] = _join_wrapped(previous["text"], value)
            continue
        blocks.append({"kind": "paragraph", "text": value})
        blank = False
    _mark_first_title(blocks)
    return blocks


def document_blocks(text: str) -> list[dict]:
    """Turn a plain-text Google Docs export into blocks without reflowing lines."""
    blocks: list[dict] = []
    for raw in clean_text(text).splitlines():
        value = raw.strip()
        if not value:
            continue
        bullet = _bullet_match(value)
        if bullet:
            marker, body = bullet
            blocks.append({"kind": "bullet", "text": body, "marker": marker})
        elif _is_meta(value):
            blocks.append({"kind": "meta", "text": value})
        elif _is_heading(value):
            blocks.append({"kind": "heading", "text": value})
        else:
            blocks.append({"kind": "paragraph", "text": value})
    _mark_first_title(blocks)
    return blocks


def render_blocks(blocks: list[dict]) -> dict:
    html: list[str] = []
    text: list[str] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        kind = block["kind"]
        if kind == "bullet":
            group = []
            while index < len(blocks) and blocks[index]["kind"] == "bullet":
                group.append(blocks[index])
                index += 1
            numeric = all(re.fullmatch(r"\d+\.", item.get("marker", "")) for item in group)
            tag = "ol" if numeric else "ul"
            html.append(f"<{tag}>" + "".join(f"<li>{escape(item['text'])}</li>" for item in group) + f"</{tag}>")
            lines = []
            for item in group:
                marker = item.get("marker", "")
                label = marker if re.fullmatch(r"\d+\.", marker) else "-"
                lines.append(f"{label} {item['text']}")
            text.append("\n".join(lines))
            continue
        value = block["text"]
        if kind == "title":
            html.append(f"<h1>{escape(value)}</h1>")
        elif kind == "heading":
            html.append(f"<h2>{escape(value)}</h2>")
        else:
            html.append(f"<p>{escape(value)}</p>")
        text.append(value)
        index += 1
    return {"html": "".join(html), "text": "\n\n".join(text), "blocks": blocks}


def extract_sections(blocks: list[dict]) -> dict:
    responsibilities: list[str] = []
    qualifications: list[str] = []
    current = None
    for block in blocks:
        if block["kind"] == "heading":
            key = heading_key(block["text"])
            if key in RESPONSIBILITY_HEADINGS:
                current = responsibilities
            elif key in QUALIFICATION_HEADINGS:
                current = qualifications
            else:
                current = None
        elif block["kind"] == "bullet" and current is not None:
            current.append(block["text"])
        elif block["kind"] == "paragraph" and current is qualifications and block["text"].lower().startswith("bonus:"):
            current.append(block["text"])
    return {
        "responsibilities": list(dict.fromkeys(responsibilities)) or None,
        "qualifications": list(dict.fromkeys(qualifications)) or None,
    }


def parse_meta(blocks: list[dict]) -> dict:
    meta: dict[str, str] = {}
    for block in blocks:
        if block["kind"] != "meta":
            continue
        key, separator, value = block["text"].partition(":")
        if separator:
            meta[heading_key(key)] = clean_text(value)
    return meta


def normalize_location(value: str | None) -> tuple[str | None, str | None]:
    value = clean_text(value)
    if not value:
        return None, None
    raw = value
    value = re.sub(r"^work\s+from\s+office\s*[-–—]\s*", "", value, flags=re.I)
    value = re.sub(r"^work\s+from\s+o(?:ff|ﬀ)ice\s*[-–—]\s*", "", value, flags=re.I)
    lower = raw.lower()
    if "remote" in lower:
        workplace = "Remote"
    elif any(term in lower for term in ("work from office", "in-office", "on-site", "onsite", "office")):
        workplace = "On-site"
    else:
        workplace = None
    value = re.sub(r"\s*\((?:on-?site|in-?office|remote)\)\s*$", "", value, flags=re.I).strip()
    return value or None, workplace


def normalize_employment(value: str | None) -> str | None:
    value = clean_text(value)
    if not value:
        return None
    lower = value.lower()
    if "internship" in lower:
        return "Internship"
    if "full" in lower and "time" in lower:
        return "Full-time"
    return value


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname not in {"gullak.money", "www.gullak.money"}
            or parsed.path.rstrip("/") not in {"", "/"}):
        raise ValueError("Gullak official entry redirected away from its inspected homepage")
    soup = BeautifulSoup(html, "lxml")
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Gullak" not in title:
        raise ValueError("Gullak official homepage identity is missing")
    links = []
    for anchor in soup.select("a[href]"):
        label = clean_text(anchor.get_text(" ", strip=True))
        target = anchor.get("href", "")
        if label.lower() != "careers" or "candid.hub.gullak.money" not in target:
            continue
        links.append(target)
    unique = list(dict.fromkeys(links))
    if unique != [LISTING_URL]:
        raise ValueError(f"Gullak careers link changed: {unique}")
    return {
        "url": response_url,
        "title": title,
        "careers_links": unique,
        "listing_url": LISTING_URL,
    }


def parse_app_shell(html: str, response_url: str = APP_SHELL_URL) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != "candid.hub.gullak.money":
        raise ValueError("Gullak Candid app shell came from an unexpected origin")
    soup = BeautifulSoup(html, "lxml")
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != "AutoHire":
        raise ValueError("Gullak Candid app shell identity changed")
    scripts = [anchor.get("src", "") for anchor in soup.select("script[src]")]
    bundles = [src for src in scripts if re.search(r"^/assets/index-[A-Za-z0-9_-]+\.js$", src)]
    if len(bundles) != 1:
        raise ValueError("Gullak Candid app shell no longer exposes one listing bundle")
    return {"url": response_url, "title": title, "bundle_path": bundles[0]}


def parse_api_base(bundle: str, shell_url: str = APP_SHELL_URL) -> str:
    if "/public/jobs" not in bundle:
        raise ValueError("Gullak Candid bundle no longer contains the public jobs request")
    candidates = re.findall(r"https://[A-Za-z0-9.-]+", bundle)
    if API_BASE in candidates:
        return API_BASE
    # The bundle declares the API origin as a string immediately before the
    # public-jobs client. Prefer the last absolute URL before that marker.
    marker = bundle.find('"/public/jobs"')
    prefix = bundle[:marker] if marker >= 0 else bundle
    local = re.findall(r"https://[A-Za-z0-9.-]+", prefix)
    if not local:
        raise ValueError("Gullak Candid bundle has no discoverable public API origin")
    base = local[-1].rstrip("/")
    if urlsplit(base).hostname is None:
        raise ValueError("Gullak Candid public API origin is not an https URL")
    return base


def parse_public_jobs(payload) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("pipelines"), list):
        raise ValueError("Gullak public jobs response is missing its pipelines list")
    rows, issues, seen = [], [], set()
    for row in payload["pipelines"]:
        if not isinstance(row, dict):
            issues.append("public_jobs_row_not_object")
            continue
        identity = str(row.get("id") or "")
        title = clean_text(row.get("name"))
        if not UUID_RE.fullmatch(identity):
            issues.append(f"public_jobs_row_missing_pipeline_id:{identity or 'none'}")
            continue
        if not title:
            issues.append(f"public_jobs_row_missing_title:{identity}")
            continue
        if identity in seen:
            issues.append(f"public_jobs_row_duplicate_id:{identity}")
            continue
        if row.get("status") not in (None, "active"):
            issues.append(f"public_jobs_row_status_not_active:{identity}:{row.get('status')}")
        for key in ("description", "experience_required", "jd_link"):
            value = row.get(key)
            if value is not None and not isinstance(value, str):
                issues.append(f"public_jobs_row_field_not_string:{identity}:{key}")
        seen.add(identity)
        rows.append({
            "id": identity,
            "name": title,
            "description": clean_text(row.get("description")) or None,
            "experience_required": clean_text(row.get("experience_required")) or None,
            "jd_link": clean_text(row.get("jd_link")) or None,
            "status": row.get("status"),
            "source_row": dict(row),
        })
    return {
        "rows": rows,
        "count": len(payload["pipelines"]),
        "ids": [row["id"] for row in rows],
        "issues": issues,
    }


def parse_jd_link(link: str | None) -> dict | None:
    link = clean_text(link)
    if not link:
        return None
    parsed = urlsplit(link)
    if parsed.scheme != "https":
        raise ValueError(f"Gullak JD link is not https: {link}")
    if parsed.hostname == "drive.google.com":
        match = DRIVE_FILE_RE.match(parsed.path)
        if match and JD_ID_RE.fullmatch(match.group(1)):
            file_id = match.group(1)
            return {
                "kind": "pdf",
                "file_id": file_id,
                "link": link,
                "download_url": f"https://drive.google.com/uc?export=download&id={file_id}",
            }
    if parsed.hostname == "docs.google.com":
        match = DOCS_FILE_RE.match(parsed.path)
        if match and JD_ID_RE.fullmatch(match.group(1)):
            file_id = match.group(1)
            return {
                "kind": "document",
                "file_id": file_id,
                "link": link,
                "download_url": f"https://docs.google.com/document/d/{file_id}/export?format=txt",
            }
    raise ValueError(f"Gullak JD link left its inspected Drive/Docs destinations: {link}")


class GullakMoney(Adapter):
    VERIFICATION_REVISION = "gullak-money.official-candid-public-jobs-and-jd-attachments.1"
    async def _fetch(self, url: str, *, purpose: str):
        started = time.perf_counter()
        response = await self.http.get(url)
        record = {
            "url": url,
            "purpose": purpose,
            "final_url": str(response.url),
            "status": response.status_code,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "content_type": response.headers.get("content-type"),
            "seconds": max(0.0, time.perf_counter() - started),
        }
        self.result.metadata.setdefault("journey", []).append(record)
        return response, record

    async def _detail(self, job):
        row = job.raw["listing"]
        if getattr(self, "blocked_reason", None):
            job.description_method = "gullak_money.stopped_after_access_block"
            job.description_source_url = job.raw.get("source_api") or API_JOBS
            job.description_unavailable = True
            job.warnings.append("gullak_money_stopped_after_access_block")
            return job
        jd_link = row.get("jd_link")
        if not jd_link:
            self._fallback(job, "gullak_money_public_listing_has_no_jd_attachment")
            job.raw["jd"] = {"link": None, "reason": "no_jd_link_on_official_public_listing"}
            return job

        try:
            jd = parse_jd_link(jd_link)
        except ValueError as exc:
            self._fallback(job, f"gullak_money_invalid_jd_link:{exc}")
            job.raw["jd"] = {"link": jd_link, "reason": str(exc)}
            return job

        try:
            started = time.perf_counter()
            response = await self.http.get(jd["download_url"])
            fetch_seconds = max(0.0, time.perf_counter() - started)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if any(token in message for token in ("HTTP 429", "HTTP 403", "CAPTCHA", "captcha")):
                self.blocked_reason = message
                self.result.metadata["access_block"] = {
                    "url": jd["download_url"], "job_id": job.job_id, "reason": message}
                self.result.errors.append(f"gullak_money_jd_access_blocked_stop:{message}")
            self._fallback(job, f"gullak_money_jd_fetch_failed:{message}")
            job.raw["jd"] = {**jd, "fetch_error": message}
            return job

        content = response.content
        digest = sha256(content).hexdigest()
        attachment_dir = self.http.output / "attachments" if self.http.output else None
        if attachment_dir:
            attachment_dir.mkdir(parents=True, exist_ok=True)
            suffix = ".pdf" if jd["kind"] == "pdf" else ".txt"
            (attachment_dir / f"{job.job_id}-{digest[:16]}{suffix}").write_bytes(content)
        self.result.timings["jd_fetch_seconds"] = self.result.timings.get("jd_fetch_seconds", 0.0) + fetch_seconds

        extracted = None
        extraction_error = None
        if jd["kind"] == "pdf":
            if not content.startswith(b"%PDF-"):
                extraction_error = "JD download did not return a PDF"
            else:
                started = time.perf_counter()
                try:
                    extracted = await asyncio.to_thread(extract_attachment, content, f"{jd['file_id']}.pdf")
                except Exception as exc:
                    extraction_error = f"{type(exc).__name__}: {exc}"
                self.result.timings["attachment_extraction_seconds"] = self.result.timings.get(
                    "attachment_extraction_seconds", 0.0) + max(0.0, time.perf_counter() - started)
            override = JD_TEXT_OVERRIDES.get(jd["file_id"])
            if extracted is None and override and override["sha256"] == digest:
                blocks = document_blocks(override["text"])
                rendered = render_blocks(blocks)
                method = override["method"]
                extraction = {
                    "method": override["method"],
                    "note": override["note"],
                    "pages": override["pages"],
                    "text_sha256": sha256(rendered["text"].encode()).hexdigest(),
                    "source": "rendered_pdf_transcription",
                }
            elif extracted is not None:
                blocks = pdf_blocks(extracted["text"])
                rendered = render_blocks(blocks)
                method = "gullak_money.google_drive_pdf_full_text"
                extraction = {
                    "method": extracted["method"],
                    "pages": extracted["pages"],
                    "page_text_lengths": extracted["page_text_lengths"],
                    "text_sha256": sha256(rendered["text"].encode()).hexdigest(),
                }
            else:
                self._fallback(job, f"gullak_money_jd_pdf_text_unavailable:{extraction_error}")
                job.raw["jd"] = {
                    **jd, "sha256": digest, "content_type": response.headers.get("content-type"),
                    "bytes": len(content), "fetch_seconds": fetch_seconds,
                    "extraction_error": extraction_error,
                }
                return job
        else:
            text = clean_text(content.decode("utf-8-sig", errors="replace"))
            if len(text) < 100:
                extraction_error = f"Google Docs export text is too short ({len(text)} chars)"
                blocks = document_blocks(text) if text else []
                rendered = render_blocks(blocks) if blocks else {"html": "", "text": "", "blocks": []}
            else:
                blocks = document_blocks(text)
                rendered = render_blocks(blocks)
            if len(rendered["text"]) < 100:
                self._fallback(job, f"gullak_money_jd_document_text_unavailable:{extraction_error}")
                job.raw["jd"] = {
                    **jd, "sha256": digest, "content_type": response.headers.get("content-type"),
                    "bytes": len(content), "fetch_seconds": fetch_seconds,
                    "extraction_error": extraction_error,
                }
                return job
            method = "gullak_money.google_docs_export_full_text"
            extraction = {
                "method": "Google Docs plain-text export",
                "text_sha256": sha256(rendered["text"].encode()).hexdigest(),
            }

        sections = extract_sections(blocks)
        meta = parse_meta(blocks)
        location, workplace = normalize_location(meta.get("location"))
        if not workplace and re.search(r"\b(?:in-office|work from office)\b", rendered["text"], re.I):
            workplace = "On-site"
        employment = normalize_employment(meta.get("role type"))
        jd_experience = meta.get("experience")
        job.description_html = rendered["html"]
        job.description_text = rendered["text"]
        job.description_method = method
        job.description_source_url = jd_link
        job.description_unavailable = False
        job.locations = [location] if location else []
        job.employment_type = employment
        job.workplace_type = workplace
        job.experience = row.get("experience_required") or jd_experience
        job.qualifications = sections["qualifications"]
        job.responsibilities = sections["responsibilities"]
        job.employer = EMPLOYER
        job.language = "en"
        job.raw["jd"] = {
            **jd,
            "sha256": digest,
            "content_type": response.headers.get("content-type"),
            "bytes": len(content),
            "fetch_seconds": fetch_seconds,
            "extraction": extraction,
            "jd_meta": meta,
        }
        job.raw["jd_meta"] = meta
        return job

    def _fallback(self, job, warning: str) -> None:
        summary = job.raw["listing"].get("description")
        if summary:
            job.description_text = summary
            job.description_html = f"<p>{escape(summary)}</p>"
            job.description_method = "gullak_money.public_jobs_listing_summary_only"
        else:
            job.description_text = ""
            job.description_html = ""
            job.description_method = "gullak_money.public_jobs_listing_no_description"
        job.description_source_url = job.raw.get("source_api") or API_JOBS
        job.description_unavailable = True
        job.employer = EMPLOYER
        job.language = "en"
        job.warnings.append(warning)
        self.result.errors.append(f"Gullak job {job.job_id}: {warning}")

    async def run(self):
        started = time.perf_counter()
        self.blocked_reason = None
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry_url=OFFICIAL_ENTRY,
            listing_url=LISTING_URL,
            app_shell_url=APP_SHELL_URL,
            listing_authority="The exact public jobs request made by Gullak's own Careers link and Candid SPA.",
            listing_membership_request={
                "method": "GET",
                "url": API_JOBS,
                "query": {},
                "scope": "Company-specific Gullak Candid app; one unfiltered public jobs response; no other tenant or board is queried.",
            },
            detail_method="Role-bearing Google Drive PDF / Google Docs attachments linked from each public listing card; no separate job-detail page exists.",
            request_scope="Only Gullak's official homepage, its Candid app shell/bundle, and its public jobs/JD requests.",
            rendered_reference="companies/gullak-money/browser-reference-worker.json",
            date_provenance="The public listing, API, and JD documents expose no posting or update date; all date fields remain null. PDF metadata creation dates are not posting dates.",
            journey=[],
        )

        entry_started = time.perf_counter()
        try:
            entry, entry_record = await self._fetch(self.company.careers_url, purpose="official_homepage")
        except Exception as exc:
            self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - entry_started)
            self.result.errors.append(f"gullak_money_official_entry_failed:{type(exc).__name__}:{exc}")
            return self._finish(started)
        self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - entry_started)
        if urlsplit(str(entry.url)).hostname not in {"gullak.money", "www.gullak.money"}:
            self.result.errors.append("gullak_money_official_entry_left_homepage")
            return self._finish(started)
        try:
            official = parse_official_entry(entry.text, str(entry.url))
        except ValueError as exc:
            self.result.errors.append(f"gullak_money_official_entry_changed:{exc}")
            return self._finish(started)
        self.result.metadata["official_entry"] = {**official, **entry_record}

        shell_started = time.perf_counter()
        try:
            shell, shell_record = await self._fetch(APP_SHELL_URL, purpose="candid_app_shell")
        except Exception as exc:
            self.result.timings["listing_shell_seconds"] = max(0.0, time.perf_counter() - shell_started)
            self.result.errors.append(f"gullak_money_candid_shell_failed:{type(exc).__name__}:{exc}")
            return self._finish(started)
        self.result.timings["listing_shell_seconds"] = max(0.0, time.perf_counter() - shell_started)
        try:
            app = parse_app_shell(shell.text, str(shell.url))
        except ValueError as exc:
            self.result.errors.append(f"gullak_money_candid_shell_changed:{exc}")
            return self._finish(started)
        self.result.metadata["candid_app_shell"] = {**app, **shell_record}

        bundle_url = APP_ORIGIN + app["bundle_path"]
        bundle_started = time.perf_counter()
        try:
            bundle, bundle_record = await self._fetch(bundle_url, purpose="candid_listing_bundle")
        except Exception as exc:
            bundle, bundle_record = None, {"error": f"{type(exc).__name__}: {exc}"}
        self.result.timings["bundle_seconds"] = max(0.0, time.perf_counter() - bundle_started)
        api_base = API_BASE
        if bundle is not None:
            try:
                api_base = parse_api_base(bundle.text, str(bundle.url))
                self.result.metadata["candid_bundle"] = {
                    **bundle_record, "api_base": api_base,
                    "contains_public_jobs": "/public/jobs" in bundle.text,
                }
            except ValueError as exc:
                self.result.warnings.append(f"gullak_money_bundle_api_base_not_verified:{exc}")
                self.result.metadata["candid_bundle"] = {**bundle_record, "api_base": api_base,
                                                         "verification_error": str(exc)}
        else:
            self.result.warnings.append("gullak_money_bundle_unavailable_using_observed_api_origin")
            self.result.metadata["candid_bundle"] = bundle_record
        jobs_url = api_base.rstrip("/") + "/public/jobs"
        self.result.metadata["listing_membership_request"]["url"] = jobs_url

        listing_started = time.perf_counter()
        try:
            payload = await self.http.json(jobs_url, headers={"Accept": "application/json"})
        except Exception as exc:
            self.result.errors.append(f"gullak_money_public_jobs_request_failed:{type(exc).__name__}:{exc}")
            return self._finish(started)
        self.result.timings["api_listing_seconds"] = max(0.0, time.perf_counter() - listing_started)
        source_collected = max(0.0, time.perf_counter() - started)
        self.result.timings["source_collection_seconds"] = source_collected
        try:
            listing = parse_public_jobs(payload)
        except ValueError as exc:
            self.result.errors.append(f"gullak_money_public_jobs_schema_changed:{exc}")
            return self._finish(started)
        self.result.metadata["initial_public_jobs"] = {
            "url": jobs_url,
            "count": listing["count"],
            "ids": listing["ids"],
            "issues": listing["issues"],
        }
        if listing["issues"]:
            self.result.warnings.extend(listing["issues"])
        if not listing["rows"]:
            self.result.errors.append("gullak_money_public_jobs_returned_no_pipelines")
            return self._finish(started)

        for row in listing["rows"]:
            apply_url = f"{APP_ORIGIN}/jobs/{row['id']}/apply"
            job = self.job(
                {"listing": row, "source_row": row["source_row"], "source_api": jobs_url,
                 "listing_url": LISTING_URL, "observed_apply_href": f"/jobs/{row['id']}/apply"},
                title=row["name"],
                url=apply_url,
                job_id=row["id"],
                apply_url=apply_url,
                employer=EMPLOYER,
                language="en",
                record_identity=sha256(f"{self.company.name}|gullak_money|{row['id']}|{apply_url}".encode()).hexdigest(),
            )
            self.add(job)
            if self.limit():
                break

        await self.hydrate(self.result.jobs, self._detail)
        self.result.expected_total = listing["count"]
        self.result.pages = 1

        recheck_started = time.perf_counter()
        try:
            final_payload = await self.http.json(jobs_url, headers={"Accept": "application/json"})
            final_listing = parse_public_jobs(final_payload)
        except Exception as exc:
            final_listing = None
            self.result.errors.append(f"gullak_money_final_membership_recheck_failed:{type(exc).__name__}:{exc}")
        self.result.timings["final_membership_recheck_seconds"] = max(0.0, time.perf_counter() - recheck_started)

        initial_ids = listing["ids"]
        membership_complete = len(initial_ids) == len(self.result.jobs) and not listing["issues"]
        changes = []
        if final_listing is None:
            membership_complete = False
        else:
            final_ids = final_listing["ids"]
            membership_complete = membership_complete and final_ids == initial_ids
            added = [identity for identity in final_ids if identity not in initial_ids]
            removed = [identity for identity in initial_ids if identity not in final_ids]
            if added or removed:
                self.result.errors.append("gullak_money_public_listing_changed_during_collection")
            initial_rows = {row["id"]: row for row in listing["rows"]}
            final_rows = {row["id"]: row for row in final_listing["rows"]}
            for identity in sorted(set(initial_rows) & set(final_rows)):
                before, after = initial_rows[identity], final_rows[identity]
                for key in ("name", "status", "description", "experience_required", "jd_link"):
                    if before.get(key) != after.get(key):
                        changes.append({"id": identity, "field": key})
            if changes:
                self.result.errors.append("gullak_money_public_job_fields_changed_during_collection")
                membership_complete = False
            self.result.metadata["membership_reconciliation"] = {
                "complete": membership_complete,
                "initial_count": listing["count"],
                "final_count": final_listing["count"],
                "initial_ids": initial_ids,
                "final_ids": final_ids,
                "added": added,
                "removed": removed,
                "field_changes": changes,
                "issues": final_listing["issues"],
            }
        self.result.listing_complete = membership_complete

        failed = [{"id": job.job_id, "url": job.url, "reason": job.warnings[-1]}
                  for job in self.result.jobs if not job.description_verified or job.description_unavailable]
        self.result.metadata["failed_jobs"] = failed
        self.result.metadata["jd_extraction"] = {
            "pdfs": sum(1 for job in self.result.jobs if job.raw.get("jd", {}).get("kind") == "pdf"),
            "documents": sum(1 for job in self.result.jobs if job.raw.get("jd", {}).get("kind") == "document"),
            "no_attachment": sum(1 for job in self.result.jobs if not job.raw.get("jd", {}).get("link")),
            "manual_transcriptions": sum(1 for job in self.result.jobs
                                         if job.description_method == "gullak_money.rendered_pdf_manual_transcription"),
            "unavailable": sum(1 for job in self.result.jobs if job.description_unavailable),
        }
        return self._finish(started)

    def _finish(self, started):
        elapsed = max(0.0, time.perf_counter() - started)
        self.result.timings["company_collection_seconds"] = elapsed
        self.result.duration_seconds = elapsed
        self.result.finished_at = now()
        return self.result
