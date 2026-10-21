import unittest

from tests.helpers import OTHER, SAMPLE, SAMPLE2
from detectors import DetectionConfig, detect, extract_solana_addresses, is_valid_solana_address, validate_regex


class SolanaDetection(unittest.TestCase):
    def test_valid_address_in_sentence_with_punctuation(self):
        self.assertEqual(extract_solana_addresses(f"gem alert!! CA: {SAMPLE}, send it."), [SAMPLE])
        self.assertEqual(extract_solana_addresses(f"({SAMPLE})"), [SAMPLE])
        self.assertEqual(extract_solana_addresses(f"https://solscan.io/account/{SAMPLE}?x=1"), [SAMPLE])

    def test_invalid_candidates_rejected(self):
        self.assertFalse(is_valid_solana_address("z" * 44))                      # decodes to 33 bytes
        self.assertFalse(is_valid_solana_address("short"))
        self.assertFalse(is_valid_solana_address(SAMPLE[:-1] + "0"))             # '0' is not base58
        self.assertEqual(extract_solana_addresses("Supercalifragilisticexpialidocious" * 2), [])
        self.assertEqual(extract_solana_addresses("0x" + "a1b2c3d4" * 5), [])    # EVM-style hex

    def test_embedded_in_longer_token_not_matched(self):
        self.assertEqual(extract_solana_addresses(SAMPLE + "zzzzzzzzzzzz"), [])

    def test_multiple_addresses_deduplicated_in_order(self):
        text = f"{SAMPLE} vs {OTHER} again {SAMPLE}"
        self.assertEqual(extract_solana_addresses(text, filter_known=False), [SAMPLE, OTHER])

    def test_known_program_filter_is_optional(self):
        self.assertEqual(extract_solana_addresses(SAMPLE2, filter_known=True), [])
        self.assertEqual(extract_solana_addresses(SAMPLE2, filter_known=False), [SAMPLE2])


class RobinhoodDetection(unittest.TestCase):
    cfg = DetectionConfig(keywords=("robinhood", "$HOOD"), regexes=(r"\bRH-[A-Z0-9]{4,}\b",))

    def test_keyword_case_insensitive_word_boundary(self):
        self.assertEqual(detect("Robinhood listing soon", self.cfg).robinhood, ("robinhood",))
        self.assertFalse(detect("robinhoods of sherwood", self.cfg).robinhood)
        self.assertEqual(detect("buy $hood now", self.cfg).robinhood, ("$HOOD",))

    def test_plain_rh_letters_do_not_match(self):
        self.assertFalse(detect("rh rh RH, my rhythm is fine", self.cfg).matched)

    def test_configured_regex(self):
        self.assertEqual(detect("new: RH-AB12CD", self.cfg).robinhood, ("RH-AB12CD",))

    def test_both_detectors_in_one_detection(self):
        d = detect(f"Robinhood token CA {SAMPLE}", self.cfg)
        self.assertEqual(d.solana, (SAMPLE,))
        self.assertEqual(d.robinhood, ("robinhood",))
        self.assertEqual(d.label, "Solana CA + Robinhood-related")

    def test_detectors_can_be_disabled_independently(self):
        cfg = DetectionConfig(solana_enabled=False, keywords=("robinhood",))
        d = detect(f"robinhood {SAMPLE}", cfg)
        self.assertEqual((d.solana, d.robinhood), ((), ("robinhood",)))

    def test_signature_changes_when_matches_change(self):
        a = detect(SAMPLE, self.cfg).signature
        self.assertEqual(a, detect(f"again {SAMPLE}", self.cfg).signature)
        self.assertNotEqual(a, detect(f"{SAMPLE} robinhood", self.cfg).signature)

    def test_regex_validation(self):
        self.assertIsNone(validate_regex(r"RH-\d+"))
        self.assertIsNotNone(validate_regex("("))
        self.assertIsNotNone(validate_regex(".*"))
        self.assertIsNotNone(validate_regex("x" * 500))


if __name__ == "__main__":
    unittest.main()
