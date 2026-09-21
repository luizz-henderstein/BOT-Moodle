#!/usr/bin/env python3
"""Detecta tareas nuevas en la Línea de tiempo de Moodle y avisa por Telegram."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


DEFAULT_BASE_URL = "https://ev1.utec.edu.uy/moodle/"
DEFAULT_TIMEZONE = "America/Montevideo"
AJAX_METHOD = "core_calendar_get_action_events_by_timesort"
PAGE_SIZE = 50  # Moodle admite entre 1 y 50 eventos por llamada.
MAX_PAGES = 50
REQUEST_TIMEOUT = 30

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("EVA_DATA_DIR", PROJECT_ROOT / "data"))
STATE_PATH = DATA_DIR / "known_tasks.json"
PENDING_PATH = DATA_DIR / ".pending_notifications.json"
SESSION_PATH = DATA_DIR / ".moodle_session.json"
TELEGRAM_OFFSET_PATH = DATA_DIR / ".telegram_offset.json"


class EvaError(RuntimeError):
    """Error controlado al acceder a EVA."""


class EvaSessionExpired(EvaError):
    """La cookie o la clave de sesión de Moodle dejó de ser válida."""


@dataclass(frozen=True)
class Task:
    key: str
    event_id: int
    assignment_id: int | None
    title: str
    course: str
    due_timestamp: int
    url: str | None

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> "Task":
        event_id = _as_positive_int(event.get("id"), "id del evento")
        assignment_id = _optional_positive_int(event.get("instance"))
        key = f"assign:{assignment_id}" if assignment_id else f"event:{event_id}"

        course_data = event.get("course")
        if isinstance(course_data, dict):
            course = str(course_data.get("fullname") or course_data.get("shortname") or "")
        else:
            course = str(event.get("coursefullname") or "")

        title = str(event.get("activityname") or event.get("name") or "Tarea sin título")
        due_timestamp = _as_positive_int(
            event.get("timesort") or event.get("timestart"),
            "fecha de vencimiento",
        )

        direct_url = _first_url(
            event.get("url"),
            event.get("viewurl"),
            (event.get("action") or {}).get("url")
            if isinstance(event.get("action"), dict)
            else None,
        )

        return cls(
            key=key,
            event_id=event_id,
            assignment_id=assignment_id,
            title=title.strip(),
            course=course.strip() or "Materia no indicada",
            due_timestamp=due_timestamp,
            url=direct_url,
        )


class MoodleClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        session_path: Path = SESSION_PATH,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "eva-telegram-monitor/2.0"})
        self.sesskey: str | None = None
        self.session_path = session_path
        self._load_cookies()

    def ensure_authenticated(self) -> None:
        """Reutiliza la sesión guardada y solo inicia sesión si ya no sirve."""
        dashboard = self._get_dashboard()
        if dashboard is not None:
            self.sesskey = _extract_sesskey(dashboard)
            if self.sesskey:
                return
        self.login()

    def login(self) -> None:
        self.session.cookies.clear()
        self.sesskey = None
        login_url = urljoin(self.base_url, "login/index.php")
        try:
            login_page = self.session.get(login_url, timeout=REQUEST_TIMEOUT)
            login_page.raise_for_status()
        except requests.RequestException as exc:
            raise EvaError("No se pudo abrir la página de acceso de EVA.") from exc

        soup = BeautifulSoup(login_page.text, "html.parser")
        token_input = soup.select_one('input[name="logintoken"]')
        form_data = {
            "username": self.username,
            "password": self.password,
            "anchor": "",
        }
        if token_input is not None:
            form_data["logintoken"] = str(token_input.get("value") or "")

        try:
            response = self.session.post(
                login_url,
                data=form_data,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise EvaError("EVA rechazó o interrumpió el inicio de sesión.") from exc

        if _contains_login_form(response.text) or "/login/index.php" in response.url:
            raise EvaError(
                "No se pudo iniciar sesión en EVA. Verifica EVA_USERNAME y EVA_PASSWORD."
            )

        dashboard = self._get_dashboard()
        if dashboard is None:
            raise EvaError("La sesión de EVA no quedó autenticada.")

        self.sesskey = _extract_sesskey(dashboard)
        if not self.sesskey:
            raise EvaError("EVA no expuso la clave de sesión necesaria para su API interna.")
        self._save_cookies()

    def _get_dashboard(self) -> str | None:
        dashboard_url = urljoin(self.base_url, "my/")
        try:
            dashboard = self.session.get(
                dashboard_url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            dashboard.raise_for_status()
        except requests.RequestException as exc:
            raise EvaError("No se pudo abrir el Área personal de EVA.") from exc

        if _contains_login_form(dashboard.text) or "/login/index.php" in dashboard.url:
            return None
        return dashboard.text

    def _load_cookies(self) -> None:
        if not self.session_path.exists():
            return
        try:
            cookies = _read_json_file(self.session_path)
            if not isinstance(cookies, list):
                raise ValueError("formato inválido")
            for item in cookies:
                if not isinstance(item, dict) or not item.get("name"):
                    raise ValueError("cookie inválida")
                self.session.cookies.set(
                    name=str(item["name"]),
                    value=str(item.get("value", "")),
                    domain=item.get("domain") or None,
                    path=item.get("path") or "/",
                    secure=bool(item.get("secure", False)),
                    expires=item.get("expires"),
                )
        except (OSError, ValueError, json.JSONDecodeError):
            # Una sesión dañada no impide arrancar: Moodle emitirá una nueva.
            self.session.cookies.clear()

    def _save_cookies(self) -> None:
        cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
            }
            for cookie in self.session.cookies
        ]
        _write_json_atomic(self.session_path, cookies)
        try:
            self.session_path.chmod(0o600)
        except OSError:
            pass

    def fetch_assignment_due_events(self, timesort_from: int) -> list[dict[str, Any]]:
        if not self.sesskey:
            raise EvaError("Debe iniciarse sesión antes de consultar la Línea de tiempo.")

        all_events: list[dict[str, Any]] = []
        after_event_id: int | None = None

        for _ in range(MAX_PAGES):
            args: dict[str, Any] = {
                "timesortfrom": timesort_from,
                "limitnum": PAGE_SIZE,
                "limittononsuspendedevents": True,
            }
            if after_event_id is not None:
                args["aftereventid"] = after_event_id

            events = self._call_timeline(args)
            all_events.extend(events)

            if len(events) < PAGE_SIZE:
                return [event for event in all_events if is_assignment_due_event(event)]

            next_after_id = _optional_positive_int(events[-1].get("id"))
            if next_after_id is None or next_after_id == after_event_id:
                raise EvaError("EVA devolvió una paginación inválida para la Línea de tiempo.")
            after_event_id = next_after_id

        raise EvaError(
            f"La Línea de tiempo superó el límite de seguridad de {MAX_PAGES * PAGE_SIZE} eventos."
        )

    def _call_timeline(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        endpoint = urljoin(self.base_url, "lib/ajax/service.php")
        payload = [{"index": 0, "methodname": AJAX_METHOD, "args": args}]

        try:
            response = self.session.post(
                endpoint,
                params={"sesskey": self.sesskey, "info": AJAX_METHOD},
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise EvaError("No se pudo consultar la Línea de tiempo de EVA.") from exc

        if response.status_code != 200:
            raise EvaError(f"La API de EVA respondió con HTTP {response.status_code}.")

        try:
            body = response.json()
        except requests.JSONDecodeError as exc:
            raise EvaError("La API de EVA no devolvió JSON válido.") from exc

        if not isinstance(body, list) or not body or not isinstance(body[0], dict):
            raise EvaError("La API de EVA devolvió una estructura inesperada.")

        result = body[0]
        if result.get("error") is not False:
            exception = result.get("exception")
            error_code = exception.get("errorcode") if isinstance(exception, dict) else None
            if error_code in {"invalidsesskey", "sessionerror", "requireloginerror"}:
                raise EvaSessionExpired("La sesión de EVA venció.")
            suffix = f" ({error_code})" if error_code else ""
            raise EvaError(f"La API de EVA rechazó la consulta{suffix}.")

        data = result.get("data")
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
            raise EvaError("La API de EVA no devolvió una lista válida de actividades.")
        return events


def is_assignment_due_event(event: dict[str, Any]) -> bool:
    """Acepta únicamente el evento de vencimiento de una actividad mod_assign."""
    module_name = str(event.get("modulename") or "").casefold()
    event_type = str(event.get("eventtype") or "").casefold()
    return module_name == "assign" and event_type == "due"


def discover(client: MoodleClient | None = None) -> int:
    username = _required_env("EVA_USERNAME")
    password = _required_env("EVA_PASSWORD")
    base_url = os.getenv("EVA_BASE_URL", DEFAULT_BASE_URL)
    timezone_name = os.getenv("EVA_TIMEZONE", DEFAULT_TIMEZONE)
    lookback_days = _env_nonnegative_int("EVA_LOOKBACK_DAYS", 30)

    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise EvaError(f"Zona horaria inválida: {timezone_name}") from exc

    now = datetime.now(timezone)
    start_day = (now - timedelta(days=lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    client = client or MoodleClient(base_url, username, password)
    tasks = fetch_tasks(client, int(start_day.timestamp()))

    state = load_state()
    now_iso = now.isoformat(timespec="seconds")

    if state is None:
        # La inicialización crea una línea base y nunca hereda avisos antiguos.
        PENDING_PATH.unlink(missing_ok=True)
        save_state(
            {
                "version": 1,
                "initialized_at": now_iso,
                "updated_at": now_iso,
                "known_task_keys": sorted(task.key for task in tasks),
            }
        )
        print(
            f"Inicialización completada: {len(tasks)} tareas existentes quedaron registradas sin notificar."
        )
        return 0

    known = set(_validate_state(state))
    new_tasks = [task for task in tasks if task.key not in known]
    if not new_tasks:
        print(f"Sin tareas nuevas. EVA devolvió {len(tasks)} tareas de vencimiento.")
        return 0

    known.update(task.key for task in new_tasks)
    state["known_task_keys"] = sorted(known)
    state["updated_at"] = now_iso
    save_state(state)
    pending = load_pending_tasks()
    pending_by_key = {task.key: task for task in pending}
    for task in new_tasks:
        pending_by_key.setdefault(task.key, task)
    _write_json_atomic(
        PENDING_PATH,
        [asdict(task) for task in pending_by_key.values()],
    )
    print(f"Detectadas {len(new_tasks)} tareas nuevas; sus IDs quedaron registrados.")
    return 0


def notify() -> int:
    if not PENDING_PATH.exists():
        print("No hay notificaciones nuevas para Telegram.")
        return 0

    token = _required_env("TELEGRAM_BOT_TOKEN")
    chat_id = _required_env("TELEGRAM_CHAT_ID")
    timezone_name = os.getenv("EVA_TIMEZONE", DEFAULT_TIMEZONE)
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise EvaError(f"Zona horaria inválida: {timezone_name}") from exc

    tasks = load_pending_tasks()
    sent = 0
    while tasks:
        task = tasks[0]
        send_telegram_message(token, chat_id, format_telegram_message(task, timezone))
        sent += 1
        tasks.pop(0)
        if tasks:
            _write_json_atomic(PENDING_PATH, [asdict(item) for item in tasks])
        else:
            PENDING_PATH.unlink(missing_ok=True)

    print(f"Telegram recibió {sent} notificaciones nuevas.")
    return 0


def load_pending_tasks() -> list[Task]:
    if not PENDING_PATH.exists():
        return []
    pending = _read_json(PENDING_PATH)
    if not isinstance(pending, list):
        raise EvaError("El archivo temporal de notificaciones es inválido.")
    try:
        return [Task(**item) for item in pending]
    except (TypeError, ValueError) as exc:
        raise EvaError("El archivo temporal de notificaciones es inválido.") from exc


def check_once(client: MoodleClient | None = None) -> int:
    discover(client)
    return notify()


def fetch_tasks(client: MoodleClient, timesort_from: int) -> list[Task]:
    client.ensure_authenticated()
    try:
        events = client.fetch_assignment_due_events(timesort_from)
    except EvaSessionExpired:
        client.login()
        events = client.fetch_assignment_due_events(timesort_from)
    return _deduplicate_tasks(Task.from_event(event) for event in events)


def pending_tasks_for_current_year(
    client: MoodleClient,
    timezone: ZoneInfo,
) -> tuple[int, list[Task]]:
    now = datetime.now(timezone)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    deadline_limit = now + timedelta(days=15)
    tasks = fetch_tasks(client, int(year_start.timestamp()))
    tasks = [
        task
        for task in tasks
        if datetime.fromtimestamp(task.due_timestamp, timezone).year == now.year
        and task.due_timestamp > int(now.timestamp())
        and task.due_timestamp <= int(deadline_limit.timestamp())
    ]
    return now.year, tasks


def format_pending_messages(
    year: int,
    tasks: list[Task],
    timezone: ZoneInfo,
) -> list[str]:
    if not tasks:
        return [
            f"<b>Pendientes de {year}</b>\n\n"
            "No hay tareas que venzan en los próximos 15 días."
        ]

    grouped: dict[str, list[Task]] = {}
    for task in tasks:
        grouped.setdefault(task.course, []).append(task)

    blocks = [f"<b>Pendientes de {year}</b>"]
    for course in sorted(grouped, key=str.casefold):
        lines = [f"<b>{html.escape(course)}</b>"]
        for task in grouped[course]:
            due = datetime.fromtimestamp(task.due_timestamp, timezone).strftime(
                "%d/%m %H:%M"
            )
            title = html.escape(task.title)
            if task.url:
                title = f'<a href="{html.escape(task.url, quote=True)}">{title}</a>'
            lines.append(f"• {title} — {due}")
        blocks.append("\n".join(lines))

    messages: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > 3800 and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def poll_telegram_commands(
    client: MoodleClient,
    timezone: ZoneInfo,
    timeout: int,
) -> None:
    token = _required_env("TELEGRAM_BOT_TOKEN")
    allowed_chat_id = _required_env("TELEGRAM_CHAT_ID")
    offset = load_telegram_offset()
    endpoint = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        response = requests.post(
            endpoint,
            json={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message"],
            },
            timeout=timeout + REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, requests.JSONDecodeError) as exc:
        raise EvaError("No se pudieron recibir comandos de Telegram.") from exc
    if body.get("ok") is not True or not isinstance(body.get("result"), list):
        raise EvaError("Telegram rechazó la consulta de comandos.")

    for update in body["result"]:
        update_id = _optional_positive_int(update.get("update_id"))
        if update_id is None:
            continue
        message = update.get("message") if isinstance(update, dict) else None
        chat = message.get("chat") if isinstance(message, dict) else None
        text = str(message.get("text") or "").strip() if isinstance(message, dict) else ""

        if isinstance(chat, dict) and str(chat.get("id")) == allowed_chat_id:
            command = text.split(maxsplit=1)[0].split("@", 1)[0].casefold()
            if command == "/pendientes":
                year, tasks = pending_tasks_for_current_year(client, timezone)
                for reply in format_pending_messages(year, tasks, timezone):
                    send_telegram_message(token, allowed_chat_id, reply)

        save_telegram_offset(update_id + 1)


def load_telegram_offset() -> int:
    if not TELEGRAM_OFFSET_PATH.exists():
        return 0
    value = _read_json(TELEGRAM_OFFSET_PATH)
    if not isinstance(value, dict):
        raise EvaError("El estado de comandos de Telegram es inválido.")
    offset = value.get("offset")
    if not isinstance(offset, int) or offset < 0:
        raise EvaError("El estado de comandos de Telegram es inválido.")
    return offset


def save_telegram_offset(offset: int) -> None:
    _write_json_atomic(TELEGRAM_OFFSET_PATH, {"offset": offset})


def run_forever() -> int:
    username = _required_env("EVA_USERNAME")
    password = _required_env("EVA_PASSWORD")
    base_url = os.getenv("EVA_BASE_URL", DEFAULT_BASE_URL)
    interval = _env_positive_int("CHECK_INTERVAL_SECONDS", 600)
    timezone_name = os.getenv("EVA_TIMEZONE", DEFAULT_TIMEZONE)
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise EvaError(f"Zona horaria inválida: {timezone_name}") from exc
    active_start = _env_clock_time("ACTIVE_START_TIME", "17:00")
    active_end = _env_clock_time("ACTIVE_END_TIME", "23:30")
    telegram_poll_seconds = _env_positive_int("TELEGRAM_POLL_SECONDS", 20)
    client = MoodleClient(base_url, username, password)

    print(
        "Monitor permanente iniciado; "
        f"intervalo: {interval} segundos; "
        f"ventana: {active_start.strftime('%H:%M')}-"
        f"{active_end.strftime('%H:%M')} ({timezone_name}).",
        flush=True,
    )
    outside_window_reported = False
    next_scheduled_check = 0.0
    while True:
        now = datetime.now(timezone)
        inside_window = _is_within_active_window(now.time(), active_start, active_end)
        if not inside_window:
            if not outside_window_reported:
                print(
                    "Fuera de la ventana activa; no se consultará EVA hasta las "
                    f"{active_start.strftime('%H:%M')}.",
                    flush=True,
                )
                outside_window_reported = True
            next_scheduled_check = 0.0
        else:
            outside_window_reported = False

        monotonic_now = time.monotonic()
        if inside_window and monotonic_now >= next_scheduled_check:
            started = monotonic_now
            try:
                check_once(client)
            except EvaError as exc:
                print(f"ERROR: {exc}", file=sys.stderr, flush=True)
            next_scheduled_check = started + interval

        try:
            poll_telegram_commands(client, timezone, telegram_poll_seconds)
        except EvaError as exc:
            print(f"ERROR: {exc}", file=sys.stderr, flush=True)
            time.sleep(min(10, telegram_poll_seconds))


def list_telegram_chats() -> int:
    token = _required_env("TELEGRAM_BOT_TOKEN")
    endpoint = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        response = requests.get(endpoint, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise EvaError("No se pudo consultar getUpdates de Telegram.") from exc

    try:
        body = response.json()
    except requests.JSONDecodeError as exc:
        raise EvaError("Telegram no devolvió JSON válido.") from exc

    if response.status_code != 200 or body.get("ok") is not True:
        raise EvaError("Telegram rechazó el token o la consulta de actualizaciones.")

    chats: dict[str, str] = {}
    for update in body.get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if "id" not in chat:
            continue
        label = chat.get("title") or " ".join(
            part for part in (chat.get("first_name"), chat.get("last_name")) if part
        )
        chats[str(chat["id"])] = label or chat.get("type", "chat")

    if not chats:
        print("No hay chats disponibles. Envía /start al bot y vuelve a ejecutar este comando.")
        return 0

    print("Chats encontrados:")
    for chat_id, label in chats.items():
        print(f"- {chat_id}: {label}")
    return 0


def format_telegram_message(task: Task, timezone: ZoneInfo) -> str:
    due = datetime.fromtimestamp(task.due_timestamp, timezone).strftime("%d/%m/%Y %H:%M")
    lines = [
        "<b>Nueva tarea en EVA</b>",
        "",
        f"<b>Tarea:</b> {html.escape(task.title)}",
        f"<b>Materia:</b> {html.escape(task.course)}",
        f"<b>Vence:</b> {due}",
    ]
    if task.url:
        lines.extend(["", f'<a href="{html.escape(task.url, quote=True)}">Abrir actividad</a>'])
    return "\n".join(lines)


def send_telegram_message(token: str, chat_id: str, message: str) -> None:
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(
            endpoint,
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise EvaError("No se pudo conectar con Telegram.") from exc

    try:
        body = response.json()
    except requests.JSONDecodeError:
        body = {}

    if response.status_code != 200 or body.get("ok") is not True:
        description = body.get("description") if isinstance(body, dict) else None
        detail = f": {description}" if description else ""
        raise EvaError(f"Telegram rechazó el mensaje (HTTP {response.status_code}){detail}.")


def load_state() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    state = _read_json(STATE_PATH)
    if not isinstance(state, dict):
        raise EvaError("data/known_tasks.json debe contener un objeto JSON.")
    _validate_state(state)
    return state


def save_state(state: dict[str, Any]) -> None:
    _write_json_atomic(STATE_PATH, state)


def _validate_state(state: dict[str, Any]) -> list[str]:
    if state.get("version") != 1:
        raise EvaError("Versión desconocida en data/known_tasks.json.")
    keys = state.get("known_task_keys")
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise EvaError("known_task_keys es inválido en data/known_tasks.json.")
    return keys


def _read_json(path: Path) -> Any:
    try:
        return _read_json_file(path)
    except (OSError, json.JSONDecodeError) as exc:
        try:
            display_path = path.relative_to(PROJECT_ROOT)
        except ValueError:
            display_path = path
        raise EvaError(f"No se pudo leer {display_path}.") from exc


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _extract_sesskey(page_html: str) -> str | None:
    match = re.search(r'"sesskey"\s*:\s*"((?:\\.|[^"\\])*)"', page_html)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return None


def _contains_login_form(page_html: str) -> bool:
    soup = BeautifulSoup(page_html, "html.parser")
    return soup.select_one('form input[name="username"]') is not None and soup.select_one(
        'form input[name="password"]'
    ) is not None


def _deduplicate_tasks(tasks: Iterable[Task]) -> list[Task]:
    unique: dict[str, Task] = {}
    for task in tasks:
        unique.setdefault(task.key, task)
    return sorted(unique.values(), key=lambda item: (item.due_timestamp, item.event_id))


def _first_url(*candidates: Any) -> str | None:
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
            return candidate
    return None


def _as_positive_int(value: Any, label: str) -> int:
    parsed = _optional_positive_int(value)
    if parsed is None:
        raise EvaError(f"Una tarea de EVA no contiene un {label} válido.")
    return parsed


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise EvaError(f"Falta la variable de entorno {name}.")
    return value


def _env_nonnegative_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise EvaError(f"{name} debe ser un número entero.") from exc
    if value < 0:
        raise EvaError(f"{name} no puede ser negativo.")
    return value


def _env_positive_int(name: str, default: int) -> int:
    value = _env_nonnegative_int(name, default)
    if value == 0:
        raise EvaError(f"{name} debe ser mayor que cero.")
    return value


def _env_clock_time(name: str, default: str) -> clock_time:
    raw = os.getenv(name, default).strip()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise EvaError(f"{name} debe usar el formato HH:MM de 24 horas.") from exc


def _is_within_active_window(
    current: clock_time,
    start: clock_time,
    end: clock_time,
) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _seconds_until_active_start(now: datetime, start: clock_time) -> float:
    target = now.replace(
        hour=start.hour,
        minute=start.minute,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("run", "check", "discover", "notify", "telegram-chat-id"),
        help="Operación que se desea ejecutar.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "run":
            return run_forever()
        if args.command == "check":
            return check_once()
        if args.command == "discover":
            return discover()
        if args.command == "notify":
            return notify()
        return list_telegram_chats()
    except EvaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
