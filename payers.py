"""
Payer Registry — Provider Finder v2
====================================
Five active payers. URLs verified against official documentation:

  Cigna: developer.cigna.com/docs/service-apis/provider-directory
    FHIR base: https://fhir.cigna.com/ProviderDirectory/v1
    Auth: NONE (public, registration optional for higher rate limits)

  UnitedHealthcare: uhc.com/legal/interoperability-apis
    FHIR base: https://flex.optum.com/fhirpublic/R4
    Auth: NONE

  Capital BlueCross: capbluecross.com/.../provider-directory-api-access-guide
    FHIR base: https://providerdirectory-api.capbluecross.com/r4
    Auth: NONE (truly public per docs)

  Humana: developers.humana.com/apis/marketplace#/hl7-apis
    FHIR base: https://fhir.humana.com/api
    Auth: NONE (public)

  Anthem / Elevance Health: IO105 v15.0
    FHIR base: https://totalview.healthos.elevancehealth.com/resources/unregistered/api/v1/fhir/cms_mandate/mcd/
    Auth: OAuth 2.0 client_credentials

REMOVED:
  - Centene: public FHIR query backend chronically returns HTTP 504.
  - Aetna:   no fully-public endpoint; credentials registration not completed.
"""

import os


# ----------------------------------------------------------------------
# Auth methods
# ----------------------------------------------------------------------
AUTH_NONE = "none"             # Truly public endpoint, no token needed
AUTH_CLIENT_CRED = "client_credentials"   # Server-to-server OAuth 2.0


PAYERS = {
    # ==================================================================
    # OPEN / PUBLIC endpoints (no auth needed)
    # ==================================================================

    "cigna": {
        "id":             "cigna",
        "display_name":   "Cigna",
        "short_name":     "Cigna",
        "tagline":        "Global health services company",
        "fhir_base":      "https://fhir.cigna.com/ProviderDirectory/v1",
        "auth":           AUTH_NONE,
        "color":          "#005CB9",
        "accent":         "#1976D2",
        "logo_letter":    "C",
        "members":        "20M+",
    },

    "uhc": {
        "id":             "uhc",
        "display_name":   "UnitedHealthcare",
        "short_name":     "UHC",
        "tagline":        "Largest US health insurer",
        "fhir_base":      "https://flex.optum.com/fhirpublic/R4",
        "auth":           AUTH_NONE,
        "color":          "#002677",
        "accent":         "#0F4FA8",
        "logo_letter":    "U",
        "members":        "50M+",
    },

    "capbluecross": {
        "id":             "capbluecross",
        "display_name":   "Capital BlueCross",
        "short_name":     "CapBlue",
        "tagline":        "Pennsylvania Blue Cross plan",
        "fhir_base":      "https://providerdirectory-api.capbluecross.com/r4",
        "auth":           AUTH_NONE,
        "color":          "#0079C2",
        "accent":         "#00A0DC",
        "logo_letter":    "B",
        "members":        "1M+",
    },

    # NOTE: Centene was removed — its public FHIR query backend is chronically
    # unreliable (HTTP 504 on nearly every search while /metadata still responds).

    # ==================================================================
    # AUTHENTICATED endpoints (require client_credentials)
    # Set these env vars in .env to enable. Without env vars, the payer
    # will gracefully appear "offline" rather than crashing the app.
    # ==================================================================

    "elevance": {
        "id":             "elevance",
        "display_name":   "Anthem (Elevance Health)",
        "short_name":     "Anthem",
        "tagline":        "Multi-state Blue Cross plans",
        "fhir_base":      "https://totalview.healthos.elevancehealth.com/resources/unregistered/api/v1/fhir/cms_mandate/mcd/",
        "auth":           AUTH_CLIENT_CRED,
        "client_id":      os.getenv("ELEVANCE_CLIENT_ID"),
        "client_secret":  os.getenv("ELEVANCE_CLIENT_SECRET"),
        "token_url":      os.getenv("ELEVANCE_TOKEN_URL"),  # provided via secure email
        "color":          "#005EB8",
        "accent":         "#0080D0",
        "logo_letter":    "E",
        "members":        "47M+",
    },

    # NOTE: Aetna was removed — it has no fully-public FHIR endpoint and the
    # registration for credentials was never completed.

    "humana": {
        "id":             "humana",
        "display_name":   "Humana",
        "short_name":     "Humana",
        "tagline":        "Medicare Advantage leader",
        "fhir_base":      "https://fhir.humana.com/api",
        "auth":           AUTH_NONE,
        "color":          "#5B8F22",
        "accent":         "#7BAA45",
        "logo_letter":    "H",
        "members":        "17M+",
    },
}


# ----------------------------------------------------------------------
# Specialty taxonomy (NUCC codes) for the browse page
# ----------------------------------------------------------------------
SPECIALTIES = [
    {"name": "Family Medicine",   "code": "207Q00000X", "icon": "🏥", "color": "from-blue-500 to-cyan-500"},
    {"name": "Internal Medicine", "code": "207R00000X", "icon": "💊", "color": "from-emerald-500 to-teal-500"},
    {"name": "Pediatrics",        "code": "208000000X", "icon": "👶", "color": "from-pink-500 to-rose-500"},
    {"name": "Cardiology",        "code": "207RC0000X", "icon": "❤️", "color": "from-red-500 to-orange-500"},
    {"name": "Dermatology",       "code": "207N00000X", "icon": "🧴", "color": "from-amber-500 to-yellow-500"},
    {"name": "Psychiatry",        "code": "2084P0800X", "icon": "🧠", "color": "from-purple-500 to-fuchsia-500"},
    {"name": "Orthopedics",       "code": "207X00000X", "icon": "🦴", "color": "from-slate-500 to-gray-500"},
    {"name": "Obstetrics & Gyn",  "code": "207V00000X", "icon": "🤰", "color": "from-rose-500 to-pink-500"},
    {"name": "Neurology",         "code": "2084N0400X", "icon": "🧬", "color": "from-indigo-500 to-violet-500"},
    {"name": "Ophthalmology",     "code": "207W00000X", "icon": "👁️", "color": "from-cyan-500 to-blue-500"},
    {"name": "Radiology",         "code": "2085R0202X", "icon": "📷", "color": "from-violet-500 to-purple-500"},
    {"name": "Emergency Medicine","code": "207P00000X", "icon": "🚑", "color": "from-red-600 to-rose-600"},
]


def list_payers():
    return list(PAYERS.values())


def get_payer(payer_id):
    return PAYERS.get(payer_id)


def get_specialty_by_code(code):
    return next((s for s in SPECIALTIES if s["code"] == code), None)


def is_payer_configured(payer):
    """Check whether an authenticated payer has its credentials set."""
    if payer.get("auth") == AUTH_NONE:
        return True
    return bool(payer.get("client_id") and
                payer.get("client_secret") and
                payer.get("token_url"))