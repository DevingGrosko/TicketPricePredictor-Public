"""Keep the static buying-window label identical to the original template."""
from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class OriginalFormattingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Requires Node for the actual JavaScript formatter')
    def test_all_quarter_hour_labels_match_python_template(self):
        code=(ROOT/'static_original/bridge.js').read_text()
        even=re.search(r'^\s*(const roundEven = .*;)$',code,re.M)
        label=re.search(r'^\s*(const buyingWindowLabel = .*;)$',code,re.M)
        self.assertIsNotNone(even)
        self.assertIsNotNone(label)
        self.assertIn("textContent=buyingWindowLabel(payload.time)",code)
        hours=[i/4 for i in range(193)]
        script=even.group(1)+'\n'+label.group(1)+'\nconsole.log(JSON.stringify('+json.dumps(hours)+'.map(buyingWindowLabel)));'
        result=subprocess.run(['node','-e',script],check=True,capture_output=True,text=True,timeout=10)
        self.assertEqual(json.loads(result.stdout),['%.1f' % value for value in hours])
        self.assertEqual('%.1f' % 15.25,'15.2')


if __name__=='__main__':unittest.main()
