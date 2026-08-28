"""Shared pytest fixtures — small, realistic FHIR resource snippets that
match the shape of what the real payer APIs actually return."""

import pytest


@pytest.fixture
def practitioner_eric_cardio():
    """A Practitioner resource with name 'Eric Smith' and a Cardiology
    qualification code, with one TX address."""
    return {
        "resource": {
            "resourceType": "Practitioner",
            "id": "prac-eric-cardio",
            "name": [{"text": "Eric Smith", "given": ["Eric"], "family": "Smith"}],
            "qualification": [{
                "code": {
                    "coding": [{
                        "system": "http://nucc.org/provider-taxonomy",
                        "code": "207RC0000X",
                        "display": "Cardiovascular Disease Physician",
                    }]
                }
            }],
            "address": [
                {"city": "Houston", "state": "TX", "postalCode": "77002"},
            ],
        },
        "search": {"mode": "match"},
        "_payer_id": "cigna",
    }


@pytest.fixture
def practitioner_eric_dentist():
    """An Eric who is a DENTIST (qualification only carries 'DDS' degree,
    no NUCC taxonomy code). Used to verify specialty filtering correctly
    drops this provider when Cardiology is requested."""
    return {
        "resource": {
            "resourceType": "Practitioner",
            "id": "prac-eric-dentist",
            "name": [{"text": "Eric Jones", "given": ["Eric"], "family": "Jones"}],
            "qualification": [{
                "code": {
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/v2-0360",
                        "code": "DDS",
                        "display": "Doctor of Dental Surgery",
                    }]
                }
            }],
            "address": [
                {"city": "Austin", "state": "TX", "postalCode": "78701"},
            ],
        },
        "search": {"mode": "match"},
        "_payer_id": "uhc",
    }


@pytest.fixture
def practitioner_no_address():
    """A Practitioner with NO address — used to verify location filters
    correctly drop entries we can't verify."""
    return {
        "resource": {
            "resourceType": "Practitioner",
            "id": "prac-no-addr",
            "name": [{"text": "Eric Ghost", "given": ["Eric"], "family": "Ghost"}],
            "qualification": [{
                "code": {
                    "coding": [{
                        "system": "http://nucc.org/provider-taxonomy",
                        "code": "207RC0000X",
                    }]
                }
            }],
        },
        "search": {"mode": "match"},
        "_payer_id": "humana",
    }


@pytest.fixture
def practitioner_role_with_location():
    """A PractitionerRole that references a Location by ID. Used to test
    address resolution from the bundle's Location resources."""
    return {
        "resource": {
            "resourceType": "PractitionerRole",
            "id": "role-1",
            "practitioner": {"display": "Eric Brown"},
            "specialty": [{
                "coding": [{
                    "system": "http://nucc.org/provider-taxonomy",
                    "code": "207RC0000X",
                    "display": "Cardiovascular Disease Physician",
                }]
            }],
            "location": [{"reference": "Location/loc-tx-1"}],
        },
        "search": {"mode": "match"},
        "_payer_id": "anthem",
    }


@pytest.fixture
def location_houston():
    return {
        "resource": {
            "resourceType": "Location",
            "id": "loc-tx-1",
            "address": {
                "city": "Houston", "state": "TX", "postalCode": "77002"
            },
        },
        "search": {"mode": "include"},
        "_payer_id": "anthem",
    }


@pytest.fixture
def operation_outcome_noise():
    """An error response that some payers (Cigna) return inside the bundle.
    Should be silently dropped by the post-filter."""
    return {
        "resource": {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "fatal", "code": "processing"}],
        },
        "search": {"mode": "match"},
        "_payer_id": "cigna",
    }
