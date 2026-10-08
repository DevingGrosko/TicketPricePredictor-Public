"""Check the main workflow's version cleanup gate without deploying or extra packages."""
from pathlib import Path
import re
import unittest


class PublicationWorkflowTests(unittest.TestCase):
    def text(self):
        return (Path(__file__).resolve().parents[1] / '.github/workflows/free-ticket-site.yml').read_text()

    def test_reviewed_source_keeps_recurring_triggers_and_pipeline_scope(self):
        text = self.text()
        self.assertIn('  FREE_SOURCE_REF: c0f940ca1199a81306dfa87249912c6cca9e8991\n', text)
        self.assertIn("    - cron: '17,47 * * * *'", text)
        self.assertIn('    workflows: [Collect ticket prices]', text)
        self.assertIn('  group: ticketsignal-free-publication\n  cancel-in-progress: false', text)
        self.assertIn('run: python -m tools.free_live_storage preflight', text)
        self.assertIn("    if: needs.ready.outputs.run == 'true'", text)

    def test_cleanup_requires_exact_version_even_when_freshness_still_fails(self):
        deploy = self.text().split('\n  deploy:\n', 1)[1]
        self.assertIn('    timeout-minutes: 15\n', deploy)
        steps = re.split(r'(?m)^      - ', deploy)
        verify = next(step for step in steps if 'id: verification\n' in step)
        self.assertIn('EXPECTED_GENERATED_AT: ${{ needs.build.outputs.generated_at }}', verify)
        self.assertIn('FREE_LIVE_VERIFICATION_REPORT: publication-verification.json', verify)
        self.assertIn('python -u -m tools.free_live_verify', verify)
        self.assertNotIn('continue-on-error', verify)
        self.assertNotIn('secrets.', verify)
        cleanup = next(step for step in steps if 'remove-current' in step)
        self.assertIn("if: always() && steps.verification.outputs.version_verified == 'true'", cleanup)
        self.assertNotIn('outcome', cleanup)
        self.assertNotIn('success()', cleanup)
        evidence = next(step for step in steps if 'name: ticketsignal-free-publication-verification' in step)
        self.assertIn('if: always()', evidence)
        self.assertIn('path: publication-verification.json', evidence)
        self.assertIn('retention-days: 1', evidence)
        self.assertNotIn('env:', evidence)
        self.assertNotIn('secrets.', evidence)


if __name__ == '__main__':
    unittest.main()
