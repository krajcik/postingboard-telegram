import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from bot import (APIError, Mirror, MirrorError, PostingBoard, State, Telegram,
                 UncertainDelivery, operation_key, request_json, split_text, utf16_length)


def post(seq, root=None, body="Полный текст"):
    return {"seq": seq, "id": f"post-{seq}", "thread_id": root,
            "author": "Автор", "topic": "general", "title": f"Тема {seq}", "body": body}


class FakeSource:
    def __init__(self, posts):
        self.posts = {p["id"]: p for p in posts}

    def activity_since(self, cursor):
        return sorted((p for p in self.posts.values() if p["seq"] > cursor), key=lambda p: p["seq"])

    def post(self, identifier):
        return self.posts[identifier]


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.failure_at = None
        self.failure = MirrorError("Соединение оборвалось после отправки")

    def mutate(self, method, payload):
        self.calls.append((method, payload))
        if self.failure_at == len(self.calls):
            raise self.failure
        field = "message_thread_id" if method == "createForumTopic" else "message_id"
        return {field: len(self.calls) + 100}


class PaginationTests(unittest.TestCase):
    def test_backlog_larger_than_page_no_gaps_and_new_arrival_deferred(self):
        source = PostingBoard("https://example.com", "unused")
        records = [post(seq) for seq in range(1, 101)]
        calls = []

        def get(path, **query):
            self.assertEqual(path, "activity")
            self.assertFalse("before" in query and "after" in query)
            calls.append(query)
            selected = [p for p in records if p["seq"] > query.get("after", 0)
                        and p["seq"] < query.get("before", 10000)]
            selected = sorted(selected, key=lambda p: p["seq"], reverse=True)
            page = selected[:query["limit"]]
            # Новое событие возникает после первой страницы.
            if len(calls) == 1:
                records.append(post(101))
            return {"items": page, "next_before": page[-1]["seq"] if len(selected) > len(page) else None,
                    "newest_cursor": records[-1]["seq"]}

        source.get = get
        result = source.activity_since(5)
        self.assertEqual([p["seq"] for p in result], list(range(6, 101)))
        self.assertEqual(len(calls), 4)
        self.assertEqual([p["seq"] for p in source.activity_since(100)], [101])

    def test_full_initial_history(self):
        source = PostingBoard("https://example.com", "unused")
        with patch.object(source, "get", return_value={"items": [post(2), post(1)], "next_before": None}):
            self.assertEqual([p["seq"] for p in source.activity_since(0)], [1, 2])

    def test_broken_pagination_fails_without_returning_partial_window(self):
        source = PostingBoard("https://example.com", "unused")
        with patch.object(source, "get", return_value={"items": [post(10)], "next_before": 10}):
            with self.assertRaisesRegex(MirrorError, "не продвигается"):
                source.activity_since(1)

    def test_reply_detail_reads_body_not_preview(self):
        source = PostingBoard("https://example.com", "unused")
        reply = post(2, "post-1", "Полный ответ")
        with patch.object(source, "get", return_value={"post": reply, "replies": {"items": []}}) as get:
            self.assertEqual(source.post("post-2")["body"], "Полный ответ")
            get.assert_called_once_with("posts/post-2")

    def test_no_cursor_commit_if_later_page_fails(self):
        source = PostingBoard("https://example.com", "unused")
        with patch.object(source, "get", side_effect=[
            {"items": [post(50)], "next_before": 50}, APIError("Posting Board", 429)
        ]):
            with tempfile.TemporaryDirectory() as directory:
                with contextlib.closing(State(Path(directory) / "test.db", "source", -100)) as state:
                    state.set("cursor", 2)
                    telegram = FakeTelegram()
                    with self.assertRaises(APIError):
                        Mirror(source, telegram, state).sync()
                    self.assertEqual(state.cursor, 2)
                    self.assertEqual(telegram.calls, [])


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "mirror.sqlite3"
        self.state = State(self.path, "https://example.com", -100)
        self.telegram = FakeTelegram()

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_roots_have_distinct_topics_replies_in_parent_and_restart_deduplicates(self):
        source = FakeSource([post(1), post(2, "post-1"), post(3)])
        self.assertEqual(Mirror(source, self.telegram, self.state).sync(), 3)
        topics = [c for c in self.telegram.calls if c[0] == "createForumTopic"]
        messages = [c[1] for c in self.telegram.calls if c[0] == "sendMessage"]
        self.assertEqual(len(topics), 2)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["message_thread_id"], messages[1]["message_thread_id"])
        self.assertNotEqual(messages[0]["message_thread_id"], messages[2]["message_thread_id"])
        calls = len(self.telegram.calls)
        self.state.close()
        self.state = State(self.path, "https://example.com", -100)
        self.state.set("cursor", 0)  # Повторно прочитанное окно тоже дедуплицируется.
        Mirror(source, self.telegram, self.state).sync()
        self.assertEqual(len(self.telegram.calls), calls)
        self.assertEqual(self.state.cursor, 3)

    def test_reply_can_recover_root_older_than_cursor(self):
        source = FakeSource([post(1), post(10, "post-1")])
        self.state.set("cursor", 5)
        Mirror(source, self.telegram, self.state).sync()
        self.assertEqual([c[0] for c in self.telegram.calls], ["createForumTopic", "sendMessage", "sendMessage"])
        self.assertIn("post-1", self.telegram.calls[1][1]["text"])

    def test_long_unicode_text_split_without_loss(self):
        text = ("Текст😀<> &\n" * 1800)
        chunks = split_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(utf16_length(chunk) <= 4000 for chunk in chunks))
        source = FakeSource([post(1, body=text)])
        Mirror(source, self.telegram, self.state).sync()
        messages = [c[1] for c in self.telegram.calls if c[0] == "sendMessage"]
        self.assertTrue(all("parse_mode" not in m for m in messages))
        self.assertIn(text, "".join(m["text"] for m in messages))

    def test_ambiguous_send_survives_restart_and_manual_resolution_continues(self):
        source = FakeSource([post(1, body="Я" * 9000)])
        self.telegram.failure_at = 3  # Создали тему, отправили часть 1, неизвестен результат части 2.
        with self.assertRaises(UncertainDelivery):
            Mirror(source, self.telegram, self.state).sync()
        self.assertEqual(self.state.cursor, 0)
        pending_key = self.state.pending()[0][0]
        self.state.close()
        self.state = State(self.path, "https://example.com", -100)
        with self.assertRaises(UncertainDelivery):
            Mirror(source, self.telegram, self.state).sync()
        self.assertEqual(len(self.telegram.calls), 3)
        self.state.resolve(pending_key, result_id=777)
        Mirror(source, self.telegram, self.state).sync()
        self.assertEqual(len(self.telegram.calls), 4)  # Только оставшаяся часть 3.
        self.assertEqual(self.state.cursor, 1)
        self.assertEqual(self.state.pending(), [])

    def test_unknown_create_and_explicit_retry(self):
        self.telegram.failure_at = 1
        source = FakeSource([post(1)])
        with self.assertRaises(UncertainDelivery):
            Mirror(source, self.telegram, self.state).sync()
        self.state.resolve(self.state.pending()[0][0], retry=True)
        self.telegram.failure_at = None
        Mirror(source, self.telegram, self.state).sync()
        self.assertEqual(len(self.telegram.calls), 3)

    def test_429_retries_only_rejected_operation(self):
        self.telegram.failure_at = 2
        self.telegram.failure = APIError("Telegram", 429, 90)
        source = FakeSource([post(1)])
        with self.assertRaises(APIError):
            Mirror(source, self.telegram, self.state).sync()
        self.assertEqual(self.state.pending(), [])
        self.assertEqual(self.state.cursor, 0)
        Mirror(source, self.telegram, self.state).sync()
        self.assertEqual([c[0] for c in self.telegram.calls], ["createForumTopic", "sendMessage", "sendMessage"])

    def test_500_is_ambiguous_not_automatically_retried(self):
        self.telegram.failure_at = 1
        self.telegram.failure = APIError("Telegram", 500)
        with self.assertRaises(UncertainDelivery):
            Mirror(FakeSource([post(1)]), self.telegram, self.state).sync()
        self.assertEqual(len(self.state.pending()), 1)

    def test_cursor_preserves_only_fully_delivered_events(self):
        source = FakeSource([post(1), post(2)])
        self.telegram.failure_at = 4
        with self.assertRaises(UncertainDelivery):
            Mirror(source, self.telegram, self.state).sync()
        self.assertEqual(self.state.cursor, 1)

    def test_parallel_process_lock(self):
        with self.assertRaisesRegex(MirrorError, "занят"):
            State(self.path, "https://example.com", -100)

    def test_state_cannot_be_reused_for_other_chat(self):
        other_path = Path(self.temp.name) / "other.db"
        with contextlib.closing(State(other_path, "https://example.com", -100)):
            pass
        with self.assertRaisesRegex(MirrorError, "другой доске или группе"):
            State(other_path, "https://example.com", -200)


class APITests(unittest.TestCase):
    def test_network_error_never_contains_bot_token(self):
        with patch("bot.build_opener") as opener:
            opener.return_value.open.side_effect = URLError("https://api.telegram.org/botSECRET/sendMessage")
            with self.assertRaises(MirrorError) as caught:
                request_json("Telegram", "https://example.com")
            self.assertNotIn("SECRET", str(caught.exception))

    def test_retry_after_header(self):
        error = HTTPError("https://example.com", 429, "rate limit", {"Retry-After": "120"},
                          io.BytesIO(json.dumps({"error": "RATE_LIMITED"}).encode()))
        with patch("bot.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(APIError) as caught:
                request_json("Posting Board", "https://example.com")
            self.assertEqual(caught.exception.retry_after, "120")

    def test_telegram_retry_after_json(self):
        with patch("bot.request_json", return_value={"ok": False, "error_code": 429,
                                                     "parameters": {"retry_after": 75}}):
            with self.assertRaises(APIError) as caught:
                Telegram("unused", -100).call("sendMessage", {})
            self.assertEqual(caught.exception.retry_after, 75)

    def test_forum_and_manage_topics_required(self):
        telegram = Telegram("unused", -100)
        with patch.object(telegram, "call", side_effect=[{"id": 1}, {"type": "supergroup", "is_forum": False}]):
            with self.assertRaisesRegex(MirrorError, "включёнными темами"):
                telegram.preflight()
        with patch.object(telegram, "call", side_effect=[{"id": 1}, {"type": "supergroup", "is_forum": True},
                                                        {"status": "administrator", "can_manage_topics": False}]):
            with self.assertRaisesRegex(MirrorError, "управление темами"):
                telegram.preflight()

    def test_credentials_only_sent_over_https(self):
        for url in ("http://example.com", "https://user:pass@example.com", "https://example.com?key=x"):
            with self.assertRaises(MirrorError):
                PostingBoard(url, "unused")


if __name__ == "__main__":
    unittest.main()
