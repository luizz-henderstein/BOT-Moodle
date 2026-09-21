import json
import os
import tempfile
import unittest
from datetime import datetime, time
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import src.check_eva as check_eva


def assignment_event(event_id=101, assignment_id=501, title="Trabajo práctico 4"):
    return {
        "id": event_id,
        "instance": assignment_id,
        "name": title,
        "modulename": "assign",
        "eventtype": "due",
        "timesort": 1788508800,
        "course": {"fullname": "Curso de ejemplo"},
        "url": "https://moodle.example.edu/mod/assign/view.php?id=780164",
    }


class CheckEvaTests(unittest.TestCase):
    def test_active_window_includes_1700_and_excludes_2330(self):
        start = time(17, 0)
        end = time(23, 30)
        self.assertFalse(check_eva._is_within_active_window(time(16, 59), start, end))
        self.assertTrue(check_eva._is_within_active_window(time(17, 0), start, end))
        self.assertTrue(check_eva._is_within_active_window(time(23, 29), start, end))
        self.assertFalse(check_eva._is_within_active_window(time(23, 30), start, end))

    def test_seconds_until_start_uses_next_local_1700(self):
        now = datetime(2026, 9, 21, 14, 0, tzinfo=ZoneInfo("America/Montevideo"))
        self.assertEqual(
            check_eva._seconds_until_active_start(now, time(17, 0)),
            3 * 60 * 60,
        )

    def test_extracts_sesskey_without_exposing_other_config(self):
        page = '<script>M.cfg = {"wwwroot":"https:\\/\\/example", "sesskey":"abc123"};</script>'
        self.assertEqual(check_eva._extract_sesskey(page), "abc123")

    def test_filters_only_assignment_due_events(self):
        event = assignment_event()
        self.assertTrue(check_eva.is_assignment_due_event(event))
        self.assertFalse(
            check_eva.is_assignment_due_event({**event, "eventtype": "open"})
        )
        self.assertFalse(
            check_eva.is_assignment_due_event({**event, "modulename": "quiz"})
        )

    def test_task_uses_assignment_id_as_stable_key(self):
        task = check_eva.Task.from_event(assignment_event())
        self.assertEqual(task.key, "assign:501")
        self.assertEqual(task.event_id, 101)

    def test_message_contains_requested_fields_and_link(self):
        task = check_eva.Task.from_event(assignment_event())
        message = check_eva.format_telegram_message(
            task, ZoneInfo("America/Montevideo")
        )
        self.assertIn("Nueva tarea en EVA", message)
        self.assertIn("Trabajo práctico 4", message)
        self.assertIn("Curso de ejemplo", message)
        self.assertIn("04/09/2026 05:00", message)
        self.assertIn("Abrir actividad", message)

    def test_pending_command_groups_tasks_by_course(self):
        first = check_eva.Task.from_event(assignment_event())
        second = check_eva.Task.from_event(
            assignment_event(
                event_id=102,
                assignment_id=502,
                title="Segunda tarea",
            )
        )
        messages = check_eva.format_pending_messages(
            2026,
            [first, second],
            ZoneInfo("America/Montevideo"),
        )
        rendered = "\n".join(messages)
        self.assertIn("Pendientes de 2026", rendered)
        self.assertEqual(rendered.count("Curso de ejemplo"), 1)
        self.assertIn("Trabajo práctico 4", rendered)
        self.assertIn("Segunda tarea", rendered)

    def test_pending_command_excludes_overdue_tasks(self):
        future_due = int(
            datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("America/Montevideo")).timestamp()
        )
        future = check_eva.Task.from_event({**assignment_event(), "timesort": future_due})
        overdue = check_eva.Task.from_event(
            assignment_event(event_id=102, assignment_id=502)
        )
        client = Mock()
        timezone = ZoneInfo("America/Montevideo")
        with (
            patch.object(check_eva, "fetch_tasks", return_value=[overdue, future]),
            patch.object(
                check_eva,
                "datetime",
                wraps=check_eva.datetime,
            ) as mocked_datetime,
        ):
            mocked_datetime.now.return_value = datetime(
                2026, 10, 1, 12, 0, tzinfo=timezone
            )
            year, tasks = check_eva.pending_tasks_for_current_year(client, timezone)

        self.assertEqual(year, 2026)
        self.assertEqual([task.key for task in tasks], [future.key])

    def test_pending_command_excludes_tasks_more_than_15_days_away(self):
        timezone = ZoneInfo("America/Montevideo")
        soon_due = int(datetime(2026, 10, 10, 12, 0, tzinfo=timezone).timestamp())
        far_due = int(datetime(2026, 11, 1, 12, 0, tzinfo=timezone).timestamp())
        soon = check_eva.Task.from_event({**assignment_event(), "timesort": soon_due})
        far = check_eva.Task.from_event(
            {
                **assignment_event(event_id=102, assignment_id=502),
                "timesort": far_due,
            }
        )
        client = Mock()
        with (
            patch.object(check_eva, "fetch_tasks", return_value=[soon, far]),
            patch.object(
                check_eva,
                "datetime",
                wraps=check_eva.datetime,
            ) as mocked_datetime,
        ):
            mocked_datetime.now.return_value = datetime(
                2026, 10, 1, 12, 0, tzinfo=timezone
            )
            _, tasks = check_eva.pending_tasks_for_current_year(client, timezone)

        self.assertEqual([task.key for task in tasks], [soon.key])

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "known_tasks.json"
            with patch.object(check_eva, "STATE_PATH", state_path):
                expected = {
                    "version": 1,
                    "initialized_at": "2026-08-29T12:00:00-03:00",
                    "updated_at": "2026-08-29T12:00:00-03:00",
                    "known_task_keys": ["assign:501"],
                }
                check_eva.save_state(expected)
                self.assertEqual(check_eva.load_state(), expected)
                self.assertEqual(json.loads(state_path.read_text("utf-8")), expected)

    def test_moodle_cookies_survive_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            session_path = Path(directory) / ".moodle_session.json"
            first = check_eva.MoodleClient(
                "https://example.test/moodle/",
                "user",
                "password",
                session_path=session_path,
            )
            first.session.cookies.set(
                "MoodleSession",
                "secret-session-value",
                domain="example.test",
                path="/moodle/",
                secure=True,
            )
            first._save_cookies()

            second = check_eva.MoodleClient(
                "https://example.test/moodle/",
                "user",
                "password",
                session_path=session_path,
            )
            self.assertEqual(
                second.session.cookies.get(
                    "MoodleSession",
                    domain="example.test",
                    path="/moodle/",
                ),
                "secret-session-value",
            )

    def test_valid_saved_session_does_not_log_in_again(self):
        with tempfile.TemporaryDirectory() as directory:
            client = check_eva.MoodleClient(
                "https://example.test/moodle/",
                "user",
                "password",
                session_path=Path(directory) / ".moodle_session.json",
            )
            dashboard = Mock()
            dashboard.text = '<script>M.cfg = {"sesskey":"still-valid"};</script>'
            dashboard.url = "https://example.test/moodle/my/"
            dashboard.raise_for_status.return_value = None

            with (
                patch.object(client.session, "get", return_value=dashboard),
                patch.object(client, "login") as login,
            ):
                client.ensure_authenticated()

            login.assert_not_called()
            self.assertEqual(client.sesskey, "still-valid")

    def test_first_discovery_creates_baseline_and_never_stages_old_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            state_path = data_dir / "known_tasks.json"
            pending_path = data_dir / ".pending_notifications.json"
            initial_event = assignment_event()
            new_event = assignment_event(
                event_id=102,
                assignment_id=502,
                title="Nueva tarea",
            )

            with (
                patch.object(check_eva, "STATE_PATH", state_path),
                patch.object(check_eva, "PENDING_PATH", pending_path),
                patch.object(check_eva.MoodleClient, "login"),
                patch.object(
                    check_eva.MoodleClient,
                    "fetch_assignment_due_events",
                    return_value=[initial_event],
                ) as fetch_events,
                patch.dict(
                    os.environ,
                    {"EVA_USERNAME": "user", "EVA_PASSWORD": "password"},
                    clear=False,
                ),
            ):
                check_eva.discover()
                self.assertFalse(pending_path.exists())
                self.assertEqual(
                    json.loads(state_path.read_text("utf-8"))["known_task_keys"],
                    ["assign:501"],
                )

                fetch_events.return_value = [initial_event, new_event]
                check_eva.discover()
                pending = json.loads(pending_path.read_text("utf-8"))
                self.assertEqual([item["key"] for item in pending], ["assign:502"])

                # Simula una caída posterior al guardado: la ejecución siguiente
                # conserva el pendiente anterior aunque ya no sea considerado nuevo.
                check_eva.discover()
                self.assertTrue(pending_path.exists())
                pending = json.loads(pending_path.read_text("utf-8"))
                self.assertEqual([item["key"] for item in pending], ["assign:502"])

    def test_notify_removes_each_message_only_after_telegram_accepts_it(self):
        with tempfile.TemporaryDirectory() as directory:
            pending_path = Path(directory) / ".pending_notifications.json"
            first = check_eva.Task.from_event(assignment_event())
            second = check_eva.Task.from_event(
                assignment_event(event_id=102, assignment_id=502, title="Segunda")
            )
            pending_path.write_text(
                json.dumps(
                    [check_eva.asdict(first), check_eva.asdict(second)],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(check_eva, "PENDING_PATH", pending_path),
                patch.dict(
                    os.environ,
                    {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"},
                    clear=False,
                ),
                patch.object(
                    check_eva,
                    "send_telegram_message",
                    side_effect=[None, check_eva.EvaError("fallo temporal")],
                ),
            ):
                with self.assertRaises(check_eva.EvaError):
                    check_eva.notify()

            remaining = json.loads(pending_path.read_text("utf-8"))
            self.assertEqual([item["key"] for item in remaining], ["assign:502"])


if __name__ == "__main__":
    unittest.main()
