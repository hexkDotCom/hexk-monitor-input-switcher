import pytest

from monitor_switcher.hidpp import (
    LONG_LEN,
    SOFTWARE_ID,
    FeatureMissing,
    HidppDevice,
    HidppError,
    HidppTimeout,
)

DEV = 0x01
CHANGE_HOST_INDEX = 0x08
NAME_INDEX = 0x03


def long_reply(index, feature_index, function, params=b""):
    header = bytes([0x11, index, feature_index, (function << 4) | SOFTWARE_ID])
    return (header + params).ljust(LONG_LEN, b"\x00")


class FakeKeyboard:
    """Answers HID++ requests like a K375s in receiver slot 1."""

    features = {0x0005: NAME_INDEX, 0x1814: CHANGE_HOST_INDEX}
    name = b"K375s Keyboard"

    def __init__(self):
        self.written: list[bytes] = []
        self.pending: list[bytes] = []

    def write(self, report: bytes) -> None:
        self.written.append(report)
        index, feature_index, function = report[1], report[2], report[3] >> 4
        if index != DEV:
            # HID++ 1.0 "unknown device" error, arrives as a short report
            self.pending.append(bytes([0x10, index, 0x8F, feature_index, report[3], 0x08, 0]))
            return
        if feature_index == 0x00:
            feature = int.from_bytes(report[4:6], "big")
            idx = self.features.get(feature, 0)
            self.pending.append(long_reply(index, 0x00, 0, bytes([idx])))
        elif feature_index == NAME_INDEX and function == 0:
            self.pending.append(long_reply(index, NAME_INDEX, 0, bytes([len(self.name)])))
        elif feature_index == NAME_INDEX and function == 1:
            offset = report[4]
            chunk = self.name[offset : offset + 16]
            self.pending.append(long_reply(index, NAME_INDEX, 1, chunk))
        elif feature_index == NAME_INDEX and function == 2:
            self.pending.append(long_reply(index, NAME_INDEX, 2, bytes([0])))
        elif feature_index == CHANGE_HOST_INDEX and function == 0:
            self.pending.append(long_reply(index, CHANGE_HOST_INDEX, 0, bytes([3, 1])))
        elif feature_index == CHANGE_HOST_INDEX and function == 1:
            pass  # device leaves for the other host: no reply
        else:
            error = bytes([0x11, index, 0xFF, feature_index, report[3], 0x02])
            self.pending.append(error.ljust(LONG_LEN, b"\x00"))

    def read(self, timeout_ms: int):
        return self.pending.pop(0) if self.pending else None


@pytest.fixture
def keyboard():
    return FakeKeyboard()


def test_request_frames_are_long_reports_with_software_id(keyboard):
    HidppDevice(keyboard, DEV).feature_index(0x1814)
    assert keyboard.written == [bytes([0x11, DEV, 0x00, SOFTWARE_ID, 0x18, 0x14]).ljust(20, b"\0")]


def test_feature_index_is_cached_and_zero_means_missing(keyboard):
    device = HidppDevice(keyboard, DEV)
    assert device.feature_index(0x1814) == CHANGE_HOST_INDEX
    assert device.feature_index(0x1814) == CHANGE_HOST_INDEX
    assert device.feature_index(0x1815) is None
    assert len(keyboard.written) == 2


def test_name_kind_and_host_info(keyboard):
    device = HidppDevice(keyboard, DEV)
    assert device.name() == "K375s Keyboard"
    assert device.kind() == 0
    assert device.host_info() == (3, 1)


def test_set_host_sends_function_1_with_zero_based_host_and_waits_for_nothing(keyboard):
    HidppDevice(keyboard, DEV).set_host(2)
    assert keyboard.written[-1] == bytes(
        [0x11, DEV, CHANGE_HOST_INDEX, 0x10 | SOFTWARE_ID, 2]
    ).ljust(20, b"\0")
    assert keyboard.pending == []


def test_hidpp10_error_for_empty_receiver_slot(keyboard):
    with pytest.raises(HidppError) as err:
        HidppDevice(keyboard, 0x02).feature_index(0x1814)
    assert (err.value.protocol, err.value.code) == (1, 0x08)


def test_hidpp20_error(keyboard):
    with pytest.raises(HidppError) as err:
        HidppDevice(keyboard, DEV).request(CHANGE_HOST_INDEX, 5)
    assert (err.value.protocol, err.value.code) == (2, 0x02)


def test_missing_feature(keyboard):
    keyboard.features = {}
    with pytest.raises(FeatureMissing):
        HidppDevice(keyboard, DEV).host_info()


def test_unrelated_reports_are_ignored(keyboard):
    device = HidppDevice(keyboard, DEV)
    real_write = keyboard.write

    def write_with_noise(report):
        keyboard.pending.append(bytes([0x11, 0x02, 0x04, 0x00]).ljust(20, b"\0"))  # other slot
        keyboard.pending.append(long_reply(DEV, 0x00, 0, b"\x99")[:5])  # truncated
        keyboard.pending.append(bytes([0x11, DEV, 0x00, 0x01, 0x42]).ljust(20, b"\0"))  # other SWID
        real_write(report)

    keyboard.write = write_with_noise
    assert device.feature_index(0x1814) == CHANGE_HOST_INDEX


def test_timeout_when_device_is_silent():
    class Silent:
        def write(self, report):
            pass

        def read(self, timeout_ms):
            return None

    with pytest.raises(HidppTimeout):
        HidppDevice(Silent(), DEV, timeout=0.05).feature_index(0x1814)
