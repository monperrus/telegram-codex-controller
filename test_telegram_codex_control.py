import importlib.util
import pathlib
import subprocess
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


class ModelPickerTests(unittest.TestCase):
    MODELS = [
        {"model": "gpt-5.6-sol", "displayName": "GPT-5.6 Sol"},
        {"model": "gpt-5.6-luna", "displayName": "GPT-5.6 Luna"},
    ]

    def test_picker_marks_the_selected_model_and_uses_inline_callbacks(self):
        text, markup = controller.model_picker(self.MODELS, "gpt-5.6-luna")

        self.assertIn("Current: gpt-5.6-luna", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "model:gpt-5.6-sol")
        self.assertEqual(markup["inline_keyboard"][0][1]["text"], "🚀 GPT-5.6 Luna")

    def test_callback_validates_and_persists_the_selected_model(self):
        original_rpc, original_api = controller.CODEX_RPC, controller.api
        calls = []

        class FakeRpc:
            def models(self):
                return ModelPickerTests.MODELS

        try:
            controller.CODEX_RPC = FakeRpc()
            controller.api = lambda method, payload=None: calls.append((method, payload))
            state = {"chat_id": 42}
            controller.handle_callback({
                "id": "callback-1",
                "data": "model:gpt-5.6-sol",
                "message": {"message_id": 7, "chat": {"id": 42}},
            }, state)
        finally:
            controller.CODEX_RPC, controller.api = original_rpc, original_api

        self.assertEqual(state["model"], "gpt-5.6-sol")
        self.assertEqual(calls[0][0], "answerCallbackQuery")
        self.assertEqual(calls[1][0], "editMessageText")


class TaskSummaryTests(unittest.TestCase):
    def test_completed_task_shows_execution_duration_not_age_since_completion(self):
        summary = controller.task_summary({
            "id": 9,
            "status": "completed",
            "created_at": 10,
            "started_at": 20,
            "finished_at": 95,
            "prompt": "test",
            "checkpoint": "",
            "error": "",
        })

        self.assertEqual(summary, "T-9 completed (1m 15s)")

    def test_tasks_summary_builds_a_detail_button_for_each_recent_task(self):
        original_tasks = controller.TASKS

        class FakeTasks:
            def recent(self, limit):
                self.limit = limit
                return [{
                    "id": 4,
                    "status": "running",
                    "created_at": 10,
                    "started_at": 20,
                    "finished_at": None,
                    "prompt": "test",
                    "checkpoint": "",
                    "error": "",
                }]

        try:
            controller.TASKS = FakeTasks()
            text, markup = controller.tasks_summary()
        finally:
            controller.TASKS = original_tasks

        self.assertEqual(text, "Select a task for details:")
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "task:4")
        self.assertIn("T-4 running", markup["inline_keyboard"][0][0]["text"])

    def test_task_callback_replies_with_task_detail(self):
        original_tasks, original_api = controller.TASKS, controller.api
        calls = []

        class FakeTasks:
            def get(self, task_id):
                self.task_id = task_id
                return {
                    "id": task_id,
                    "status": "completed",
                    "created_at": 10,
                    "started_at": 20,
                    "finished_at": 80,
                    "prompt": "detail request",
                    "checkpoint": "done",
                    "error": "",
                }

        try:
            controller.TASKS = FakeTasks()
            controller.api = lambda method, payload=None: calls.append((method, payload))
            controller.handle_callback({
                "id": "callback-2",
                "data": "task:4",
                "message": {"message_id": 8, "chat": {"id": 42}},
            }, {"chat_id": 42})
        finally:
            controller.TASKS, controller.api = original_tasks, original_api

        self.assertEqual(calls[0][0], "answerCallbackQuery")
        self.assertEqual(calls[1][0], "sendMessage")
        self.assertIn("Request: detail request", calls[1][1]["text"])


class SharedSessionQueueTests(unittest.TestCase):
    def test_queue_uses_cli_arguments_and_optional_remote_settings(self):
        original_run = controller.subprocess.run
        original_remote = controller.CODEX_REMOTE
        original_auth_env = controller.CODEX_REMOTE_AUTH_TOKEN_ENV
        calls = []
        try:
            controller.CODEX_REMOTE = "ws://127.0.0.1:8787"
            controller.CODEX_REMOTE_AUTH_TOKEN_ENV = "CODEX_DAEMON_TOKEN"
            controller.subprocess.run = lambda *args, **kwargs: calls.append((args, kwargs)) or subprocess.CompletedProcess(args[0], 0, "queued\n", "")
            output = controller.queue_shared_session("session-123", "continue the refactor")
        finally:
            controller.subprocess.run = original_run
            controller.CODEX_REMOTE = original_remote
            controller.CODEX_REMOTE_AUTH_TOKEN_ENV = original_auth_env

        self.assertEqual(output, "queued")
        self.assertEqual(calls[0][0][0], controller.codex_cli_command(
            "queue", "--thread", "session-123",
            "--message", "continue the refactor", "--remote", "ws://127.0.0.1:8787",
            "--remote-auth-token-env", "CODEX_DAEMON_TOKEN",
        ))
        self.assertEqual(calls[0][1], {"text": True, "capture_output": True, "timeout": 30})

    def test_queue_reports_cli_error(self):
        original_run = controller.subprocess.run
        try:
            controller.subprocess.run = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "session not found")
            with self.assertRaisesRegex(RuntimeError, "session not found"):
                controller.queue_shared_session("missing", "hello")
        finally:
            controller.subprocess.run = original_run


if __name__ == "__main__":
    unittest.main()
