# Provider Finder — Project Documentation

## 1. Executive Summary

**Provider Finder** is a Python / Flask web application that searches the
public provider directories of **five major US health insurers
simultaneously** and presents the results in a single unified interface.
It was built to demonstrate practical interoperability across heterogeneous
HL7 FHIR R4 healthcare APIs in compliance with the CMS Patient Access and
Interoperability Rule (CMS-9115-F).

One search box → parallel queries to Cigna, UnitedHealthcare, Capital
BlueCross, Humana, and Anthem → unified, filtered, verified results.

---

## 2. The Problem

When a patient picks an insurance plan, they want to know which doctors are
in-network. Today they have to check each insurer's website individually —
five sites, five different search experiences, no easy way to compare.

The underlying data is required by law to be **publicly accessible** via
HL7 FHIR APIs (per CMS-9115-F, the Patient Access & Interoperability Rule),
but each insurer's implementation is different. Different URLs, different
auth, different search parameters, different bugs. The data is technically
"open" but practically scattered.

This project shows that **a single application can bridge those
differences** and give a consumer-friendly, cross-payer view of who's
covered where.

---

## 3. The Intention

The goals of this project are:

1. **Demonstrate real FHIR interoperability** — connect to multiple
   production FHIR R4 servers and reconcile their inconsistent
   implementations into one user experience.

2. **Surface real, live provider data** — every result is fetched live from
   the insurer's official, government-mandated public FHIR endpoint. No
   scraping, no caching, no fabrication.

3. **Be honest about API quality differences** — payers' FHIR servers vary
   widely in compliance and capability. The app routes around each one's
   quirks rather than pretending they all behave the same.

4. **Show practical engineering judgment** — when an upstream API is
   fundamentally limited or unreliable, fail gracefully and tell the user
   what happened, rather than silently dropping data or producing wrong
   results.

---

## 4. What the Application Does

### Networks searched

| Insurer | Auth | Coverage |
| --- | --- | --- |
| Cigna | Public | National |
| UnitedHealthcare / Optum | Public | National |
| Humana | Public | National |
| Capital BlueCross | Public | Pennsylvania (regional) |
| Anthem / Elevance Health | OAuth 2.0 client_credentials | Multi-state |

### Search filters supported

- **Provider name** (first, last, or substring)
- **Medical specialty** — selected from 12 common NUCC taxonomy codes
  (Cardiology, Family Medicine, Pediatrics, Dermatology, Psychiatry, etc.)
- **City**
- **State** — accepts either `Texas` or `TX`; normalizes 50 states
- **ZIP code** — prefix matches so `78701` also matches `78701-1234`

### What each result card shows

- Provider name
- Specialty / license
- Practice location (city, state)
- Phone number
- NPI (National Provider Identifier)
- Source insurer (color-coded)

### Additional features

- "View details" page per provider with full record (qualifications,
  languages, addresses, telecom)
- Side-by-side **comparison** of up to 3 providers
- Per-network sidebar showing how many results each insurer contributed
- Smart empty-state with "Drop filter X" chips when combined searches
  return zero matches
- Live network-health check at `/admin/health`

---

## 5. How It Works — Technical Architecture

### High-level flow

```
Browser  ──same origin──▶  Flask app  ──parallel, 5 threads──▶  5 FHIR APIs
                              │
                              ▼
                       Aggregator → Resolver → Post-filter → UI
```

The Flask backend acts as a **proxy and aggregator**:

1. **Fan-out** — when a user searches, the backend fires parallel HTTP
   requests to all 5 payer FHIR servers via `ThreadPoolExecutor`.
2. **Resolution** — once responses arrive, related FHIR resources
   (`PractitionerRole` → `Practitioner` → `Location`) are stitched together.
   Some payers bundle these via `_include`; some don't, requiring direct
   follow-up fetches.
3. **Post-filtering** — the app re-applies every user-requested filter
   against the aggregated results so payer-side filter bugs can't leak
   wrong data into the UI.
4. **Display** — results render through Jinja2 templates with light
   Alpine.js for compare / load-more / favorites.

### Code layout

| File | Responsibility |
| --- | --- |
| `app.py` | Flask routes (`/search`, `/provider/...`, `/compare`, `/api/load-more`, `/admin/health`) |
| `fhir_client.py` | FHIR client: per-payer routing, parallel-merge combined search, location backfill, universal post-filter |
| `payers.py` | Static payer registry (FHIR base URLs, auth, branding) + NUCC specialty taxonomy |
| `templates/*.html` | Jinja2 templates for home, search, provider detail, compare, network health |

### No database — by design

The app is **stateless**. It owns no provider data — the data lives in the
insurers' FHIR servers and is fetched on every request. User-side concepts
(favorites, compare list, search history) live in the browser's
`localStorage`, which means no user accounts, no PII to store, and no
privacy compliance burden.

This was a deliberate architectural choice: a stateless aggregator is the
right shape for a real-time interoperability tool.

---

## 6. The Real-World Engineering Challenge

The hard part of this project isn't the FHIR spec — it's that real payer
servers implement FHIR inconsistently. Discovering and working around each
payer's quirks took systematic probing. The app handles every quirk found:

| Quirk | Worked around by |
| --- | --- |
| Cigna requires `specialty=system\|code` form (bare codes return 0) | Token format applied per-payer |
| Cigna's chained `practitioner.name` returns a fatal `OperationOutcome` | Routed to `/Practitioner` endpoint + per-candidate role lookup |
| Cigna's `_include` accepts only a single parameter | Only the practitioner include is sent to Cigna |
| Cigna's chained `location.address-*` always returns 0 | Skipped; Location resources fetched directly |
| UHC's `_count` is capped at 100, regardless of request | Accepted as a hard limit |
| UHC's `_include` deduplicates aggressively (~10 unique practitioners per page) | Two-strategy parallel merge to compensate |
| CapBlue ignores `_include` entirely → no Location resources returned | Per-PR Location resources fetched directly (`_backfill_locations`) |
| CapBlue returns roles regardless of the `specialty` parameter | Roles re-filtered locally by NUCC code |
| Humana times out (>40s) on chained `location.*` combined with specialty | Chained params stripped; post-filter handles location |
| Centene's gateway is chronically slow (15-35s) and returns HTTP 504 frequently | **Removed from the registry** — judgment call |
| Aetna's public endpoint requires registration credentials | **Removed from the registry** |

### Cross-cutting solution: the universal post-filter

Because payer-side filtering can't be trusted, **every result that reaches
the user has been verified locally** against the requested filters:

- **Name** — verified against the resolved `Practitioner` name
- **Specialty** — verified against `PractitionerRole.specialty[].coding[]`
  or `Practitioner.qualification[].code` NUCC codes
- **State / City / ZIP** — verified against the linked `Location.address`,
  fetching missing Location resources directly when a payer omits the
  include

If the app cannot verify a result, it drops the result rather than risk
showing wrong data. This conservative approach trades volume for trust.

### The hardest case: combined name + specialty

A search like *"Eric + Cardiology"* is hard because Cigna, UHC, CapBlue,
and Humana **cannot answer it in a single query** — their FHIR endpoints
break, ignore parameters, or time out. The app runs **two strategies in
parallel and merges the results:**

1. **Specialty-first** —
   `PractitionerRole?specialty=Cardiology&_include=practitioner`.
   Strong when the specialty is dense and includes name matches in the
   first page.

2. **Name-first (role lookup)** —
   `Practitioner?name=Eric` → for each candidate, look up
   `PractitionerRole?practitioner=<id>&specialty=Cardiology` to verify.
   Strong when the name is rare in the network.

Both run concurrently, results are deduplicated by resource ID, and the
union is returned. This recovered combined-query results from Cigna,
CapBlue, and Humana that previously returned **zero** entries.

---

## 7. Limitations and Design Trade-offs

### What works perfectly

- **Single-filter searches** (name only, specialty only) — all 5 networks
  contribute reliably with full pages of results.
- **Combined searches on Anthem** — Anthem's API handles them natively in
  one query, returning 20 verified results consistently.

### What's intentionally limited

- **UHC for combined name + specialty searches** — UHC's API orders
  Practitioners with a sort that puts dentists / NPs at the top for common
  names. Specific name+specialty combinations may require paginating 20+
  pages deep to surface a match. The latency cost (30+ seconds per search)
  was judged not worth it for a demo-friendly response time.
- **CapBlue is geographically limited** — Capital BlueCross is a
  Pennsylvania-only regional plan. Non-PA searches will always return 0
  from CapBlue. This is correct behavior, not a defect.

### What was explicitly cut

- **Centene** — removed despite being a major insurer because its public
  FHIR gateway returns HTTP 504 errors on most queries. An unreliable
  source is worse than fewer sources.
- **Aetna** — wired up in code but disabled because credentials
  registration was not completed.

---

## 8. Future Improvements

- **Response caching** — short-lived in-memory cache (Python dict or Redis)
  keyed by query parameters. Would dramatically improve perceived
  reliability and speed for repeat searches.
- **Deep pagination for UHC** — accept the latency cost as a "thorough
  mode" toggle to surface UHC combined-search matches.
- **User accounts** — sync favorites and compare lists across devices
  (would require a database).
- **Production deployment** — replace Flask's dev server with `gunicorn`,
  move secrets to host-managed environment, deploy to Render or Railway
  with automatic deploys from GitHub.
- **OpenAPI / Swagger docs** for the internal JSON endpoints.

---

## 9. Tech Stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.11, Flask |
| HTTP client | `requests` + `concurrent.futures.ThreadPoolExecutor` |
| Templates | Jinja2 |
| Styling | Tailwind CSS (via CDN) |
| Frontend interactivity | Alpine.js (no build step) |
| FHIR profile | HL7 FHIR R4 + DaVinci PDEX Plan Net IG |
| Authentication (Anthem only) | OAuth 2.0 client_credentials |
| Persistence | None (stateless aggregator) |

---

## 10. Why This Project Matters

This project is a demonstration that **healthcare interoperability is
possible today** with the public APIs payers are already required to
expose — but it also shows how much engineering effort it takes to bridge
the gaps between specifications and real-world implementations. The
universal post-filter, the per-payer routing, the parallel-merge search
strategy — every line of that logic exists because real production FHIR
servers don't quite behave the way the spec says they should.

The result is a tool that's useful to anyone who needs to verify cross-
payer provider coverage in real time, and a portfolio piece that
demonstrates working knowledge of FHIR R4, OAuth 2.0, parallel HTTP, and
the kind of practical decision-making (when to retry, when to drop a
payer, when to trust a server, when to verify locally) that real-world
systems engineering requires.
