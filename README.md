# senate-efd-mirror

Self-owned mirror of U.S. Senate financial-disclosure **Periodic Transaction
Reports** (STOCK Act insider trades), for the Watchtower project.

## Why this exists

The official Senate eFD portal (`efdsearch.senate.gov`) blocks automated
access from many datacenter IPs at the Akamai edge (the whole `senate.gov`
domain returns 403 to Watchtower's host). GitHub Actions runners are **not**
in that block, so a scheduled Action here scrapes the official portal and
commits clean JSON that Watchtower reads via `raw.githubusercontent.com`
(which Watchtower's host *can* reach).

This is the official data, routed through an un-blocked IP, and self-owned
so it can't be abandoned by a third party.

## Status

Bootstrapping. `probe.yml` first verifies the runner can reach the eFD;
the full daily scraper follows once reachability is confirmed.
