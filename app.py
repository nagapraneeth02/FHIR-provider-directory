"""
Provider Finder v2 — Multi-Payer FHIR Provider Directory
=========================================================
A light-themed interface for searching across 5 major US health insurers'
public provider directories simultaneously.

Active payers:
  - Fully public (no auth):  Cigna, UnitedHealthcare, Capital BlueCross, Humana
  - OAuth client_credentials: Anthem / Elevance Health

Anthem credentials live in .env (ELEVANCE_CLIENT_ID / _SECRET / _TOKEN_URL).
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify

# Load .env BEFORE importing payers (which reads env vars at import time)
from dotenv import load_dotenv
load_dotenv()

from payers import PAYERS, SPECIALTIES, list_payers, get_payer, get_specialty_by_code, is_payer_configured
from fhir_client import DirectoryClient, parallel_search, _normalize_state


app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# Startup banner
print("=" * 64, file=sys.stderr)
print(" Provider Finder v2 — multi-payer FHIR search", file=sys.stderr)
print(f" Loaded {len(PAYERS)} payers", file=sys.stderr)
for p in list_payers():
    status = "✓ ready" if is_payer_configured(p) else "✗ missing creds"
    print(f"   {p['short_name']:14s} {status}", file=sys.stderr)
print("=" * 64, file=sys.stderr)


# ======================================================================
@app.route("/")
def home():
    return render_template(
        "home.html",
        payers=list_payers(),
        specialties=SPECIALTIES,
        configured_count=sum(1 for p in list_payers() if is_payer_configured(p)),
    )


@app.route("/search")
@app.route("/search")
def search():
    query = {
        "name":           (request.args.get("name") or "").strip(),
        "family":         (request.args.get("family") or "").strip(),
        "given":          (request.args.get("given") or "").strip(),
        "state":          _normalize_state(request.args.get("state") or ""),
        "city":           (request.args.get("city") or "").strip(),
        "zip":            (request.args.get("zip") or "").strip(),
        "specialty_code": (request.args.get("specialty") or "").strip(),
        "search_type":    request.args.get("type") or "auto",
    }

    # Default to all *configured* payers if user didn't pick any
    selected_ids = request.args.getlist("payer")
    if not selected_ids:
        selected_ids = [p["id"] for p in list_payers() if is_payer_configured(p)]
    selected_payers = [PAYERS[pid] for pid in selected_ids if pid in PAYERS]

    accepting = request.args.get("accepting") == "yes"
    gender    = request.args.get("gender") or ""
    language  = (request.args.get("language") or "").strip().lower()

    # Decide which FHIR call to make
    has_query = any([query["name"], query["family"], query["given"],
                    query["city"], query["state"], query["zip"], query["specialty_code"]])

    if not selected_payers:
        return render_template("search.html", query=query, results=None,
                              specialties=SPECIALTIES, payers=list_payers(),
                              selected_payer_ids=selected_ids, specialty=None,
                              filters=None)

    # ---> THE NEW REPLACEMENT SECTION IS HERE <---
    if not has_query:
        result = parallel_search(selected_payers, "search_practitioner_roles")
        
    elif query["specialty_code"] or query["search_type"] == "specialty" or query["zip"] or query["city"] or query["state"]:
        result = parallel_search(
            selected_payers, "search_practitioner_roles",
            specialty_code=query["specialty_code"] or None,
            city=query["city"] or None, 
            state=query["state"] or None,
            postalcode=query["zip"] or None,
            practitioner_name=query["name"] or None # <-- THE FIX IS HERE
        )
        
    elif query["search_type"] == "organization":
        result = parallel_search(
            selected_payers, "search_organizations",
            name=query["name"] or None,
            city=query["city"] or None, state=query["state"] or None,
        )
    else:
        result = parallel_search(
            selected_payers, "search_practitioners",
            name=query["name"] or None, family=query["family"] or None,
            given=query["given"] or None, state=query["state"] or None,
        )

    # Faceted post-filter
    entries = result["entries"]
    if gender:
        entries = [e for e in entries
                   if (e.get("resource", {}).get("gender") or "").lower() == gender.lower()]
    if language:
        def has_lang(e):
            for c in e.get("resource", {}).get("communication", []) or []:
                if language in (c.get("text") or "").lower():
                    return True
                for cd in c.get("coding") or []:
                    if language in (cd.get("display") or "").lower():
                        return True
            return False
        entries = [e for e in entries if has_lang(e)]
    if accepting:
        entries = [e for e in entries
                   if e.get("resource", {}).get("active") in (True, None)]

    result["entries"] = entries
    specialty = get_specialty_by_code(query["specialty_code"]) if query["specialty_code"] else None

    return render_template(
        "search.html", query=query, results=result, specialties=SPECIALTIES,
        payers=list_payers(), selected_payer_ids=selected_ids,
        specialty=specialty,
        filters={"accepting": accepting, "gender": gender, "language": language},
    )

@app.route("/provider/<payer_id>/<resource_type>/<resource_id>")
def provider_detail(payer_id, resource_type, resource_id):
    payer = get_payer(payer_id)
    if not payer:
        return "Unknown payer", 404

    client = DirectoryClient(payer)
    import requests

    def _fetch(ref):
        """Fetch a referenced resource. `ref` may be absolute or relative."""
        url = ref if ref.startswith("http") else f"{client.base}/{ref}"
        try:
            rr = requests.get(url, headers=client._auth_headers(), timeout=10)
            if rr.status_code == 200:
                return rr.json()
        except Exception as e:
            print(f"[provider_detail] ref fetch failed {ref}: {e}", file=sys.stderr)
        return None

    try:
        r = requests.get(
            f"{client.base}/{resource_type}/{resource_id}",
            headers=client._auth_headers(), timeout=12,
        )
        resource = r.json() if r.status_code == 200 else None

        # A PractitionerRole on its own is sparse — name, NPI, gender,
        # qualifications all live on the linked Practitioner, and the address
        # lives on the linked Location. Follow those references and merge the
        # data in so the detail page renders fully for every payer.
        if resource and resource_type == "PractitionerRole":
            # --- linked Practitioner ---
            pref = (resource.get("practitioner") or {}).get("reference")
            if pref:
                prac = _fetch(pref)
                if prac:
                    for field in ("name", "gender", "identifier",
                                  "qualification", "communication"):
                        if prac.get(field) and not resource.get(field):
                            resource[field] = prac[field]

            # --- linked Location(s) → addresses ---
            # Some providers list dozens of practice locations; fetch up to 10
            # in parallel so the page never hangs.
            loc_refs = [(l or {}).get("reference")
                        for l in (resource.get("location") or [])]
            loc_refs = [ref for ref in loc_refs if ref][:10]
            addrs = []
            if loc_refs:
                with ThreadPoolExecutor(max_workers=10) as ex:
                    for loc in ex.map(_fetch, loc_refs):
                        if loc and loc.get("address"):
                            a = loc["address"]
                            addrs.extend(a if isinstance(a, list) else [a])
            if addrs and not resource.get("address"):
                resource["address"] = addrs

    except Exception as e:
        resource = None
        print(f"[provider_detail] {payer_id}: {e}", file=sys.stderr)

    return render_template("provider_detail.html",
                          payer=payer, resource=resource,
                          resource_type=resource_type)


@app.route("/compare")
def compare():
    raw = request.args.get("ids", "")
    items = [i.strip() for i in raw.split(",") if i.strip()][:3]
    providers = []
    
    for spec in items:
        parts = spec.split("|")
        if len(parts) < 3:
            continue
            
        payer_id = parts[0]
        rtype = parts[1]
        rid = parts[2]
        
        payer = get_payer(payer_id)
        if not payer:
            continue
            
        client = DirectoryClient(payer)
        import requests
        try:
            r = requests.get(f"{client.base}/{rtype}/{rid}",
                           headers=client._auth_headers(), timeout=10)
            if r.status_code == 200:
                resource = r.json()
                
                # 1. FETCH HUMAN DATA: Get missing Name & Gender
                if rtype == "PractitionerRole" and "practitioner" in resource:
                    ref = resource["practitioner"].get("reference")
                    if ref:
                        url = ref if ref.startswith("http") else f"{client.base}/{ref}"
                        prac_req = requests.get(url, headers=client._auth_headers(), timeout=5)
                        if prac_req.status_code == 200:
                            prac_data = prac_req.json()
                            if "name" in prac_data: 
                                resource["name"] = prac_data["name"]
                            # Inject the gender back into the main resource
                            if "gender" in prac_data:
                                resource["gender"] = prac_data["gender"]
                                
                # 2. FETCH CLINIC DATA: Get missing Address from Location reference
                if rtype == "PractitionerRole" and "location" in resource and len(resource["location"]) > 0:
                    loc_ref = resource["location"][0].get("reference")
                    if loc_ref:
                        url = loc_ref if loc_ref.startswith("http") else f"{client.base}/{loc_ref}"
                        loc_req = requests.get(url, headers=client._auth_headers(), timeout=5)
                        if loc_req.status_code == 200:
                            loc_data = loc_req.json()
                            if "address" in loc_data:
                                # FHIR Locations use objects, our template expects a list. Wrap it safely.
                                addr = loc_data["address"]
                                resource["address"] = addr if isinstance(addr, list) else [addr]

                # Flatten the complex FHIR name array into a simple string
                display_name = "Unnamed"
                if "name" in resource and isinstance(resource["name"], list) and len(resource["name"]) > 0:
                    n = resource["name"][0]
                    parts = [
                        " ".join(n.get("prefix", [])),
                        " ".join(n.get("given", [])),
                        n.get("family", ""),
                        " ".join(n.get("suffix", []))
                    ]
                    display_name = " ".join(filter(bool, parts))
                elif "practitioner" in resource and "display" in resource["practitioner"]:
                    display_name = resource["practitioner"]["display"]
                elif "name" in resource and isinstance(resource["name"], str):
                    display_name = resource["name"]

                # Pass the safe data to the template
                providers.append({
                    "payer": payer, 
                    "resource": resource,
                    "resource_type": rtype,
                    "clean_name": display_name
                })
        except Exception as e:
            print(f"Compare error: {e}")
            pass
            
    return render_template("compare.html", providers=providers)


@app.route("/admin/health")
def admin_health():
    def check(payer):
        configured = is_payer_configured(payer)
        cs = None
        if configured:
            client = DirectoryClient(payer)
            cs = client.get_capability_statement()
        return {
            "payer":      payer,
            "configured": configured,
            "ok":         cs is not None,
            "name":       (cs or {}).get("name") or "—",
            "version":    (cs or {}).get("fhirVersion") or "—",
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(check, list_payers()))
    return render_template("admin_health.html", results=results)


@app.route("/api/network-stats")
def api_network_stats():
    state = (request.args.get("state") or "").strip().upper() or None

    def count_one(payer):
        if not is_payer_configured(payer):
            return {
                "payer_id": payer["id"], "payer": payer["display_name"],
                "short": payer["short_name"], "color": payer["color"],
                "total": 0, "error": "Not configured",
            }
        client = DirectoryClient(payer)
        bundle = client.search_practitioners(state=state, count=1)
        return {
            "payer_id": payer["id"], "payer": payer["display_name"],
            "short": payer["short_name"], "color": payer["color"],
            "total": bundle.get("total", 0),
            "error": bundle.get("_error"),
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        stats = list(ex.map(count_one, list_payers()))
    stats.sort(key=lambda x: x["total"], reverse=True)
    return jsonify({"state": state, "stats": stats})

@app.route("/api/resolve-practitioner")
def resolve_practitioner():
    """Proxy route for the frontend to lazily load missing names from strict payers like Cigna."""
    payer_id = request.args.get("payer_id")
    ref = request.args.get("ref")
    
    if not payer_id or not ref:
        return jsonify({"error": "Missing parameters"}), 400
        
    payer = get_payer(payer_id)
    if not payer:
        return jsonify({"error": "Unknown payer"}), 404
        
    client = DirectoryClient(payer)
    import requests
    try:
        r = requests.get(f"{client.base}/{ref}", headers=client._auth_headers(), timeout=5)
        if r.status_code == 200:
            data = r.json()
            # Extract and format the name
            name_obj = data.get("name", [{}])[0]
            parts = [
                " ".join(name_obj.get("prefix", [])),
                " ".join(name_obj.get("given", [])),
                name_obj.get("family", ""),
                " ".join(name_obj.get("suffix", []))
            ]
            full_name = " ".join(filter(bool, parts))
            return jsonify({"name": full_name or "Unnamed Provider"})
    except Exception:
        pass
        
    return jsonify({"error": "Failed to resolve"}), 500


@app.route("/api/load-more")
def api_load_more():
    payer_id = request.args.get("payer_id")
    next_url = request.args.get("next_url")
    if not payer_id or not next_url:
        return jsonify({"error": "Missing parameters"}), 400

    payer = get_payer(payer_id)
    client = DirectoryClient(payer)
    import requests
    from fhir_client import (REQUEST_TIMEOUT, _resolve_role_names,
                             _inject_role_addresses, _post_filter)

    # The user's original filters travel with the load-more call so we can
    # apply the same post-filter the initial /search route ran. Without this
    # every Load More click would leak unfiltered entries (the prior bug).
    name_filter       = (request.args.get("name") or "").strip()
    state_filter      = _normalize_state(request.args.get("state") or "")
    city_filter       = (request.args.get("city") or "").strip()
    specialty_filter  = (request.args.get("specialty") or "").strip()
    postalcode_filter = (request.args.get("zip") or "").strip()

    try:
        r = requests.get(next_url, headers=client._auth_headers(),
                         timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            bundle = client._tag_results(r.json())
            entries = bundle.get("entry") or []

            # Same enrichment + filter pipeline /search uses
            _resolve_role_names(entries)
            _inject_role_addresses(entries)
            if (name_filter or state_filter or city_filter
                    or specialty_filter or postalcode_filter):
                entries = _post_filter(entries, name_filter, state_filter,
                                       city_filter, specialty_filter,
                                       postalcode_filter)
            bundle["entry"] = entries
            return jsonify(bundle)
    except Exception:
        pass
    return jsonify({"error": "Failed to load more"}), 500


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    # Local dev entry point. In production, Render / any WSGI host runs the
    # Procfile's `gunicorn app:app` command instead, so this block is skipped.
    # Debug defaults ON locally; set FLASK_DEBUG=false to turn it off.
    port  = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() != "false"
    app.run(debug=debug, port=port, host="0.0.0.0")