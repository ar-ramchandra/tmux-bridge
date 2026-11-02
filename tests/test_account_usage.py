import json
import io
import os
from pathlib import Path
import sys
import tempfile
import shlex
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from activity import ActivityStore
from activity_mcp import ActivityReader
import account_usage as usage


class AccountUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def codex(self, now=None, used=33):
        return usage.normalize_codex({'type':'chatgpt','email':'person@example.com','planType':'pro'},
            {'accountId':'workspace-identity','rateLimits':{'primary':{'usedPercent':used,'windowDurationMins':10080,'resetsAt':time.time()+1000}}},now=now)

    def test_account_identity_shared_but_no_credentials_or_full_email(self):
        value = self.codex()
        self.assertEqual(value['account_key'], self.codex()['account_key'])
        self.assertEqual(value['windows'][0]['remaining_percent'],67)
        self.assertEqual(value['windows'][0]['label'],'Weekly')
        self.assertNotIn('person@example.com',json.dumps(value))
        self.assertNotIn('workspace-identity',json.dumps(value))
        self.assertEqual(self.codex(used=100)['windows'][0]['remaining_percent'],0)

    def test_actual_window_duration_not_assumed_to_be_five_hours(self):
        value=usage.normalize_codex({'type':'chatgpt'}, {'rateLimitsByLimitId':{
            'codex':{'primary':{'usedPercent':0,'windowDurationMins':300}},
            'other':{'secondary':{'usedPercent':25,'windowDurationMins':60}}}})
        self.assertEqual([w['label'] for w in value['windows']],['5 hours','other · 60 minutes'])
        self.assertEqual(usage.normalize_codex({}, {})['status'],'signed_out')
        self.assertEqual(usage.normalize_codex({'type':'apiKey'}, {})['status'],'unsupported')
        self.assertEqual(usage.normalize_codex({'type':'chatgpt'}, {})['status'],'unavailable')

    def test_claude_quotas_are_not_context_window_or_cost(self):
        data={'context_window':{'remaining_percentage':94},'cost':{'total_cost_usd':12},'prompt':'PRIVATE',
              'rate_limits':{'five_hour':{'used_percentage':75,'resets_at':1234},'seven_day':{'used_percentage':100}}}
        result=usage.normalize_claude(data,{'email':'person@example.com','subscriptionType':'max'})
        self.assertEqual([w['remaining_percent'] for w in result['windows']],[25,0])
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertNotIn('total_cost',json.dumps(result))
        self.assertEqual(usage.normalize_claude({'cost':{'total_cost_usd':12}}, {})['status'],'unavailable')

    def test_node_boundary_whitelists_fields_and_rejects_bad_numbers(self):
        value=self.codex();value['access_token']='SECRET';value['windows'][0]['remaining_percent']=999
        result=usage.sanitized_snapshot({'accounts':[value],'collected_at':time.time(),'token':'SECRET'})
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertEqual(result['accounts'][0]['windows'][0]['remaining_percent'],67)
        for invalid in [float('nan'),float('inf'),-1,101,True,'50']:
            self.assertIsNone(usage.window('bad',invalid,None))
        value['observed_at']=time.time()+3600
        with self.assertRaises(ValueError):usage.sanitized_snapshot({'accounts':[value]})

    def test_stale_reset_and_node_scope_never_invent_fresh_quota(self):
        store=ActivityStore(self.root/'activity.sqlite3');usage.ensure_table(store)
        account=self.codex(now=time.time()-3600);account['windows'][0]['resets_at']=time.time()-1
        usage.store_snapshot(store,'node',{'accounts':[account],'collected_at':time.time()})
        usage.store_snapshot(store,'private',{'accounts':[self.codex()]})
        self.assertEqual(ActivityReader(store.path,[('node','work')]).accounts(),[])
        values=ActivityReader(store.path,[('node','*')]).accounts()
        self.assertEqual(len(values),1)
        self.assertTrue(values[0]['accounts'][0]['stale'])
        self.assertTrue(values[0]['accounts'][0]['windows'][0]['reset_passed'])
        self.assertEqual(values[0]['accounts'][0]['windows'][0]['remaining_percent'],67)

    def test_statusline_install_preserves_original_and_is_idempotent(self):
        home=self.root/'home';settings=home/'.claude/settings.json';settings.parent.mkdir(parents=True)
        original={'type':'command','command':'printf existing-status','padding':2}
        settings.write_text(json.dumps({'theme':'dark','statusLine':original,'hooks':{'Stop':[]}}))
        agent=self.root/'agent';usage.install_statusline(agent,home)
        first=json.loads(settings.read_text());usage.install_statusline(agent,home)
        self.assertEqual(first,json.loads(settings.read_text()))
        self.assertEqual(first['hooks'],{'Stop':[]})
        self.assertEqual(json.loads((agent/'claude-statusline-original.json').read_text()),original)
        self.assertEqual(settings.stat().st_mode & 0o777,0o600)

    def test_statusline_failure_does_not_drop_valid_quota_or_existing_output(self):
        # Failed identity discovery must not discard the limit that Claude sent.
        raw=json.dumps({'session_id':'x','rate_limits':{'five_hour':{'used_percentage':20}}}).encode()
        with patch.object(usage,'find_cli',return_value='/missing'),patch.object(usage,'claude_auth',side_effect=OSError):
            usage.record_statusline(self.root,raw)
        files=list((self.root/'claude-usage').glob('*.json'))
        self.assertEqual(len(files),1)
        self.assertEqual(json.loads(files[0].read_text())['windows'][0]['remaining_percent'],80)
        usage.private_json(self.root/'claude-statusline-original.json',{'type':'command','command':shlex.join([
            sys.executable,'-c','import json,sys; print("existing:"+json.load(sys.stdin)["session_id"])'])})
        output=io.BytesIO()
        with patch.object(usage,'find_cli',return_value=None),patch.object(usage.sys,'stdout',SimpleNamespace(buffer=output)):
            usage.record_statusline(self.root,raw)
        self.assertEqual(output.getvalue(),b'existing:x\n')

    def test_stdio_protocol_read_is_bounded_and_does_not_start_threads(self):
        script=self.root/'codex'
        script.write_text('#!'+sys.executable+'\n'+'''import json,sys
for line in sys.stdin:
 d=json.loads(line);method=d['method']
 if method=='initialized':continue
 if method=='initialize':result={}
 elif method=='account/read':result={'account':{'type':'chatgpt','email':'test@example.com'}}
 elif method=='account/rateLimits/read':result={'rateLimits':{'primary':{'usedPercent':20,'windowDurationMins':300}}}
 else:raise RuntimeError('Unexpected side effect: '+method)
 print(json.dumps({'id':d['id'],'result':result}),flush=True)
''')
        script.chmod(0o700)
        value=usage.codex_read(str(script),timeout=2)
        self.assertEqual(value['windows'][0]['remaining_percent'],80)
        script.write_text('#!'+sys.executable+'\nimport time\ntime.sleep(30)\n')
        started=time.monotonic()
        with self.assertRaises(TimeoutError):usage.codex_read(str(script),timeout=.1)
        self.assertLess(time.monotonic()-started,3)


if __name__=='__main__':unittest.main()
