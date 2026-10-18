import pytest

from aioecam.lan.crypto import LanSession, SessionError

KEY = "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdA=="
PARAMS = {"random_1": "abcdefghijklmnop", "random_2": "QUJDREVGR0hJSktM",
          "time_1": "123456", "time_2": "1700000000000"}  # fmt: skip

# Known answers produced by cremalink's independent implementation (protocol.py).
VECTOR = {
    "enc0": "GBSVEjtqqhaSPywljdlOB/s8TquMcJkrv2WYqQ2QKMQ=",
    "sign0": "whBhog+jdTd+qhw3LcC6cB0XQrOGydieS9H/wqARKuQ=",
    "enc1": "xsGCtTLrbD6PcAjidom5gikLvHSKpf/X+EzcWS64pcYlk5lTsF2nqbPupy5AfPDh",
    "sign1": "yiwy0Zqd1kwLuAPetNyXmrKGycNDx1Ni40Lh23dDR8I=",
}


def pair() -> tuple[LanSession, LanSession]:
    return LanSession(KEY, **PARAMS, role="app"), LanSession(KEY, **PARAMS, role="device")


def test_known_answer_against_cremalink() -> None:
    app = LanSession(KEY, **PARAMS)
    first = app.seal({})
    second = app.seal({"cmds": [1]})
    assert first == {"enc": VECTOR["enc0"], "sign": VECTOR["sign0"]}
    assert second == {"enc": VECTOR["enc1"], "sign": VECTOR["sign1"]}


def test_roundtrip_both_directions() -> None:
    app, device = pair()
    for i in range(3):  # several messages: the CBC chain must stay in sync
        assert device.open(app.seal({"n": i})) == {"seq_no": str(i), "data": {"n": i}}
        assert app.open(device.seal({"m": i})) == {"seq_no": str(i), "data": {"m": i}}


def test_block_aligned_plaintext_gets_full_padding_block() -> None:
    app, device = pair()
    # {"seq_no":"0","data":{"k":"x"}} is 30 bytes; pick a value that makes it exactly 32
    message = app.seal({"k": "xxx"})
    assert len(message["enc"]) == 64  # 48 bytes of ciphertext -> at least one zero byte
    assert device.open(message)["data"] == {"k": "xxx"}


def test_tampered_signature_rejected() -> None:
    app, device = pair()
    message = app.seal({"x": 1})
    message["sign"] = message["sign"][:-4] + "AAA="
    with pytest.raises(SessionError, match="signature"):
        device.open(message)


def test_wrong_key_rejected() -> None:
    app = LanSession(KEY, **PARAMS, role="app")
    other = LanSession("b3RoZXJrZXlvdGhlcmtleW90aGVya2V5MTI=", **PARAMS, role="device")
    with pytest.raises(SessionError):
        other.open(app.seal({"x": 1}))


@pytest.mark.parametrize(
    "message",
    [{}, {"enc": "not base64!", "sign": ""}, {"enc": "AAAA", "sign": ""}, {"enc": 5}],
)
def test_malformed_messages(message: dict[str, object]) -> None:
    _, device = pair()
    with pytest.raises(SessionError):
        device.open(message)
