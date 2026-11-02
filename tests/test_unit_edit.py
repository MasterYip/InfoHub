import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from infohub import config
from infohub.apps import server
from infohub.organize import units
from infohub.store import Store


class UnitEdit(unittest.TestCase):
    def test_edit_and_clear_details_over_http_and_reject_invalid_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = copy.deepcopy(config.DEFAULTS)
            cfg['hub']['database'] = str(Path(tmp) / 'hub.db')
            cfg['hub']['dashboard']['port'] = 0
            cfg['hub']['auth']['users_file'] = str(Path(tmp) / 'users.json')
            cfg['hub']['backup'].update(enabled=False, directory=str(Path(tmp) / 'backups'))
            store = Store(cfg['hub']['database'])
            uid = units.upsert(store, None, kind='todo', title='Old title', summary='Old summary', start_at=100, end_at=200)
            store.db.commit()
            captured, ready = [], threading.Event()
            factory = server.ThreadingHTTPServer
            def capture(address, handler):
                httpd = factory(address, handler)
                captured.append(httpd)
                ready.set()
                return httpd
            with patch.object(server, 'ThreadingHTTPServer', side_effect=capture):
                worker = threading.Thread(target=server.serve, args=(cfg, store, lambda: {}), daemon=True)
                worker.start()
                self.assertTrue(ready.wait(3))
                httpd = captured[0]
                def post(data):
                    conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
                    conn.request('POST', f'/api/unit/{uid}', json.dumps(data),
                                 {'Host':'127.0.0.1:0', 'Content-Type':'application/json'})
                    response = conn.getresponse()
                    result = json.loads(response.read())
                    conn.close()
                    return response.status, result
                try:
                    status, result = post({'title':'New title', 'summary':'', 'priority':'P1', 'kind':'event',
                                           'start_at':300, 'end_at':400, 'due_at':500, 'add_tags':['project:test']})
                    self.assertEqual(status, 200, result)
                    saved = units.get(store, uid)
                    self.assertEqual((saved['title'], saved['summary'], saved['kind'], saved['priority']), ('New title','','event','P1'))
                    self.assertIn('type:event', saved['tags'])
                    self.assertIn('project:test', saved['tags'])
                    for invalid in ({'title':' '}, {'priority':'wrong'}, {'due_at':True}, {'due_at':'tomorrow'},
                                    {'start_at':600}, {'end_at':100}, {'summary':None}, {'status':'wrong'}):
                        self.assertEqual(post(invalid)[0], 400, invalid)
                    self.assertEqual(units.get(store, uid)['start_at'], 300)
                    self.assertEqual(post({'start_at':None,'end_at':None,'due_at':None,'priority':None})[0], 200)
                    self.assertIsNone(units.get(store, uid)['due_at'])
                    self.assertEqual(post({'status':'done'})[0], 200)
                    self.assertEqual(post({'status':'open'})[0], 200)
                finally:
                    httpd.shutdown()
                    worker.join(timeout=5)
                    httpd.server_close()
                    store.db.close()
