import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from nfl_collector import VividNFLBrowser
from nfl_metadata import extract_map_geometry_from_json, extract_map_geometry_from_svg, match_section_name


CASES = (
    ("5", ["C05"], None),
    ("C05", ["C05", "Premier Seating 5"], "C05"),
    ("C5", ["C05", "Premier Seating 5"], "C05"),
    ("3", ["B03", "Premier Seating 3"], "Premier Seating 3"),
    ("section-119", ["Loge 119"], "Loge 119"),
    ("styles-module-scss-module__row9", ["Premier Seating 9"], None),
    ("map-layer-9", ["Premier Seating 9"], None),
    ("5", ["Premier Seating 5", "Lower Level 5"], None),
    ("5", ["Premier Seating 5", "Section 5"], "Section 5"),
)


class GeometryMatchingTests(unittest.TestCase):
    def test_listing_only_json_skips_expensive_label_matching(self):
        payload = {"global": [{"productionName": "Home at Away", "viewBox": "0 0 100 100"}],
                   "tickets": [{"sectionName": str(i), "l": str(i), "p": "25", "q": "2"}
                               for i in range(2000)]}
        with patch('nfl_metadata.match_section_name', side_effect=AssertionError('No shapes to match')):
            self.assertIsNone(extract_map_geometry_from_json(payload, [str(i) for i in range(200)]))

    def test_shape_preflight_preserves_nested_inherited_path_polygon_and_rectangle(self):
        shapes = ({'SVG_Path':'M0 0 L10 0 L10 10 Z'},
                  {'Polygon':[[0,0],[10,0],[10,10],[0,10]]},
                  {'x':0,'y':0,'width':10,'height':10})
        for shape in shapes:
            with self.subTest(shape=shape):
                payload = {'viewBox':'0 0 100 100','nested':{'sectionName':'Section101','child':[shape]}}
                geometry=extract_map_geometry_from_json(payload,['Section101'])
                self.assertIsNotNone(geometry)
                self.assertEqual([row['name'] for row in geometry['sections']],['Section101'])
                self.assertTrue(geometry['sections'][0]['shapes'][0]['path'])
        keyed={'viewBox':'0 0 100 100','map':{'Section101':{'pathData':'M0 0 L10 0 L10 10 Z'}}}
        geometry=extract_map_geometry_from_json(keyed,['Section101'])
        self.assertEqual([row['name'] for row in geometry['sections']],['Section101'])

    def test_invalid_or_out_of_bounds_shapes_do_not_trigger_matching(self):
        deep={'sectionName':'Section101','d':'M0 0 L10 0 L10 10 Z'}
        for _ in range(13):deep={'nested':deep}
        invalid = ({'name':'Section101','path':'not-an-svg-path'},
                   {'name':'Section101','points':[[0,0]]},
                   {'name':'Section101','width':0,'height':10},deep)
        for payload in invalid:
            with self.subTest(payload=payload),patch('nfl_metadata.match_section_name',side_effect=AssertionError('No supported shape')):
                self.assertIsNone(extract_map_geometry_from_json(payload,['Section101']))

    def test_numeric_labels_do_not_match_suite_letter_codes_or_ui_hashes(self):
        for hint, known, expected in CASES:
            with self.subTest(hint=hint, known=known):
                self.assertEqual(match_section_name(hint, known), expected)

    def test_provider_polygon_for_premier_five_is_not_assigned_to_suite_c05(self):
        svg = "<svg viewBox='0 0 100 100'><path id='5' d='M0 0 L10 0 L10 10 Z'/></svg>"
        self.assertIsNone(extract_map_geometry_from_svg(svg, ["C05"]))
        geometry = extract_map_geometry_from_svg(svg, ["C05", "Premier Seating 5"])
        self.assertEqual([row["name"] for row in geometry["sections"]], ["Premier Seating 5"])

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the DOM matcher")
    def test_actual_dom_matcher_ignores_incidental_ancestor_numbers(self):
        # Execute the actual matcher sent to Selenium, before its DOM traversal.
        script = next(value for value in VividNFLBrowser._dom_map_geometry.__code__.co_consts
                      if isinstance(value, str) and "const known = arguments[0]" in value)
        matcher = script.split("function viewBox(svg)", 1)[0]
        source = (
            "const matcher = new Function(" + json.dumps(matcher + "return match(arguments[2]);") + ");\n"
            "const cases = " + json.dumps(CASES) + ";\n"
            "process.stdout.write(JSON.stringify(cases.map(([hint, known]) => matcher(known, '', [hint]))));"
        )
        result = subprocess.run([shutil.which("node")], input=source, text=True,
                                capture_output=True, check=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), [row[2] for row in CASES])


class NHLGeometrySmokeTests(unittest.TestCase):
    def _validate(self, geometry, **overrides):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/nhl-smoke-test.yml").read_text()
        script = workflow.split("python - <<'PY'\n", 1)[1].split("          PY", 1)[0]
        script = "\n".join(line[10:] for line in script.splitlines())
        payload = {"status": "success", "section_count": 40, "currency": "USD", "map_geometry": geometry,
                   "source_id": "123", "inventory_listing_count": 59,
                   "capture_diagnostics": {"production_id": "123", "responses": [{"status": 200}]}}
        payload.update(overrides)
        output = io.StringIO()
        with patch.object(Path, "read_text", return_value=json.dumps(payload)), contextlib.redirect_stdout(output):
            exec(compile(script, "NHL smoke validation", "exec"), {})
        return output.getvalue()

    def test_partial_geometry_keeps_valid_prices_and_reports_rink_fallback(self):
        result = self._validate({"mapped_section_count": 15, "known_section_count": 40,
                                 "coverage_ratio": 0.375,
                                 "sections": [{"shapes": [{}]} for _ in range(15)]})
        self.assertIn("geometry is partial", result)
        self.assertIn("rink fallback", result)

    def test_invalid_geometry_still_fails(self):
        with self.assertRaisesRegex(SystemExit, "geometry is invalid"):
            self._validate({"mapped_section_count": 15, "known_section_count": 40,
                            "coverage_ratio": 0.9, "sections": []})

    def test_missing_inventory_evidence_still_fails_without_geometry(self):
        with self.assertRaisesRegex(SystemExit, "inventory count"):
            self._validate(None, inventory_listing_count=0)
        with self.assertRaisesRegex(SystemExit, "do not match"):
            self._validate(None, source_id="456")


if __name__ == "__main__":
    unittest.main()
