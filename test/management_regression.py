import json
import sys
import tempfile
import threading
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
    def test_management_search_finds_old_records_after_2000_new_traces(self):
        engine = self.svc.engine
        with engine._tx() as conn:
            conn.execute("UPDATE traces SET recorded_at='2000-01-01T00:00:00Z' WHERE trace_id=?", (self.tid,))
            for i in range(2000):
                engine.record_trace(self.scope, f'stress:{i}', 'owner', 'user_statement', f'测试记忆编号{i:04d}')
        self.assertEqual(self.m.snapshot(self.scope)['stats']['traces'], 2001)
        self.assertTrue(self.m.snapshot(self.scope)['truncated'])
        self.assertEqual(self.m.search(self.scope, '我不吃辣')['hits'][0]['trace_id'], self.tid)
        self.assertFalse(any(hit['trace_id'] == self.tid for hit in self.m.memory.search(self.scope, '我不吃辣')))
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
    def test_summary_and_anchor_projections_keep_real_evidence(self):
        self.m.memory.mark(self.scope,self.tid,5)
        summary = SummaryStore(self.svc.engine).create(self.scope,[self.tid])
        snap = self.m.snapshot(self.scope)
        self.assertTrue(any(n['kind']=='summary' and self.tid in n.get('evidence',[]) for n in snap['nodes']))
        self.assertTrue(any(n['kind']=='anchor' and n['trace_id']==self.tid for n in snap['nodes']))
        self.assertTrue(any(e['kind']=='summary_input' and e['source']==self.tid for e in snap['edges']))
        self.assertTrue(any(e['kind']=='anchor_projection' and e['source']==self.tid for e in snap['edges']))
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
