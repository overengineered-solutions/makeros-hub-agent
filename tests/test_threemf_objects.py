"""Tests for parse_plate_objects — extracting skippable {id,name} from a 3MF."""

import tempfile
import unittest
import zipfile
from pathlib import Path

from makeros_hub.printers.threemf_objects import parse_plate_objects

_SLICE = "Metadata/slice_info.config"


def make_3mf(path: Path, slice_info: str | None) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")  # decoy, ignored
        if slice_info is not None:
            zf.writestr(_SLICE, slice_info)


def plate_xml(objects: str, plate_index: int = 1, label_enabled: str = "true") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="{plate_index}"/>
    <metadata key="label_object_enabled" value="{label_enabled}"/>
    {objects}
  </plate>
</config>"""


TWO_OBJECTS = (
    '<object identify_id="286" name="Cube" skipped="false"/>'
    '<object identify_id="287" name="Sphere" skipped="false"/>'
)


class TestParsePlateObjects(unittest.TestCase):
    def _write(self, slice_info):
        path = Path(self._d) / "job.3mf"
        make_3mf(path, slice_info)
        return path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._d = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_parses_id_and_name_for_the_plate(self):
        path = self._write(plate_xml(TWO_OBJECTS))
        self.assertEqual(
            parse_plate_objects(path, 1),
            [{"id": 286, "name": "Cube"}, {"id": 287, "name": "Sphere"}],
        )

    def test_label_object_disabled_yields_empty(self):
        path = self._write(plate_xml(TWO_OBJECTS, label_enabled="false"))
        self.assertEqual(parse_plate_objects(path, 1), [])

    def test_single_object_is_not_skippable(self):
        path = self._write(plate_xml('<object identify_id="5" name="Only"/>'))
        self.assertEqual(parse_plate_objects(path, 1), [])

    def test_over_64_objects_yields_empty(self):
        objs = "".join(f'<object identify_id="{i}" name="o{i}"/>' for i in range(65))
        path = self._write(plate_xml(objs))
        self.assertEqual(parse_plate_objects(path, 1), [])

    def test_selects_the_matching_plate(self):
        two_plates = """<?xml version="1.0"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="label_object_enabled" value="true"/>
    <object identify_id="1" name="A"/><object identify_id="2" name="B"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <metadata key="label_object_enabled" value="true"/>
    <object identify_id="9" name="X"/><object identify_id="10" name="Y"/>
  </plate>
</config>"""
        path = self._write(two_plates)
        self.assertEqual(parse_plate_objects(path, 2), [{"id": 9, "name": "X"}, {"id": 10, "name": "Y"}])

    def test_dedups_repeated_ids_preserving_order(self):
        objs = (
            '<object identify_id="3" name="A"/>'
            '<object identify_id="3" name="A-dup"/>'
            '<object identify_id="4" name="B"/>'
        )
        path = self._write(plate_xml(objs))
        self.assertEqual(parse_plate_objects(path, 1), [{"id": 3, "name": "A"}, {"id": 4, "name": "B"}])

    def test_non_integer_identify_id_skipped(self):
        objs = (
            '<object identify_id="x" name="bad"/>'
            '<object identify_id="7" name="good1"/>'
            '<object identify_id="8" name="good2"/>'
        )
        path = self._write(plate_xml(objs))
        self.assertEqual(parse_plate_objects(path, 1), [{"id": 7, "name": "good1"}, {"id": 8, "name": "good2"}])

    def test_oversized_slice_info_yields_empty_no_parse(self):
        # An uncompressed slice_info above the cap must be rejected BEFORE the XML
        # parser sees it (zip-bomb / DoS guard). 'x'*11MB compresses tiny but its
        # ZipInfo.file_size is >10MB, so the size gate trips.
        big = "<config>" + ("x" * (11 * 1024 * 1024)) + "</config>"
        path = self._write(big)
        self.assertEqual(parse_plate_objects(path, 1), [])

    def test_missing_slice_info_yields_empty_no_raise(self):
        path = self._write(None)  # zip without slice_info.config
        self.assertEqual(parse_plate_objects(path, 1), [])

    def test_unreadable_file_yields_empty_no_raise(self):
        path = Path(self._d) / "nope.3mf"
        path.write_bytes(b"not a zip")
        self.assertEqual(parse_plate_objects(path, 1), [])

    def test_plate_not_found_yields_empty(self):
        path = self._write(plate_xml(TWO_OBJECTS, plate_index=1))
        self.assertEqual(parse_plate_objects(path, 3), [])


if __name__ == "__main__":
    unittest.main()
