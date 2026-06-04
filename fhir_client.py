"""
FHIR Client — Provider Directory v2
=====================================
All FHIR search parameters used here are defined in HL7 FHIR R4:
  https://hl7.org/fhir/R4/

Resource-specific spec references:
  - Practitioner search params:      https://hl7.org/fhir/R4/practitioner.html#search
  - PractitionerRole search params:  https://hl7.org/fhir/R4/practitionerrole.html#search
  - Location search params:          https://hl7.org/fhir/R4/location.html#search
  - Organization search params:      https://hl7.org/fhir/R4/organization.html#search
  - Search semantics (chaining, _include, token format, modifiers):
      https://hl7.org/fhir/R4/search.html

Token param format (FHIR R4 §3.1.1.5.4):
  - "code"           — match the code regardless of system
  - "system|code"    — match both system AND code (most precise)
  - "|code"          — match code with no system property

Chained params (§3.1.1.5.1):
  reference params may be followed by `.` and a search param on the target
  resource. e.g. PractitionerRole?location.address-state=TX joins through
  PractitionerRole.location → Location.address.state.

Supports two auth styles seamlessly:
  1. AUTH_NONE        — public unauthenticated GET requests (Cigna, UHC, CapBlue,
                        Humana)
  2. AUTH_CLIENT_CRED — OAuth 2.0 client_credentials grant (Anthem / Elevance)
"""

import time
import base64
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Optional

from payers import AUTH_NONE, AUTH_CLIENT_CRED, is_payer_configured


REQUEST_TIMEOUT = 45  # seconds — generous headroom for slower payer gateways.


# ----------------------------------------------------------------------
# Token cache (in-memory) — one Bearer token per payer, keyed by payer_id
# ----------------------------------------------------------------------
_token_cache = {}
_token_lock  = Lock()


def _get_client_credentials_token(payer: dict) -> Optional[str]:
    """
    Get a Bearer token via OAuth 2.0 client_credentials grant.
    Caches the token until ~60 seconds before expiry to avoid races.
    Returns None on failure (logged to stderr).
    """
    payer_id = payer["id"]
    now = time.time()

    with _token_lock:
        cached = _token_cache.get(payer_id)
        if cached and cached["expires_at"] > now + 60:
            return cached["access_token"]

        # Build basic auth header from client_id:client_secret
        creds = f"{payer['client_id']}:{payer['client_secret']}"
        b64   = base64.b64encode(creds.encode()).decode()

        try:
            r = requests.post(
                payer["token_url"],
                headers={
                    "Content-Type":  "application/x-www-form-urlencoded",
                    "Authorization": f"Basic {b64}",
                },
                data={"grant_type": "client_credentials"},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code != 200:
                print(f"[{payer_id}] token endpoint returned {r.status_code}: {r.text[:200]}")
                return None
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[{payer_id}] token error: {e}")
            return None

        token = data.get("access_token")
        if not token:
            return None

        # Default to 1 hour if expires_in not provided
        expires_in = int(data.get("expires_in", 3600))
        _token_cache[payer_id] = {
            "access_token": token,
            "expires_at":   now + expires_in,
        }
        return token


# ----------------------------------------------------------------------
# Directory client
# ----------------------------------------------------------------------
class DirectoryClient:
    """One client per payer. Handles auth automatically per the payer config."""

    def __init__(self, payer: dict):
        self.payer = payer
        self.base = payer["fhir_base"].rstrip("/")
        self.headers = {
            "Accept":     "application/fhir+json",
            "User-Agent": "ProviderFinder/2.0",
        }

    # --- Auth helper ----------------------------------------------------
    def _auth_headers(self):
        # Present a browser-like User-Agent and explicitly request FHIR JSON —
        # some payer gateways behind enterprise firewalls reject default
        # User-Agents or return 406 Not Acceptable without this.
        headers = {
            "Accept": "application/fhir+json, application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        # If the payer requires a token (Anthem / Elevance), attach it here
        if self.payer.get("auth") == AUTH_CLIENT_CRED:
            token = _get_client_credentials_token(self.payer)
            if token:
                headers["Authorization"] = f"Bearer {token}"
                
        return headers

    def _tag_results(self, bundle: dict) -> dict:
        bundle.setdefault("entry", [])
        for entry in bundle["entry"]:
            entry["_payer_id"]    = self.payer["id"]
            entry["_payer_name"]  = self.payer["display_name"]
            entry["_payer_short"] = self.payer["short_name"]
            entry["_payer_color"] = self.payer["color"]
        return bundle

    def _request(self, path: str, params: dict) -> dict:
        # Pre-flight: is this payer reachable at all?
        if not is_payer_configured(self.payer):
            return {"entry": [], "total": 0,
                    "_error": "Not configured (missing credentials)",
                    "_payer_id": self.payer["id"]}

        url = f"{self.base}/{path}"

        def _do_request():
            return requests.get(url, params=params,
                                headers=self._auth_headers(),
                                timeout=REQUEST_TIMEOUT)

        try:
            r = _do_request()
            # Retry transient gateway errors (502/503/504) once — these are
            # usually momentary upstream hiccups that clear on a second try.
            if r.status_code in (502, 503, 504):
                r = _do_request()
            if r.status_code == 200:
                return self._tag_results(r.json())
            if r.status_code == 401:
                # Token may have just expired; force refresh and retry once
                _token_cache.pop(self.payer["id"], None)
                r = _do_request()
                if r.status_code == 200:
                    return self._tag_results(r.json())
            return {"entry": [], "total": 0,
                    "_error": f"HTTP {r.status_code}",
                    "_payer_id": self.payer["id"]}
        except requests.Timeout:
            return {"entry": [], "total": 0, "_error": "Timeout",
                    "_payer_id": self.payer["id"]}
        except requests.RequestException as e:
            return {"entry": [], "total": 0,
                    "_error": f"{type(e).__name__}",
                    "_payer_id": self.payer["id"]}
        except ValueError:
            return {"entry": [], "total": 0,
                    "_error": "Invalid JSON response",
                    "_payer_id": self.payer["id"]}

    # --- Search methods -------------------------------------------------
    def search_practitioners(self, name=None, family=None, given=None,
                             state=None, city=None, postalcode=None,
                             gender=None, identifier=None, active=None,
                             count=20) -> dict:
        """Search the FHIR Practitioner endpoint.

        Per FHIR R4 §3.1.1 (https://hl7.org/fhir/R4/practitioner.html#search):
            name               string  → Practitioner.name (matches family/given/prefix/suffix/text)
            family             string  → Practitioner.name.family
            given              string  → Practitioner.name.given
            address-state      string  → Practitioner.address.state
            address-city       string  → Practitioner.address.city
            address-postalcode string  → Practitioner.address.postalCode
            gender             token   → Practitioner.gender
            identifier         token   → Practitioner.identifier (NPI etc.)
            active             token   → Practitioner.active
            _count             control → max results per page
        """
        params = {"_count": count}
        if name:   params["name"]   = name
        if family: params["family"] = family
        if given:  params["given"]  = given
        # Cigna's Practitioner endpoint rejects address-* params with HTTP 400.
        # Skip them for Cigna — the server-side post-filter applies location
        # against the inline addresses Cigna's Practitioner records carry.
        if self.payer["id"] != "cigna":
            # Always send the 2-letter abbreviation upstream — every FHIR server
            # we tested expects that form, full state names return 0 entries.
            if state:      params["address-state"]      = _normalize_state(state)
            if city:       params["address-city"]       = city
            if postalcode: params["address-postalcode"] = postalcode
        # Token params (FHIR R4 §3.1.1.5.4). Sent as bare codes; the spec also
        # allows "system|code" but bare code is broadly accepted.
        if gender:     params["gender"]             = gender
        if identifier: params["identifier"]         = identifier
        if active is not None:
            params["active"] = "true" if active else "false"
        return self._request("Practitioner", params)

    def search_practitioner_roles(self, specialty_code=None, city=None,
                                  state=None, postalcode=None,
                                  practitioner_name=None, active=None,
                                  count=20) -> dict:
        """Search the FHIR PractitionerRole endpoint.

        Per FHIR R4 §3.1.4 (https://hl7.org/fhir/R4/practitionerrole.html#search):
            specialty                       token     → PractitionerRole.specialty
            active                          token     → PractitionerRole.active
            location                        reference → PractitionerRole.location
            practitioner                    reference → PractitionerRole.practitioner

        Chained params (FHIR R4 §3.1.1.5.1) — reference param + `.` + target's
        search param. We chain through .location → Location.address.*:
            location.address-state          string    → Location.address.state
            location.address-city           string    → Location.address.city
            location.address-postalcode     string    → Location.address.postalCode

        And through .practitioner → Practitioner.name:
            practitioner.name               string    → Practitioner.name

        Token format for `specialty` (FHIR R4 §3.1.1.5.4):
            "system|code"   matches both (most precise)
            "code"          matches code regardless of system
        Cigna requires the explicit system form; the others accept bare codes.
        """
        params = {"_count": count}
        if specialty_code:
            # Per FHIR R4 §3.1.1.5.4 the system|code form is the spec-correct
            # way to match a token in a known coding system. Cigna's server
            # rejects the bare code; others accept either. Use the precise form
            # for Cigna only to avoid breaking the rest.
            if self.payer["id"] == "cigna":
                params["specialty"] = f"http://nucc.org/provider-taxonomy|{specialty_code}"
            else:
                params["specialty"] = specialty_code

        # Routing for name searches across payers:
        #   - Anthem natively handles chained `practitioner.name` + specialty in
        #     one query, so we let it through the standard path.
        #   - EVERY OTHER PAYER (Cigna, UHC, CapBlue, Humana) has issues with
        #     chained name on PractitionerRole (broken, ignored, or times out).
        #     For those we route to the Practitioner endpoint and, when a
        #     specialty is also requested, run a parallel two-strategy merge:
        #       (a) PR?specialty=…&_include=practitioner  → strong when specialty
        #           is dense and the linked Practitioners include name matches
        #       (b) Role-lookup: Practitioner?name=… then per-candidate
        #           PractitionerRole?practitioner=&specialty=  → strong when the
        #           name is rare and a deep specialty list would miss it
        #     Together they recover matches that either strategy alone misses.
        is_cigna  = self.payer["id"] == "cigna"
        is_anthem = self.payer["id"] == "elevance"
        use_combined_lookup = not is_anthem

        if use_combined_lookup and practitioner_name:
            if specialty_code:
                return self._combined_name_specialty_search(
                    name=practitioner_name, specialty_code=specialty_code,
                    count=count)
            # Name-only: the Practitioner endpoint honors `name` everywhere.
            return self.search_practitioners(name=practitioner_name, state=state,
                                             city=city, postalcode=postalcode,
                                             count=count)

        if not is_cigna:
            if city:       params["location.address-city"]       = city
            if state:      params["location.address-state"]      = _normalize_state(state)
            if postalcode: params["location.address-postalcode"] = postalcode

        # Only Anthem reaches this with a name (others were routed above).
        if practitioner_name:
            params["practitioner.name"] = practitioner_name

        if active is not None:
            params["active"] = "true" if active else "false"

        # Humana's PractitionerRole endpoint times out (>40s) on ANY chained
        # location.* / practitioner.* param. It only answers quickly when the
        # query is scoped by specialty.
        if self.payer["id"] == "humana":
            if specialty_code:
                # specialty + _include works (~7s). Strip the chained filters;
                # the server-side post-filter verifies name/location instead.
                params = {
                    "_count": count,
                    "specialty": specialty_code,
                    "_include": [
                        "PractitionerRole:practitioner",
                        "PractitionerRole:location",
                        "PractitionerRole:organization",
                    ],
                }
            elif city or state or postalcode or practitioner_name:
                # No specialty to scope by — a location/name-only PractitionerRole
                # query would hang ~45s. Fail fast with a helpful message.
                return {"entry": [], "total": 0,
                        "_error": "Add a specialty filter to include Humana",
                        "_payer_id": self.payer["id"]}

        # _include pulls related resources into the same bundle so we get
        # names / NPI / addresses in one request.
        if self.payer["id"] == "cigna":
            # Cigna supports only a SINGLE _include — passing multiple, or the
            # location include, makes Cigna return an empty bundle. The
            # practitioner include is the valuable one (name, NPI, gender,
            # qualifications). Cigna Locations are fetched via _backfill_locations.
            params["_include"] = "PractitionerRole:practitioner"
        else:
            params["_include"] = [
                "PractitionerRole:practitioner",
                "PractitionerRole:location",
                "PractitionerRole:organization"
            ]
        bundle = self._request("PractitionerRole", params)
        # Some payers (notably Capital BlueCross) ignore _include entirely and
        # return zero Location resources — so we can't verify a provider's
        # address and every result gets dropped on a location search. When the
        # user IS filtering by location, fetch the missing Locations directly.
        if city or state or postalcode:
            self._backfill_locations(bundle)
        return bundle

    def _combined_name_specialty_search(self, name: str, specialty_code: str,
                                        count: int = 20) -> dict:
        """For Cigna/UHC name+specialty searches: run TWO complementary strategies
        in parallel and merge their results.

        Why two strategies?
          (a) PR + _include + post-filter is STRONG when the specialty is dense
              in the payer's network. UHC's PractitionerRole?specialty=Cardiology
              returns 50 cardiologists per page, many named Eric.
          (b) Role-lookup is STRONG when the name is rare. Cigna has fewer Erics,
              so fetching Practitioner?name=Eric (20 candidates) and verifying
              each one's specialty via PractitionerRole?practitioner=... finds
              real matches that the dense-specialty page would miss.

        Together they cover both cases. Both run in parallel so total latency
        ≈ max(strategy A, strategy B) ≈ 4-5s instead of summing."""

        def branch_pr_include():
            # Larger page so the include set has more unique linked Practitioners
            # to match the name against (UHC's _include deduplicates aggressively).
            spec_token = (f"http://nucc.org/provider-taxonomy|{specialty_code}"
                          if self.payer["id"] == "cigna" else specialty_code)
            params = {"_count": 100, "specialty": spec_token}
            if self.payer["id"] == "cigna":
                # Cigna only accepts a single _include
                params["_include"] = "PractitionerRole:practitioner"
            else:
                params["_include"] = [
                    "PractitionerRole:practitioner",
                    "PractitionerRole:location",
                    "PractitionerRole:organization",
                ]
            return self._request("PractitionerRole", params)

        def branch_role_lookup():
            # UHC orders Practitioner?name= by some internal sort that puts
            # dentists/MDs at the top, so the first 50 of a common name rarely
            # include the target specialty. Paginate 3 pages (150 candidates)
            # for UHC; one page is enough for everyone else.
            max_pages = 3 if self.payer["id"] == "uhc" else 1
            return self._lookup_roles_by_name_and_specialty(
                name=name, specialty_code=specialty_code, count=50,
                max_pages=max_pages)

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_pr = ex.submit(branch_pr_include)
            f_rl = ex.submit(branch_role_lookup)
            try:
                pr_bundle = f_pr.result(timeout=REQUEST_TIMEOUT)
            except Exception:
                pr_bundle = {"entry": []}
            try:
                rl_bundle = f_rl.result(timeout=REQUEST_TIMEOUT)
            except Exception:
                rl_bundle = {"entry": []}

        # Merge — dedupe by (resourceType, id). Both branches return tagged
        # entries with _payer_id etc., so downstream pipeline works unchanged.
        seen, merged = set(), []
        for src in (pr_bundle, rl_bundle):
            for e in src.get("entry") or []:
                r = e.get("resource", {})
                key = (r.get("resourceType"), r.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(e)

        return {
            "entry":     merged,
            "total":     len(merged),
            "_payer_id": self.payer["id"],
        }

    def _lookup_roles_by_name_and_specialty(self, name: str,
                                            specialty_code: str,
                                            count: int = 20,
                                            max_pages: int = 1) -> dict:
        """For Cigna/UHC/CapBlue/Humana: their PractitionerRole endpoint can't
        filter by name (chained practitioner.name is broken/ignored). Instead:
          1. Fetch Practitioner?name=<name>  → up to `count * max_pages` candidates
             (UHC orders dentists/MDs first for common names, so deeper pagination
             is needed to reach a target specialty).
          2. For each candidate, look up PractitionerRole?practitioner=<id>&specialty=<code>
             in parallel. Verify specialty locally (some payers ignore the param).
          3. Inject the candidate's display name onto each matched role.
        Returns a bundle of PractitionerRole entries (mode=match) ready for the
        standard downstream pipeline (_resolve, _inject, _post_filter)."""
        # Step 1: fetch the name candidates, paginating up to max_pages
        cand_bundle = self.search_practitioners(name=name, count=count)
        candidates = [
            e for e in (cand_bundle.get("entry") or [])
            if e.get("resource", {}).get("resourceType") == "Practitioner"
        ]
        seen_ids = {e["resource"].get("id") for e in candidates
                    if e["resource"].get("id")}

        # Walk the FHIR `next` link for additional pages (UHC needs this)
        current = cand_bundle
        for _ in range(max_pages - 1):
            next_url = None
            for link in current.get("link") or []:
                if link.get("relation") == "next":
                    next_url = link.get("url")
                    break
            if not next_url:
                break
            try:
                r = requests.get(next_url, headers=self._auth_headers(),
                                 timeout=REQUEST_TIMEOUT)
                if r.status_code != 200:
                    break
                current = r.json()
                for e in current.get("entry") or []:
                    res = e.get("resource", {})
                    if res.get("resourceType") != "Practitioner":
                        continue
                    rid = res.get("id")
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        candidates.append(e)
            except (requests.RequestException, ValueError):
                break

        if not candidates:
            return {"entry": [], "total": 0, "_payer_id": self.payer["id"]}

        # Cigna's specialty needs the explicit system|code; others accept bare
        spec_token = (f"http://nucc.org/provider-taxonomy|{specialty_code}"
                      if self.payer["id"] == "cigna" else specialty_code)

        spec_upper = specialty_code.upper()

        def lookup(candidate):
            prac = candidate.get("resource", {})
            pid = prac.get("id")
            if not pid:
                return []
            try:
                # Fetch a generous page since some payers (CapBlue) IGNORE the
                # specialty parameter when `practitioner` is set and return ALL
                # of the practitioner's roles regardless. We have to filter
                # locally below.
                r = requests.get(
                    f"{self.base}/PractitionerRole",
                    params={"_count": 50, "practitioner": pid,
                            "specialty": spec_token},
                    headers=self._auth_headers(),
                    timeout=REQUEST_TIMEOUT,
                )
                if r.status_code != 200:
                    return []
                display = _build_practitioner_name(prac) or pid
                matched = []
                for ent in r.json().get("entry") or []:
                    role = ent.get("resource", {})
                    if role.get("resourceType") != "PractitionerRole":
                        continue
                    # Verify the role's specialty[] actually contains the
                    # requested NUCC code — defends against payers that
                    # ignore the `specialty` query parameter.
                    role_codes = set()
                    for s in role.get("specialty") or []:
                        for c in (s.get("coding") or []):
                            if c.get("code"):
                                role_codes.add(c["code"].upper())
                    if spec_upper not in role_codes:
                        continue
                    # Inject the resolved practitioner name + carry NPI from the
                    # candidate so the frontend can show it without extra fetches
                    role.setdefault("practitioner", {})["display"] = display
                    if prac.get("identifier") and not role.get("identifier"):
                        role["identifier"] = prac["identifier"]
                    matched.append({
                        "resource":     role,
                        "search":       {"mode": "match"},
                        "_payer_id":    self.payer["id"],
                        "_payer_name":  self.payer["display_name"],
                        "_payer_short": self.payer["short_name"],
                        "_payer_color": self.payer["color"],
                    })
                return matched
            except (requests.RequestException, ValueError):
                return []

        all_matched = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            for roles in ex.map(lookup, candidates):
                all_matched.extend(roles)

        return {
            "entry":      all_matched,
            "total":      len(all_matched),
            "_payer_id":  self.payer["id"],
        }

    def _backfill_locations(self, bundle: dict) -> None:
        """Directly fetch Location resources that a PractitionerRole references
        but the payer failed to include via _include. Fetched Locations are
        appended to the bundle as search.mode='include' entries so the address
        post-filter can resolve them (and they're dropped from final output)."""
        entries = bundle.get("entry") or []
        have_ids = {
            r["id"] for e in entries
            if (r := e.get("resource", {})).get("resourceType") == "Location"
            and r.get("id")
        }
        missing, seen = [], set()
        for e in entries:
            r = e.get("resource", {})
            if r.get("resourceType") != "PractitionerRole":
                continue
            for loc in (r.get("location") or []):
                ref = (loc or {}).get("reference") or ""
                if not ref:
                    continue
                lid = ref.rsplit("/", 1)[-1]
                if lid and lid not in have_ids and lid not in seen:
                    seen.add(lid)
                    missing.append((lid, ref))
        if not missing:
            return
        missing = missing[:60]  # bound the work — never fetch an unbounded set

        def fetch_one(item):
            lid, ref = item
            url = ref if ref.startswith("http") else f"{self.base}/Location/{lid}"
            try:
                r = requests.get(url, headers=self._auth_headers(), timeout=12)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("resourceType") == "Location":
                        return data
            except (requests.RequestException, ValueError):
                pass
            return None

        with ThreadPoolExecutor(max_workers=16) as ex:
            for loc in ex.map(fetch_one, missing):
                if loc:
                    bundle.setdefault("entry", []).append({
                        "resource":     loc,
                        "search":       {"mode": "include"},
                        "_payer_id":    self.payer["id"],
                        "_payer_name":  self.payer["display_name"],
                        "_payer_short": self.payer["short_name"],
                        "_payer_color": self.payer["color"],
                    })

    def search_organizations(self, name=None, city=None, state=None,
                             postalcode=None, identifier=None, active=None,
                             count=20) -> dict:
        """Search the FHIR Organization endpoint.

        Per FHIR R4 §3.1.5 (https://hl7.org/fhir/R4/organization.html#search):
            name               string  → Organization.name | Organization.alias
            address-state      string  → Organization.address.state
            address-city       string  → Organization.address.city
            address-postalcode string  → Organization.address.postalCode
            identifier         token   → Organization.identifier
            active             token   → Organization.active
        """
        params = {"_count": count}
        if name:       params["name"]               = name
        if city:       params["address-city"]       = city
        if state:      params["address-state"]      = _normalize_state(state)
        if postalcode: params["address-postalcode"] = postalcode
        if identifier: params["identifier"]         = identifier
        if active is not None:
            params["active"] = "true" if active else "false"
        return self._request("Organization", params)

    def get_capability_statement(self) -> Optional[dict]:
        """Hit /metadata to test the endpoint."""
        if not is_payer_configured(self.payer):
            return None
        try:
            r = requests.get(f"{self.base}/metadata",
                           headers=self._auth_headers(), timeout=8)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        return None


# ----------------------------------------------------------------------
# Result post-processing
# ----------------------------------------------------------------------
def _build_practitioner_name(resource: dict) -> Optional[str]:
    """Extract a human-readable name from a Practitioner resource."""
    for n in (resource.get("name") or []):
        if n.get("text"):
            return n["text"]
        parts = []
        given = n.get("given")
        if isinstance(given, list): parts += given
        elif given: parts.append(given)
        family = n.get("family")
        if family: parts.append(family)
        if parts:
            return " ".join(parts)
    return None


def _resolve_role_names(entries: list) -> None:
    """Enrich every PractitionerRole from its _include'd Practitioner sibling.

    A PractitionerRole on its own carries almost no human-readable detail —
    name, NPI, gender, qualifications and (for some payers) specialty all live
    on the linked Practitioner. We copy those fields onto the role so the
    search cards, post-filter and detail page all have data to show.

    Only fills fields the role doesn't already have — never overwrites.
    """
    # Index Practitioners by their FHIR id, scoped per-payer
    prac_index = {}
    for e in entries:
        r = e.get("resource", {})
        if r.get("resourceType") == "Practitioner" and r.get("id"):
            prac_index[(e.get("_payer_id"), r["id"])] = r

    for e in entries:
        r = e.get("resource", {})
        if r.get("resourceType") != "PractitionerRole":
            continue
        ref = (r.get("practitioner") or {}).get("reference") or ""
        ref_id = ref.rsplit("/", 1)[-1] if ref else None
        target = prac_index.get((e.get("_payer_id"), ref_id)) if ref_id else None
        if not target:
            continue

        # Name → practitioner.display (search cards) + name[] array (detail page)
        name = _build_practitioner_name(target)
        if name and not (r.get("practitioner") or {}).get("display"):
            r.setdefault("practitioner", {})["display"] = name
        if target.get("name") and not r.get("name"):
            r["name"] = target["name"]

        # Copy detail fields the role lacks: NPI, gender, qualifications, languages
        for field in ("identifier", "gender", "qualification", "communication"):
            if target.get(field) and not r.get(field):
                r[field] = target[field]

        # Specialty fallback: if the role has no specialty (UHC often doesn't),
        # synthesize one from the practitioner's NUCC-coded qualification so the
        # card subtitle and detail page still show the clinical area.
        if not r.get("specialty"):
            for q in (target.get("qualification") or []):
                code = q.get("code") or {}
                for c in (code.get("coding") or []):
                    if (c.get("system") or "").endswith("provider-taxonomy"):
                        r["specialty"] = [{"coding": [c],
                                           "text": c.get("display")}]
                        break
                if r.get("specialty"):
                    break


def _entry_display_name(entry: dict) -> Optional[str]:
    """Return the best human name for an aggregated entry, or None if we can't tell."""
    r = entry.get("resource", {})
    rt = r.get("resourceType", "")
    if rt == "Practitioner":
        return _build_practitioner_name(r)
    if rt == "PractitionerRole":
        return (r.get("practitioner") or {}).get("display")
    if rt == "Organization":
        return r.get("name")
    return None


def _build_location_index(entries: list) -> dict:
    """Index Location resources by (payer, id) so PractitionerRole entries
    can cross-reference their linked location's address."""
    index = {}
    for e in entries:
        r = e.get("resource", {})
        if r.get("resourceType") == "Location" and r.get("id"):
            index[(e.get("_payer_id"), r["id"])] = r
    return index


def _inject_role_addresses(entries: list) -> None:
    """Copy each PractitionerRole's linked Location.address onto the role
    resource as `address`, so the frontend's getLocation() (which reads
    `resource.address[0]`) can render the city/state for role-based results."""
    if not entries:
        return
    location_index = _build_location_index(entries)
    if not location_index:
        return
    for e in entries:
        r = e.get("resource", {})
        if r.get("resourceType") != "PractitionerRole":
            continue
        if r.get("address"):
            continue  # already has inline addresses
        addrs = []
        for loc_ref in (r.get("location") or []):
            ref = (loc_ref or {}).get("reference") or ""
            ref_id = ref.rsplit("/", 1)[-1] if ref else None
            if not ref_id:
                continue
            loc = location_index.get((e.get("_payer_id"), ref_id))
            if loc:
                a = loc.get("address")
                if isinstance(a, dict):
                    addrs.append(a)
                elif isinstance(a, list):
                    addrs.extend(a)
        if addrs:
            r["address"] = addrs


def _addresses_for_entry(entry: dict, location_index: dict) -> list:
    """Return the list of address dicts (with state/city) attributable to this entry.
    Practitioner: inline address[]. PractitionerRole: address from the _include'd Location."""
    r = entry.get("resource", {})
    rt = r.get("resourceType", "")
    if rt == "Practitioner":
        return r.get("address") or []
    if rt == "PractitionerRole":
        addrs = []
        for loc_ref in (r.get("location") or []):
            ref = (loc_ref or {}).get("reference") or ""
            ref_id = ref.rsplit("/", 1)[-1] if ref else None
            if not ref_id:
                continue
            loc = location_index.get((entry.get("_payer_id"), ref_id))
            if loc:
                a = loc.get("address")
                if isinstance(a, dict):
                    addrs.append(a)
                elif isinstance(a, list):
                    addrs.extend(a)
        return addrs
    return []


_US_STATE_ABBR = {
    "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA",
    "colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA",
    "hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS",
    "kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA",
    "michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO","montana":"MT",
    "nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM",
    "new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK",
    "oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC",
    "south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT",
    "virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY",
    "district of columbia":"DC","puerto rico":"PR",
}


def _normalize_state(s: str) -> str:
    """Accept either 'TX' or 'Texas' (any case); return the 2-letter abbreviation."""
    s = (s or "").strip()
    if not s:
        return ""
    if len(s) == 2:
        return s.upper()
    return _US_STATE_ABBR.get(s.lower(), s.upper())


def _entry_specialty_codes(entry: dict) -> set:
    """Collect every NUCC taxonomy code we can see on this entry — from
    PractitionerRole.specialty[].coding[], Practitioner.qualification[].code.coding[],
    plus a few free-text fallbacks. Returned codes are uppercased."""
    codes = set()
    r = entry.get("resource", {})
    rt = r.get("resourceType", "")
    if rt == "PractitionerRole":
        for s in r.get("specialty") or []:
            for c in (s.get("coding") or []):
                if c.get("code"):
                    codes.add(c["code"].upper())
            if s.get("text"):
                codes.add(s["text"].upper())
    elif rt == "Practitioner":
        for q in r.get("qualification") or []:
            code = q.get("code") or {}
            for c in (code.get("coding") or []):
                if c.get("code"):
                    codes.add(c["code"].upper())
            if code.get("text"):
                codes.add(code["text"].upper())
    return codes


def _post_filter(entries: list, name_filter: str, state_filter: str,
                 city_filter: str, specialty_filter: str = "",
                 postalcode_filter: str = "") -> list:
    """Enforce the user's filters server-side.

    Several payers (CapBlue, Anthem, sometimes Humana/UHC) silently ignore
    chained search params and return unfiltered results. We can't fix their FHIR
    implementations, but we can drop entries that don't actually match the
    user's intent before showing them.

    Address fields validated per FHIR R4 §11.3 (Address datatype):
      Address.state, Address.city, Address.postalCode.
    Specialty validated by NUCC taxonomy codes carried on PractitionerRole.specialty
    or Practitioner.qualification[].code.coding[].
    """
    if not (name_filter or state_filter or city_filter or specialty_filter
            or postalcode_filter):
        return entries

    name_lc      = name_filter.lower()
    state_uc     = _normalize_state(state_filter)
    city_lc      = city_filter.lower()
    specialty_uc = (specialty_filter or "").strip().upper()
    zip_str      = (postalcode_filter or "").strip()
    need_loc     = bool(state_uc or city_lc or zip_str)

    # Build a Location index for resolving PractitionerRole addresses
    location_index = _build_location_index(entries) if need_loc else {}

    kept = []
    for entry in entries:
        r = entry.get("resource", {})
        rt = r.get("resourceType", "")

        # Drop server-side noise that the frontend would just confuse for results
        if rt in ("OperationOutcome", "Location"):
            continue

        # Name filter: drop entries we can't confidently match
        if name_lc:
            disp = _entry_display_name(entry)
            if not disp or name_lc not in disp.lower():
                continue

        # Specialty filter: some payers ignore the specialty param entirely and
        # return providers of every kind. We verify the entry's own NUCC
        # taxonomy codes against the requested specialty.
        if specialty_uc and rt in ("Practitioner", "PractitionerRole"):
            codes = _entry_specialty_codes(entry)
            if specialty_uc not in codes:
                continue

        # State/city filter: drop entries we can't verify against the location.
        # If a payer returns records with no resolvable address, the only honest
        # move is to require a verifiable address match before showing a result
        # for a state/city/zip query.
        if need_loc and rt in ("Practitioner", "PractitionerRole"):
            addrs = _addresses_for_entry(entry, location_index)
            if not addrs:
                continue  # can't verify location
            matching_index = -1
            for i, a in enumerate(addrs):
                # Normalize BOTH sides — a payer might store "Texas" / "texas"
                # / "TX". We always compare 2-letter codes.
                if state_uc:
                    addr_state = _normalize_state(a.get("state") or "")
                    if addr_state != state_uc:
                        continue
                if city_lc and city_lc not in (a.get("city") or "").lower():
                    continue
                # FHIR R4 §11.3: Address.postalCode (camelCase in the resource;
                # search param is address-postalcode). Match as prefix so 78701
                # also matches 78701-1234.
                if zip_str and not (a.get("postalCode") or "").startswith(zip_str):
                    continue
                matching_index = i
                break
            if matching_index < 0:
                continue
            # Reorder the resource's address list so the matching address is
            # at index 0. The frontend renders address[0] — without this, a
            # provider with multiple practice locations could display the
            # wrong-zip/state address even though the filter passed correctly.
            # Applies to both Practitioner (inline addresses) and PractitionerRole
            # (addresses injected from _include'd Location resources).
            if matching_index > 0:
                resource_addrs = r.get("address")
                if resource_addrs and matching_index < len(resource_addrs):
                    resource_addrs.insert(0, resource_addrs.pop(matching_index))

        kept.append(entry)

    return kept


# ----------------------------------------------------------------------
# Parallel-search helper
# ----------------------------------------------------------------------
def parallel_search(payers: list, search_fn_name: str, **kwargs) -> dict:
    aggregated_entries = []
    summaries = []

    def run_one(payer):
        client = DirectoryClient(payer)
        method = getattr(client, search_fn_name)
        bundle = method(**kwargs)
        return payer, bundle

    if not payers:
        return {"entries": [], "summaries": [], "total": 0}

    ex = ThreadPoolExecutor(max_workers=min(8, len(payers)))
    try:
        future_to_payer = {ex.submit(run_one, p): p for p in payers}
        try:
            for fut in as_completed(list(future_to_payer.keys()),
                                    timeout=REQUEST_TIMEOUT + 5):
                payer = future_to_payer[fut]
                try:
                    _, bundle = fut.result()
                    entries = bundle.get("entry", [])
                    aggregated_entries.extend(entries)

                    next_url = None
                    for link in bundle.get("link", []):
                        if link.get("relation") == "next":
                            next_url = link.get("url")

                    summaries.append({
                        "payer":    payer,
                        "total":    bundle.get("total", len(entries)),
                        "returned": len(entries),
                        "error":    bundle.get("_error"),
                        "next_url": next_url,
                    })
                except Exception as e:
                    summaries.append({
                        "payer":    payer,
                        "total":    0, "returned": 0,
                        "error":    f"{type(e).__name__}: {e}",
                        "next_url": None,
                    })
        except TimeoutError:
            # Outer wall-clock budget hit; whichever payers haven't returned
            # are marked Timeout below. Don't crash the whole search.
            pass

        # Any future that didn't complete inside the budget gets a Timeout entry
        # so the sidebar shows the warning instead of an empty slot.
        for fut, payer in future_to_payer.items():
            if not fut.done():
                summaries.append({
                    "payer":    payer,
                    "total":    0, "returned": 0,
                    "error":    "Timeout",
                    "next_url": None,
                })
    finally:
        # Don't block the response on stragglers — their HTTP timeout will
        # clean them up in the background thread.
        ex.shutdown(wait=False)

    # Eagerly resolve PractitionerRole names from _include'd Practitioner siblings.
    # This prevents "Practitioner Role" / "Unnamed Provider" placeholders from
    # leaking past the frontend filter.
    _resolve_role_names(aggregated_entries)

    # Inject linked Location addresses onto PractitionerRole resources so the
    # frontend can render city/state for role-based results (without this the
    # cards show no location for CapBlue / Humana / Anthem etc.).
    _inject_role_addresses(aggregated_entries)

    # Apply the user's filters universally — many payers ignore chained params.
    # Must run BEFORE we strip include siblings, because _post_filter builds a
    # Location index from the bundle to verify PractitionerRole addresses.
    name_filter = (kwargs.get("name")
                   or kwargs.get("practitioner_name")
                   or kwargs.get("family")
                   or kwargs.get("given") or "").strip()
    state_filter      = (kwargs.get("state") or "").strip()
    city_filter       = (kwargs.get("city") or "").strip()
    specialty_filter  = (kwargs.get("specialty_code") or "").strip()
    postalcode_filter = (kwargs.get("postalcode") or "").strip()

    if (name_filter or state_filter or city_filter
            or specialty_filter or postalcode_filter):
        aggregated_entries = _post_filter(
            aggregated_entries, name_filter, state_filter, city_filter,
            specialty_filter, postalcode_filter,
        )

    # Drop _include'd siblings now that we've used them for enrichment.
    # Per FHIR R4 §3.1.1.7, each Bundle.entry has a search.mode that's either
    # "match" (a real search hit) or "include" (a sibling brought in by _include).
    # Without this, Organizations / Practitioner siblings appear as "results"
    # even though they bypass the location/specialty filter (the post-filter
    # only validates Practitioner & PractitionerRole entries against location).
    aggregated_entries = [
        e for e in aggregated_entries
        if (e.get("search") or {}).get("mode", "match") != "include"
    ]

    # Per-payer cap — keep results balanced so no single network dominates the
    # UI if a payer ignores _count and returns an oversized page.
    PER_PAYER_CAP = 25
    seen_per_payer = {}
    capped = []
    for entry in aggregated_entries:
        pid = entry.get("_payer_id") or "unknown"
        if seen_per_payer.get(pid, 0) >= PER_PAYER_CAP:
            continue
        seen_per_payer[pid] = seen_per_payer.get(pid, 0) + 1
        capped.append(entry)
    aggregated_entries = capped

    # Recompute per-payer counts so the sidebar reflects what's actually visible.
    per_payer = {}
    for e in aggregated_entries:
        pid = e.get("_payer_id")
        per_payer[pid] = per_payer.get(pid, 0) + 1
    for s in summaries:
        if not s.get("error"):
            s["returned"] = per_payer.get(s["payer"]["id"], 0)

    return {
        "entries":   aggregated_entries,
        "summaries": summaries,
        "total":     sum(s["returned"] for s in summaries if not s["error"]),
    }