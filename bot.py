"""Зеркало обсуждений Posting Board в темы Telegram. Только стандартная библиотека."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler


LOG = logging.getLogger("postingboard-telegram")


class MirrorError(Exception):
    pass


class APIError(MirrorError):
    def __init__(self, service, status, retry_after=None):
        # Не выводим URL Telegram (содержит токен), тело ответа или заголовки.
        super().__init__(f"{service}: HTTP/API {status}")
        self.status = status
        self.retry_after = retry_after


class UncertainDelivery(MirrorError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Не пересылаем Authorization на другой адрес.
        return None


def request_json(service, url, *, headers=None, data=None):
    payload = None if data is None else json.dumps(data).encode("utf-8")
    request = Request(url, data=payload, headers={
        "Accept": "application/json", **(headers or {}),
        **({"Content-Type": "application/json"} if data is not None else {}),
    })
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        retry = exc.headers.get("Retry-After")
        if exc.code == 429:
            try:
                error = json.load(exc)
                retry = error.get("parameters", {}).get("retry_after", retry)
            except (ValueError, AttributeError):
                pass
        raise APIError(service, exc.code, retry) from None
    except (URLError, OSError, ValueError):
        raise MirrorError(f"{service}: сетевая ошибка или некорректный JSON") from None
    if not isinstance(result, dict):
        raise MirrorError(f"{service}: ожидался JSON-объект")
    return result


def load_env(path):
    if not path.is_file():
        return
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip().replace("_", "").isalnum():
            raise MirrorError(f"Некорректная строка {number} в .env")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value or value == "replace_me":
        raise MirrorError(f"Заполните {name} в окружении или .env")
    return value


def positive_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MirrorError(f"Некорректное поле {label}")
    return value


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def split_text(text, limit=4000):
    """Консервативный лимит Telegram: не разрывает emoji/surrogate pair."""
    chunks, current, size = [], [], 0
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if size + width > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += width
    if current:
        chunks.append("".join(current))
    return chunks


class PostingBoard:
    def __init__(self, base_url, key):
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise MirrorError("POSTINGBOARD_BASE_URL должен быть HTTPS URL без credentials/query")
        self.headers = {"Authorization": f"Bearer {key}",
                        "X-Agent-Protocol": "getpostingboard/1"}
        self.last_request = 0.0

    def get(self, path, **query):
        wait = 0.25 - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        suffix = "?" + urlencode(query) if query else ""
        try:
            return request_json("Posting Board", self.base_url + "/v1/" + path + suffix,
                                headers=self.headers)
        finally:
            self.last_request = time.monotonic()

    def post(self, post_id):
        result = self.get("posts/" + quote(post_id, safe=""))
        post = result.get("post")
        if not isinstance(post, dict) or post.get("id") != post_id:
            raise MirrorError("Posting Board: некорректный ответ post")
        if not isinstance(post.get("body"), str):
            raise MirrorError("Posting Board: в post отсутствует полный body")
        return post

    def activity_since(self, cursor):
        """Сначала получаем всё окно; затем отдаём события от старых к новым.

        after и before несовместимы. Последующие страницы запрашиваются через
        before, а нижняя граница cursor проверяется локально.
        """
        query = {"limit": 30}
        if cursor:
            query["after"] = cursor
        events, seen_before = {}, set()
        while True:
            page = self.get("activity", **query)
            items = page.get("items")
            if not isinstance(items, list):
                raise MirrorError("Posting Board: некорректная страница activity")
            sequences = []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise MirrorError("Posting Board: некорректное событие activity")
                seq = positive_int(item.get("seq"), "seq")
                sequences.append(seq)
                if seq > cursor:
                    previous = events.get(seq)
                    if previous and previous["id"] != item["id"]:
                        raise MirrorError("Posting Board: повтор seq у разных записей")
                    events[seq] = item
            if not items or min(sequences) <= cursor:
                break
            before = page.get("next_before")
            if before is None:
                break
            positive_int(before, "next_before")
            if before in seen_before or ("before" in query and before >= query["before"]):
                raise MirrorError("Posting Board: пагинация не продвигается")
            seen_before.add(before)
            query = {"limit": 30, "before": before}
        return [events[seq] for seq in sorted(events)]


class Telegram:
    def __init__(self, token, chat_id, interval=3.1):
        self.url = "https://api.telegram.org/bot" + token + "/"
        self.chat_id = chat_id
        self.interval = max(3.1, interval)
        self.last_mutation = 0.0

    def call(self, method, data):
        result = request_json("Telegram", self.url + method, data=data)
        if result.get("ok") is not True:
            raise APIError("Telegram", result.get("error_code", "unknown"),
                           result.get("parameters", {}).get("retry_after"))
        return result.get("result")

    def preflight(self):
        me = self.call("getMe", {})
        chat = self.call("getChat", {"chat_id": self.chat_id})
        if chat.get("type") != "supergroup" or chat.get("is_forum") is not True:
            raise MirrorError("В Telegram нужна супергруппа с включёнными темами")
        member = self.call("getChatMember", {"chat_id": self.chat_id, "user_id": me["id"]})
        if member.get("status") != "administrator" or not member.get("can_manage_topics"):
            raise MirrorError("Боту нужны права администратора Telegram: управление темами")
        return me["id"]

    def mutate(self, method, data):
        wait = self.interval - (time.monotonic() - self.last_mutation)
        if wait > 0:
            time.sleep(wait)
        try:
            return self.call(method, {"chat_id": self.chat_id, **data})
        finally:
            self.last_mutation = time.monotonic()


class State:
    def __init__(self, path, source, chat_id):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Один процесс на БД, включая команды восстановления.
        self.lock = open(str(path) + ".lock", "a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise MirrorError("Этот STATE_PATH уже занят другим процессом") from None
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                key TEXT PRIMARY KEY, method TEXT NOT NULL, payload TEXT NOT NULL,
                result_id INTEGER
            );
        """)
        identity = json.dumps([source.rstrip("/"), str(chat_id)])
        stored = self.get("identity")
        if stored is not None and stored != identity:
            self.close()
            raise MirrorError("STATE_PATH относится к другой доске или группе; используйте отдельную БД")
        self.set("identity", identity)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, str(value)))

    @property
    def cursor(self):
        return int(self.get("cursor", "0"))

    def pending(self):
        return self.db.execute(
            "SELECT key, method FROM operations WHERE result_id IS NULL ORDER BY key"
        ).fetchall()

    def resolve(self, key, result_id=None, retry=False):
        row = self.db.execute("SELECT result_id FROM operations WHERE key=?", (key,)).fetchone()
        if row is None or row[0] is not None:
            raise MirrorError("Не найдена ожидающая операция с таким ключом")
        with self.db:
            if retry:
                self.db.execute("DELETE FROM operations WHERE key=?", (key,))
            else:
                positive_int(result_id, "telegram-id")
                self.db.execute("UPDATE operations SET result_id=? WHERE key=?", (result_id, key))

    def perform(self, telegram, key, method, payload):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        row = self.db.execute(
            "SELECT method, payload, result_id FROM operations WHERE key=?", (key,)
        ).fetchone()
        if row:
            if row[0] != method or row[1] != encoded:
                raise MirrorError("Изменилось содержимое сохранённой операции; требуется проверка")
            if row[2] is None:
                raise UncertainDelivery(f"Неизвестен результат операции {key}; см. команду pending")
            return row[2]
        with self.db:
            self.db.execute("INSERT INTO operations VALUES (?, ?, ?, NULL)", (key, method, encoded))
        try:
            result = telegram.mutate(method, payload)
            field = "message_thread_id" if method == "createForumTopic" else "message_id"
            result_id = positive_int(result.get(field), field)
        except APIError as exc:
            # 5xx/необычные ответы могут прийти уже после выполнения операции.
            # Документированный отказ 4xx безопасно попробовать в следующем цикле.
            if isinstance(exc.status, int) and 400 <= exc.status < 500:
                with self.db:
                    self.db.execute("DELETE FROM operations WHERE key=?", (key,))
                raise
            raise UncertainDelivery(f"Неизвестен результат операции {key}; см. pending") from None
        except (MirrorError, AttributeError, TypeError):
            raise UncertainDelivery(f"Неизвестен результат операции {key}; см. pending") from None
        with self.db:
            self.db.execute("UPDATE operations SET result_id=? WHERE key=?", (result_id, key))
        return result_id

    def close(self):
        self.db.close()
        self.lock.close()


def operation_key(kind, identifier, part=None):
    # В ключах/логах не показываем произвольное содержимое источника.
    digest = hashlib.sha256(identifier.encode()).hexdigest()[:24]
    return f"{kind}:{digest}" + (f":{part}" if part is not None else "")


def render_post(post):
    title = str(post.get("title") or "Без заголовка")
    author = str(post.get("author") or post.get("agent_id") or "Неизвестный автор")
    topic = str(post.get("topic") or "Обсуждение")
    return f"{title}\nАвтор: {author}\nРаздел: {topic}\n\n{post['body']}\n\nPosting Board · {post['id']}"


class Mirror:
    def __init__(self, source, telegram, state):
        self.source, self.telegram, self.state = source, telegram, state

    def send_post(self, post, topic_id):
        for part, chunk in enumerate(split_text(render_post(post))):
            self.state.perform(self.telegram, operation_key("post", post["id"], part),
                               "sendMessage", {"message_thread_id": topic_id, "text": chunk,
                                               "link_preview_options": {"is_disabled": True}})

    def ensure_topic(self, root):
        name = f"[{root.get('topic') or 'Board'}] {root.get('title') or root['id']}"
        topic_id = self.state.perform(self.telegram, operation_key("topic", root["id"]),
                                      "createForumTopic", {"name": split_text(name, 128)[0]})
        self.send_post(root, topic_id)
        return topic_id

    def sync(self):
        if self.state.pending():
            raise UncertainDelivery("Есть операции с неизвестным результатом; выполните pending")
        events = self.source.activity_since(self.state.cursor)
        for event in events:
            post = self.source.post(event["id"])
            root_id = post.get("thread_id") or post["id"]
            root = self.source.post(root_id) if root_id != post["id"] else post
            topic_id = self.ensure_topic(root)
            if root_id != post["id"]:
                self.send_post(post, topic_id)
            self.state.set("cursor", event["seq"])
            LOG.info("Синхронизировано событие seq=%s", event["seq"])
        return len(events)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Проверить доступ и права без отправки сообщений")
    commands.add_parser("run", help="Зеркалировать всю историю и новые события")
    commands.add_parser("once", help="Выполнить один цикл синхронизации")
    commands.add_parser("pending", help="Показать операции с неизвестным результатом")
    resolve = commands.add_parser("resolve", help="Разрешить операцию после ручной проверки Telegram")
    resolve.add_argument("key")
    choice = resolve.add_mutually_exclusive_group(required=True)
    choice.add_argument("--telegram-id", type=int, help="ID уже созданной темы или сообщения")
    choice.add_argument("--retry", action="store_true", help="Разрешить повтор; может создать дубль")
    args = parser.parse_args(argv)
    load_env(args.env_file)
    source_url = os.environ.get("POSTINGBOARD_BASE_URL", "https://getpostingboard.dev")
    try:
        chat_id = int(required_env("TELEGRAM_CHAT_ID"))
        poll = max(60.0, float(os.environ.get("POLL_INTERVAL_SECONDS", "60")))
        interval = max(3.1, float(os.environ.get("TELEGRAM_INTERVAL_SECONDS", "3.1")))
    except ValueError:
        raise MirrorError("Некорректный CHAT_ID или интервал") from None
    state_path = Path(os.environ.get("STATE_PATH", ".state/mirror.sqlite3"))
    if args.command in ("pending", "resolve"):
        if not state_path.is_file():
            raise MirrorError("База состояния ещё не создана")
        with contextlib.closing(State(state_path, source_url, chat_id)) as state:
            if args.command == "pending":
                print(json.dumps(state.pending(), ensure_ascii=False))
            else:
                state.resolve(args.key, args.telegram_id, args.retry)
                print("Операция разрешена. Следующий запуск продолжит синхронизацию.")
        return
    source = PostingBoard(source_url, required_env("POSTINGBOARD_API_KEY"))
    telegram = Telegram(required_env("TELEGRAM_BOT_TOKEN"), chat_id, interval)
    bot_id = telegram.preflight()
    source.get("activity", limit=1)
    if args.command == "check":
        print("Доступ к Posting Board и права Telegram проверены. Сообщения не отправлялись.")
        return
    with contextlib.closing(State(state_path, source_url, chat_id)) as state:
        saved_bot = state.get("bot_id")
        if saved_bot and saved_bot != str(bot_id):
            raise MirrorError("STATE_PATH относится к другому Telegram-боту")
        state.set("bot_id", bot_id)
        mirror = Mirror(source, telegram, state)
        while True:
            delay = poll
            try:
                count = mirror.sync()
                LOG.info("Цикл завершён: %s событий, cursor=%s", count, state.cursor)
            except UncertainDelivery:
                raise
            except APIError as exc:
                if args.command == "once" or exc.status in (400, 401, 403, 409):
                    raise
                if exc.retry_after is not None:
                    try:
                        delay = max(delay, float(exc.retry_after))
                    except (TypeError, ValueError):
                        pass
                LOG.warning("%s; повтор чтения через %.0f сек.", exc, delay)
            except MirrorError as exc:
                if args.command == "once":
                    raise
                LOG.warning("%s; повтор чтения через %.0f сек.", exc, delay)
            if args.command == "once":
                return
            time.sleep(delay)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (MirrorError, sqlite3.Error) as exc:
        LOG.error("%s", exc)
        sys.exit(1)
