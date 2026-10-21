"""Guards the one manual step when a new model ships: the pinned ids in
plugin/hooks/register.ts (MODEL_IDS) must each have a row in plugin/hooks/prices.json,
and the classifier/counterfactual models must be priced too. Stdlib only."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTER = (ROOT / "plugin/hooks/register.ts").read_text()
PRICES = json.loads((ROOT / "plugin/hooks/prices.json").read_text())


def model_ids():
    block = re.search(r"const MODEL_IDS[^{]*\{(.*?)\}", REGISTER, re.S)
    assert block, "MODEL_IDS not found in register.ts"
    return dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))


class ModelIdsPriced(unittest.TestCase):
    def test_every_tier_is_pinned(self):
        self.assertEqual(set(model_ids()), {"haiku", "sonnet", "opus"})

    def test_every_pinned_id_has_a_price_row(self):
        for tier, mid in model_ids().items():
            self.assertIn(mid, PRICES["models"], f"{tier} -> {mid}: add it to prices.json")

    def test_classifier_and_counterfactual_are_priced(self):
        for key in ("classifier_model", "counterfactual_model"):
            self.assertIn(PRICES[key], PRICES["models"], key)

    def test_classifier_is_the_pinned_haiku(self):
        self.assertEqual(PRICES["classifier_model"], model_ids()["haiku"])


if __name__ == "__main__":
    unittest.main()
