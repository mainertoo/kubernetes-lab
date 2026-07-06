import base64
import hashlib
import hmac

import pytest

from beam.turncreds import mint_turn_credentials

URIS = ["stun:turn.example:3478", "turn:turn.example:3478?transport=udp"]


def test_username_is_expiry_colon_label():
    creds = mint_turn_credentials("s3cret", URIS, ttl_seconds=600, now=1000.0)
    assert creds["username"] == "1600:beam"
    assert creds["ttl"] == 600


def test_credential_matches_independent_hmac():
    creds = mint_turn_credentials("s3cret", URIS, ttl_seconds=600, now=1000.0)
    expected = base64.b64encode(
        hmac.new(b"s3cret", b"1600:beam", hashlib.sha1).digest()
    ).decode()
    assert creds["credential"] == expected


def test_credential_is_20_byte_sha1_digest():
    creds = mint_turn_credentials("s3cret", URIS)
    assert len(base64.b64decode(creds["credential"])) == 20


def test_uris_passed_through():
    assert mint_turn_credentials("s3cret", URIS)["uris"] == URIS


def test_empty_secret_raises():
    with pytest.raises(ValueError):
        mint_turn_credentials("", URIS)
