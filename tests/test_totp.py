"""One-time codes, checked against the RFC rather than against themselves.

A wrong TOTP implementation fails in one direction only: it refuses valid
codes. Nobody gets in, somebody files a bug, it gets fixed. The dangerous
version is the one that is *almost* right, and there is no way to tell almost
from exactly by reading your own output.

So the vectors below are RFC 6238 Appendix B, reproduced, not values this code
printed about itself. They are why writing TOTP here instead of taking a
dependency is defensible: the correctness is checkable.
"""
import base64
import time

import pytest

from core import totp

# RFC 6238 Appendix B. The secret is the ASCII string "12345678901234567890";
# the published codes are eight digits, so the tests ask for eight.
_RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize("seconds, expected", [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
])
def test_the_codes_match_the_published_vectors(seconds, expected):
    assert totp.code_for_counter(
        _RFC_SECRET, seconds // totp.STEP_SECONDS, digits=8,
    ) == expected


# ── the secret ───────────────────────────────────────────────────────────────


def test_a_fresh_secret_is_base32_and_long_enough():
    secret = totp.new_secret()

    assert len(base64.b32decode(secret + "=" * (-len(secret) % 8))) == 20


def test_two_secrets_differ():
    """A generator that repeats is the worst defect here and the cheapest to
    check."""
    assert len({totp.new_secret() for _ in range(64)}) == 64


def test_a_secret_survives_how_people_actually_type_it():
    """Apps show the secret in spaced lowercase groups, and somebody will
    paste it that way."""
    secret = totp.new_secret()
    mangled = " ".join(secret.lower()[i:i + 4] for i in range(0, len(secret), 4))

    assert totp.code_at(mangled, 1000) == totp.code_at(secret, 1000)


def test_a_secret_that_is_not_base32_is_refused(  ):
    with pytest.raises(totp.TOTPError):
        totp.code_at("not base32 at all!!", 1000)


def test_an_empty_secret_is_refused():
    with pytest.raises(totp.TOTPError):
        totp.code_at("", 1000)


# ── verifying ────────────────────────────────────────────────────────────────


def test_the_current_code_verifies():
    secret = totp.new_secret()
    now = time.time()

    assert totp.matching_counter(secret, totp.code_at(secret, now), when=now) is not None


def test_a_wrong_code_does_not():
    secret = totp.new_secret()
    now = time.time()
    wrong = "000000" if totp.code_at(secret, now) != "000000" else "111111"

    assert totp.matching_counter(secret, wrong, when=now) is None


@pytest.mark.parametrize("drift", [-30, 30])
def test_a_clock_half_a_minute_out_still_works(drift):
    """A phone that drifts is common and is not an attack."""
    secret = totp.new_secret()
    now = 1_700_000_000

    code = totp.code_at(secret, now + drift)

    assert totp.matching_counter(secret, code, when=now) is not None


@pytest.mark.parametrize("drift", [-90, 90])
def test_a_code_from_further_away_is_refused(drift):
    """The window is a replay opportunity, so it stays narrow."""
    secret = totp.new_secret()
    now = 1_700_000_000

    code = totp.code_at(secret, now + drift)

    assert totp.matching_counter(secret, code, when=now) is None


def test_verification_returns_which_step_the_code_belongs_to():
    """Not a boolean. The caller needs the step to refuse a code it already
    accepted: without that a code stays usable for its whole window, and
    somebody who read it over a shoulder can walk to a machine and use it."""
    secret = totp.new_secret()
    now = 1_700_000_000

    counter = totp.matching_counter(secret, totp.code_at(secret, now), when=now)

    assert counter == totp.counter_at(now)


def test_a_code_of_the_wrong_length_is_refused():
    secret = totp.new_secret()

    assert totp.matching_counter(secret, "12345", when=1000) is None
    assert totp.matching_counter(secret, "1234567", when=1000) is None
    assert totp.matching_counter(secret, "", when=1000) is None
    assert totp.matching_counter(secret, "abcdef", when=1000) is None


def test_codes_are_compared_in_constant_time():
    """Read from the source, because this is a property of *how long* the
    comparison takes and no black-box test can observe it. A comparison that
    returns on the first wrong digit tells whoever is guessing how much of the
    code was right, one digit at a time, and six digits fall quickly to that.

    A mutation replacing `compare_digest` with `==` passes every behavioural
    test in this file, which is exactly why this one reads the code instead.
    """
    import inspect

    source = inspect.getsource(totp.matching_counter)

    assert "hmac.compare_digest" in source
    # `==` between the generated code and the input is the shape being ruled
    # out; the loop must not fall back to it.
    assert "digits) == text" not in source


def test_the_code_may_arrive_with_a_space_in_the_middle():
    """Apps display `123 456`, and people copy what they see."""
    secret = totp.new_secret()
    now = 1_700_000_000
    code = totp.code_at(secret, now)

    spaced = f"{code[:3]} {code[3:]}"

    assert totp.matching_counter(secret, spaced, when=now) is not None


# ── what the app is handed ───────────────────────────────────────────────────


def test_the_uri_carries_everything_an_app_needs():
    secret = totp.new_secret()

    uri = totp.provisioning_uri(secret, account="rui@demo-firm.example", issuer="Demo & Associados")

    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "digits=6" in uri and "period=30" in uri and "algorithm=SHA1" in uri


def test_the_uri_escapes_a_name_with_characters_that_would_break_it():
    """The firm's name has an ampersand in it, which ends a query string."""
    uri = totp.provisioning_uri("ABCDEFGH", account="a b@c.br", issuer="Demo & Associados")

    assert "&issuer=Demo%20%26%20Associados" in uri
    assert " " not in uri
