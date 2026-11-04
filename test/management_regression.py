import json
import datetime
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from xujie_memory.server import MemoryService, serve
from xujie_memory.management import Management, ManagementError
from xujie_memory.summary import SummaryStore
from xujie_memory.profile import ProfileStore
from xujie_memory.episode_compaction import compact_recall, compact_session
from xujie_memory.qq_budget import QQBudget, BudgetPolicy, BudgetDenied
from xujie_memory.qq_settings import QQSettings

class ManagementRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'memory.db'
        self.svc = MemoryService(self.db, ROOT / 'python/specs')
        self.m = Management(self.svc)
        self.svc.turn('one', '我不吃辣', user='owner', context={'peer_id':'assistant'})
        self.scope = self.svc.scope_of('one', 'owner')
        self.tid = self.svc.engine._conn().execute('SELECT trace_id FROM traces WHERE scope_id=?', (self.scope,)).fetchone()[0]
        self.hash = self.m.detail(self.scope,self.tid)['trace']['content_hash']
    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()
    def test_catalog_and_graph_use_real_records(self):
        self.assertEqual(self.m.catalog()['scopes'][0]['scope_id'],self.scope)
        snap = self.m.snapshot(self.scope)
        self.assertTrue(any(n['id']==self.tid for n in snap['nodes']))
        self.assertTrue(any(n['layer']=='working' for n in snap['nodes']))
        self.assertTrue(any(n['layer']=='semantic' for n in snap['nodes']))
        self.assertTrue(snap['cards'])
        self.assertTrue(all(e['kind'] in ('evidence','working_projection') for e in snap['edges']))
    def test_scope_isolation(self):
        other = self.svc.scope_of('other','different-user')
        for method,args in [(self.m.detail,(other,self.tid)),(self.m.edit,(other,self.tid,'我喜欢咖啡',self.hash)),(self.m.forget,(other,self.tid,self.hash))]:
            with self.assertRaisesRegex(ManagementError,'trace_not_found'): method(*args)
        self.assertEqual(self.m.search(other,'吃辣')['hits'],[])
    def test_cross_session_user_scope(self):
        self.assertEqual(self.scope,self.svc.scope_of('two','owner'))

    def test_context_hints_do_not_cross_sessions_in_a_shared_memory_scope(self):
        self.svc.turn('debt-session', '我欠你五元。', user='same-user', context={'peer_id': 'assistant'})
        scope = self.svc.scope_of('debt-session', 'same-user')
        ingest = self.svc.orchestrator.ingest
        self.assertEqual(ingest._context_hint(scope, 'debt-session')[0], 'debt')
        self.assertEqual(ingest._context_hint(scope, 'other-session'), (None, {}))
        self.svc.turn('preference-session', '我喜欢茉莉花茶。', user='same-user')
        self.assertEqual(ingest._context_hint(scope, 'debt-session')[0], 'debt')
        self.assertEqual(ingest._context_hint(scope, 'preference-session')[0], 'preference')
        self.assertEqual(ingest._context_hint(scope, 'other-session'), (None, {}))
        result = self.svc.turn('other-session', '是的，他是一个电影演员。', user='same-user')
        self.assertFalse(result['clarifications'])
        self.assertFalse(any(item['relation'] == 'preference' and item['decision'] == 'pending'
                             for item in result['outcomes']))

    def test_shared_user_scope_keeps_each_turn_session(self):
        self.svc.turn('two', '周末计划去爬山', user='owner')
        conn = self.svc.engine._conn()
        rows = conn.execute('SELECT content,context_ref FROM traces WHERE scope_id=?', (self.scope,)).fetchall()
        sessions = {row['content']: json.loads(row['context_ref'])['_xjm_session'] for row in rows}
        self.assertEqual(sessions['我不吃辣'], 'one')
        self.assertEqual(sessions['周末计划去爬山'], 'two')
        changed = self.m.edit(self.scope, self.tid, '我喜欢微辣', self.hash)
        replacement = self.svc.engine.get_trace(changed['trace']['trace_id'])
        self.assertEqual(json.loads(replacement['context_ref'])['_xjm_session'], 'one')

    def test_session_excerpt_is_generated_without_crossing_sessions_and_forgotten(self):
        for i in range(16):
            self.svc.turn('trip', '我们看《西湖之行》。湖边风景很美，沿途还看到很多树。', user='traveller')
        scope = self.svc.scope_of('trip', 'traveller')
        conn = self.svc.engine._conn()
        rows = conn.execute("SELECT * FROM summaries WHERE scope_id=? AND lifecycle_state='active'", (scope,)).fetchall()
        self.assertEqual(len(rows), 1)
        refs = json.loads(rows[0]['input_refs_json'])
        self.assertEqual(len(refs), 8)
        self.assertTrue(all(json.loads(self.svc.engine.get_trace(tid)['context_ref'])['_xjm_session'] == 'trip' for tid in refs))
        related = [{'trace_id': tid, 'content': self.svc.engine.get_trace(tid)['content'], 'kind': 'related'} for tid in refs[:2]]
        compressed = compact_recall(self.svc.engine, scope, related, '西湖在哪', 500)
        self.assertEqual(len(compressed), 1)
        self.assertIn('会话片段摘录', compressed[0]['content'])
        self.assertIn('会话片段摘录', self.svc.context('trip', '西湖', user='traveller')['context'])
        item = self.m.detail(scope, refs[0])['trace']
        self.m.forget(scope, refs[0], item['content_hash'])
        archived = conn.execute('SELECT text,lifecycle_state FROM summaries WHERE summary_id=?', (rows[0]['summary_id'],)).fetchone()
        self.assertEqual((archived['text'], archived['lifecycle_state']), ('', 'archived'))

    def test_editing_summary_evidence_removes_outdated_excerpt(self):
        for _ in range(16):
            self.svc.turn('edit-episode', '我喜欢《三体》。这部小说让我印象深刻。', user='editor')
        scope = self.svc.scope_of('edit-episode', 'editor')
        conn = self.svc.engine._conn()
        summary = conn.execute("SELECT summary_id,input_refs_json FROM summaries WHERE scope_id=? AND lifecycle_state='active'", (scope,)).fetchone()
        self.assertIsNotNone(summary)
        trace_id = json.loads(summary['input_refs_json'])[0]
        trace = self.m.detail(scope, trace_id)['trace']
        self.m.edit(scope, trace_id, '我现在不喜欢《三体》。', trace['content_hash'])
        old = conn.execute('SELECT text,lifecycle_state FROM summaries WHERE summary_id=?', (summary['summary_id'],)).fetchone()
        self.assertEqual((old['text'], old['lifecycle_state']), ('', 'archived'))
        self.assertNotIn('会话片段摘录', self.svc.context('edit-episode', '三体', user='editor')['context'])

    def test_excerpt_scan_advances_past_old_ineligible_records(self):
        scope = self.svc.scope_of('long-session', 'scan-user')
        conn = self.svc.engine._conn()
        for index in range(144):
            content = ('关于普通话题的记录。' if index < 128 else '我们讨论《同一部电影》。还有其他细节。')
            conn.execute(
                'INSERT INTO traces(trace_id,scope_id,source_ref,actor_id,source_kind,content,content_hash,recorded_at,context_ref)'
                ' VALUES(?,?,?,?,?,?,?,?,?)',
                (f'scan-{index:03}', scope, f'ref-{index:03}', 'user', 'user', content,
                 f'hash-{index:03}', f'2026-01-01T00:00:{index:03}', json.dumps({'_xjm_session': 'long-session'})),
            )
        self.assertIsNone(compact_session(self.svc.engine, scope, 'long-session'))
        result = compact_session(self.svc.engine, scope, 'long-session')
        self.assertIsNotNone(result)
        row = conn.execute('SELECT input_refs_json FROM summaries WHERE summary_id=?', (result,)).fetchone()
        self.assertTrue(all(int(ref.split('-')[1]) >= 128 for ref in json.loads(row['input_refs_json'])))

    def test_invalid_excerpt_does_not_block_safe_reprocessing(self):
        for _ in range(16):
            self.svc.turn('retry-episode', '我读《三体》。这本书很有趣。', user='retry-reader')
        scope = self.svc.scope_of('retry-episode', 'retry-reader')
        conn = self.svc.engine._conn()
        first = conn.execute("SELECT summary_id FROM summaries WHERE scope_id=? AND lifecycle_state='active'", (scope,)).fetchone()[0]
        conn.execute("UPDATE summaries SET validation_status='failed' WHERE summary_id=?", (first,))
        replacement = compact_session(self.svc.engine, scope, 'retry-episode')
        self.assertIsNotNone(replacement)
        self.assertNotEqual(first, replacement)
        self.assertEqual(conn.execute('SELECT validation_status FROM summaries WHERE summary_id=?', (replacement,)).fetchone()[0], 'ok')

    def test_different_sessions_are_never_compacted_together(self):
        for i in range(16):
            self.svc.turn('left' if i % 2 == 0 else 'right', '我们去看电影。晚一点再讨论剧情。', user='viewer')
        scope = self.svc.scope_of('left', 'viewer')
        count = self.svc.engine._conn().execute('SELECT COUNT(*) FROM summaries WHERE scope_id=?', (scope,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_excerpt_does_not_drop_numeric_details(self):
        for i in range(16):
            self.svc.turn('prices', f'我买了一本书。价格是{i + 10}元。', user='reader')
        scope = self.svc.scope_of('prices', 'reader')
        count = self.svc.engine._conn().execute('SELECT COUNT(*) FROM summaries WHERE scope_id=?', (scope,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_two_corroborating_title_mentions_can_form_an_excerpt(self):
        texts = ['今天去了公园。看到了很多花。', '我读《三体》。设定很有趣。',
                 '下午要去买菜。记得带购物袋。', '这次重读《三体》。我关注人物的选择。']
        texts += ['天气不错。想去走走。'] * 12
        for text in texts:
            self.svc.turn('two-mentions', text, user='two-reader')
        scope = self.svc.scope_of('two-mentions', 'two-reader')
        rows = self.svc.engine._conn().execute("SELECT input_refs_json FROM summaries WHERE scope_id=? AND lifecycle_state='active'", (scope,)).fetchall()
        self.assertEqual(len(rows), 1)
        refs = json.loads(rows[0]['input_refs_json'])
        self.assertEqual(len(refs), 2)
        self.assertTrue(all('《三体》' in self.svc.engine.get_trace(ref)['content'] for ref in refs))

    def test_excerpt_excludes_unrelated_turns_in_same_window(self):
        texts = [
            '我读《三体》。故事背景很宏大。', '今天给小狗打了疫苗。明天还要观察。',
            '《三体》让我印象深刻。故事中的文明很神秘。', '午餐吃了番茄面。味道有点酸。',
            '我想重读《三体》。这次想注意人物细节。', '下午准备开会。需要先整理材料。',
            '《三体》的情节很吸引人。读完会推荐给朋友。', '周末要去修自行车。车胎有点漏气。',
            '最近在练钢琴。每天坚持半小时。', '天气变凉了。晚上记得添衣。',
            '想整理旧照片。可能需要几个小时。', '桌上的植物长高了。下周需要换盆。',
            '明天计划去散步。顺便买些水果。', '今天早睡。明天还要工作。',
            '最近看了一部纪录片。内容很有趣。', '这周需要洗衣服。',
        ]
        for text in texts:
            self.svc.turn('mixed-events', text, user='event-reader')
        scope = self.svc.scope_of('mixed-events', 'event-reader')
        rows = self.svc.engine._conn().execute("SELECT text,input_refs_json FROM summaries WHERE scope_id=? AND lifecycle_state='active'", (scope,)).fetchall()
        self.assertEqual(len(rows), 1)
        refs = json.loads(rows[0]['input_refs_json'])
        self.assertEqual(len(refs), 4)
        self.assertTrue(all('《三体》' in self.svc.engine.get_trace(ref)['content'] for ref in refs))
        self.assertNotIn('小狗', rows[0]['text'])
        self.assertNotIn('番茄面', rows[0]['text'])

    def test_excerpt_does_not_turn_questions_into_statements(self):
        texts = ['你看过《三体》吗？', '我读过《三体》。故事很有趣。',
                 '《三体》的作者是谁？', '《三体》的设定很特别。以后再讨论。']
        texts += ['今天的天气很好。可以出去散步。'] * 12
        for text in texts:
            self.svc.turn('question-episode', text, user='question-reader')
        scope = self.svc.scope_of('question-episode', 'question-reader')
        rows = self.svc.engine._conn().execute("SELECT text,input_refs_json FROM summaries WHERE scope_id=? AND lifecycle_state='active'", (scope,)).fetchall()
        # 带未来条件的原话不应被截断成经过校验的摘要。
        self.assertEqual(rows, [])

    def test_excerpt_excludes_question_evidence(self):
        texts = ['你看过《三体》吗？', '我读过《三体》。故事很有趣。',
                 '《三体》的作者是谁？', '《三体》的设定很特别。故事背景很大。']
        texts += ['今天的天气很好。可以出去散步。'] * 12
        for text in texts:
            self.svc.turn('question-filter', text, user='question-reader')
        scope = self.svc.scope_of('question-filter', 'question-reader')
        row = self.svc.engine._conn().execute("SELECT text,input_refs_json FROM summaries WHERE scope_id=? AND lifecycle_state='active'", (scope,)).fetchone()
        self.assertIsNotNone(row)
        refs = json.loads(row['input_refs_json'])
        self.assertEqual(len(refs), 2)
        self.assertTrue(all('？' not in self.svc.engine.get_trace(ref)['content'] for ref in refs))
        self.assertNotIn('作者是谁', row['text'])

    def test_excerpt_keeps_the_sentence_with_the_actual_title(self):
        texts = ['今天听了歌。《你的背包》的旋律很动人。'] * 16
        for text in texts:
            self.svc.turn('music-event', text, user='music-reader')
        scope = self.svc.scope_of('music-event', 'music-reader')
        row = self.svc.engine._conn().execute("SELECT text FROM summaries WHERE scope_id=? AND lifecycle_state='active'", (scope,)).fetchone()
        self.assertIsNotNone(row)
        self.assertIn('《你的背包》的旋律很动人', row['text'])
        self.assertNotIn('今天听了歌', row['text'])

    def test_unrelated_topics_are_not_combined_into_an_excerpt(self):
        subjects = ['西湖游船', '量子计算', '生日蛋糕', '小狗疫苗', '职场面试', '烹饪茄子', '钢琴练习', '自行车胎']
        for _ in range(2):
            for subject in subjects:
                self.svc.turn('mixed', f'今天说说{subject}。关于{subject}有些想法。', user='mixed-viewer')
        scope = self.svc.scope_of('mixed', 'mixed-viewer')
        count = self.svc.engine._conn().execute('SELECT COUNT(*) FROM summaries WHERE scope_id=?', (scope,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_qq_budget_reserves_before_call_and_keeps_unknown_usage(self):
        budget = QQBudget(self.svc.engine)
        policy = BudgetPolicy(per_request=200, daily=200, monthly=300, input_price=1_000_000, output_price=1_000_000, daily_calls=3, cache_price=100_000)
        today = datetime.datetime(2026, 11, 3, tzinfo=datetime.timezone.utc)
        budget.configure(self.scope, policy)
        first = budget.reserve(self.scope, 'chat', 50, 50, now=today)
        second = budget.reserve(self.scope, 'qzone', 50, 50, now=today)
        with self.assertRaises(BudgetDenied):
            budget.reserve(self.scope, 'proactive', 1, 1, now=today)
        self.assertEqual(budget.settle(first, 20, 10, cached_tokens=10), 31)
        third = budget.reserve(self.scope, 'chat', 30, 30, now=today)
        budget.cancel_unstarted(third)
        row = self.svc.engine._conn().execute('SELECT status,amount FROM qq_budget_calls WHERE call_id=?', (second,)).fetchone()
        self.assertEqual((row['status'], row['amount']), ('reserved', 100))
        with self.assertRaises(ValueError):
            budget.settle(first, 20, 10)

    def test_qq_budget_concurrent_reservations_cannot_exceed_limit(self):
        policy = BudgetPolicy(per_request=100, daily=100, monthly=100, input_price=1_000_000, output_price=1_000_000, daily_calls=10)
        now = datetime.datetime(2026, 11, 3, tzinfo=datetime.timezone.utc)
        QQBudget(self.svc.engine).configure(self.scope, policy)
        def reserve():
            try:
                return QQBudget(self.svc.engine).reserve(self.scope, 'chat', 50, 50, now=now)
            except BudgetDenied:
                return None
            finally:
                self.svc.engine.close_thread_connection()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: reserve(), range(4)))
        self.assertEqual(sum(bool(value) for value in results), 1)

    def test_qq_budget_keeps_reservation_when_cache_price_unknown(self):
        budget = QQBudget(self.svc.engine)
        policy = BudgetPolicy(per_request=100, daily=100, monthly=100, input_price=1_000_000, output_price=1_000_000, daily_calls=2)
        with self.assertRaises(BudgetDenied):
            budget.reserve(self.scope, 'chat', 40, 40)
        budget.configure(self.scope, policy)
        call = budget.reserve(self.scope, 'chat', 40, 40)
        with self.assertRaisesRegex(ValueError, '缓存价格未知'):
            budget.settle(call, 10, 10, cached_tokens=5)
        row = self.svc.engine._conn().execute('SELECT status,amount FROM qq_budget_calls WHERE call_id=?', (call,)).fetchone()
        self.assertEqual((row['status'], row['amount']), ('reserved', 80))

    def test_qq_space_and_chat_have_separate_activity_limits(self):
        budget = QQBudget(self.svc.engine)
        policy = BudgetPolicy(per_request=100, daily=1000, monthly=1000, input_price=1_000_000, output_price=1_000_000, daily_calls=10)
        budget.configure(self.scope, policy)
        budget.configure_activity(self.scope, 'qzone', 1)
        budget.reserve(self.scope, 'qzone', 5, 5)
        with self.assertRaisesRegex(BudgetDenied, '该活动'):
            budget.reserve(self.scope, 'qzone', 5, 5)
        self.assertTrue(budget.reserve(self.scope, 'chat', 5, 5))

    def test_qq_settings_hide_token_and_require_budget_to_enable(self):
        store = QQSettings(self.svc.engine)
        settings = {'enabled': True, 'wsUrl': 'ws://127.0.0.1:3001', 'httpUrl': 'http://127.0.0.1:3000',
                    'selfId': '99', 'privateAllow': ['10'], 'groupAllow': [], 'requireMention': True,
                    'provider': 'deepseek', 'model': 'test', 'maxTokens': 100, 'accessToken': 'secret-test'}
        with self.assertRaisesRegex(ValueError, '预算'):
            store.save(self.scope, settings)
        settings['policy'] = {'per_request': 100, 'daily': 1000, 'monthly': 5000, 'input_price': 1000000, 'output_price': 1000000, 'daily_calls': 10}
        store.save(self.scope, settings)
        public = store.get(self.scope)
        self.assertTrue(public['has_token'])
        self.assertNotIn('secret-test', json.dumps(public))
        self.assertEqual(store.active()['accessToken'], 'secret-test')
        settings.pop('accessToken')
        store.save(self.scope, settings)
        self.assertEqual(store.active()['accessToken'], 'secret-test')
        settings['enabled'] = False
        settings['accessToken'] = ''
        store.save(self.scope, settings)
        self.assertIsNone(store.active())
        self.assertFalse(store.get(self.scope)['has_token'])
    def test_qq_schedule_requires_allowlisted_target_and_activity_limit(self):
        store = QQSettings(self.svc.engine)
        raw = {'privateAllow': ['10'], 'activityLimits': {'proactive': 1, 'qzone': 1},
               'schedule': {'proactive': {'enabled': True, 'target': '10', 'intervalMinutes': 60, 'prompt': '问候'},
                            'qzone': {'enabled': True, 'intervalMinutes': 120, 'prompt': '分享', 'visibility': 64}}}
        store.save(self.scope, raw)
        self.assertEqual(store.get(self.scope)['settings']['schedule']['qzone']['visibility'], 64)
        raw['schedule']['proactive']['target'] = '11'
        with self.assertRaisesRegex(ValueError, '白名单'):
            store.save(self.scope, raw)
        raw['schedule']['proactive'].update(channel='group', target='20')
        with self.assertRaisesRegex(ValueError, '白名单'):
            store.save(self.scope, raw)
        raw['groupAllow'] = ['20']
        store.save(self.scope, raw)
        self.assertEqual(store.get(self.scope)['settings']['schedule']['proactive']['channel'], 'group')
        raw['schedule']['qzone']['intervalMinutes'] = 1
        with self.assertRaisesRegex(ValueError, '间隔'):
            store.save(self.scope, raw)
        raw['schedule']['qzone']['intervalMinutes'] = 120
        del raw['activityLimits']['qzone']
        with self.assertRaisesRegex(ValueError, '次数上限'):
            store.save(self.scope, raw)

    def test_other_people_names_do_not_create_user_name_clarifications(self):
        for text in ['知道，他的日文名字是やました よりえ。', '这个名字听起来更像是一家公司。']:
            result = self.svc.turn('names', text, user='name-reader')
            self.assertFalse(result['clarifications'])
        result = self.svc.turn('names', '我的名字叫小鲸。', user='name-reader')
        self.assertTrue(any(item['relation'] == 'nickname' and item['decision'] == 'commit' for item in result['outcomes']))

    def test_elliptical_preferences_remain_searchable_without_false_clarifications(self):
        examples = ['对，超级喜欢，经典就是无法超越的，你说对吧', '看后我也很喜欢。', '他呀，我也特别喜欢。']
        for text in examples:
            result = self.svc.turn('elliptical', text, user='chat-user')
            self.assertFalse(result['clarifications'])
        scope = self.svc.scope_of('elliptical', 'chat-user')
        saved = self.svc.engine._conn().execute('SELECT content FROM traces WHERE scope_id=?', (scope,)).fetchall()
        self.assertEqual([row['content'] for row in saved], examples)

    def test_preference_labels_do_not_create_spurious_clarifications(self):
        sentences = ['我偏爱简洁清爽的界面风格，设计偏好编号0022',
                     '我希望别人讨论问题时先说结论，沟通偏好编号0025',
                     '我希望工作时先处理待办清单，工作安排编号0038',
                     '我偏好咖啡']
        for text in sentences:
            self.svc.turn('preference', text, user='owner', context={'peer_id':'assistant'})
        count = self.svc.engine._conn().execute(
            "SELECT COUNT(*) FROM clarifications WHERE scope_id=? AND status='open'", (self.scope,)
        ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertTrue(self.m.search(self.scope, '偏爱简洁清爽')['hits'])
        self.svc.turn('preference', '我偏爱柔和留白的界面风格', user='owner', context={'peer_id':'assistant'})
        states = self.svc.engine._conn().execute("SELECT state_key FROM states WHERE scope_id=? AND state_key LIKE '%|preference|%'", (self.scope,)).fetchall()
        self.assertTrue(any('柔和留白' in row['state_key'] for row in states))

    def test_debt_verbs_are_not_matched_inside_ordinary_words(self):
        texts = ['功课还是有欠缺的哦，还漏说了两个景点。', '在这部电影里我对导演还了解些。', '片长达到了237分钟，真可算我迄今看过最长的电影。']
        for text in texts:
            result = self.svc.turn('ordinary-words', text, user='word-reader')
            self.assertFalse(any(item['relation'] == 'debt' for item in result['outcomes']))
            self.assertFalse(result['clarifications'])
        debt = self.svc.turn('ordinary-words', '我欠你五元。', user='word-reader', context={'peer_id': 'assistant'})
        self.assertTrue(any(item['relation'] == 'debt' and item['decision'] == 'commit' for item in debt['outcomes']))
        repayment = self.svc.turn('ordinary-words', '我还了你两元。', user='word-reader', context={'peer_id': 'assistant'})
        self.assertTrue(any(item['relation'] == 'debt' for item in repayment['outcomes']))

    def test_routine_topics_and_numbers_do_not_create_debts(self):
        for text in ['我常用文档整理自己的任务，工具记录编号11994',
                     '今年我想在毕业纪念日给朋友写卡片，节日计划编号11989',
                     '我计划读完一本关于植物的书，阅读目标编号11980',
                     '我想做一个会记日程的机器人（并发测试第75次）']:
            self.svc.turn('one', text, user='owner', context={'peer_id':'assistant'})
        conn = self.svc.engine._conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE scope_id=? AND state_key LIKE ? AND void=0", (self.scope, '%|debt|%')).fetchone()[0], 0)
        self.svc.turn('one', '我欠你五十元', user='owner', context={'peer_id':'assistant'})
        sql = "SELECT COUNT(*) FROM events WHERE scope_id=? AND state_key LIKE ? AND void=0"
        before = conn.execute(sql, (self.scope, '%|debt|%')).fetchone()[0]
        self.assertGreater(before, 0)
        self.svc.turn('one', '今年我想在毕业纪念日给朋友写卡片，节日计划编号11989', user='owner', context={'peer_id':'assistant'})
        self.svc.turn('one', '我计划读完一本关于植物的书，阅读目标编号11980', user='owner', context={'peer_id':'assistant'})
        self.assertEqual(conn.execute(sql, (self.scope, '%|debt|%')).fetchone()[0], before)

    def test_film_conversation_does_not_create_debt_clarifications(self):
        texts = [
            '我最喜欢的是《达拉斯买家俱乐部》，它是2013年11月1日上映的，我还去影院看了呢。',
            '这部电影1998年上映，我还是买票看的。',
            '他凭借这首歌获得了2013年的奖项。',
        ]
        for text in texts:
            self.svc.turn('film', text, user='film-viewer', context={'peer_id': 'assistant'})
        scope = self.svc.scope_of('film', 'film-viewer')
        conn = self.svc.engine._conn()
        count = conn.execute("SELECT COUNT(*) FROM clarifications WHERE scope_id=? AND status='open'", (scope,)).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertTrue(self.m.search(scope, '达拉斯买家俱乐部')['hits'])

    def test_attributive_liked_photo_is_not_an_invented_preference(self):
        self.svc.turn('one', '我曾经在西湖边拍过一张很喜欢的照片，经历编号0006', user='owner', context={'peer_id':'assistant'})
        states = self.svc.engine._conn().execute("SELECT state_key FROM states WHERE scope_id=? AND state_key LIKE '%|preference|%'", (self.scope,)).fetchall()
        self.assertFalse(any('的照片' in row['state_key'] for row in states))
        self.assertTrue(self.m.search(self.scope, '西湖边')['hits'])
    def test_attributive_before_a_real_preference_does_not_replace_its_object(self):
        self.svc.turn('one', '为了自己喜欢的音乐，我很喜欢唱歌', user='owner', context={'peer_id':'assistant'})
        states = self.svc.engine._conn().execute("SELECT state_key FROM states WHERE scope_id=? AND state_key LIKE '%|preference|%'", (self.scope,)).fetchall()
        self.assertTrue(any('唱歌' in row['state_key'] for row in states))
        self.assertFalse(any('的音乐' in row['state_key'] for row in states))
    def test_ambiguous_or_overlong_preference_objects_are_not_invented(self):
        texts = ['习惯了我对此表示淡定', '不吃饿呀', '不爱吃', '讨厌别说实话', '讨厌～', '我喜欢甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉']
        for text in texts:
            self.svc.turn('one', text, user='owner', context={'peer_id':'assistant'})
        snap = self.m.snapshot(self.scope)
        contents = [n['content'] for n in snap['nodes'] if n['kind']=='state']
        self.assertFalse(any(any(word in item for word in ('了我对此','饿','甲乙丙丁')) for item in contents))
        self.assertTrue(all(self.m.search(self.scope, text)['hits'] for text in texts))
        self.svc.turn('one', '我习惯喝热茶', user='owner', context={'peer_id':'assistant'})
        snap = self.m.snapshot(self.scope)
        self.assertTrue(any(n['kind']=='state' and '热茶' in n['content'] for n in snap['nodes']))

    def test_movie_title_does_not_become_user_preference(self):
        self.svc.turn('one', '有些了解，是电影《喜欢你》的主题曲。', user='owner', context={'peer_id':'assistant'})
        snap = self.m.snapshot(self.scope)
        self.assertFalse(any(n['kind'] == 'state' and '主题曲' in n['content'] for n in snap['nodes']))
        self.assertTrue(self.m.search(self.scope, '主题曲')['hits'])

    def test_clarification_uses_readable_text_and_links_its_evidence(self):
        self.svc.turn('preference', '我喜欢', user='owner', context={'peer_id':'assistant'})
        nodes = [node for node in self.m.snapshot(self.scope)['nodes'] if node['kind'] == 'clarification']
        self.assertTrue(nodes)
        node = nodes[0]
        self.assertIn('具体对象', node['content'])
        self.assertNotIn('missing_slot', node['content'])
        self.assertTrue(node['evidence'])
        self.assertEqual(self.m.detail(self.scope, node['evidence'][0])['trace']['content'], '我喜欢')

    def test_persona_tags_can_be_edited_deleted_and_added_persistently(self):
        store = ProfileStore(self.svc.engine)
        derived = next(tag for tag in store.card(self.scope, 'owner')['tags'] if '辣' in tag['value'])
        self.m.action(self.scope, 'tag_edit', 'owner', derived['tag_id'], '喜欢清淡', '', derived['value'])
        self.m.action(self.scope, 'tag_add', 'owner', '', '喜欢散步', 'habit', '')
        store.derive_from_memory(self.scope, 'owner')
        tags = store.card(self.scope, 'owner', limit=50)['tags']
        self.assertTrue(any(t['value'] == '喜欢清淡' for t in tags))
        self.assertTrue(any(t['value'] == '喜欢散步' for t in tags))
        self.assertFalse(any(t['value'] == derived['value'] for t in tags))
        manual = next(t for t in tags if t['value'] == '喜欢散步')
        with self.assertRaisesRegex(ManagementError, 'version_conflict'):
            self.m.action(self.scope, 'tag_delete', 'owner', manual['tag_id'], '', '', '过时内容')
        self.m.action(self.scope, 'tag_delete', 'owner', manual['tag_id'], '', '', manual['value'])
        self.svc.close()
        self.svc = MemoryService(self.db, ROOT/'python/specs')
        self.m = Management(self.svc)
        values = [t['value'] for t in ProfileStore(self.svc.engine).card(self.scope, 'owner', limit=50)['tags']]
        self.assertIn('喜欢清淡', values)
        self.assertNotIn('喜欢散步', values)
        self.assertNotIn(derived['value'], values)
        self.assertIn('喜欢清淡', self.svc.context('one', user='owner')['context'])

    def test_clarification_can_be_answered_or_archived(self):
        self.svc.turn('one', '我喜欢', user='owner', context={'peer_id':'assistant'})
        pending = next(n for n in self.m.snapshot(self.scope)['nodes'] if n['kind'] == 'clarification')
        self.m.action(self.scope, 'clarify', '', pending['id'], '我喜欢茉莉花茶', '', '')
        self.assertTrue(self.m.search(self.scope, '我喜欢茉莉花茶')['hits'])
        status = self.svc.engine._conn().execute('SELECT status FROM clarifications WHERE request_id=?', (pending['id'],)).fetchone()[0]
        self.assertEqual(status, 'answered')
        self.svc.turn('one', '我讨厌', user='owner', context={'peer_id':'assistant'})
        pending = next(n for n in self.m.snapshot(self.scope)['nodes'] if n['kind'] == 'clarification')
        self.m.action(self.scope, 'archive', '', pending['id'], '', '', '')
        self.assertEqual(self.svc.engine._conn().execute('SELECT status FROM clarifications WHERE request_id=?', (pending['id'],)).fetchone()[0], 'dismissed')

    def test_emotion_settings_persist_and_reach_prompt(self):
        self.m.action(self.scope, 'emotion_set', '', '嫉妒', '35', '', '')
        self.m.action(self.scope, 'emotion_set', '', '开心', '80', '', '')
        with self.assertRaisesRegex(ManagementError, 'invalid_emotion_intensity'):
            self.m.action(self.scope, 'emotion_set', '', '开心', '101', '', '')
        self.svc.close()
        self.svc = MemoryService(self.db, ROOT/'python/specs')
        self.m = Management(self.svc)
        self.assertEqual({e['name']: e['intensity'] for e in self.m.snapshot(self.scope)['emotions']}, {'嫉妒': 35, '开心': 80})
        context = self.svc.context('one', user='owner')['context']
        self.assertIn('开心 80/100', context)
        self.assertIn('嫉妒 35/100', context)
        self.m.action(self.scope, 'emotion_delete', '', '嫉妒', '', '', '')
        self.assertNotIn('嫉妒', self.svc.context('one', user='owner')['context'])

    def test_manual_profile_and_emotions_do_not_leak_to_other_users(self):
        self.m.action(self.scope, 'tag_add', 'owner', '', '只属于甲的标签', 'habit', '')
        self.m.action(self.scope, 'emotion_set', '', '甲的开心', '80', '', '')
        self.svc.turn('other', '我喜欢苹果', user='another', context={'peer_id':'assistant'})
        other_scope = self.svc.scope_of('other', 'another')
        self.assertNotEqual(self.scope, other_scope)
        other = self.svc.context('other', user='another')['context']
        self.assertNotIn('只属于甲的标签', other)
        self.assertNotIn('甲的开心', other)
        with self.assertRaisesRegex(ManagementError, 'profile_not_found'):
            self.m.action(other_scope, 'tag_add', 'owner', '', '错误用户', 'habit', '')
        self.assertEqual(self.m.snapshot(other_scope)['emotions'], [])

    def test_new_database_starts_without_example_memories(self):
        with tempfile.TemporaryDirectory() as fresh_dir:
            fresh = MemoryService(Path(fresh_dir)/'empty.db', ROOT/'python/specs')
            try:
                self.assertEqual(Management(fresh).catalog()['scopes'], [])
                scope = fresh.scope_of('new-session', 'new-user')
                snapshot = Management(fresh).snapshot(scope)
                self.assertEqual(snapshot['stats']['traces'], 0)
                self.assertEqual(snapshot['nodes'], [])
                self.assertEqual(snapshot['cards'], [])
                self.assertEqual(snapshot['emotions'], [])
                self.assertNotIn('管家', fresh.context('new-session', user='new-user')['context'])
            finally:
                fresh.close()
    def test_management_search_finds_old_records_after_2000_new_traces(self):
        engine = self.svc.engine
        with engine._tx() as conn:
            conn.execute("UPDATE traces SET recorded_at='2000-01-01T00:00:00Z' WHERE trace_id=?", (self.tid,))
            for i in range(2000):
                engine.record_trace(self.scope, f'stress:{i}', 'owner', 'user_statement', f'测试记忆编号{i:04d}')
        self.assertEqual(self.m.snapshot(self.scope)['stats']['traces'], 2001)
        self.assertTrue(self.m.snapshot(self.scope)['truncated'])
        self.assertEqual(self.m.search(self.scope, '我不吃辣')['hits'][0]['trace_id'], self.tid)
        self.assertTrue(any(hit['trace_id'] == self.tid for hit in self.m.memory.search(self.scope, '我不吃辣')))
        self.assertTrue(any(hit['trace_id'] == self.tid for hit in self.m.memory.search(self.scope, '我之前不吃辣')))
        self.assertTrue(any(hit['trace_id'] == self.tid for hit in self.m.memory.search(self.scope, '吃辣')))
        self.assertFalse(any(hit['trace_id'] == self.tid for hit in self.m.memory.search(self.svc.scope_of('different', 'another-user'), '吃辣')))
        self.assertFalse(any(hit['trace_id'] == self.tid for hit in self.m.memory.search(self.scope, '我不吃辣', exclude=self.tid)))
        context = self.svc.context('one', '你以前不吃辣吗', user='owner')['context']
        self.assertIn('我不吃辣', context)
        self.m.forget(self.scope, self.tid, self.hash)
        self.assertFalse(any(hit['trace_id'] == self.tid for hit in self.m.search(self.scope, '我不吃辣')['hits']))
    def test_edit_replaces_and_reindexes_persistently(self):
        result = self.m.edit(self.scope,self.tid,'我喜欢咖啡',self.hash)
        new = result['trace']['trace_id']
        self.assertNotEqual(new,self.tid)
        self.assertEqual(self.svc.engine.get_trace(self.tid)['content'],'')
        self.assertEqual(self.m.search(self.scope,'咖啡')['hits'][0]['trace_id'],new)
        self.assertFalse(self.m.search(self.scope,'吃辣')['hits'])
        card = ProfileStore(self.svc.engine).card(self.scope,'owner')
        self.assertTrue(any('咖啡' in t['value'] for t in card['tags']))
        self.assertFalse(any('辣' in t['value'] for t in card['tags']))
        self.svc.close()
        self.svc = MemoryService(self.db, ROOT/'python/specs')
        self.m = Management(self.svc)
        self.assertEqual(self.m.detail(self.scope,new)['trace']['content'],'我喜欢咖啡')
    def test_stale_edit_and_delete_are_rejected(self):
        for method,args in [(self.m.edit,(self.scope,self.tid,'我喜欢咖啡','stale')),(self.m.forget,(self.scope,self.tid,'stale'))]:
            with self.assertRaisesRegex(ManagementError,'version_conflict'): method(*args)
        self.assertEqual(self.m.detail(self.scope,self.tid)['trace']['content'],'我不吃辣')
    def test_edit_is_atomic_on_encoder_failure(self):
        with patch.object(self.svc.orchestrator.ingest.encoder,'encode',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaisesRegex(RuntimeError,'synthetic failure'):
                self.m.edit(self.scope,self.tid,'我喜欢咖啡',self.hash)
        self.assertEqual(self.m.detail(self.scope,self.tid)['trace']['content'],'我不吃辣')
        self.assertTrue(self.m.search(self.scope,'吃辣')['hits'])
        self.assertEqual(self.svc.engine._conn().execute('SELECT COUNT(*) FROM memory_edits').fetchone()[0],0)
    def test_edit_preserves_anchor(self):
        self.m.memory.mark(self.scope,self.tid,5)
        result = self.m.edit(self.scope,self.tid,'我喜欢咖啡',self.hash)
        self.assertTrue(result['trace']['anchor'])
        self.assertEqual(result['trace']['importance'],5)
    def test_edit_preserves_original_source_and_event_time(self):
        original_time = "2025-03-12T08:30:00+00:00"
        self.svc.engine._conn().execute(
            "UPDATE traces SET source_time=?,source_time_precision='second' WHERE trace_id=?",
            (original_time,self.tid))
        result = self.m.edit(self.scope,self.tid,'我喜欢咖啡',self.hash)
        replacement = self.svc.engine.get_trace(result['trace']['trace_id'])
        self.assertEqual(replacement['source_time'],original_time)
        self.assertEqual(replacement['source_time_precision'],'second')
        rows = self.svc.engine._conn().execute(
            'SELECT e.effective_at FROM events e JOIN event_evidence v ON v.event_id=e.event_id '
            'WHERE v.trace_id=? AND e.void=0',(replacement['trace_id'],)).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row['effective_at']==original_time for row in rows))
    def test_summary_rejects_other_users_deleted_or_missing_evidence(self):
        store = SummaryStore(self.svc.engine)
        other_scope = self.svc.scope_of('other-summary', 'other-user')
        with self.assertRaisesRegex(ValueError, '不属于当前记忆库'):
            store.create(other_scope, [self.tid])
        with self.assertRaisesRegex(ValueError, '不存在'):
            store.create(self.scope, ['missing-trace'])
        self.svc.turn('other-summary', '我喜欢青茶。', user='other-user')
        other = self.svc.engine._conn().execute('SELECT trace_id FROM traces WHERE scope_id=?', (other_scope,)).fetchone()[0]
        foreign = store.create(other_scope, [other])
        with self.assertRaisesRegex(ValueError, '当前记忆库'):
            store.merge(self.scope, [foreign.summary_id])
        self.m.forget(self.scope, self.tid, self.hash)
        with self.assertRaisesRegex(ValueError, '已删除'):
            store.create(self.scope, [self.tid])

    def test_summaries_created_at_the_same_time_have_distinct_ids(self):
        engine = self.svc.engine
        original_clock = engine._clock
        engine._clock = lambda: '2026-11-03T00:00:00+00:00'
        try:
            store = SummaryStore(engine)
            first = store.create(self.scope, [self.tid])
            second = store.create(self.scope, [self.tid])
            self.assertNotEqual(first.summary_id, second.summary_id)
            ids = [item['summary_id'] for item in store.active(self.scope)]
            self.assertLess(ids.index(second.summary_id), ids.index(first.summary_id))
        finally:
            engine._clock = original_clock

    def test_summary_validation_rejects_lost_conditions_and_invented_numbers(self):
        self.svc.turn('summary-validation', '如果明天下雨，我会待在家里。', user='validation-user')
        scope = self.svc.scope_of('summary-validation', 'validation-user')
        trace_id = self.svc.engine._conn().execute('SELECT trace_id FROM traces WHERE scope_id=?', (scope,)).fetchone()[0]
        store = SummaryStore(self.svc.engine)
        lost = store.create(scope, [trace_id], summarizer=lambda _: '我会待在家里。')
        invented = store.create(scope, [trace_id], summarizer=lambda _: '如果明天下雨，我会待在家里三天，花费200元。')
        self.assertEqual(lost.validation_status, 'failed')
        self.assertEqual(invented.validation_status, 'failed')
        self.assertFalse(any(node['kind'] == 'summary' for node in self.m.snapshot(scope)['nodes']))
        self.assertEqual(self.m.snapshot(scope)['stats']['summaries'], 0)

    def test_summary_rejects_lost_arabic_and_chinese_quantities(self):
        for text, omitted in [('我买了3本书。', '我买了书。'), ('我欠你五元。', '我欠你钱。')]:
            self.svc.turn('quantity-summary', text, user='quantity-reader')
            scope = self.svc.scope_of('quantity-summary', 'quantity-reader')
            trace_id = self.svc.engine._conn().execute('SELECT trace_id FROM traces WHERE scope_id=? AND content=?', (scope, text)).fetchone()[0]
            summary = SummaryStore(self.svc.engine).create(scope, [trace_id], summarizer=lambda _: omitted)
            self.assertEqual(summary.validation_status, 'failed')
        self.assertEqual(self.m.snapshot(scope)['stats']['summaries'], 0)

    def test_summary_counts_exclude_archived_and_invalid_results(self):
        store = SummaryStore(self.svc.engine)
        valid = store.create(self.scope, [self.tid])
        invalid = store.create(self.scope, [self.tid], summarizer=lambda _: '错误内容')
        archived = store.create(self.scope, [self.tid])
        conn = self.svc.engine._conn()
        conn.execute("UPDATE summaries SET validation_status='failed' WHERE summary_id=?", (invalid.summary_id,))
        conn.execute("UPDATE summaries SET lifecycle_state='archived' WHERE summary_id=?", (archived.summary_id,))
        snapshot = self.m.snapshot(self.scope)
        visible = [node['id'] for node in snapshot['nodes'] if node['kind'] == 'summary']
        self.assertEqual(visible, [valid.summary_id])
        self.assertEqual(snapshot['stats']['summaries'], 1)
        self.assertEqual(self.m.memory.layers(self.scope)['episodic']['summaries'], 1)

    def test_summary_and_anchor_projections_keep_real_evidence(self):
        self.m.memory.mark(self.scope,self.tid,5)
        summary = SummaryStore(self.svc.engine).create(self.scope,[self.tid])
        snap = self.m.snapshot(self.scope)
        self.assertTrue(any(n['kind']=='summary' and self.tid in n.get('evidence',[]) for n in snap['nodes']))
        self.assertTrue(any(n['kind']=='anchor' and n['trace_id']==self.tid for n in snap['nodes']))
        self.assertTrue(any(e['kind']=='summary_input' and e['source']==self.tid for e in snap['edges']))
        self.assertTrue(any(e['kind']=='anchor_projection' and e['source']==self.tid for e in snap['edges']))
    def test_old_anchor_is_visible_outside_recent_trace_limit(self):
        self.m.memory.mark(self.scope, self.tid, 5)
        self.svc.turn('one', '今天在整理照片', user='owner', context={'peer_id':'assistant'})
        self.svc.engine._conn().execute("UPDATE traces SET recorded_at='2000-01-01T00:00:00Z' WHERE trace_id=?", (self.tid,))
        snap = self.m.snapshot(self.scope, limit=1)
        self.assertFalse(any(n['kind']=='trace' and n['trace_id']==self.tid for n in snap['nodes']))
        self.assertTrue(any(n['kind']=='anchor' and n['trace_id']==self.tid for n in snap['nodes']))
    def test_old_state_evidence_remains_connected_when_recent_traces_fill_graph(self):
        self.svc.turn('one', '今天在整理照片', user='owner', context={'peer_id':'assistant'})
        self.svc.engine._conn().execute("UPDATE traces SET recorded_at='2000-01-01T00:00:00Z' WHERE trace_id=?", (self.tid,))
        snap = self.m.snapshot(self.scope, limit=2)
        states = [n for n in snap['nodes'] if n['kind']=='state' and self.tid in n.get('evidence', [])]
        self.assertTrue(states)
        self.assertTrue(any(n['kind']=='trace' and n['trace_id']==self.tid for n in snap['nodes']))
        self.assertTrue(any(e['kind']=='evidence' and e['source']==self.tid and e['target']==states[0]['id'] for e in snap['edges']))
    def test_activity_requests_are_not_saved_as_names(self):
        for text in ['下次别忘了叫我一起', '怎么不叫我一起', '记得叫我看看', '有热闹叫我看戏', '以后就没人叫我小名了']:
            self.svc.turn('one', text, user='owner', context={'peer_id':'assistant'})
        snap = self.m.snapshot(self.scope)
        self.assertFalse(any(n['kind']=='state' and '|nickname|' in n['id'] for n in snap['nodes']))
        self.assertFalse(any(tag['kind']=='address' for card in snap['cards'] for tag in card['tags']))
        self.svc.turn('one', '以后叫我小鲸', user='owner', context={'peer_id':'assistant'})
        snap = self.m.snapshot(self.scope)
        self.assertTrue(any(tag['kind']=='address' and '小鲸' in tag['value'] for card in snap['cards'] for tag in card['tags']))
        self.svc.turn('one', '不要叫我小鲸，叫我阿蓝', user='owner', context={'peer_id':'assistant'})
        snap = self.m.snapshot(self.scope)
        self.assertTrue(any(tag['kind']=='address' and '阿蓝' in tag['value'] for card in snap['cards'] for tag in card['tags']))
    def test_other_people_and_pet_preferences_do_not_become_owner_preferences(self):
        for text in ['我家兔子不爱吃胡萝卜', '我哥最喜欢吃馒头', '她喜欢喝咖啡']:
            self.svc.turn('one', text, user='owner', context={'peer_id':'assistant'})
        snap = self.m.snapshot(self.scope)
        states = [n['content'] for n in snap['nodes'] if n['kind']=='state']
        self.assertFalse(any(any(word in state for word in ('胡萝卜','馒头','咖啡')) for state in states))
        self.svc.turn('one', '我哥喜欢吃馒头，我喜欢喝茶', user='owner', context={'peer_id':'assistant'})
        self.svc.turn('one', '我喜欢她', user='owner', context={'peer_id':'assistant'})
        snap = self.m.snapshot(self.scope)
        self.assertTrue(any(n['kind']=='state' and '茶' in n['content'] for n in snap['nodes']))
        self.assertTrue(any(n['kind']=='state' and '她' in n['content'] for n in snap['nodes']))
    def test_questions_denials_and_common_words_do_not_create_false_facts(self):
        for text in ['你喜欢喝咖啡吗', '难道你不喜欢甜食', '你爱不爱喝奶茶', '我没有说我不爱喝茶', '我打了一路的哈欠', '我看了一篇流水账文章', '再给你三次机会']:
            self.svc.turn('one', text, user='owner', context={'peer_id':'assistant'})
        snap = self.m.snapshot(self.scope)
        state_texts = [n['content'] for n in snap['nodes'] if n['kind']=='state']
        self.assertFalse(any(any(word in item for word in ('咖啡','甜食','茶')) for item in state_texts))
        count = self.svc.engine._conn().execute("SELECT COUNT(*) FROM events WHERE scope_id=? AND state_key LIKE ? AND void=0", (self.scope, '%|debt|%')).fetchone()[0]
        self.assertEqual(count, 0)
        self.svc.turn('one', '我欠你五十元', user='owner', context={'peer_id':'assistant'})
        count = self.svc.engine._conn().execute("SELECT COUNT(*) FROM events WHERE scope_id=? AND state_key LIKE ? AND void=0", (self.scope, '%|debt|%')).fetchone()[0]
        self.assertGreater(count, 0)
    def test_forgetting_clears_search_cache_and_hides_old_text(self):
        from xujie_memory.memory import _prepared
        _prepared.cache_clear()
        self.m.search(self.scope, '吃辣')
        self.assertGreater(_prepared.cache_info().currsize, 0)
        self.m.forget(self.scope, self.tid, self.hash)
        self.assertEqual(_prepared.cache_info().currsize, 0)
        self.assertFalse(self.m.search(self.scope, '吃辣')['hits'])
        self.assertFalse(self.m.memory.search(self.scope, '吃辣'))

    def test_edit_clears_search_cache_for_replaced_text(self):
        from xujie_memory.memory import _prepared
        _prepared.cache_clear()
        self.m.search(self.scope, '吃辣')
        self.assertGreater(_prepared.cache_info().currsize, 0)
        self.m.edit(self.scope, self.tid, '我喜欢清淡口味', self.hash)
        self.assertEqual(_prepared.cache_info().currsize, 0)
        self.assertFalse(self.m.search(self.scope, '吃辣')['hits'])

    def test_delete_purges_summary_and_person_card(self):
        summary = SummaryStore(self.svc.engine).create(self.scope,[self.tid])
        self.m.forget(self.scope,self.tid,self.hash)
        saved = SummaryStore(self.svc.engine).get(summary.summary_id)
        self.assertEqual(saved['text'],'')
        self.assertEqual(saved['lifecycle_state'],'archived')
        self.assertFalse(self.m.search(self.scope,'吃辣')['hits'])
        self.assertFalse(any('辣' in t['value'] for t in ProfileStore(self.svc.engine).card(self.scope,'owner')['tags']))
        self.assertEqual(self.svc.engine._conn().execute('SELECT COUNT(*) FROM traces_fts WHERE trace_id=?',(self.tid,)).fetchone()[0],0)
        self.svc.close()
        self.svc = MemoryService(self.db, ROOT/'python/specs')
        self.assertTrue(self.svc.engine.get_trace(self.tid)['erased_at'])
    def test_upgrade_repairs_old_erased_summary_and_keeps_private_backup(self):
        summary = SummaryStore(self.svc.engine).create(self.scope,[self.tid])
        self.m.forget(self.scope,self.tid,self.hash)
        # Reproduce the pre-0.7 residue; the old package is not required by tests.
        c = self.svc.engine._conn()
        c.execute("UPDATE summaries SET text='我不吃辣',lifecycle_state='active' WHERE summary_id=?",(summary.summary_id,))
        c.execute('DROP TABLE memory_edits')
        c.execute('DROP TABLE memory_maintenance')
        self.svc.close()
        self.svc = MemoryService(self.db, ROOT/'python/specs')
        self.m = Management(self.svc)
        saved = SummaryStore(self.svc.engine).get(summary.summary_id)
        self.assertEqual(saved['text'],'')
        self.assertEqual(saved['lifecycle_state'],'archived')
        self.assertTrue(Path(str(self.db)+'.pre-0.7.0.bak').is_file())
        self.assertFalse(self.m.search(self.scope,'吃辣')['hits'])
    def test_session_scope_remains_separate(self):
        other = MemoryService(Path(self.tmp.name)/'session.db',ROOT/'python/specs',scope_mode='session')
        try:
            self.assertNotEqual(other.scope_of('s1','owner'),other.scope_of('s2','owner'))
        finally:
            other.close()
    def test_unchanged_edit_is_noop(self):
        self.assertFalse(self.m.edit(self.scope,self.tid,'我不吃辣',self.hash)['changed'])
    def test_invalid_text_rejected(self):
        for s in (' ', '字'*2001):
            with self.assertRaisesRegex(ManagementError,'invalid_content_length'):
                self.m.edit(self.scope,self.tid,s,self.hash)
    def test_concurrent_edit_has_exactly_one_winner(self):
        outcomes = []
        def edit(content):
            try: outcomes.append(self.m.edit(self.scope,self.tid,content,self.hash)['ok'])
            except ManagementError: outcomes.append(False)
        ts = [threading.Thread(target=edit,args=(s,)) for s in ('我喜欢咖啡','我喜欢茶')]
        for t in ts:t.start()
        for t in ts:t.join()
        self.assertEqual(sorted(outcomes),[False,True])

class HttpRegression(unittest.TestCase):
    def test_http_connections_are_released_after_requests(self):
        with tempfile.TemporaryDirectory() as d:
            h = serve(Path(d)/'m.db', ROOT/'python/specs', port=0, token='test-token-only')
            thread = threading.Thread(target=h.serve_forever, daemon=True)
            thread.start()
            try:
                for _ in range(150):
                    request = urllib.request.Request(
                        f'http://127.0.0.1:{h.server_port}/manage/catalog', b'{}',
                        headers={'Content-Type': 'application/json', 'X-Xujie-Token': 'test-token-only'},
                    )
                    with urllib.request.urlopen(request) as response:
                        self.assertEqual(response.status, 200)
                deadline = time.monotonic() + 3
                while len(h.service.engine._conns) > 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertLessEqual(len(h.service.engine._conns), 1)
            finally:
                h.shutdown(); h.server_close(); h.service.close(); thread.join()

    def test_http_auth_read_write_conflict_and_no_cors(self):
        with tempfile.TemporaryDirectory() as d:
            h = serve(Path(d)/'m.db',ROOT/'python/specs',port=0,token='test-token-only')
            thread = threading.Thread(target=h.serve_forever,daemon=True);thread.start()
            url = f'http://127.0.0.1:{h.server_port}'
            def post(path,body,token='test-token-only'):
                req=urllib.request.Request(url+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Xujie-Token':token})
                try:
                    with urllib.request.urlopen(req) as r:return r.status,json.load(r),r.headers
                except urllib.error.HTTPError as r:return r.code,json.load(r),r.headers
            try:
                self.assertEqual(post('/manage/catalog',{},'wrong')[0],401)
                status,_,headers=post('/manage/catalog',{})
                self.assertEqual(status,200);self.assertNotIn('Access-Control-Allow-Origin',headers)
                self.assertEqual(post('/turn',{'session':'s','text':'我不吃辣'})[0],200)
                scope=post('/manage/catalog',{})[1]['scopes'][0]['scope_id']
                config={'scope_id':scope,'policy':{'per_request':200,'daily':300,'monthly':1000,'input_price':1000000,'output_price':1000000,'daily_calls':3}}
                self.assertEqual(post('/qq/budget/configure',config,'wrong')[0],401)
                self.assertEqual(post('/qq/budget/configure',config)[0],200)
                reserved=post('/qq/budget/reserve',{'scope_id':scope,'activity':'chat','input_limit':30,'output_limit':20})
                self.assertEqual(reserved[0],200)
                self.assertEqual(post('/qq/budget/status',{'scope_id':scope})[1]['pending'],50)
                self.assertEqual(post('/turn',{'session':'other','user':'other','text':'另一位用户'})[0],200)
                other_scope=h.service.scope_of('other','other')
                self.assertEqual(post('/qq/budget/settle',{'scope_id':other_scope,'call_id':reserved[1]['call_id'],'input_tokens':1,'output_tokens':1})[0],400)
                settled=post('/qq/budget/settle',{'scope_id':scope,'call_id':reserved[1]['call_id'],'input_tokens':10,'output_tokens':5})
                self.assertEqual((settled[0],settled[1]['amount']),(200,15))
                snap=post('/manage/snapshot',{'scope_id':scope})[1]
                trace=next(n for n in snap['nodes'] if n['kind']=='trace')
                args={'scope_id':scope,'trace_id':trace['trace_id'],'content':'我喜欢咖啡','expected_hash':'stale'}
                self.assertEqual(post('/manage/edit',args)[0],409)
                args['expected_hash']=trace['content_hash']
                status,edited,_=post('/manage/edit',args);self.assertEqual(status,200)
                new=edited['trace']
                self.assertEqual(post('/manage/forget',{'scope_id':scope,'trace_id':new['trace_id'],'expected_hash':new['content_hash']})[0],200)
            finally:
                h.shutdown();h.server_close();h.service.close();thread.join()

if __name__=='__main__':unittest.main(verbosity=2)
