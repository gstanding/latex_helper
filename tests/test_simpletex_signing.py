"""Unit tests for SimpletexConverter._make_headers signature algorithm.

SimpleTex's signing protocol is documented in their developer guide. The
algorithm is::

    fields = {"app-id": APP_ID, "random-str": RAND, "timestamp": TS, **extra}
    sign_src = "&".join(f"{k}={v}" for k, v in sorted(fields.items())) + "&secret=SECRET"
    sign = md5(sign_src.encode()).hexdigest()

The signature is what protects the request from tampering; if SimpleTex
changes their spec, all calls would fail silently. These tests pin the
current behaviour so a refactor catches the regression immediately.
"""
import hashlib
import unittest

from latex_helper.converter import SimpletexConverter


APP_ID = "test-app-id"
APP_SECRET = "test-app-secret-xyz"


def _build(monkeypatch_time=None, monkeypatch_random=None) -> SimpletexConverter:
    """Construct a SimpletexConverter with no LLM fallback."""
    import random
    import string
    import time as _time

    class _FixedRandom(random.Random):
        def __init__(self):
            super().__init__()
            self._calls = 0

        def choices(self, population, k=1):
            # Return the same deterministic 16-char string on every call
            return [list("fixedrandom12345")[:k][0] if False else "f" * k]

    # Patch the module-level random. SimpletexConverter._make_headers uses
    # `random.choices` and `time.time` directly. We patch them via monkeypatch
    # in the individual test methods.
    return SimpletexConverter(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        api_host="https://example.invalid",
        llm_converter=None,
    )


class SignatureShape(unittest.TestCase):
    def test_returns_four_required_keys(self):
        c = _build()
        h = c._make_headers()
        for key in ("app-id", "random-str", "timestamp", "sign"):
            self.assertIn(key, h)

    def test_sign_is_md5_hex_32(self):
        c = _build()
        h = c._make_headers()
        self.assertEqual(len(h["sign"]), 32)
        int(h["sign"], 16)  # raises if not hex

    def test_random_str_is_16_chars(self):
        c = _build()
        h = c._make_headers()
        self.assertEqual(len(h["random_str"] if "random_str" in h else h["random-str"]), 16)


class SignatureDeterminism(unittest.TestCase):
    def test_same_inputs_produce_same_sign(self):
        import random as _random
        import time as _time
        _random.choices = lambda population, k=1: ["a" * 16][0] if k == 16 else ["x" * k][0]
        # The above replacement doesn't preserve list semantics; use a proper
        # function that returns a list of length k.
        def _fake_choices(population, k=1):
            return ["a" * 1] * k
        _random.choices = _fake_choices
        _time.time = lambda: 1_700_000_000

        c = _build()
        h1 = c._make_headers()
        h2 = c._make_headers()
        self.assertEqual(h1["sign"], h2["sign"])
        self.assertEqual(h1["random-str"], h2["random-str"])
        self.assertEqual(h1["timestamp"], h2["timestamp"])

    def test_extra_fields_change_sign(self):
        import random as _random
        import time as _time
        def _fake_choices(population, k=1):
            return ["a"] * k
        _random.choices = _fake_choices
        _time.time = lambda: 1_700_000_000

        c = _build()
        h_no_extra = c._make_headers()
        h_with_extra = c._make_headers(extra_fields={"rec_mode": "document"})
        self.assertNotEqual(h_no_extra["sign"], h_with_extra["sign"])

    def test_extra_fields_order_does_not_matter(self):
        import random as _random
        import time as _time
        def _fake_choices(population, k=1):
            return ["a"] * k
        _random.choices = _fake_choices
        _time.time = lambda: 1_700_000_000

        c = _build()
        # Pass the same fields in two different orders
        a = {"alpha": "1", "beta": "2", "gamma": "3"}
        b = {"gamma": "3", "alpha": "1", "beta": "2"}
        h_a = c._make_headers(extra_fields=a)
        h_b = c._make_headers(extra_fields=b)
        self.assertEqual(h_a["sign"], h_b["sign"])


class SignatureValue(unittest.TestCase):
    def test_sign_matches_documented_md5_algorithm(self):
        import random as _random
        import time as _time
        # Pin both time and randomness so we can hand-compute the expected sign.
        def _fake_choices(population, k=1):
            return ["z" * 1] * k  # we'll override per-test below
        # Use a fully deterministic 16-char string
        def _fake_choices_str(population, k=1):
            return list("fixedrandom1234")[:k]
        _random.choices = _fake_choices_str
        _time.time = lambda: 1_700_000_000

        c = _build()
        h = c._make_headers()

        # Reconstruct the canonical sign string per the documented algorithm
        expected_fields = {
            "app-id": APP_ID,
            "random-str": "fixedrandom1234",
            "timestamp": "1700000000",
        }
        sign_src = "&".join(f"{k}={v}" for k, v in sorted(expected_fields.items()))
        sign_src += f"&secret={APP_SECRET}"
        expected_sign = hashlib.md5(sign_src.encode()).hexdigest()

        self.assertEqual(h["sign"], expected_sign)

    def test_secret_change_changes_sign(self):
        import random as _random
        import time as _time
        def _fake_choices(population, k=1):
            return list("fixedrandom1234")[:k]
        _random.choices = _fake_choices
        _time.time = lambda: 1_700_000_000

        c1 = SimpletexConverter(
            app_id=APP_ID, app_secret="secret-A", api_host="https://x", llm_converter=None
        )
        c2 = SimpletexConverter(
            app_id=APP_ID, app_secret="secret-B", api_host="https://x", llm_converter=None
        )
        self.assertNotEqual(c1._make_headers()["sign"], c2._make_headers()["sign"])


if __name__ == "__main__":
    unittest.main()
