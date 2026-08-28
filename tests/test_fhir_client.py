"""Unit tests for the pure logic in fhir_client.py.

These tests cover the deterministic functions that operate on FHIR
resource dicts — no network calls, no payer-server dependencies.

Run with:
    pytest -v
"""

import pytest

from fhir_client import (
    _normalize_state,
    _build_practitioner_name,
    _entry_display_name,
    _entry_specialty_codes,
    _build_location_index,
    _addresses_for_entry,
    _inject_role_addresses,
    _resolve_role_names,
    _post_filter,
)


# ---------------------------------------------------------------------------
# _normalize_state — state-name → 2-letter abbreviation
# ---------------------------------------------------------------------------
class TestNormalizeState:

    def test_two_letter_code_passes_through(self):
        assert _normalize_state("TX") == "TX"
        assert _normalize_state("tx") == "TX"

    def test_full_state_name_is_abbreviated(self):
        assert _normalize_state("Texas") == "TX"
        assert _normalize_state("california") == "CA"
        assert _normalize_state("New York") == "NY"

    def test_empty_input_returns_empty(self):
        assert _normalize_state("") == ""
        assert _normalize_state("   ") == ""
        assert _normalize_state(None) == ""

    def test_unknown_state_returns_input_uppercased(self):
        # Falls back to the user's input (uppercased) rather than crashing
        assert _normalize_state("Atlantis") == "ATLANTIS"


# ---------------------------------------------------------------------------
# _build_practitioner_name — extract a display name from FHIR HumanName[]
# ---------------------------------------------------------------------------
class TestBuildPractitionerName:

    def test_uses_text_when_present(self):
        resource = {"name": [{"text": "Dr. Eric Smith"}]}
        assert _build_practitioner_name(resource) == "Dr. Eric Smith"

    def test_falls_back_to_given_plus_family(self):
        resource = {"name": [{"given": ["Eric", "James"], "family": "Smith"}]}
        assert _build_practitioner_name(resource) == "Eric James Smith"

    def test_returns_none_when_no_name(self):
        assert _build_practitioner_name({}) is None
        assert _build_practitioner_name({"name": []}) is None


# ---------------------------------------------------------------------------
# _entry_display_name — extract the user-facing name from a Bundle entry
# ---------------------------------------------------------------------------
class TestEntryDisplayName:

    def test_practitioner_uses_built_name(self, practitioner_eric_cardio):
        assert _entry_display_name(practitioner_eric_cardio) == "Eric Smith"

    def test_practitioner_role_uses_practitioner_display(
            self, practitioner_role_with_location):
        assert _entry_display_name(practitioner_role_with_location) == "Eric Brown"

    def test_unknown_resource_type_returns_none(self):
        assert _entry_display_name({"resource": {"resourceType": "Foo"}}) is None


# ---------------------------------------------------------------------------
# _entry_specialty_codes — collect NUCC taxonomy codes from an entry
# ---------------------------------------------------------------------------
class TestEntrySpecialtyCodes:

    def test_practitioner_qualification_codes_are_collected(
            self, practitioner_eric_cardio):
        codes = _entry_specialty_codes(practitioner_eric_cardio)
        assert "207RC0000X" in codes

    def test_practitioner_role_specialty_codes_are_collected(
            self, practitioner_role_with_location):
        codes = _entry_specialty_codes(practitioner_role_with_location)
        assert "207RC0000X" in codes

    def test_codes_are_uppercased(self):
        entry = {"resource": {
            "resourceType": "Practitioner",
            "qualification": [{"code": {"coding": [{"code": "207rc0000x"}]}}],
        }}
        codes = _entry_specialty_codes(entry)
        # Codes are normalized to uppercase to avoid case mismatch bugs
        assert "207RC0000X" in codes


# ---------------------------------------------------------------------------
# _build_location_index + _addresses_for_entry
# ---------------------------------------------------------------------------
class TestLocationIndex:

    def test_index_keys_by_payer_and_id(
            self, location_houston, practitioner_role_with_location):
        entries = [practitioner_role_with_location, location_houston]
        index = _build_location_index(entries)
        assert ("anthem", "loc-tx-1") in index

    def test_practitioner_role_resolves_through_location_index(
            self, location_houston, practitioner_role_with_location):
        entries = [practitioner_role_with_location, location_houston]
        index = _build_location_index(entries)
        addrs = _addresses_for_entry(practitioner_role_with_location, index)
        assert len(addrs) == 1
        assert addrs[0]["state"] == "TX"
        assert addrs[0]["city"] == "Houston"

    def test_practitioner_returns_inline_addresses(self, practitioner_eric_cardio):
        addrs = _addresses_for_entry(practitioner_eric_cardio, {})
        assert addrs[0]["state"] == "TX"


# ---------------------------------------------------------------------------
# _inject_role_addresses — copy linked Location.address onto the role
# ---------------------------------------------------------------------------
class TestInjectRoleAddresses:

    def test_role_gets_address_from_linked_location(
            self, location_houston, practitioner_role_with_location):
        entries = [practitioner_role_with_location, location_houston]
        _inject_role_addresses(entries)
        role = practitioner_role_with_location["resource"]
        assert role.get("address")
        assert role["address"][0]["state"] == "TX"

    def test_role_without_locations_in_bundle_gets_no_address(
            self, practitioner_role_with_location):
        # The Location resource is NOT included in the bundle
        entries = [practitioner_role_with_location]
        _inject_role_addresses(entries)
        role = practitioner_role_with_location["resource"]
        assert not role.get("address")


# ---------------------------------------------------------------------------
# _resolve_role_names — inject Practitioner names onto PractitionerRole.display
# ---------------------------------------------------------------------------
class TestResolveRoleNames:

    def test_unresolved_role_gets_name_from_linked_practitioner(self):
        role = {
            "resource": {
                "resourceType": "PractitionerRole",
                "id": "role-2",
                "practitioner": {"reference": "Practitioner/prac-1"},
                # NO display field
            },
            "_payer_id": "cigna",
        }
        practitioner = {
            "resource": {
                "resourceType": "Practitioner",
                "id": "prac-1",
                "name": [{"text": "Eric Smith"}],
            },
            "_payer_id": "cigna",
        }
        _resolve_role_names([role, practitioner])
        assert role["resource"]["practitioner"]["display"] == "Eric Smith"

    def test_already_resolved_role_is_not_overwritten(
            self, practitioner_role_with_location):
        # practitioner_role_with_location already has display="Eric Brown"
        _resolve_role_names([practitioner_role_with_location])
        role = practitioner_role_with_location["resource"]
        assert role["practitioner"]["display"] == "Eric Brown"


# ---------------------------------------------------------------------------
# _post_filter — the universal verification layer
# ---------------------------------------------------------------------------
class TestPostFilter:

    def test_name_filter_keeps_matching_practitioner(
            self, practitioner_eric_cardio):
        result = _post_filter(
            [practitioner_eric_cardio],
            name_filter="Eric", state_filter="", city_filter="",
        )
        assert len(result) == 1

    def test_name_filter_drops_non_matching_practitioner(
            self, practitioner_eric_cardio):
        result = _post_filter(
            [practitioner_eric_cardio],
            name_filter="Xander", state_filter="", city_filter="",
        )
        assert len(result) == 0

    def test_specialty_filter_keeps_matching_codes(
            self, practitioner_eric_cardio):
        result = _post_filter(
            [practitioner_eric_cardio],
            name_filter="", state_filter="", city_filter="",
            specialty_filter="207RC0000X",
        )
        assert len(result) == 1

    def test_specialty_filter_drops_wrong_specialty(
            self, practitioner_eric_dentist):
        # Eric the dentist has only DDS in qualification — no Cardiology code
        result = _post_filter(
            [practitioner_eric_dentist],
            name_filter="", state_filter="", city_filter="",
            specialty_filter="207RC0000X",
        )
        assert len(result) == 0

    def test_state_filter_normalizes_input(self, practitioner_eric_cardio):
        # User typing 'Texas' should match an address with state='TX'
        result = _post_filter(
            [practitioner_eric_cardio],
            name_filter="", state_filter="Texas", city_filter="",
        )
        assert len(result) == 1

    def test_state_filter_drops_wrong_state(self, practitioner_eric_cardio):
        result = _post_filter(
            [practitioner_eric_cardio],
            name_filter="", state_filter="CA", city_filter="",
        )
        assert len(result) == 0

    def test_zip_filter_matches_prefix(self, practitioner_eric_cardio):
        # 77002 should match an address with postalCode 77002 (also 77002-1234)
        result = _post_filter(
            [practitioner_eric_cardio],
            name_filter="", state_filter="", city_filter="",
            postalcode_filter="77002",
        )
        assert len(result) == 1

    def test_no_address_entry_dropped_when_location_filter_active(
            self, practitioner_no_address):
        # Conservative: if we can't verify location, drop the entry
        result = _post_filter(
            [practitioner_no_address],
            name_filter="", state_filter="TX", city_filter="",
        )
        assert len(result) == 0

    def test_operation_outcome_is_dropped(self, operation_outcome_noise):
        # Error resources should never appear in user-visible results
        result = _post_filter(
            [operation_outcome_noise],
            name_filter="Eric", state_filter="", city_filter="",
        )
        assert len(result) == 0

    def test_practitioner_role_state_verified_via_linked_location(
            self, location_houston, practitioner_role_with_location):
        entries = [practitioner_role_with_location, location_houston]
        _inject_role_addresses(entries)
        result = _post_filter(
            entries,
            name_filter="", state_filter="TX", city_filter="",
        )
        # The role should pass — it links to a TX Location
        role_results = [e for e in result
                        if e["resource"].get("resourceType") == "PractitionerRole"]
        assert len(role_results) == 1
