"""Configuration for the Thalian Hall scraper."""

SITE_ID = "thalian_hall"
BASE_URL = "https://www.thalianhall.org/"
RUN_HEADLESS = True
DEFAULT_CURRENCY = "USD"

PAGES = [
    ("https://www.thalianhall.org/community-theatre", "Drama"),
    ("https://www.thalianhall.org/holidays", "musical"),
    ("https://www.thalianhall.org/Main_Attractions", "musical"),
    ("https://www.thalianhall.org/local-events", "musical"),
]

COOKIE_BTN_XPATH = (
    "//button[@id='CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll']"
)

# The seating-area dropdown includes a combined, zoomed-out overview area
# (no individually priced seats, just clickable zones for the real areas)
# alongside the actual bookable sub-areas -- confirmed live on the
# ChooseSeats page for "Live and Let Die": the dropdown's first/default
# option is "Seating" (the overview), followed by the real bookable areas
# (Parquet, Table 1-5, Dress Circle, Opera Suite A/B, Skybox).
# COMBINED_SEATING_AREA_LABEL = "Seating"
COMBINED_SEATING_AREA_LABEL = [
    "Seating",
    "Table 1",
    "Table 2",
    "Table 3",
    "Table 4",
    "Table 5",
    "Opera Suite A",
    "Opera Suite B",
    "Skybox",
]

# Fallback only -- the real venue name/address is scraped live from the
# Spektrix ChooseSeats page's own VenueName/VenueAddress spans (confirmed
# live: "Main Stage at Thalian Hall" / "310 Chestnut St, Wilmington, NC
# 28401"). This is used only if a show has no bookable performance at all,
# so we never get a chance to scrape the real thing.
DEFAULT_VENUE_DETAILS = {
    "venue": "Main Stage at Thalian Hall",
    "address": "310 Chestnut St, Wilmington, NC 28401",
    "city": "Wilmington",
    "country": "US",
}

SELECTORS = {
    "cookie_button": "//button[@id='CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll']",
    # -- Listing pages (Duda "photo gallery" widget, confirmed live on
    # /Main_Attractions -- static server-rendered HTML, no JS wait needed) --
    "show_card": ".photoGalleryThumbs",
    "show_title": ".caption-title",
    # Each card's .caption-text holds 1+ <p class="rteBlock"> lines: series
    # label first, then a date/time line (format varies a lot: "FRI · OCT 9
    # · 7:30PM", "MON · NOV 30 · 4:00 & 7:30 PM", or even just "DEC 4 & 5"
    # with no day-of-week/time at all) -- no year on this page at all, so
    # these lines are only used as a fallback; the real date/time/year comes
    # from the Spektrix instance dropdown on the detail page.
    "show_caption_lines": "p.rteBlock",
    "show_link": "a.caption-button",
    # -- Detail page (Duda dynamic-page template -- the wrapping
    # div#1247167444/dmNewParagraph is confirmed identical across every show
    # checked, but the h2/h3 lines INSIDE it are freeform per-show content,
    # not a fixed title+pretitle pair -- e.g. "The Goonies" has an extra
    # leading h2 ("THALIAN HALL CINEMA · FAMILY SERIES") before its real
    # title, and "PSL's Sweet 16 Anniversary Show" has a leading h2
    # (presenting company "PINEAPPLE-SHAPED LAMPS") + h3 ("presents") before
    # its real title. Confirmed live across every case checked: the real
    # title is always the LAST h2 among that leading run of headings, right
    # before the body-text <p> tags start -- :last-of-type (siblings under
    # the same div) picks that one out regardless of how many h2/h3 lines
    # precede it.
    "title": "div.dmNewParagraph h2:last-of-type",
    "pretitle": "div.dmNewParagraph h3",
    # The Spektrix booking widget always lives here on this site -- confirmed
    # live, including on the "Rhythm Streets Movement" page. Its presence vs.
    # absence is the ground-truth signal for whether a show has Spektrix
    # booking at all.
    "spektrix_iframe": "#SpektrixIFrame",
    # -- Spektrix eventDetails.aspx (instance picker) -- confirmed live for
    # "Live and Let Die": <select id="...InstanceList"><option value=
    # "332401">Fri Oct 09, 2026 - 7:30 PM</option></select> + a "Book now"
    # submit button. Selecting an instance and clicking Book now postbacks
    # the SAME page into the ChooseSeats view below.
    "calendar_date_card": "td[data-date]",
    "calendar_date": "data-date",
    "calendar_time": ".fc-event-time",
    "calendar_event": "a.fc-event",
    "instance_dropdown": "select[id*='InstanceList']",
    "book_now_button": "input[id*='BookNowButton']",
    "no_dates": "p.NoDates",
    "sold_out_text": "p.SoldOutText",
    # -- Spektrix ChooseSeats view -- confirmed live: classic seat-image
    # markup, tooltip format "A117 - $74.90" (US venue -- dollars, not
    # pounds).
    "seating_dropdown": "select[id*='AvailableAreas']",
    "all_seats": "img.Seat.NotDimmed, img.SeatSelectable.NotDimmed",
    "available_seats": "img.SeatSelectable.NotDimmed",
    "venue_name": ".VenueName",
    "venue_address": ".VenueAddress",
}
