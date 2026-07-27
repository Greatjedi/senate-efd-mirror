"""Reachability probe: can a GitHub Actions runner reach the Senate eFD
search portal (efdsearch.senate.gov) and pull the report/data JSON?

This does NOT parse filings -- it only proves the handshake + search API
work from GitHub's egress IPs (which are NOT in senate.gov's Akamai block
that rejects Samuel's box). If this prints recordsTotal > 0 for recent
PTRs, the self-owned-mirror architecture is viable.
"""
import sys
import requests

HOME = "https://efdsearch.senate.gov/search/home/"
DATA = "https://efdsearch.senate.gov/search/report/data/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def main() -> int:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json",
        "Accept-Language": "en-US,en;q=0.9",
    })

    r = s.get(HOME, timeout=30)
    print(f"[home] status={r.status_code} csrftoken_cookie={bool(s.cookies.get('csrftoken'))}")
    if r.status_code != 200:
        print(f"[home] BLOCKED body[:200]={r.text[:200]!r}")
        return 1

    tok = s.cookies.get("csrftoken")
    r2 = s.post(HOME, data={"csrfmiddlewaretoken": tok, "prohibition_agreement": "1"},
                headers={"Referer": HOME}, timeout=30)
    print(f"[accept] status={r2.status_code}")

    tok = s.cookies.get("csrftoken")
    payload = {
        "draw": "1", "start": "0", "length": "25", "search[value]": "",
        "report_types": "[11]",            # 11 = Periodic Transaction Report
        "filer_types": "[]",
        "submitted_start_date": "01/01/2026 00:00:00",
        "submitted_end_date": "", "candidate_state": "", "senator_state": "",
        "office_id": "", "first_name": "", "last_name": "",
    }
    r3 = s.post(DATA, data=payload,
                headers={"Referer": HOME, "X-CSRFToken": tok,
                         "X-Requested-With": "XMLHttpRequest"}, timeout=30)
    print(f"[report/data] status={r3.status_code} ctype={r3.headers.get('content-type')}")
    if r3.status_code != 200:
        print(f"[report/data] BLOCKED body[:300]={r3.text[:300]!r}")
        return 1

    try:
        j = r3.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[report/data] non-JSON: {exc} body[:300]={r3.text[:300]!r}")
        return 1

    print(f"[report/data] recordsTotal={j.get('recordsTotal')} rows_in_page={len(j.get('data', []))}")
    for row in j.get("data", [])[:5]:
        print("  row:", row)

    # Capture ONE electronic PTR detail page so we can build the parser
    # against real HTML. Find the first /ptr/ link (skip /paper/ scans).
    import re
    detail_path = None
    for row in j.get("data", []):
        for cell in row:
            m = re.search(r'href="(/search/view/ptr/[0-9a-f-]+/)"', str(cell))
            if m:
                detail_path = m.group(1)
                break
        if detail_path:
            break

    # Save the search JSON so we can see every column the row API returns.
    with open("artifact_search.json", "w") as f:
        f.write(r3.text)

    # Collect up to 10 electronic /ptr/ detail links, then save each page so
    # we can pick fixtures that contain REAL tickers (not just structured
    # notes / "--"). Paper (/paper/) scans are image-only and skipped.
    ptr_paths = []
    for row in j.get("data", []):
        for cell in row:
            m = re.search(r'href="(/search/view/ptr/[0-9a-f-]+/)"', str(cell))
            if m and m.group(1) not in ptr_paths:
                ptr_paths.append(m.group(1))
    print(f"[detail] found {len(ptr_paths)} electronic PTR links; saving up to 10")
    for idx, path in enumerate(ptr_paths[:10]):
        url = "https://efdsearch.senate.gov" + path
        try:
            rd = s.get(url, headers={"Referer": HOME}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"[detail] {idx} ERROR {exc}")
            continue
        fn = f"artifact_detail_{idx:02d}.html"
        with open(fn, "w") as f:
            f.write(rd.text)
        has_ticker = bool(re.search(r"<td>\s*[A-Z]{1,5}(\.[A-Z])?\s*</td>", rd.text))
        print(f"[detail] {idx} status={rd.status_code} len={len(rd.text)} "
              f"maybe_real_ticker={has_ticker} {path}")

    print("PROBE_RESULT:", "REACHABLE" if j.get("recordsTotal") else "EMPTY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
