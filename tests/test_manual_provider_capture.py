import base64
import json
from pathlib import Path
import tempfile
import unittest

from manual_provider_capture import (
    ProviderResponse,
    analyze_responses,
    detect_provider,
    extract_candidate_records,
    load_har_responses,
    sanitize_json,
    validate_event_url,
)


class ProviderDetectionTests(unittest.TestCase):
    def test_detects_supported_provider_domains(self):
        self.assertEqual(
            detect_provider("https://www.ticketmaster.com/event/123"),
            "ticketmaster",
        )
        self.assertEqual(
            detect_provider("https://www.ticketmaster.ca/event/123"),
            "ticketmaster",
        )
        self.assertEqual(
            detect_provider("https://seatgeek.com/test-tickets/123"),
            "seatgeek",
        )

    def test_rejects_other_domains_and_provider_mismatch(self):
        with self.assertRaisesRegex(ValueError, "Only Ticketmaster"):
            validate_event_url("https://example.com/event/123")
        with self.assertRaisesRegex(ValueError, "not the requested provider"):
            validate_event_url(
                "https://www.ticketmaster.com/event/123",
                "seatgeek",
            )


class SanitizationTests(unittest.TestCase):
    def test_sensitive_fields_are_removed_without_losing_ticket_data(self):
        value = {
            "section": "110",
            "price": 75,
            "authorizationToken": "secret",
            "customer": {"email": "person@example.com"},
            "nested": [{"row": "A"}],
        }
        sanitized = sanitize_json(value)
        self.assertEqual(sanitized["section"], "110")
        self.assertEqual(sanitized["price"], 75)
        self.assertNotIn("authorizationToken", sanitized)
        self.assertNotIn("customer", sanitized)


class CandidateExtractionTests(unittest.TestCase):
    def test_extracts_nested_section_price_and_quantity(self):
        payload = {
            "data": {
                "offers": [
                    {
                        "seatLocation": {
                            "sectionName": "Section 110",
                            "rowLabel": "A",
                        },
                        "pricing": {
                            "displayPrice": "$124.50",
                            "currencyCode": "USD",
                        },
                        "availableQuantity": 4,
                    }
                ]
            }
        }
        records = extract_candidate_records(
            payload,
            provider="ticketmaster",
            response_url="https://availability.ticketmaster.com/inventory",
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.section, "Section 110")
        self.assertEqual(record.row, "A")
        self.assertEqual(record.price_numeric, 124.50)
        self.assertEqual(record.quantity_numeric, 4)
        self.assertEqual(record.currency, "USD")

    def test_does_not_treat_event_level_price_as_section_inventory(self):
        payload = {"event": {"minimumPrice": 50, "maximumPrice": 200}}
        records = extract_candidate_records(
            payload,
            provider="ticketmaster",
            response_url="https://www.ticketmaster.com/event",
        )
        self.assertEqual(records, [])


class HARAnalysisTests(unittest.TestCase):
    def test_reads_plain_and_base64_json_and_ignores_other_provider(self):
        ticketmaster_payload = json.dumps(
            {
                "inventory": [
                    {
                        "section": "201",
                        "row": "B",
                        "price": {"amount": 88, "currency": "USD"},
                        "quantity": 2,
                    }
                ]
            }
        )
        seatgeek_payload = json.dumps(
            {"listings": [{"section": "10", "price": 20}]}
        )
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "url": "https://inventory.ticketmaster.com/offers"
                        },
                        "response": {
                            "status": 200,
                            "content": {
                                "mimeType": "application/json",
                                "text": base64.b64encode(
                                    ticketmaster_payload.encode("utf-8")
                                ).decode("ascii"),
                                "encoding": "base64",
                            },
                        },
                    },
                    {
                        "request": {
                            "url": "https://seatgeek.com/api/listings"
                        },
                        "response": {
                            "status": 200,
                            "content": {
                                "mimeType": "application/json",
                                "text": seatgeek_payload,
                            },
                        },
                    },
                ]
            }
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.har"
            path.write_text(json.dumps(har), encoding="utf-8")
            responses = load_har_responses(path, "ticketmaster")

        self.assertEqual(len(responses), 1)
        report = analyze_responses(
            responses,
            provider="ticketmaster",
            event_url="https://www.ticketmaster.com/event/123",
            capture_mode="har",
            include_sanitized_payloads=True,
        )
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["candidates"][0]["section"], "201")
        self.assertEqual(report["candidates"][0]["price_numeric"], 88.0)
        self.assertEqual(report["candidates"][0]["quantity_numeric"], 2)
        self.assertEqual(len(report["sanitized_payloads"]), 1)

    def test_non_ticket_json_is_not_reported(self):
        responses = [
            ProviderResponse(
                url="https://analytics.ticketmaster.com/pixel",
                status=200,
                mime_type="application/json",
                body=json.dumps({"ok": True}),
            )
        ]
        report = analyze_responses(
            responses,
            provider="ticketmaster",
            event_url="https://www.ticketmaster.com/event/123",
            capture_mode="har",
            include_sanitized_payloads=False,
        )
        self.assertEqual(report["candidate_count"], 0)
        self.assertEqual(report["json_responses_parsed"], 0)


if __name__ == "__main__":
    unittest.main()
