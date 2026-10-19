import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot import APIError, Bot


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.fail_student = False

    def call(self, method, **payload):
        if self.fail_student and payload.get('chat_id') == 1374449699:
            raise APIError(403)
        self.calls.append((method, payload))
        return {}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.api = FakeAPI()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.bot = Bot(self.api, self.path)
        self.counter = 0

    def tearDown(self):
        self.bot.db.close()
        self.temp.cleanup()

    def message(self, text='', uid=1252270301, **extra):
        self.counter += 1
        update = {'update_id': self.counter, 'message': {
            'from': {'id': uid}, 'chat': {'id': uid, 'type': 'private'},
            'text': text, **extra}}
        self.bot.process(update)
        return update

    def callback(self, data, uid=1374449699):
        self.counter += 1
        self.bot.process({'update_id': self.counter, 'callback_query': {
            'id': str(self.counter), 'from': {'id': uid}, 'data': data,
            'message': {'chat': {'id': uid}}}})

    def publish(self):
        self.message('/new Відмінки')
        self.message(document={'file_id': 'pdf-file', 'file_name': 'lesson.pdf'})
        self.message('Практика: https://example.org/practice')
        self.message(voice={'file_id': 'voice-file'})
        self.message('/publish')
        return self.bot.row('active')['id']

    def queued(self, uid=1374449699):
        return self.bot.db.execute('SELECT * FROM outbox WHERE recipient=? ORDER BY id', (uid,)).fetchall()

    def test_materials_publish_in_order_and_voice_stays_voice(self):
        self.publish()
        items = self.queued()
        self.assertEqual([r['method'] for r in items],
                         ['sendMessage', 'sendDocument', 'sendMessage', 'sendVoice', 'sendMessage'])
        self.assertEqual(json.loads(items[1]['payload'])['document'], 'pdf-file')
        self.assertEqual(json.loads(items[3]['payload'])['voice'], 'voice-file')

    def test_schedule_both_days_boundaries_and_no_duplicates(self):
        self.publish()
        for day in (23, 24):  # Friday and Saturday, October 2026
            for hour in (16, 17, 18, 19, 20, 21, 22, 23):
                now = datetime(2026, 10, day, hour, 0, tzinfo=self.bot.tz)
                self.bot.tick(now)
                self.bot.tick(now)
        self.bot.tick(datetime(2026, 10, 25, 17, 0, tzinfo=self.bot.tz))
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM reminders').fetchone()[0], 12)

    def test_no_catchup_outside_scheduled_minute(self):
        self.publish()
        self.bot.tick(datetime(2026, 10, 23, 17, 2, tzinfo=self.bot.tz))
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM reminders').fetchone()[0], 0)

    def test_done_stops_reminders_and_reports_once(self):
        lesson = self.publish()
        self.bot.tick(datetime(2026, 10, 23, 17, 0, tzinfo=self.bot.tz))
        self.callback(f'done:{lesson}')
        self.callback(f'done:{lesson}')
        self.bot.tick(datetime(2026, 10, 24, 18, 0, tzinfo=self.bot.tz))
        self.assertIsNone(self.bot.row('active'))
        self.assertFalse(any(r['tag'] for r in self.queued()))
        notices = [json.loads(r['payload'])['text'] for r in self.queued(1252270301)]
        self.assertEqual(sum('Ученик подтвердил' in t for t in notices), 1)

    def test_rating_is_single_and_after_completion(self):
        lesson = self.publish()
        self.callback(f'rate:{lesson}:5')
        self.assertIsNone(self.bot.row('active')['rating'])
        self.callback(f'done:{lesson}')
        self.callback(f'rate:{lesson}:4')
        self.callback(f'rate:{lesson}:1')
        self.assertEqual(self.bot.row('done')['rating'], 4)
        notices = [json.loads(r['payload'])['text'] for r in self.queued(1252270301)]
        self.assertEqual(sum(t.startswith('Оценка темы') for t in notices), 1)

    def test_next_request_deduplicates(self):
        lesson = self.publish()
        self.callback(f'next:{lesson}')
        self.message('/next', uid=1374449699)
        notices = [json.loads(r['payload'])['text'] for r in self.queued(1252270301)]
        self.assertEqual(sum('Ученик просит следующую' in t for t in notices), 1)

    def test_only_authorized_users_and_roles(self):
        self.message('/new чужая тема', uid=999)
        self.message('/new чужая тема', uid=1374449699)
        self.assertIsNone(self.bot.row('draft'))
        lesson = self.publish()
        self.callback(f'done:{lesson}', uid=1252270301)
        self.assertIsNotNone(self.bot.row('active'))

    def test_cannot_replace_unfinished_lesson(self):
        lesson = self.publish()
        self.message('/new Следующая')
        self.message('https://example.org')
        self.message('/publish')
        self.assertEqual(self.bot.row('active')['id'], lesson)
        self.assertIsNotNone(self.bot.row('draft'))
        self.callback(f'done:{lesson}')
        self.message('/publish')
        self.assertNotEqual(self.bot.row('active')['id'], lesson)
        self.callback(f'next:{lesson}')  # stale button cannot request a third lesson
        self.assertFalse(self.bot.row('active')['requested'])

    def test_restart_keeps_state_offset_and_reminder_deduplication(self):
        self.publish()
        now = datetime(2026, 10, 23, 17, 0, tzinfo=self.bot.tz)
        self.bot.tick(now)
        before = len(self.queued())
        self.bot.db.close()
        self.bot = Bot(self.api, self.path)
        self.bot.tick(now)
        self.assertEqual(len(self.queued()), before)
        saved = self.bot.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()[0]
        self.assertEqual(int(saved), self.counter + 1)

    def test_replayed_update_does_not_add_material_twice(self):
        self.message('/new Тема')
        update = self.message('https://example.org')
        self.bot.process(update)
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM materials').fetchone()[0], 1)

    def test_failed_delivery_keeps_queue_and_does_not_block_teacher(self):
        with self.bot.db:
            self.bot.say(1374449699, 'first')
            self.bot.say(1374449699, 'second')
            self.bot.say(1252270301, 'teacher')
        self.api.fail_student = True
        self.bot.flush()
        self.assertEqual(len(self.queued()), 2)
        self.bot.flush()
        self.assertEqual(self.api.calls[-1][1]['text'], 'teacher')
        self.api.fail_student = False
        with patch('bot.time.time', return_value=9999999999):
            self.bot.flush()
            self.bot.flush()
        sent = [p['text'] for _, p in self.api.calls if p.get('chat_id') == 1374449699]
        self.assertEqual(sent, ['first', 'second'])

    def test_expired_reminder_is_not_delivered(self):
        with self.bot.db:
            self.bot.say(1374449699, 'old reminder', expires=1, tag='reminder:1')
        self.bot.flush()
        self.assertEqual(self.api.calls, [])


if __name__ == '__main__':
    unittest.main()
