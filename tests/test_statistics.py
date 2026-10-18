import pytest

from aioecam.ecam.frame import Frame
from aioecam.ecam.statistics import interpret, parse_statistics, statistics_request
from aioecam.exceptions import FrameError

# Real A2 replies from an ECAM610.75.MB (taken from the cremalink fork's tests).
MAINTENANCE = (
    "d041a20f0064000ef8a300650000019a00690000001e006a0029bfa1006c00000016"
    "006d0000d46c006f000000120073000010e5007400000660" + "0bb80000018cd572"
)
BEVERAGES = (
    "d041a20f0bb80000018c0bb90000146f0bba000000020bbb000000b40bbc00000154"
    "0bbd000000610bbe000000030bbf000000000bc0000000010bc100000030100d"
)
SPARSE = "d017a20fa806000002d2a807000000f5a808000000008d1b"


def test_request_matches_known_frames() -> None:
    assert statistics_request(100, 10).encode().hex() == "0d08a20f00640a2397"
    assert statistics_request(3000, 10).encode().hex() == "0d08a20f0bb80a832c"


@pytest.mark.parametrize(("start", "count"), [(-1, 1), (0x10000, 1), (100, 0), (100, 11)])
def test_request_bounds(start: int, count: int) -> None:
    with pytest.raises(ValueError):
        statistics_request(start, count)


def test_parse_maintenance_page() -> None:
    assert parse_statistics(Frame.from_hex(MAINTENANCE)) == {
        100: 981155, 101: 410, 105: 30, 106: 2736033, 108: 22,
        109: 54380, 111: 18, 115: 4325, 116: 1632, 3000: 396,
    }  # fmt: skip


def test_parse_sparse_ids() -> None:
    assert parse_statistics(Frame.from_hex(SPARSE)) == {43014: 722, 43015: 245, 43016: 0}


def test_parse_rejects_other_frames() -> None:
    with pytest.raises(FrameError, match="not an A2"):
        parse_statistics(Frame.from_hex("0d07840f02015512"))


def test_parse_rejects_ragged_payload() -> None:
    frame = Frame(0xD0, 0xA2, 0x0F, b"\x00\x64\x00\x00\x00")
    with pytest.raises(FrameError, match="6-byte"):
        parse_statistics(frame)


def test_interpret_soul() -> None:
    raw = parse_statistics(Frame.from_hex(MAINTENANCE)) | parse_statistics(
        Frame.from_hex(BEVERAGES)
    )
    snap = interpret("ecam61x", raw)
    assert snap.known["descale_count"] == 30
    assert snap.known["total_water_l"] == pytest.approx(1368.0165)
    assert snap.known["water_since_filter_change_l"] == pytest.approx(27.19)
    assert snap.known["total_milk_beverages"] == 5231 + 180
    # no 43010 in these pages -> derived from the four categories
    assert snap.known["total_beverages"] == 396 + 5231 + 2 + 180
    assert snap.unknown == {101: 410, 111: 18, 116: 1632}
    assert snap.raw == raw


def test_interpret_prefers_machine_total() -> None:
    snap = interpret("ecam61x", {3000: 1, 3001: 1, 3002: 1, 3003: 1, 43010: 99})
    assert snap.known["total_beverages"] == 99


def test_interpret_unmapped_family_keeps_everything_unknown() -> None:
    snap = interpret("ecam45x", {100: 5, 3005: 7})
    assert snap.known == {} and snap.unknown == {100: 5, 3005: 7}
