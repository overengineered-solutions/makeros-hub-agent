"""v0.75: an RTSPS still comes from the camera gateway when it holds the printer's stream; any failure but a timeout falls
back to the direct ffmpeg path, and a timeout sends that printer direct for ten minutes."""
from unittest import mock

import pytest

from makeros_hub.printers import camera
from makeros_hub.printers.rtsp_camera import CaptureResult

JPEG = b"\xff\xd8\xff\xe0gw\xff\xd9"
X1 = {"printerId": "p1", "vendor": "bambu", "model": "X1C", "host": "1.2.3.4", "accessCode": "AbCd1234"}


class G:
    def __init__(self, answer, serves=True):
        self.answer, self._serves, self.asked = answer, serves, 0

    def serves(self, pid):
        return self._serves and pid == "p1"

    def frame(self, pid):
        self.asked += 1
        return self.answer


@pytest.fixture(autouse=True)
def _fresh_skip():
    camera._gateway_skip.clear()
    yield
    camera._gateway_skip.clear()


def _direct(jpeg=b"\xff\xd8direct"):
    return mock.patch.object(camera, "_rtsp_capture_with_reason", return_value=CaptureResult(jpeg=jpeg, reason=None, stderr_tail=""))


def test_gateway_still_wins_and_ffmpeg_never_runs():
    with mock.patch.object(camera, "GATEWAY", G((JPEG, None, ""))), _direct() as direct, \
         mock.patch.object(camera, "_rtsp_capture_frame") as legacy:
        assert camera.capture_printer_frame_with_reason(X1) == (JPEG, None, "")
        assert camera.capture_printer_frame(X1) == JPEG
    direct.assert_not_called()
    legacy.assert_not_called()


@pytest.mark.parametrize("answer", [(None, "bad-jpeg", "via the camera gateway"), (None, "unreachable", "x"), (None, "unknown", "HTTP 500")])
def test_any_other_gateway_failure_takes_the_direct_path(answer):
    with mock.patch.object(camera, "GATEWAY", G(answer)), _direct() as direct:
        assert camera.capture_printer_frame_with_reason(X1) == (b"\xff\xd8direct", None, "")
    direct.assert_called_once()


def test_a_gateway_timeout_is_the_answer_then_the_printer_goes_direct_for_a_while():
    g = G((None, "timeout", "via the camera gateway"))
    with mock.patch.object(camera, "GATEWAY", g), _direct() as direct:
        assert camera.capture_printer_frame_with_reason(X1) == (None, "timeout", "via the camera gateway")
        direct.assert_not_called()
        assert camera.capture_printer_frame_with_reason(X1)[0] == b"\xff\xd8direct"
        direct.assert_called_once()
        assert g.asked == 1


def test_not_on_the_gateway_is_todays_path():
    with mock.patch.object(camera, "GATEWAY", G((JPEG, None, ""), serves=False)), _direct() as direct:
        assert camera.capture_printer_frame_with_reason(X1)[0] == b"\xff\xd8direct"
    direct.assert_called_once()
