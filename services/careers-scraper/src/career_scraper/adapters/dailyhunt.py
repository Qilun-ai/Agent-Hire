"""Dailyhunt (VerSe Innovation) official careers journey: assigned /careers entry,
live VerSe homepage, and its single published hosted board link.

The company-published journey observed on 2026-11-07 is:
  - Assigned entry https://www.verse.in/careers returns 404 (Apache).
  - The live VerSe homepage https://www.verse.in/ carries exactly one live
    ``Career`` nav anchor to https://jobs.careerswith-us.com/verse.
    The legacy ``career.php`` Talent-Network link is HTML-commented out.
  - That hosted board is unreachable (its public DNS resolves to 127.0.0.1 and
    TCP 80/443 is refused). No board listing, pagination, filter, or full
    detail body could be inspected live.

This adapter follows only those company-published URLs. It never enumerates
an ATS/hosted board from a guessed slug, never treats the Talent Network form
or any footer text as a posting, and never fabricates job IDs, dates, or
descriptions. When the board is unreachable it records the exact transport
error, collects zero jobs, and marks the source incomplete so the run lands
in failed_waitlist with retry evidence instead of a false empty success.
"""
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter

ASSIGNED_ENTRY = "https://www.verse.in/careers"
HOME_URL = "https://www.verse.in/"
BOARD_URL = "https://jobs.careerswith-us.com/verse"
TALENT_NETWORK_URL = "https://www.verse.in/career.php"

VERIFIED_HOSTS = {"verse.in", "www.verse.in"}
EXPECTED_BOARD_HOST = "jobs.careerswith-us.com"
EXPECTED_BOARD_PATH = "/verse"

BLOCKED_MARKERS = ("401", "403", "429")


def _is_blocked_error(error) -> bool:
    message = str(error)
    return any(
        marker in message for marker in BLOCKED_MARKERS
    )


class Dailyhunt(Adapter):
    """Follow only Dailyhunt's company-published VerSe homepage nav to its board."""

    VERIFICATION_REVISION = "dailyhunt.verse-homepage-nav-to-cwus-board.1"

    @staticmethod
    def _career_board_url(html, label):
        soup = BeautifulSoup(html or "", "lxml")
        matches = []
        for anchor in soup.select("a[href]"):
            href = (anchor.get("href") or "").strip()
            # Live DOM anchors only: HTML comments are not returned by the parser,
            # so the commented-out career.php link cannot pass here.
            if href == BOARD_URL and anchor.get_text(" ", strip=True) == "Career":
                matches.append(anchor)
        if len(matches) != 1:
            raise ValueError(
                f"Dailyhunt {label} page does not expose exactly one live Career nav link "
                f"to {BOARD_URL} (found {len(matches)})"
            )
        anchor = matches[0]
        parsed = urlsplit(BOARD_URL)
        if (
            parsed.scheme != "https"
            or parsed.hostname != EXPECTED_BOARD_HOST
            or parsed.path != EXPECTED_BOARD_PATH
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"Dailyhunt inspected board URL changed: {BOARD_URL}")
        if (anchor.get("target") or "") != "_blank":
            raise ValueError("Dailyhunt Career nav link no longer opens in a new tab as observed")
        if "menutitle" not in (anchor.get("class") or []):
            raise ValueError("Dailyhunt Career nav link no longer uses the observed menutitle class")
        return BOARD_URL

    @staticmethod
    def _home_title(html):
        soup = BeautifulSoup(html or "", "lxml")
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        return None

    def _assert_verse_url(self, url, *, expected_path, label):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in VERIFIED_HOSTS:
            raise ValueError(f"Dailyhunt {label} left the official verse.in HTTPS site: {url}")
        path = parsed.path.rstrip("/") or "/"
        expected = expected_path.rstrip("/") or "/"
        if path != expected:
            raise ValueError(f"Dailyhunt {label} has an unexpected path: {url}")
        return str(url)

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        parsed_entry = urlsplit(entry_url)
        if (
            parsed_entry.scheme != "https"
            or parsed_entry.hostname not in VERIFIED_HOSTS
            or parsed_entry.path.rstrip("/") != "/careers"
        ):
            raise ValueError(
                "Dailyhunt dedicated flow must start from its assigned https://www.verse.in/careers entry"
            )
        if not self.page_guard():
            return self.result

        entry_status = None
        entry_started = time.perf_counter()
        try:
            entry_response = await self.http.get(entry_url)
            entry_status = entry_response.status_code
        except Exception as exc:  # FetchError for 404/transport; quarantine only blocked statuses
            entry_error = f"{type(exc).__name__}: {exc}"
            if _is_blocked_error(exc):
                self.result.errors.append(
                    f"Dailyhunt assigned entry {entry_url} blocked; stopping without further source requests: {entry_error}"
                )
                self.result.metadata.update({
                    "verification_revision": self.VERIFICATION_REVISION,
                    "official_entry_url": entry_url,
                    "official_entry_status": "blocked",
                    "official_entry_error": entry_error,
                    "homepage_url": None,
                    "board_url": None,
                    "board_status": "not_attempted",
                    "board_error": None,
                    "membership_authority": (
                        "Assigned https://www.verse.in/careers entry; the live VerSe homepage "
                        "nav Career link to the hosted board would be membership authority but the entry was blocked."
                    ),
                    "ui_pagination": "Unknown: source blocked before homepage/board inspection.",
                    "description_source": "None: source blocked before any listing or detail request.",
                    "collection_wall_seconds": round(time.perf_counter() - started, 6),
                })
                self.result.timings["entry_phase_seconds"] = round(
                    time.perf_counter() - entry_started, 6
                )
                self.result.timings["homepage_phase_seconds"] = 0.0
                self.result.timings["board_phase_seconds"] = 0.0
                self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata[
                    "collection_wall_seconds"
                ]
                self.result.listing_complete = False
                self.result.pages = 0
                return self.result
            # Expected observed case: assigned /careers is 404. Continue to the
            # same host's live homepage, which is the current published journey.
        entry_seconds = time.perf_counter() - entry_started

        home_started = time.perf_counter()
        homepage_final_url = None
        homepage_title = None
        board_url = None
        try:
            home_response = await self.http.get(HOME_URL)
            homepage_final_url = self._assert_verse_url(
                str(home_response.url), expected_path="/", label="homepage"
            )
            homepage_title = self._home_title(home_response.text)
            board_url = self._career_board_url(home_response.text, "homepage")
        except Exception as exc:
            homepage_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(
                f"Dailyhunt VerSe homepage {HOME_URL} / Career nav inspection failed: {homepage_error}"
            )
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": entry_url,
                "official_entry_status": entry_status,
                "homepage_url": HOME_URL,
                "homepage_final_url": homepage_final_url,
                "homepage_title": homepage_title,
                "board_url": board_url,
                "board_status": "not_attempted",
                "board_error": None,
                "membership_authority": (
                    "Assigned https://www.verse.in/careers entry followed on the same verse.in host "
                    "to the live homepage; no live Career nav to the hosted board was verified."
                ),
                "ui_pagination": "Unknown: homepage/nav inspection failed.",
                "description_source": "None: homepage/nav failed before any listing or detail request.",
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
            })
            self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
            self.result.timings["homepage_phase_seconds"] = round(
                time.perf_counter() - home_started, 6
            )
            self.result.timings["board_phase_seconds"] = 0.0
            self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata[
                "collection_wall_seconds"
            ]
            self.result.listing_complete = False
            self.result.pages = 0
            return self.result
        homepage_seconds = time.perf_counter() - home_started

        board_started = time.perf_counter()
        board_status = None
        board_error = None
        try:
            board_response = await self.http.get(board_url)
            board_status = board_response.status_code
        except Exception as exc:
            board_error = f"{type(exc).__name__}: {exc}"
            board_status = "unreachable"
            board_seconds = time.perf_counter() - board_started
            self.result.errors.append(
                f"Dailyhunt company-published board {board_url} unreachable: {board_error}"
            )
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": entry_url,
                "official_entry_status": entry_status,
                "homepage_url": HOME_URL,
                "homepage_final_url": homepage_final_url,
                "homepage_title": homepage_title,
                "career_nav_board_url": board_url,
                "board_url": board_url,
                "board_status": board_status,
                "board_error": board_error,
                "membership_authority": (
                    "Live https://www.verse.in/ homepage Career nav link (target _blank, class menutitle) "
                    "to the hosted board https://jobs.careerswith-us.com/verse; no other company-published "
                    "listing was enumerated."
                ),
                "ui_pagination": (
                    "Unknown: the company-published board is unreachable, so no listing page, "
                    "pagination, or filter control could be inspected."
                ),
                "description_source": (
                    "None: the company-published board is unreachable, so no full detail body could be "
                    "retrieved. Footer text, the Talent Network form, and application questions are not descriptions."
                ),
                "talent_network_page": TALENT_NETWORK_URL,
                "talent_network_note": (
                    "https://www.verse.in/career.php (Talent Network form) is no longer live-linked from "
                    "the homepage nav (its href survives only inside an HTML comment) and is not a job listing; "
                    "it was not counted, not posted, and not treated as an evergreen posting."
                ),
                "ats_wide_search_or_enumeration_used": False,
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
            })
            self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
            self.result.timings["homepage_phase_seconds"] = round(homepage_seconds, 6)
            self.result.timings["board_phase_seconds"] = round(board_seconds, 6)
            self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata[
                "collection_wall_seconds"
            ]
            self.result.listing_complete = False
            self.result.pages = 1
            return self.result
        board_seconds = time.perf_counter() - board_started

        # A future run whose board is reachable with a format never inspected live
        # must not invent a parser. Keep the raw evidence via the HTTP archive
        # (get already archived the response) but enumerate zero jobs and fail.
        self.result.errors.append(
            f"Dailyhunt company-published board {board_url} returned HTTP {board_status} but its listing "
            "membership/format was never inspected live; refusing to enumerate an uninspected board. "
            "Inspect the actual board UI, pagination, and >=2 full detail bodies before collecting."
        )
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": entry_url,
            "official_entry_status": entry_status,
            "homepage_url": HOME_URL,
            "homepage_final_url": homepage_final_url,
            "homepage_title": homepage_title,
            "career_nav_board_url": board_url,
            "board_url": board_url,
            "board_status": f"reachable_uninspected_{board_status}",
            "board_error": None,
            "membership_authority": (
                "Live https://www.verse.in/ homepage Career nav link to the hosted board; board "
                "format never inspected live, so no enumeration was performed."
            ),
            "ui_pagination": "Unknown: board became reachable without a prior live inspection of its UI.",
            "description_source": "None: board format never inspected live; no detail bodies retrieved.",
            "ats_wide_search_or_enumeration_used": False,
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
        self.result.timings["homepage_phase_seconds"] = round(homepage_seconds, 6)
        self.result.timings["board_phase_seconds"] = round(board_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata[
            "collection_wall_seconds"
        ]
        self.result.listing_complete = False
        self.result.pages = 1
        return self.result
