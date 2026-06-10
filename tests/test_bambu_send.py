"""Tests for the stdlib Bambu send helper core."""

import ssl
import unittest

from makeros_hub.printers.bambu_send import (
    BambuSendError,
    ImplicitFTP_TLS,
    build_print_start_payload,
    upload_3mf,
)


class TestPrintStartPayload(unittest.TestCase):
    def test_required_fields_and_defaults(self):
        payload = build_print_start_payload("bracket.3mf", sequence_id="seq123")
        p = payload["print"]
        self.assertEqual(p["command"], "project_file")
        self.assertEqual(p["project_id"], "0")
        self.assertEqual(p["profile_id"], "0")
        self.assertEqual(p["task_id"], "0")
        self.assertEqual(p["subtask_id"], "0")
        self.assertEqual(p["param"], "Metadata/plate_1.gcode")
        self.assertEqual(p["file"], "bracket.3mf")
        self.assertEqual(p["url"], "ftp:///bracket.3mf")
        self.assertEqual(p["subtask_name"], "bracket")
        self.assertEqual(p["bed_type"], "textured_plate")
        self.assertIs(p["bed_leveling"], True)
        self.assertIs(p["bed_levelling"], True)
        self.assertIs(p["flow_cali"], False)
        self.assertIs(p["vibration_cali"], True)
        self.assertIs(p["layer_inspect"], False)
        self.assertIs(p["timelapse"], False)
        self.assertEqual(p["md5"], "")
        self.assertIs(p["use_ams"], False)
        self.assertEqual(p["ams_mapping"], [])
        self.assertIsInstance(p["sequence_id"], str)
        self.assertEqual(p["sequence_id"], "seq123")

    def test_options_override_defaults(self):
        payload = build_print_start_payload(
            "plate-set.3mf",
            plate=3,
            use_ams=True,
            ams_mapping="0,1",
            sequence_id=55,
            subtask_name="Customer order",
            bed_type="auto",
        )
        p = payload["print"]
        self.assertEqual(p["param"], "Metadata/plate_3.gcode")
        self.assertEqual(p["bed_type"], "auto")
        self.assertIs(p["use_ams"], True)
        self.assertEqual(p["ams_mapping"], [0, 1])
        self.assertEqual(p["sequence_id"], "55")
        self.assertEqual(p["subtask_name"], "Customer order")

    def test_unparseable_ams_mapping_becomes_empty(self):
        payload = build_print_start_payload(
            "plate-set.3mf",
            ams_mapping="0,nope",
            sequence_id="seq123",
        )

        self.assertEqual(payload["print"]["ams_mapping"], [])

    def test_list_ams_mapping_still_supported(self):
        payload = build_print_start_payload(
            "plate-set.3mf",
            ams_mapping=[2, 0],
            sequence_id="seq123",
        )

        self.assertEqual(payload["print"]["ams_mapping"], [2, 0])


class TestImplicitFTP(unittest.TestCase):
    def test_constructs_with_non_verifying_context(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ftp = ImplicitFTP_TLS(context=ctx)
        self.assertIsNone(ftp.sock)
        ftp.close()

    def test_upload_rejects_non_root_remote_name_without_network(self):
        with self.assertRaises(BambuSendError) as caught:
            upload_3mf("printer.local", "secret-code", __file__, "nested/file.3mf")
        self.assertNotIn("secret-code", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
