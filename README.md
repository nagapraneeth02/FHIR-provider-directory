# Provider Finder — Multi-Payer FHIR Provider Directory

A Python / Flask web application that searches **five major US health insurers'
public provider directories simultaneously** through their HL7 FHIR R4 APIs,
returning unified, filtered results in one place.

> Built to demonstrate cross-payer interoperability under the CMS Patient
> Access & Interoperability Rule (CMS-9115-F), which mandates publicly
> accessible provider-directory APIs from major US health insurers.

## Networks searched

| Payer | Auth |
| --- | --- |
| Cigna | Public |
| UnitedHealthcare (Optum) | Public |
| Capital BlueCross | Public |
| Humana | Public |
| Anthem / Elevance Health | OAuth 2.0 (client_credentials) |

## What it does

- One search bar → parallel queries to all 5 payer FHIR servers
- Search by **name**, **specialty** (NUCC taxonomy codes), **city**, **state**, or **ZIP**
- Cross-payer specialty filtering using the official NUCC provider-taxonomy
  codes (e.g. `207RC0000X` for Cardiology)
- State normalization — accepts both `Texas` and `TX`
- ZIP-code prefix matching (`78701` matches `78701-1234`)
- Compare up to 3 providers side-by-side
- Provider detail page with name, NPI, address, phone, qualifications,
  specialties, languages

## How it handles broken upstream APIs

Real-world payer FHIR servers behave inconsistently. The app routes around
each known limitation:

| Payer | Quirk handled |
| --- | --- |
| Cigna | Chained `practitioner.name` returns a fatal `OperationOutcome` → routed to `/Practitioner` endpoint + per-candidate role-lookup |
| Cigna | `_include` accepts only one parameter at a time → send the practitioner include only |
| UHC | `_count` capped at 100, `_include` deduplicates to ~10 unique practitioners per page |
| CapBlue | Ignores `_include` entirely → Location resources fetched directly per reference |
| CapBlue | Returns roles regardless of `specialty` param → local specialty-code verification |
| Humana | Times out on chained `location.*` / `practitioner.*` with specialty → strip + post-filter |

A server-side post-filter then enforces the user's name, location, specialty
universally so payer-side filter bugs don't leak wrong results to the UI.

## Tech stack

- **Backend**: Python 3.11, Flask
- **Frontend**: Jinja2 templates, Tailwind CSS (via CDN), Alpine.js
- **HTTP**: `requests` with ThreadPoolExecutor for parallel fan-out
- **No database** — the app is a stateless proxy / aggregator

## Local setup

```bash
git clone <this repo>
cd "Provider Directory App"

python -m venv venv
source venv/bin/activate          # Linux / macOS
# .\venv\Scripts\activate          # Windows PowerShell

pip install -r requirements.txt

# Anthem requires credentials. Create a .env file:
# ELEVANCE_CLIENT_ID=...
# ELEVANCE_CLIENT_SECRET=...
# ELEVANCE_TOKEN_URL=...

python app.py
# Open http://127.0.0.1:5050
```

The other four payers (Cigna, UHC, CapBlue, Humana) are fully public — no
credentials needed.

## Spec compliance

Every search parameter the app sends maps to a documented FHIR R4 search
parameter — see the docstrings in [`fhir_client.py`](fhir_client.py) for the
exact spec sections (HL7 FHIR R4 §3.1.1 Practitioner, §3.1.4 PractitionerRole,
§3.1.5 Organization, §3.1.1.5 search semantics).

## Project layout

```
app.py            Flask routes (/search, /provider, /compare, /admin/health, /api/*)
fhir_client.py    DirectoryClient + parallel_search + post-filter + role-lookup
payers.py         Payer registry + NUCC specialty taxonomy
templates/        Jinja2 templates (home, search, provider_detail, compare, admin_health)
.env              Anthem OAuth credentials (gitignored)
```
