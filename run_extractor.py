"""Thalian Hall extractor implementation using the framework.

Site shape (all confirmed live against real pages, not guessed):
  - Listing pages (/Main_Attractions, /cinema, /holidays, /local-events) are
    static, server-rendered Duda "photo gallery" grids -- no JS wait needed,
    parsed straight out of the page source via BeautifulSoup.
  - Detail pages are Duda dynamic-page-template pages that embed a Spektrix
    booking widget in iframe#SpektrixIFrame, pointed at
    tickets.thalianhall.org/thalianhall/website/eventDetails.aspx.
  - That Spektrix page is navigated to DIRECTLY as its own top-level page
    (not interacted with inside the iframe) -- it's on its own subdomain, so
    this sidesteps all iframe-switching flakiness. It first shows an
    <select id="...InstanceList"> of performance date/times (each option's
    text carries the real date+year+time, which the listing pages never
    show) plus a "Book now" button. Selecting an instance and clicking Book
    now postbacks the SAME url into the classic Spektrix "ChooseSeats" view:
    an area dropdown (select[id*='AvailableAreas'], first option is always a
    combined zoomed-out overview with no seats of its own) and, per area,
    seat images (img.Seat / img.SeatSelectable) whose tooltip carries the
    seat id and USD price, e.g. "A117 - $74.90".
"""
import json
import re
import sys

import pandas as pd
from bs4 import BeautifulSoup
from dateutil import parser
from selenium.webdriver.common.by import By
from seleniumbase import SB

from utils.base_extractor import BaseExtractor
from utils.logger import setup_logger
from utils.scraping_helpers import (
    format_datetime_key,
    get_currency_from_price,
    get_scrape_datetime,
    human_delay,
    human_scroll,
    normalize_country,
    safe_get_denver,
    standardize_category,
)

from .thalian_hall_config import (
    BASE_URL,
    COMBINED_SEATING_AREA_LABEL,
    DEFAULT_CURRENCY,
    DEFAULT_VENUE_DETAILS,
    PAGES,
    RUN_HEADLESS,
    SELECTORS,
)

logger = setup_logger(__name__, log_to_file=False)


class ThalianHallExtractor(BaseExtractor):
    """Extractor for the Thalian Hall website."""

    # Spektrix seat tooltip format, e.g. "A117 - $74.90" -- USD, not GBP,
    # since this is a US (Wilmington, NC) venue.
    _SEAT_TOOLTIP_RE = re.compile(r"^([A-Za-z0-9]+)\s*-\s*\$([\d,]+(?:\.\d{1,2})?)")

    def __init__(self, local_test=False, show_count=2, **kwargs):
        """Set up the extractor's site_id/logging/local-test config via BaseExtractor."""
        super().__init__(
            site_id="thalian_hall",
            log_to_file=False,
            log_to_terminal=True,
            local_test=local_test,
            show_count=show_count,
            **kwargs,
        )
        self.all_data = []

    def safe_get(self, sb, url, wait=10):
        """Navigate to `url` via UC-reconnect, solving a captcha/bot-check if one appears.

        Returns True on success, None on failure (caller treats None as
        "page didn't load" and retries).
        """
        try:
            sb.uc_open_with_reconnect(url, reconnect_time=wait if wait > 4 else 4)
            if (
                "captcha" in sb.get_current_url().lower()
                or "distil" in sb.get_page_source().lower()
            ):
                self.custom_logger.warning("Bot protection detected. Solving...")
                sb.uc_gui_handle_captcha()
                human_delay(2, 4)
            self.custom_logger.info("Page loaded successfully: %s", url)
            return True
        except Exception as e:
            self.custom_logger.error(
                "Failed to load page: %s | Exception: %s", url, repr(e)
            )
            return None

    def accept_cookies(self, sb):
        """Dismiss the Cookiebot consent banner if visible; no-op otherwise."""
        cookie_xpath = SELECTORS["cookie_button"]
        try:
            if sb.is_element_visible(cookie_xpath):
                human_delay(1, 2.5)
                sb.click(cookie_xpath)
                human_delay(2, 3)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Listing pages (static Duda photo gallery grid)                       #
    # ------------------------------------------------------------------ #

    def _parse_show_cards(self, sb) -> list[dict]:
        """Extract {title, url} for every real show card on the current listing page."""
        soup = BeautifulSoup(sb.get_page_source(), "html.parser")
        cards = soup.select(SELECTORS["show_card"])
        results = []
        for card in cards:
            title_el = card.select_one(SELECTORS["show_title"])
            link_el = card.select_one(SELECTORS["show_link"])
            if not title_el or not link_el or not link_el.get("href"):
                continue
            title = re.sub(r"\s+", " ", title_el.get_text(strip=True))
            href = link_el["href"]
            # href is normally an absolute-path relative link ("/thma-...")
            # but tolerate an already-absolute one too.
            url = (
                href
                if href.startswith("http")
                else BASE_URL.rstrip("/") + "/" + href.lstrip("/")
            )
            results.append({"title": title, "url": url})
        return results

    # ------------------------------------------------------------------ #
    # Detail page: title + Spektrix entry point                            #
    # ------------------------------------------------------------------ #

    def _get_show_title(self, sb) -> str | None:
        """Read and clean the show detail page's title (the paragraph widget's <h2>)."""
        try:
            title = sb.get_text(SELECTORS["title"]).strip()
            return re.sub(r"\s+", " ", title) or None
        except Exception:
            return None

    def _get_pretitle(self, sb) -> str | None:
        """Read the series/pretitle line (the same widget's <h3>), for logging only."""
        try:
            return sb.get_text(SELECTORS["pretitle"]).strip() or None
        except Exception:
            return None

    def _get_spektrix_event_url(self, sb) -> str | None:
        """Return the Spektrix booking iframe's src, or None if this show has no online booking."""
        try:
            src = sb.find_element(SELECTORS["spektrix_iframe"]).get_attribute("src")
            return src or None
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Spektrix eventDetails.aspx: performance instance picker               #
    # ------------------------------------------------------------------ #

    def _parse_date_option(self, raw_text: str) -> tuple[str, str] | None:
        """Parse an <option> like 'Fri Oct 09, 2026 - 7:30 PM' into (YYYY-MM-DD, HH:MM)."""
        if not raw_text:
            return None
        text = raw_text.replace("\xa0", " ").strip()
        parts = text.split(" - ")
        try:
            if len(parts) >= 2:
                dt_date = parser.parse(parts[0].strip())
                dt_time = parser.parse(parts[1].strip())
                return dt_date.strftime("%Y-%m-%d"), dt_time.strftime("%H:%M")
            dt = parser.parse(text)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except Exception:
            return None

    def _get_performance_instances(self, sb) -> list[dict]:
        """Return every performance from whichever booking mechanism the
        current page actually has: the Spektrix InstanceList dropdown, or
        the #calendarWrap calendar grid (e.g. "Tony" / "Teenage Sex and
        Death at Camp Miasma"). Both are attempted unconditionally -- a
        given page only ever has one of them, so the other's block just
        finds nothing and contributes no performances, rather than one
        mechanism's absence short-circuiting the other from ever being
        tried.
        """
        performances = []

        # -- Dropdown mechanism (Spektrix eventDetails.aspx) --
        has_dropdown = False
        try:
            sb.wait_for_element_present(SELECTORS["instance_dropdown"], timeout=15)
            has_dropdown = True
        except Exception:
            pass

        if not has_dropdown:
            # No dropdown at all -- check the page's own explanation ("no
            # dates" / "sold out") purely for a clearer log message; this
            # does NOT mean the show has no bookable performances, since
            # the calendar-grid check below may still find some.
            try:
                no_dates_text = sb.get_text(SELECTORS["no_dates"]).strip()
            except Exception:
                no_dates_text = ""

            try:
                sold_out_text = sb.get_text(SELECTORS["sold_out_text"]).strip()
            except Exception:
                sold_out_text = ""

            reason = (
                no_dates_text or sold_out_text or "instance dropdown never appeared"
            )
            self.custom_logger.info("  No InstanceList dropdown: %s", reason)
        else:
            try:
                raw_options = sb.execute_script(
                    """
                    var select = document.querySelector(arguments[0]);
                    if (!select) return [];
                    var out = [];
                    for (var i = 0; i < select.options.length; i++) {
                        out.push({value: select.options[i].value, text: select.options[i].text.trim()});
                    }
                    return out;
                    """,
                    SELECTORS["instance_dropdown"],
                )
                self.custom_logger.info(
                    f" Found {len(raw_options)} performances / instance options"
                )
                for opt in raw_options:
                    parsed = self._parse_date_option(opt.get("text", ""))
                    if not parsed:
                        self.custom_logger.warning(
                            "  Skipping unparseable date option: %r", opt.get("text")
                        )
                        continue
                    date_str, time_str = parsed
                    performances.append(
                        {"value": opt["value"], "date": date_str, "time": time_str}
                    )
            except Exception as e:
                self.custom_logger.warning(
                    "  Failed to read/parse InstanceList options: %s", e
                )

        # -- Calendar mechanism (#calendarWrap grid, e.g. "Tony" / "Teenage
        # Sex and Death at Camp Miasma") -- merged the old duplicate comment
        # here into this section header since both said the same thing.
        try:
            days = sb.find_elements(SELECTORS["calendar_date_card"])
            today = get_scrape_datetime()[:10]
            for day_idx, day in enumerate(days):
                raw_date = day.get_attribute("data-date")
                if not raw_date:
                    continue
                date = parser.parse(raw_date.strip()).strftime("%Y-%m-%d")
                if date < today:
                    continue

                # Get all clickable performance events for this day --
                # `day` is a raw Selenium WebElement (not `sb`), so
                # find_elements/find_element need an explicit By.CSS_SELECTOR.
                events = day.find_elements(By.CSS_SELECTOR, SELECTORS["calendar_event"])
                for event_idx, event in enumerate(events):
                    try:
                        raw_time = event.find_element(
                            By.CSS_SELECTOR, SELECTORS["calendar_time"]
                        ).text.strip()
                    except Exception:
                        continue
                    time_str = self._parse_calendar_time(raw_time)
                    if not time_str:
                        self.custom_logger.warning(
                            "  Skipping unparseable calendar time: %r", raw_time
                        )
                        continue

                    performances.append(
                        {
                            "date": date,
                            "time": time_str,
                            "day_idx": day_idx,
                            "event_idx": event_idx,
                            "layout": "calendar",
                        }
                    )

        except Exception as e:
            self.custom_logger.warning("  Failed to parse calendar date options: %s", e)

        return performances

    _CALENDAR_TIME_RE = re.compile(
        r"^(\d{1,2})(?::(\d{2}))?\s*([ap])m?$", re.IGNORECASE
    )

    def _parse_calendar_time(self, raw_time: str) -> str | None:
        """Parse a FullCalendar event time like '1:15p', '4p', '7:15p' into 'HH:MM' (24hr)."""
        if not raw_time:
            return None
        m = self._CALENDAR_TIME_RE.match(raw_time.strip())
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        meridiem = m.group(3).lower()
        if meridiem == "p" and hour != 12:
            hour += 12
        elif meridiem == "a" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"

    def click_perf_date_and_book_button(self, sb, instance_value: str) -> bool:
        """Select one performance instance and click 'Book now', landing on ChooseSeats.

        Returns False if the ChooseSeats view never appears (e.g. this
        instance turned out unbookable) -- caller treats that as a failed
        attempt and retries.
        """
        try:
            sb.wait_for_element_present(SELECTORS["instance_dropdown"], timeout=15)
            sb.select_option_by_value(SELECTORS["instance_dropdown"], instance_value)
            human_delay(1, 2)
            sb.click(SELECTORS["book_now_button"])
            sb.wait_for_ready_state_complete()
            sb.wait_for_element_present(SELECTORS["seating_dropdown"], timeout=15)
            return True
        except Exception as e:
            self.custom_logger.warning(
                "  Failed to reach ChooseSeats view for instance %s: %s",
                instance_value,
                e,
            )
            return False

    def click_calendar_event_and_reach_seats(
        self, sb, day_idx: int, event_idx: int
    ) -> bool:
        """Click the `event_idx`-th performance time in the `event_idx`-th
        calendar day cell, landing directly on ChooseSeats.

        Assumes the caller has just freshly reloaded the show's own detail
        page (there's no separate "InstanceList picker" page for
        calendar-widget shows -- the calendar itself lives on the detail
        page, and each event has no href, JS-driven click only, confirmed
        live). Re-locates the day/event by index rather than reusing any
        previously-found element, since a fresh page load invalidates any
        earlier element reference.
        """
        try:
            days = sb.find_elements(SELECTORS["calendar_date_card"])
            events = days[day_idx].find_elements(
                By.CSS_SELECTOR, SELECTORS["calendar_event"]
            )
            events[event_idx].click()
            sb.wait_for_ready_state_complete()
            sb.wait_for_element_present(SELECTORS["seating_dropdown"], timeout=15)
            return True
        except Exception as e:
            self.custom_logger.warning(
                "  Failed to reach ChooseSeats view for calendar day %d event %d: %s",
                day_idx,
                event_idx,
                e,
            )
            return False

    # ------------------------------------------------------------------ #
    # Spektrix ChooseSeats view: areas + seat map                          #
    # ------------------------------------------------------------------ #

    def _get_venue_details_from_page(self, sb) -> dict | None:
        """Scrape venue name/address live from the ChooseSeats page's own spans.

        Confirmed live: <span class="VenueName">Main Stage at Thalian
        Hall</span>, <span class="VenueAddress">310 Chestnut St, Wilmington,
        NC 28401</span>. City is pulled out of that address text; every
        Thalian Hall show is in Wilmington, NC, US regardless, so that's a
        safe default if the address text doesn't parse cleanly.
        """
        try:
            venue_name = sb.get_text(SELECTORS["venue_name"]).strip()
        except Exception:
            return None
        if not venue_name:
            return None

        try:
            raw_address = sb.get_text(SELECTORS["venue_address"]).strip()
        except Exception:
            raw_address = ""

        city_match = re.search(r",\s*([A-Za-z .'-]+),\s*[A-Z]{2}\s*\d{5}", raw_address)
        city = (
            city_match.group(1).strip() if city_match else DEFAULT_VENUE_DETAILS["city"]
        )

        self.custom_logger.info(
            "  Scraped venue from booking page: %s | %s", venue_name, raw_address
        )

        return {
            "venue": venue_name,
            "address": raw_address or venue_name,
            "city": city,
            "country": "US",
        }

    def extract_seats(self, sb) -> tuple[list, int | None, str | None, dict | None]:
        """Extract seats/pricing/venue details from the currently-loaded ChooseSeats view.

        Only reserved-seating pages (an AvailableAreas dropdown -- first
        option always a combined, zoomed-out overview,
        COMBINED_SEATING_AREA_LABEL, confirmed "Seating" here, with no
        priced seats of its own, so excluded -- + individual seat images per
        real sub-area: Parquet, Table 1-5, Dress Circle, Opera Suite A/B,
        Skybox) produce real seat_pricing entries here, with tooltips like
        "A117 - $74.90".

        Some shows (e.g. "Tony" / "Teenage Sex and Death at Camp Miasma" at
        the Stein Theatre - GA) use a General Admission ticket-type-quantity
        UI instead (no dropdown, no seat images -- a div.Ticket_Types_
        Selection with Adult/Senior/Student quantity rows). That is
        deliberately NOT treated as seatmap data here: per spec, a show with
        no real seatmap gets seat_pricing = {} regardless of whether it
        still sells GA tickets by some other means -- so for those, this
        just returns capacity=None (no seat images found at all), and the
        caller's retry loop naturally never confirms a seatmap for any of
        that show's performances, collapsing seat_pricing to {} as intended.
        """
        seat_data = []
        perf_capacity = 0
        sample_tooltip = None
        venue_details = self._get_venue_details_from_page(sb)

        dropdown_selector = SELECTORS["seating_dropdown"]
        has_dropdown = False
        sb.wait_for_ready_state_complete()

        try:
            sb.wait_for_element_present(dropdown_selector, timeout=8)
            human_delay(2, 3)
            sb.execute_script("window.scrollTo(0, 300);")
            human_delay(1, 2)
            sb.execute_script("window.scrollTo(0, 0);")
            has_dropdown = True
        except Exception:
            pass

        if has_dropdown:
            raw_options = sb.execute_script(
                """
                var select = document.querySelector(arguments[0]);
                if (!select) return [];
                var options = [];
                for (var i = 0; i < select.options.length; i++) {
                    options.push(select.options[i].text.trim());
                }
                return options;
                """,
                dropdown_selector,
            )
            # areas = [o for o in raw_options if o and o != COMBINED_SEATING_AREA_LABEL]
            areas = [
                o for o in raw_options if o and o not in COMBINED_SEATING_AREA_LABEL
            ]
            self.custom_logger.info("Found seating areas: %s", areas)
        else:
            # No dropdown at all -- treat the whole plan as one area.
            areas = ["stalls"]
            # areas = [COMBINED_SEATING_AREA_LABEL]

        prev_seat_count = -1
        _AREA_SELECT_MAX_ATTEMPTS = 3

        for area in areas:
            try:
                area_confirmed = not has_dropdown

                if has_dropdown:
                    for select_attempt in range(1, _AREA_SELECT_MAX_ATTEMPTS + 1):
                        self.custom_logger.info(
                            "  Selecting area '%s' (attempt %d/%d)",
                            area,
                            select_attempt,
                            _AREA_SELECT_MAX_ATTEMPTS,
                        )
                        # Set the option by visible text, then fire a native
                        # 'change' event so the __doPostBack the dropdown's
                        # own onchange handler wires up actually fires.
                        selected = sb.execute_script(
                            """
                            var select = document.querySelector(arguments[0]);
                            if (!select) return false;
                            var areaName = arguments[1];
                            for (var i = 0; i < select.options.length; i++) {
                                if (select.options[i].text.trim() === areaName) {
                                    select.value = select.options[i].value;
                                    select.dispatchEvent(new Event('change', { bubbles: true }));
                                    return true;
                                }
                            }
                            return false;
                            """,
                            dropdown_selector,
                            area,
                        )
                        if not selected:
                            self.custom_logger.warning(
                                "  Area '%s' not found in dropdown (attempt %d/%d)",
                                area,
                                select_attempt,
                                _AREA_SELECT_MAX_ATTEMPTS,
                            )
                            human_delay(1, 2)
                            continue

                        # Wait for the postback to re-render: poll until the
                        # seat count changes from the previous area's count,
                        # proving THIS area's seats actually loaded (a stale
                        # count means we're still mid-render on the previous
                        # area).
                        sb.wait_for_ready_state_complete()
                        for _ in range(15):
                            human_delay(2, 3)
                            _cur_count = len(
                                sb.find_elements(
                                    By.CSS_SELECTOR, SELECTORS["all_seats"]
                                )
                            )
                            if _cur_count > 0 and _cur_count != prev_seat_count:
                                area_confirmed = True
                                break

                        if area_confirmed:
                            break

                        self.custom_logger.warning(
                            "  Seat count for '%s' never changed (attempt %d/%d) -- retrying",
                            area,
                            select_attempt,
                            _AREA_SELECT_MAX_ATTEMPTS,
                        )

                    if not area_confirmed:
                        self.custom_logger.error(
                            "  Giving up on area '%s' after %d attempts -- skipping it",
                            area,
                            _AREA_SELECT_MAX_ATTEMPTS,
                        )
                        continue

                # Total seats (available + unavailable) in this area, for capacity.
                all_seats = sb.find_elements(By.CSS_SELECTOR, SELECTORS["all_seats"])
                area_capacity = len(all_seats)
                prev_seat_count = area_capacity
                perf_capacity += area_capacity
                self.custom_logger.info("Area: %s | Seats: %s", area, area_capacity)

                # Pull every available seat's tooltip/title text in one JS call.
                seat_tooltips = sb.execute_script(
                    """
                    var elems = document.querySelectorAll(arguments[0]);
                    var out = [];
                    for (var i = 0; i < elems.length; i++) {
                        out.push(elems[i].getAttribute('tooltip') || elems[i].getAttribute('title') || '');
                    }
                    return out;
                    """,
                    SELECTORS["available_seats"],
                )

                for tooltip in seat_tooltips:
                    if not tooltip or tooltip.lower() == "unavailable":
                        continue
                    match = self._SEAT_TOOLTIP_RE.match(tooltip)
                    if not match:
                        continue
                    seat_id = match.group(1)
                    ticket_price = float(match.group(2).replace(",", ""))
                    seat_data.append(
                        {
                            # Prefix with the area name so seat ids don't
                            # collide across areas.
                            "seat": f"{area} {seat_id}",
                            "ticket_price": ticket_price,
                        }
                    )
                    if sample_tooltip is None:
                        sample_tooltip = tooltip

            except Exception as area_error:
                self.custom_logger.warning(
                    "Failed to process area %s: %s", area, area_error
                )
                continue

        return (
            seat_data,
            (perf_capacity if perf_capacity > 0 else None),
            sample_tooltip,
            venue_details,
        )

    def extract_seat_metrics(
        self,
        sb,
        performances: list,
        spektrix_url: str | None = None,
        show_url: str | None = None,
    ) -> tuple[dict, str | None, int | None, dict | None]:
        """Visit each performance and collect its seat pricing.

        Three outcomes this must keep distinct:
          - Seats available: {"<date> <time>": [{"seat": ..., "ticket_price": ...}, ...]}
          - Sold out, OR no seat map could be confirmed for this specific
            performance while at least one OTHER performance of the same
            show does have a confirmed seatmap: {"<date> <time>": []}.
          - No seatmap for this show at ALL -- seat_pricing = {} instead,
            meaning the show has no online booking whatsoever.

        `performances` entries come in two shapes, and each is reached
        differently -- checked per-performance via `perf.get("layout")`,
        since `_get_performance_instances` returns whichever kind (or
        neither) a given show actually has:
          - Dropdown (default, no "layout" key): reload `spektrix_url` fresh
            (a plain GET back to the InstanceList picker), then select
            `perf["value"]` and click "Book now".
          - Calendar (`perf["layout"] == "calendar"`, e.g. "Tony" / "Teenage
            Sex and Death at Camp Miasma"): reload `show_url` fresh (the
            show's own detail page, which embeds the #calendarWrap grid
            directly -- there is no separate picker page for these), then
            click the `perf["event_idx"]`-th event in the
            `perf["day_idx"]`-th day cell.
        Both cases reload from scratch every attempt rather than trying to
        navigate "back" from a previous ChooseSeats view, which neither
        ASP.NET WebForms postbacks nor the calendar's JS-driven clicks
        handle reliably via browser history.
        """
        seat_pricing = {}
        capacity_values = []
        currency = None
        venue_details = None
        any_seatmap_confirmed = False

        for i, perf in enumerate(performances, start=1):
            key = format_datetime_key(perf["date"], perf["time"])
            if not key:
                self.custom_logger.warning(
                    "  Skipping performance with unformattable date/time: %s", perf
                )
                continue

            self.custom_logger.info(
                "[%d/%d] Processing performance: %s %s",
                i,
                len(performances),
                perf["date"],
                perf["time"],
            )

            # Check if the performance is sold out.
            # if not self.click_perf_date_and_book_button(sb, perf["value"]):
            # seat_pricing[key] = []
            # self.custom_logger.warning(
            # " Performance %s has no bookable link",
            # key,
            # )
            # continue
            try:
                _SEATMAP_MAX_ATTEMPTS = 3
                seats, capacity, sample_tooltip, venue = (
                    [],
                    None,
                    None,
                    None,
                )

                is_calendar = perf.get("layout") == "calendar"

                for attempt in range(1, _SEATMAP_MAX_ATTEMPTS + 1):
                    if is_calendar:
                        reload_url = show_url
                        reload_label = "show"
                    else:
                        reload_url = spektrix_url
                        reload_label = "Spektrix event"

                    if not safe_get_denver(sb, reload_url):
                        self.custom_logger.warning(
                            "  [Attempt %d/%d] Failed to reload %s page for %s",
                            attempt,
                            _SEATMAP_MAX_ATTEMPTS,
                            reload_label,
                            key,
                        )
                        seat_pricing[key] = []
                        reached = False
                    elif is_calendar:
                        self.accept_cookies(sb)
                        human_delay(1, 2)
                        reached = self.click_calendar_event_and_reach_seats(
                            sb, perf["day_idx"], perf["event_idx"]
                        )
                    else:
                        reached = self.click_perf_date_and_book_button(
                            sb, perf["value"]
                        )

                    if not reached:
                        seat_pricing[key] = []
                        self.custom_logger.warning(
                            "  [Attempt %d/%d] Could not reach ChooseSeats for %s",
                            attempt,
                            _SEATMAP_MAX_ATTEMPTS,
                            key,
                        )
                    else:
                        human_delay(2, 3)
                        human_scroll(sb)
                        (seats, capacity, sample_tooltip, venue) = self.extract_seats(
                            sb
                        )
                        if capacity:
                            self.custom_logger.info(
                                "  [Attempt %d/%d] Seatmap confirmed for %s: "
                                "%d total seat(s), %d available",
                                attempt,
                                _SEATMAP_MAX_ATTEMPTS,
                                key,
                                capacity,
                                len(seats),
                            )
                            break
                        self.custom_logger.warning(
                            "  [Attempt %d/%d] No seatmap evidence found for "
                            "%s (0 seat elements) -- may be a show with no "
                            "online booking, or General Admission page",
                            attempt,
                            _SEATMAP_MAX_ATTEMPTS,
                            key,
                        )
                        seat_pricing[key] = []

                    if attempt < _SEATMAP_MAX_ATTEMPTS:
                        self.custom_logger.info(
                            "  Retrying seat map extraction for %s...", key
                        )
                        human_delay(5, 9)

                if venue and not venue_details:
                    venue_details = venue

                seat_pricing[key] = seats

                if capacity:
                    any_seatmap_confirmed = True
                    capacity_values.append(capacity)
                    if sample_tooltip and currency is None:
                        currency = get_currency_from_price(
                            sample_tooltip,
                            DEFAULT_VENUE_DETAILS["country"],
                            DEFAULT_CURRENCY,
                        )
                    if seats:
                        self.custom_logger.info(
                            "  Seats: %d | Capacity: %s | Currency: %s",
                            len(seats),
                            capacity,
                            currency,
                        )
                else:
                    self.custom_logger.error(
                        "  No seatmap evidence found for %s %s after %d attempt(s) -- "
                        "recording [] for this performance",
                        perf["date"],
                        perf["time"],
                        _SEATMAP_MAX_ATTEMPTS,
                    )

            except Exception as e:
                # Safety net: one performance's seat scrape failing shouldn't
                # abort the rest of this show's performances -- still record
                # [] for it rather than leaving it out entirely.
                self.custom_logger.error(
                    "  Unexpected error extracting seats for %s: %s", key, repr(e)
                )
                seat_pricing[key] = []

            human_delay(5, 7)
            human_scroll(sb)

        if not any_seatmap_confirmed:
            self.custom_logger.info(
                "No performance for this show ever showed seatmap evidence -- "
                "this show has no seat map at all. seat_pricing = {}"
            )
            seat_pricing = {}

        capacity = max(capacity_values) if capacity_values else None
        return seat_pricing, currency, capacity, venue_details

    # ------------------------------------------------------------------ #
    # Per-show orchestration                                               #
    # ------------------------------------------------------------------ #

    def _scrape_one_show(
        self, sb, show_url: str, category: str
    ) -> tuple[dict | None, list[tuple[str, str, str]]]:
        """Scrape a single show page end-to-end.

        Returns (row, sub_links):
          - row is a completed row dict on success, or None if the show
            page did not render, has no bookable performances, or hit an
            unexpected error -- the caller retries.
          - sub_links is normally [], but non-empty when show_url turns out
            to be a HUB page rather than a real bookable show -- confirmed
            live for "Star Trek 60" (/star-trek-60): no h2 title, no
            SpektrixIFrame, but it embeds its own nested .photoGalleryThumbs
            gallery linking to 4 real per-screening pages, each of which
            DOES follow the normal single-show template. Those get queued
            in place of this page, which has nothing bookable of its own.

        The whole body is wrapped in one try/except so a single bad show
        can't kill the rest of the category's shows.
        """
        try:
            if not safe_get_denver(sb, show_url):
                return None, []

            self.accept_cookies(sb)
            human_delay(2, 3)
            human_scroll(sb)

            title = self._get_show_title(sb)
            if not title:
                sub_cards = self._parse_show_cards(sb)
                if sub_cards:
                    self.custom_logger.info(
                        "'%s' has no title of its own but contains %d nested "
                        "show card(s) -- treating it as a hub page and "
                        "queuing those instead",
                        show_url,
                        len(sub_cards),
                    )
                    return None, [
                        (card["url"], category, card["title"]) for card in sub_cards
                    ]
                self.custom_logger.warning("No title found for: %s", show_url)
                return None, []
            pretitle = self._get_pretitle(sb)
            self.custom_logger.info("Title: %s | Series: %s", title, pretitle)

            # Two entry points, checked in order:
            #   1. #SpektrixIFrame -> its eventDetails.aspx InstanceList
            #      dropdown (the common case, e.g. "Live and Let Die").
            #   2. No iframe at all -- some shows (e.g. "Tony" / "Teenage
            #      Sex and Death at Camp Miasma") instead embed a
            #      #calendarWrap FullCalendar grid directly on THIS page,
            #      with no separate picker page. _get_performance_instances
            #      tries both mechanisms itself; which one applies just
            #      depends on which page we're standing on when we call it.
            spektrix_url = self._get_spektrix_event_url(sb)
            if spektrix_url:
                if not safe_get_denver(sb, spektrix_url):
                    self.custom_logger.warning(
                        "Failed to load Spektrix event page for '%s'", title
                    )
                    return None, []
                human_delay(2, 3)
            else:
                self.custom_logger.info(
                    "No Spektrix booking iframe found for '%s' -- checking "
                    "this page for a calendar-widget booking flow instead",
                    title,
                )

            performances = self._get_performance_instances(sb)
            if not performances:
                self.custom_logger.warning(
                    "No bookable performances found for '%s', skipping", title
                )
                return None, []

            sorted_dates = sorted(p["date"] for p in performances)
            open_date, close_date = sorted_dates[0], sorted_dates[-1]

            (
                seat_pricing,
                currency,
                capacity,
                scraped_venue_details,
            ) = self.extract_seat_metrics(
                sb, performances, spektrix_url=spektrix_url, show_url=show_url
            )

            venue_details = scraped_venue_details or DEFAULT_VENUE_DETAILS
            venue_name = venue_details.get("venue") or DEFAULT_VENUE_DETAILS["venue"]
            address = venue_details.get("address") or DEFAULT_VENUE_DETAILS["address"]
            city = venue_details.get("city") or DEFAULT_VENUE_DETAILS["city"]
            country = normalize_country(
                venue_details.get("country") or DEFAULT_VENUE_DETAILS["country"]
            )

            self.custom_logger.info("Title: %s", title)
            self.custom_logger.info("Category: %s", category)
            self.custom_logger.info("Header dates: %s - %s", open_date, close_date)

            self.custom_logger.info("Venue: %s", venue_name)
            self.custom_logger.info("Address: %s", address)
            self.custom_logger.info("City: %s", city)
            self.custom_logger.info("Country: %s", country)

            self.custom_logger.info(
                "Performances: %d | Seat keys: %d", len(performances), len(seat_pricing)
            )

            self.custom_logger.info("Capacity: %s | Currency: %s", capacity, currency)

            return {
                "title": title,
                "category": standardize_category(category),
                "venue": venue_name,
                "venue_url": show_url,
                "address": address,
                "city": city,
                "country": country,
                "open_date": open_date,
                "close_date": close_date,
                "booking_start_date": None,
                "booking_end_date": close_date,
                "upcoming_performances": [
                    {"date": p["date"], "time": p["time"]} for p in performances
                ],
                "seat_pricing": seat_pricing,
                "capacity": int(capacity) if capacity is not None else None,
                "currency": currency or DEFAULT_CURRENCY,
                "is_limited_run": None,
                "scrape_datetime": get_scrape_datetime(),
            }, []
        except Exception as e:
            self.custom_logger.error(
                "Unexpected error scraping '%s': %s", show_url, repr(e)
            )
            return None, []

    def _scrape_shows(self, sb, show_queue: list, seen_links: set) -> None:
        """Scrape individual show pages with multi-pass retry.

        `show_queue` is a list of (url, category, title_hint) tuples.
        `seen_links` is the same dedup set used while building the initial
        queue from listing pages -- also used here so a hub page's newly
        discovered sub-show links (see _scrape_one_show) never get queued
        twice, including if the same sub-show is also linked directly from
        another category's listing page.
        """
        _MAX_PASSES = 3
        pending = list(show_queue)
        show_numbers = {item[0]: i + 1 for i, item in enumerate(show_queue)}

        for _pass in range(1, _MAX_PASSES + 1):
            if not pending:
                break

            self.custom_logger.info(
                "Show pass %d/%d — %d show(s)", _pass, _MAX_PASSES, len(pending)
            )
            still_pending = []

            for show_url, category, title_hint in pending:
                self.custom_logger.info(
                    "[%s] Show %d/%d: %s (%s)",
                    category,
                    show_numbers[show_url],
                    len(show_numbers),
                    title_hint,
                    show_url,
                )
                row, sub_links = self._scrape_one_show(sb, show_url, category)

                if sub_links:
                    # Hub page (e.g. "Star Trek 60"): nothing bookable on
                    # this URL itself, but it pointed at real sub-shows --
                    # queue those in its place instead of deferring/dropping
                    # this URL.
                    for link_url, link_category, link_title in sub_links:
                        if link_url in seen_links:
                            continue
                        seen_links.add(link_url)
                        show_numbers[link_url] = len(show_numbers) + 1
                        still_pending.append((link_url, link_category, link_title))
                    continue

                if row is None:
                    still_pending.append((show_url, category, title_hint))
                    self.custom_logger.warning(
                        "Pass %d: show deferred — %s", _pass, show_url
                    )
                else:
                    self.all_data.append(row)
                    self.log_record(row)
                    human_delay(8, 15)

            pending = still_pending

            if pending and _pass < _MAX_PASSES:
                self.custom_logger.info(
                    "Pass %d complete — %d show(s) still pending. Cooling down before pass %d",
                    _pass,
                    len(pending),
                    _pass + 1,
                )
                human_scroll(sb)
                human_delay(60, 120)

        if pending:
            self.custom_logger.warning(
                "%d show(s) could not be scraped after %d passes: %s",
                len(pending),
                _MAX_PASSES,
                [p[0] for p in pending],
            )

    # ------------------------------------------------------------------ #
    # BaseExtractor interface                                              #
    # ------------------------------------------------------------------ #

    def extract(self) -> bytes:
        """Open SB session, scrape all shows across PAGES, return JSON bytes."""
        self.all_data = []
        seen_links: set[str] = set()

        # uc=True: undetected-Chrome mode, in case any bot protection shows
        # up on the listing/detail/booking pages.
        with SB(
            uc=True,
            test=True,
            headless=RUN_HEADLESS,
            browser="chrome",
            locale="en-US",
            chromium_arg="--enable-features=TranslateUI",
        ) as sb:
            self.custom_logger.info("Starting extraction from Thalian Hall")

            show_queue: list[tuple[str, str, str]] = []

            for url, category in PAGES:
                self.custom_logger.info("[Listing] %s: %s", category, url)
                if not safe_get_denver(sb, url):
                    continue

                human_delay(3, 5)
                sb.maximize_window()
                self.accept_cookies(sb)
                human_scroll(sb)

                cards = self._parse_show_cards(sb)
                self.custom_logger.info("Found %d show card(s) on %s", len(cards), url)

                for card in cards:
                    link = card["url"]
                    if link in seen_links:
                        continue
                    seen_links.add(link)
                    show_queue.append((link, category, card["title"]))

            if self.local_test:
                self.custom_logger.info(
                    "LOCAL TEST MODE: Limiting to %s shows", self.show_count
                )
                if self.show_count:
                    show_queue = show_queue[: self.show_count]

            self._scrape_shows(sb, show_queue, seen_links)

        return json.dumps(self.all_data, default=str).encode("utf-8")

    def _parse(self, _raw: bytes):
        """Build DataFrame from self.all_data collected during extract()."""
        df = pd.DataFrame(self.all_data)
        self.custom_logger.info("Parsing completed. Extracted %s shows", len(df))
        return df


def main():
    """Example usage of the Thalian Hall extractor."""
    extractor = ThalianHallExtractor(save_csv_locally=False, csv_incremental_mode=False)
    result = extractor.run()
    logger.info(f"Extraction result: {result}")
    if result.get("status") != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
