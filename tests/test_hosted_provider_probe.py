import unittest

from hosted_provider_probe import (
    ProbeState,
    _safe_click_text,
    classify_probe,
    detect_block_markers,
)


class HostedProviderProbeTests(unittest.TestCase):
    def state(
        self,
        *,
        statuses=(200,),
        blocked=(),
    ):
        return ProbeState(
            event_url="https://www.ticketmaster.com/example/event/123",
            final_url="https://www.ticketmaster.com/example/event/123",
            page_title="Example event",
            main_document_statuses=tuple(statuses),
            provider_statuses=tuple(statuses),
            blocked_markers=tuple(blocked),
            safe_clicks=(),
            scroll_steps=3,
            screenshot="provider_probe.png",
            elapsed_seconds=5.0,
        )

    def test_detects_common_challenge_copy(self):
        markers = detect_block_markers(
            "Pardon the interruption. Please verify you are human."
        )
        self.assertIn("pardon the interruption", markers)
        self.assertIn("verify you are human", markers)

    def test_403_and_429_are_classified_as_blocked(self):
        for status in (403, 429):
            with self.subTest(status=status):
                outcome = classify_probe(
                    {"candidate_count": 0, "json_responses_parsed": 0},
                    self.state(statuses=(status,)),
                )
                self.assertEqual(outcome, "blocked")

    def test_inventory_candidates_take_precedence_after_normal_page_load(self):
        outcome = classify_probe(
            {"candidate_count": 12, "json_responses_parsed": 2},
            self.state(),
        )
        self.assertEqual(outcome, "section_inventory_found")

    def test_json_without_sections_is_reported_separately(self):
        outcome = classify_probe(
            {"candidate_count": 0, "json_responses_parsed": 3},
            self.state(),
        )
        self.assertEqual(outcome, "provider_json_found_no_section_records")

    def test_page_without_provider_json_is_not_mislabeled_as_blocked(self):
        outcome = classify_probe(
            {"candidate_count": 0, "json_responses_parsed": 0},
            self.state(),
        )
        self.assertEqual(outcome, "page_loaded_no_inventory_json")

    def test_only_non_purchasing_inventory_actions_are_allowed(self):
        self.assertTrue(_safe_click_text("View Tickets"))
        self.assertTrue(_safe_click_text("Open Seat Map"))
        self.assertFalse(_safe_click_text("Buy Now"))
        self.assertFalse(_safe_click_text("Continue to Checkout"))
        self.assertFalse(_safe_click_text("Sign In"))


if __name__ == "__main__":
    unittest.main()
