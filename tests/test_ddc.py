import pytest

from monitor_switcher import ddc

# Capabilities string reported by the MSI MPG 321CURX (real device output).
MSI_CAPS = (
    "(prot(monitor)type(lcd)model(FALCON)cmds(01 02 03 07 0C 4E F3 E3)vcp(02 04 05 08 0B 0C 10 "
    "12 14(01 04 05 06 07 08 0A 0B) 16 18 1A 6C 6E 70 AC AE B6 C0 C6 C8 C9 CA CC(00 01 02 03 "
    "04 05 06 07 08 09 0A 0B 0C 0D 0F 12 13 14 16 18 19 1A 1D 1E 21 23 25) D6(05) "
    "DC(00 01 02 03 04) DF 60(11 12 0F 10) 62 8D FF)mswhql(1)mccs_ver(2.0)asset_eep(32)"
    "mpu_ver(01))"
)


def test_supported_inputs_from_msi_caps():
    assert ddc.supported_inputs(MSI_CAPS) == [0x11, 0x12, 0x0F, 0x10]


def test_supported_inputs_missing():
    assert ddc.supported_inputs("(vcp(02 10 12))") == []
    assert ddc.supported_inputs("garbage") == []


class FakeMonitor:
    def __init__(self, pnp_id, answers=True):
        self.pnp_id = pnp_id
        self.description = "Generic PnP Monitor"
        self.answers = answers

    def get_input(self):
        if not self.answers:
            raise ddc.DdcError("no reply")
        return 0x0F


def test_select_by_pnp_id():
    laptop, msi = FakeMonitor("BOE0A1B", answers=False), FakeMonitor("MSI3DD6")
    assert ddc.select_monitor([laptop, msi], "msi3dd6") is msi


def test_select_only_ddc_capable_monitor_without_id():
    laptop, msi = FakeMonitor("BOE0A1B", answers=False), FakeMonitor("MSI3DD6")
    assert ddc.select_monitor([laptop, msi], None) is msi


def test_select_fails_when_missing_or_ambiguous():
    with pytest.raises(ddc.DdcError, match="not found"):
        ddc.select_monitor([FakeMonitor("BOE0A1B")], "MSI3DD6")
    with pytest.raises(ddc.DdcError, match="several"):
        ddc.select_monitor([FakeMonitor("MSI3DD6"), FakeMonitor("DEL4321")], None)
