"""Senate eFD PTR (Periodic Transaction Report) mirror scraper.

Why this exists: efdsearch.senate.gov 403s at the Akamai edge for
Samuel's home box (the whole senate.gov domain, not just this endpoint),
but a GitHub Actions runner's egress IPs are NOT in that block (proven
live by probe.py in this same repo). So this script runs on a schedule
INSIDE GitHub Actions, scrapes the official Senate portal directly, and
commits a clean JSON mirror (data/senate_ptrs.json) that the Watchtower
box reads over plain HTTPS via raw.githubusercontent.com -- no scraping,
no WAF, from that side.

Handshake (verified working by probe.py, reused here verbatim):
  1. GET  /search/home/               -> obtains a csrftoken cookie
  2. POST /search/home/ (accept the "prohibition agreement" click-through)
  3. POST /search/report/data/ with report_types=[11] (11 = Periodic
     Transaction Report) and a submitted_start_date lookback window ->
     JSON {"draw","recordsTotal","recordsFiltered","data":[...],"result"}

Each `data` row is a 5-element list:
  [first_name, last_name, "Last, First (Senator)",
   '<a href="/search/view/ptr/{uuid}/" target="_blank">Periodic '
   'Transaction Report for MM/DD/YYYY</a>',
   "MM/DD/YYYY"]                                    # filed date
The href path is either /search/view/ptr/{uuid}/ (electronic, has a real
transaction table we can parse) or /search/view/paper/{uuid}/ (a scanned
image -- no machine-readable table exists, so these are SKIPPED and
COUNTED, never silently dropped -- see Samuel's standing dislike of
silent truncation, same policy as bots/sources/congress.py's House side
of this same feed on the Watchtower box).

Each electronic PTR detail page has exactly one
`<table class="table table-striped">` whose body rows are 9 <td>s each:
  #, Transaction Date, Owner, Ticker, Asset Name, Asset Type, Type,
  Amount, Comment

Public contract emitted to data/senate_ptrs.json (this is what
bots/sources/congress.py's fetch_senate() on the Watchtower side reads --
see that file's docstring/fetch_senate() for the consumer side):
  {
    "generated_utc": "<ISO8601 Z>",
    "source": "efdsearch.senate.gov",
    "lookback_days": 60,
    "skipped_image_only": <int>,
    "count": <len(filings)>,
    "filings": [
      {
        "senator": str,               # "First Last"
        "ticker": str,                # uppercased, "" if none ("--")
        "type": str,                  # House-convention mapped type
        "amount": str,                # verbatim Amount cell
        "transaction_date": str,      # ISO YYYY-MM-DD
        "report_date": str,           # ISO YYYY-MM-DD (filed date)
        "asset_description": str,     # verbatim Asset Name cell
        "ptr_link": str,              # absolute detail-page URL
        "type_raw": str,              # original portal Type string
      },
      ...
    ]
  }

Honesty rule throughout: never fabricate a ticker/type/date. An unmapped
Type string passes through verbatim rather than being guessed at. A
missing/unparseable date passes through verbatim rather than being
dropped or zeroed. skipped_image_only is COUNTED, never hidden.

Fail-soft per item: a single detail-page fetch/parse error is caught,
logged to stderr, and skipped -- it must never crash the whole run. The
one exception: if the initial handshake or the FIRST report/data POST
fails (non-200 or non-JSON), main() exits non-zero so the GitHub Action
run shows red (a visible signal that Akamai re-blocked the runner, or the
portal changed shape) rather than silently committing an empty file.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from html import unescape
from typing import Any, Dict, List, Optional, Tuple

import requests

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

HOME_URL = "https://efdsearch.senate.gov/search/home/"
DATA_URL = "https://efdsearch.senate.gov/search/report/data/"
BASE_URL = "https://efdsearch.senate.gov"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30
PAGE_LENGTH = 100
LOOKBACK_DAYS = 60

OUTPUT_PATH = os.path.join("data", "senate_ptrs.json")

# House-convention type mapping (see bots/sources/congress.py on the
# Watchtower side -- House PTR text already uses "P"/"S"/"S (partial)"/"E").
# Anything not in this table passes through verbatim (honesty rule).
TYPE_MAP = {
    "Purchase": "P",
    "Sale (Full)": "S",
    "Sale (Partial)": "S (partial)",
    "Exchange": "E",
}

_HREF_RE = re.compile(r'href="(/search/view/(ptr|paper)/([0-9a-fA-F-]+)/)"')
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------

def _parse_mdy(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _to_iso_date(value: Optional[str]) -> str:
    """MM/DD/YYYY -> ISO YYYY-MM-DD. Honesty rule: an unparseable date is
    passed through verbatim rather than dropped or fabricated."""
    parsed = _parse_mdy(value)
    return parsed.isoformat() if parsed else (value or "")


def _map_type(type_raw: str) -> str:
    return TYPE_MAP.get(type_raw, type_raw)


def _clean_cell(raw_html: str) -> str:
    """Strip inner HTML tags, replace &nbsp; with a space, unescape any
    other entities, collapse whitespace, strip. Shared by every <td> in
    the 9-column transaction table."""
    text = raw_html.replace("&nbsp;", " ")
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# --------------------------------------------------------------------------
# Pure parsing functions (no I/O -- directly testable against fixtures)
# --------------------------------------------------------------------------

def parse_search_rows(search_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One entry per row of a /search/report/data/ JSON response's "data"
    array. Each entry:
      {"uuid","kind"("ptr"/"paper"),"path","senator","report_date_iso"}
    A row with no recognizable /search/view/(ptr|paper)/{uuid}/ href (e.g.
    unexpected shape) is skipped rather than raising."""
    rows: List[Dict[str, Any]] = []
    for row in search_json.get("data", []) or []:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        first, last, _title, link_html, filed = row[0], row[1], row[2], row[3], row[4]
        m = _HREF_RE.search(str(link_html))
        if not m:
            continue
        path, kind, uuid = m.group(1), m.group(2), m.group(3)
        senator = f"{(first or '').strip()} {(last or '').strip()}".strip()
        rows.append(
            {
                "uuid": uuid,
                "kind": kind,  # "ptr" (electronic) or "paper" (scanned image)
                "path": path,
                "senator": senator,
                "report_date_iso": _to_iso_date(filed),
            }
        )
    return rows


def parse_detail_html(html: str) -> List[Dict[str, Any]]:
    """Every transaction row in one electronic PTR detail page's single
    `<table class="table table-striped">`. Returns "raw building block"
    dicts with keys:
      txdate_iso, owner, ticker, asset_description, asset_type,
      type_mapped, type_raw, amount
    A malformed/short <tr> (not exactly 9 <td>s -- e.g. the header row,
    which uses <th>) is simply not a match and is skipped."""
    out: List[Dict[str, Any]] = []
    for tr_match in _TR_RE.finditer(html):
        tds = _TD_RE.findall(tr_match.group(1))
        if len(tds) != 9:
            continue
        cells = [_clean_cell(td) for td in tds]
        # cells: [#, Transaction Date, Owner, Ticker, Asset Name,
        #         Asset Type, Type, Amount, Comment]
        txdate_iso = _to_iso_date(cells[1])
        owner = cells[2]
        ticker_raw = cells[3]
        ticker = "" if ticker_raw in ("", "--") else ticker_raw.upper().strip()
        asset_description = cells[4]
        asset_type = cells[5]
        type_raw = cells[6]
        type_mapped = _map_type(type_raw)
        amount = cells[7]
        out.append(
            {
                "txdate_iso": txdate_iso,
                "owner": owner,
                "ticker": ticker,
                "asset_description": asset_description,
                "asset_type": asset_type,
                "type_mapped": type_mapped,
                "type_raw": type_raw,
                "amount": amount,
            }
        )
    return out


def build_filings(
    search_rows: List[Dict[str, Any]],
    detail_html_by_path: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Assemble the final public-contract dicts (see module docstring) by
    joining each electronic search row to its already-fetched detail HTML.
    A search row with no entry in detail_html_by_path (e.g. the fetch
    failed and was skipped upstream) is silently not represented here --
    the caller is responsible for fail-soft logging at the fetch site."""
    filings: List[Dict[str, Any]] = []
    for row in search_rows:
        if row.get("kind") != "ptr":
            continue  # paper/image-only -- handled (counted) by the caller
        path = row["path"]
        html = detail_html_by_path.get(path)
        if html is None:
            continue
        try:
            transactions = parse_detail_html(html)
        except Exception as exc:  # noqa: BLE001 -- fail-soft per item
            print(f"[scrape] WARN: failed to parse detail page {path}: {exc}", file=sys.stderr)
            continue
        ptr_link = BASE_URL + path
        for t in transactions:
            filings.append(
                {
                    "senator": row["senator"],
                    "ticker": t["ticker"],
                    "type": t["type_mapped"],
                    "amount": t["amount"],
                    "transaction_date": t["txdate_iso"],
                    "report_date": row["report_date_iso"],
                    "asset_description": t["asset_description"],
                    "ptr_link": ptr_link,
                    "type_raw": t["type_raw"],
                }
            )
    return filings


# --------------------------------------------------------------------------
# Network (the only part that touches the real portal)
# --------------------------------------------------------------------------

def _handshake(session: requests.Session) -> str:
    """GET the home page (obtains a csrftoken cookie) then POST the
    "prohibition agreement" click-through. Returns the csrf token to use
    on the report/data POST. Raises requests.RequestException /
    RuntimeError on failure -- caller decides how loud to be."""
    r = session.get(HOME_URL, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    token = session.cookies.get("csrftoken")
    if not token:
        raise RuntimeError("no csrftoken cookie after GET /search/home/")

    r2 = session.post(
        HOME_URL,
        data={"csrfmiddlewaretoken": token, "prohibition_agreement": "1"},
        headers={"Referer": HOME_URL},
        timeout=REQUEST_TIMEOUT,
    )
    r2.raise_for_status()

    token = session.cookies.get("csrftoken") or token
    return token


def _fetch_search_page(
    session: requests.Session, csrf_token: str, start: int, submitted_start_date: str
) -> Dict[str, Any]:
    payload = {
        "draw": str((start // PAGE_LENGTH) + 1),
        "start": str(start),
        "length": str(PAGE_LENGTH),
        "search[value]": "",
        "report_types": "[11]",  # 11 = Periodic Transaction Report
        "filer_types": "[]",
        "submitted_start_date": submitted_start_date,
        "submitted_end_date": "",
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": "",
    }
    r = session.post(
        DATA_URL,
        data=payload,
        headers={
            "Referer": HOME_URL,
            "X-CSRFToken": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _fetch_detail_html(session: requests.Session, path: str) -> Optional[str]:
    try:
        r = session.get(BASE_URL + path, headers={"Referer": HOME_URL}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text
    except requests.RequestException as exc:
        print(f"[scrape] WARN: failed to GET detail page {path}: {exc}", file=sys.stderr)
        return None


def _collect_search_rows(
    session: requests.Session, csrf_token: str, cutoff: date
) -> Tuple[List[Dict[str, Any]], int]:
    """Paginate /search/report/data/ (length=100) until either all
    recordsTotal rows are collected, or a page's rows have all aged past
    the lookback cutoff. Returns (search_rows, skipped_image_only_count)."""
    submitted_start_date = cutoff.strftime("%m/%d/%Y 00:00:00")

    all_rows: List[Dict[str, Any]] = []
    start = 0
    records_total: Optional[int] = None

    while True:
        page = _fetch_search_page(session, csrf_token, start, submitted_start_date)
        if records_total is None:
            records_total = page.get("recordsTotal", 0)

        page_rows = parse_search_rows(page)
        if not page_rows:
            break

        stop = False
        for row in page_rows:
            # report_date_iso is already ISO by this point (parse_search_rows
            # converts it); parse it back to compare against cutoff.
            try:
                rdate = date.fromisoformat(row["report_date_iso"]) if row["report_date_iso"] else None
            except ValueError:
                rdate = None
            if rdate is not None and rdate < cutoff:
                stop = True
                break
            all_rows.append(row)

        start += PAGE_LENGTH
        if stop:
            break
        if records_total is not None and start >= records_total:
            break
        if len(page_rows) < PAGE_LENGTH:
            break

    skipped_image_only = sum(1 for r in all_rows if r.get("kind") == "paper")
    return all_rows, skipped_image_only


# --------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------

def main() -> int:
    today = datetime.utcnow().date()
    cutoff = today - timedelta(days=LOOKBACK_DAYS)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    # Handshake + first report/data call: loud failure (non-zero exit) so
    # a re-block by Akamai (or a portal shape change) is visible in the
    # Action's run status, per spec.
    try:
        csrf_token = _handshake(session)
        search_rows, skipped_image_only = _collect_search_rows(session, csrf_token, cutoff)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"[scrape] FATAL: handshake/initial report/data call failed: {exc}", file=sys.stderr)
        return 1

    # Fetch each electronic PTR's detail page. Fail-soft per item: a
    # single bad fetch/parse must not sink the whole run.
    detail_html_by_path: Dict[str, str] = {}
    for row in search_rows:
        if row.get("kind") != "ptr":
            continue
        path = row["path"]
        html = _fetch_detail_html(session, path)
        if html is None:
            continue
        detail_html_by_path[path] = html

    filings = build_filings(search_rows, detail_html_by_path)

    filings.sort(
        key=lambda f: (
            f.get("report_date", ""),
            f.get("senator", ""),
            f.get("transaction_date", ""),
            f.get("ticker", ""),
        )
    )

    doc = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "efdsearch.senate.gov",
        "lookback_days": LOOKBACK_DAYS,
        "skipped_image_only": skipped_image_only,
        "count": len(filings),
        "filings": filings,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, indent=2, sort_keys=True))
        f.write("\n")

    print(
        f"[scrape] wrote {len(filings)} filings ({skipped_image_only} image-only skipped) "
        f"to {OUTPUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
