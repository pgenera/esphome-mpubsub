"""XXTEA payload encryption tests against the Python reference."""

from __future__ import annotations

import pytest

from reference import (
    ENC_MODE_NONE,
    ENC_MODE_XXTEA,
    ENCODING_PROTOBUF,
    ENCODING_RAW,
    HEADER_LEN,
    MAX_DATAGRAM,
    WireError,
    decode,
    derive_key,
    encode,
    topic_crc32,
    xxtea_ciphertext_len,
    xxtea_decrypt,
    xxtea_encrypt,
)


# --- XXTEA primitive ---------------------------------------------------------


def test_xxtea_roundtrip() -> None:
    key = list(range(8))  # arbitrary 8x uint32
    words = [0x11111111, 0x22222222, 0x33333333, 0x44444444]
    original = list(words)
    xxtea_encrypt(words, key)
    assert words != original  # actually encrypted
    xxtea_decrypt(words, key)
    assert words == original


def test_xxtea_wrong_key_does_not_recover() -> None:
    key = list(range(8))
    bad = [k ^ 0x1 for k in key]
    words = [0xDEADBEEF, 0xCAFEBABE, 0x12345678, 0x9ABCDEF0]
    original = list(words)
    xxtea_encrypt(words, key)
    xxtea_decrypt(words, bad)
    assert words != original


# --- Ciphertext length math --------------------------------------------------


@pytest.mark.parametrize(
    "plaintext_len,expected",
    [
        (0, 8),     # 0+4 < 8 floor -> 8
        (1, 8),     # 1+4 = 5 < 8 -> 8
        (3, 8),     # 3+4 = 7 < 8 -> 8
        (4, 8),     # 4+4 = 8 -> 8
        (5, 12),    # 5+4 = 9 -> 12
        (8, 12),    # 8+4 = 12 -> 12
        (9, 16),    # 9+4 = 13 -> 16
        (100, 104), # 100+4 = 104 -> 104
    ],
)
def test_xxtea_ciphertext_len(plaintext_len: int, expected: int) -> None:
    assert xxtea_ciphertext_len(plaintext_len) == expected


# --- End-to-end encode/decode -----------------------------------------------


def test_encrypted_roundtrip_raw() -> None:
    key = derive_key("hunter2")
    payload = b"hello world"
    pkt = encode("home/x", payload, encoding=ENCODING_RAW, key=key)
    crc, encoding, body = decode(pkt, key=key)
    assert crc == topic_crc32("home/x")
    assert encoding == ENCODING_RAW
    assert body == payload


def test_encrypted_roundtrip_protobuf() -> None:
    key = derive_key("topsecret")
    payload = bytes.fromhex("0d0000a8410d0000484200000000")
    pkt = encode("topic/y", payload, encoding=ENCODING_PROTOBUF, key=key)
    crc, encoding, body = decode(pkt, key=key)
    assert crc == topic_crc32("topic/y")
    assert encoding == ENCODING_PROTOBUF
    assert body == payload


def test_encrypted_empty_payload() -> None:
    """0-byte payloads still produce a valid 8-byte ciphertext (XXTEA floor)."""
    key = derive_key("k")
    pkt = encode("t", b"", key=key)
    # 12-byte header + 8-byte minimum ciphertext = 20 bytes total
    assert len(pkt) == HEADER_LEN + 8
    crc, encoding, body = decode(pkt, key=key)
    assert crc == topic_crc32("t")
    assert body == b""


def test_encrypted_header_has_zero_crc_field() -> None:
    """When encrypted, bytes 4-7 (header CRC field) must be zero on the wire.

    The real CRC32 lives at the start of the ciphertext; leaking it in
    cleartext would let a passive observer fingerprint topics.
    """
    key = derive_key("k")
    pkt = encode("home/leaky", b"x", key=key)
    assert pkt[4:8] == b"\x00\x00\x00\x00"


def test_encrypted_enc_mode_byte_is_xxtea() -> None:
    key = derive_key("k")
    pkt = encode("t", b"x", key=key)
    assert pkt[10] == ENC_MODE_XXTEA


def test_encrypted_pay_len_is_plaintext_length() -> None:
    key = derive_key("k")
    payload = b"hello"  # 5 bytes -> 12-byte ciphertext
    pkt = encode("t", payload, key=key)
    pay_len = int.from_bytes(pkt[8:10], "little")
    assert pay_len == len(payload)
    assert len(pkt) == HEADER_LEN + xxtea_ciphertext_len(len(payload))


def test_wrong_key_produces_wrong_crc() -> None:
    """The integrity check IS the recovered topic CRC: a wrong key gives a
    random CRC that won't match any subscribed topic.
    """
    key = derive_key("right")
    bad = derive_key("wrong")
    payload = b"sensitive"
    pkt = encode("home/x", payload, key=key)
    crc, _, body = decode(pkt, key=bad)
    assert crc != topic_crc32("home/x")
    assert body != payload  # body is also garbage; receiver drops on CRC mismatch


def test_decode_encrypted_without_key_raises() -> None:
    key = derive_key("k")
    pkt = encode("t", b"x", key=key)
    with pytest.raises(WireError, match="no key"):
        decode(pkt)


def test_unknown_enc_mode_rejected() -> None:
    pkt = bytearray(encode("t", b""))
    pkt[10] = 0x7F
    with pytest.raises(WireError, match="unknown enc_mode"):
        decode(bytes(pkt))


def test_encrypted_length_mismatch_rejected() -> None:
    key = derive_key("k")
    pkt = bytearray(encode("t", b"hello", key=key))
    pkt.append(0xAA)  # corrupt total length
    with pytest.raises(WireError, match="encrypted length mismatch"):
        decode(bytes(pkt), key=key)


def test_max_payload_under_encryption() -> None:
    """Largest payload that still fits the 1232-byte datagram cap when encrypted."""
    key = derive_key("k")
    # 4 (crc) + 1216 (payload) = 1220 -> roundup4 = 1220 -> fits exactly
    pkt = encode("t", b"x" * 1216, key=key)
    assert len(pkt) == MAX_DATAGRAM
    crc, _, body = decode(pkt, key=key)
    assert crc == topic_crc32("t")
    assert body == b"x" * 1216


def test_oversize_encrypted_payload_rejected() -> None:
    key = derive_key("k")
    with pytest.raises(ValueError, match="encrypted payload too large"):
        encode("t", b"x" * 1217, key=key)


def test_plaintext_decode_with_key_still_works() -> None:
    """Decoding a plaintext packet with a key set MUST return the plaintext.

    Mixed-mode deployments (some publishers encrypted, others not) are
    supported -- the decoder picks the path from the header's ENC_MODE byte.
    """
    key = derive_key("k")
    pkt = encode("t", b"plain", key=None)
    crc, _, body = decode(pkt, key=key)
    assert crc == topic_crc32("t")
    assert body == b"plain"


def test_encrypted_packet_is_not_decodable_as_plaintext() -> None:
    """An encrypted packet's body is gibberish to a plaintext-only decoder."""
    key = derive_key("k")
    pkt = encode("t", b"x" * 12, key=key)  # 12 -> 16-byte ciphertext
    # Drop the key requirement and call decode again: it correctly identifies
    # the packet as encrypted and refuses to silently return garbage.
    with pytest.raises(WireError):
        decode(pkt)
