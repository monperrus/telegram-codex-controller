import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("telegram-codex-control.py")
SPEC = importlib.util.spec_from_file_location("telegram_codex_control", MODULE_PATH)
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class UsageFormattingTests(unittest.TestCase):
    def test_formats_quota_and_credits_without_token_totals(self):
        output = controller.format_usage(
            {
                "rateLimits": {
                    "planType": "pro",
                    "primary": {"usedPercent": 35, "resetsAt": 0},
                    "secondary": {"usedPercent": 80},
                    "credits": {"hasCredits": True, "unlimited": False, "balance": "12"},
                },
                "rateLimitResetCredits": {"availableCount": 2},
            }
        )

        self.assertIn("Plan: pro", output)
        self.assertIn("Primary quota: 65% remaining (35% used)", output)
        self.assertIn("Secondary quota: 20% remaining (80% used)", output)
        self.assertIn("Credits: 12", output)
        self.assertIn("Rate-limit reset credits: 2", output)
        self.assertNotIn("Tokens:", output)

    def test_hides_unavailable_credits(self):
        output = controller.format_usage(
            {
                "rateLimits": {"credits": {"hasCredits": False, "unlimited": False}},
                "rateLimitResetCredits": {"availableCount": 0},
            }
        )

        self.assertEqual(output, "Codex usage")

        zero_balance = controller.format_usage(
            {"rateLimits": {"credits": {"hasCredits": True, "unlimited": False, "balance": "0"}}}
        )
        self.assertEqual(zero_balance, "Codex usage")

    def test_handles_missing_optional_usage_fields(self):
        self.assertEqual(controller.format_usage({"rateLimits": {}}), "Codex usage")


if __name__ == "__main__":
    unittest.main()
