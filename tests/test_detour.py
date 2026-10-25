import base64
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import generate_detour as detour
import generate_podcast as shared


class DetourTests(unittest.TestCase):
    def setUp(self):
        self.cfg = detour.load_config()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / 'docs/detour'
        self.out.mkdir(parents=True)
        (self.root / 'config').mkdir()
        (self.root / 'config/detour-topics.json').write_text('[]')
        self.today = dt.datetime(2026, 11, 1, 7, 30, tzinfo=shared.JST)
        self.sources = [dict(url='https://www.nobelprize.org/example/', publisher='Source <title>',
                             sha256='abc', retrieved_at='2026-11-01T00:00:00Z',
                             text='An actual source sentence explains the discovery and its evidence. ' * 100)]
        self.episode = dict(title='発見 <script>alert(1)</script>', description='a & b',
                            script='Risa: hello\nKenta: hello', claims=[])
        self.topic = dict(id='test', category='自然')

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *, duration=420, skip_tts=False):
        with patch.object(detour, 'ROOT', self.root), \
             patch.dict(os.environ, {'GEMINI_API_KEY': 'test-only', 'GCP_SA_KEY': '{}'}), \
             patch.object(detour, 'collect_story', return_value=(self.topic, self.sources)), \
             patch.object(detour, 'build_episode', return_value=self.episode), \
             patch.object(shared, 'synthesize', return_value=b'pcm'), \
             patch.object(shared, 'pcm_to_mp3', side_effect=lambda pcm, path: path.write_bytes(b'mp3')), \
             patch.object(shared, 'mp3_duration_seconds', return_value=duration):
            return detour.run(self.cfg, '2026-11-01', self.today, skip_tts=skip_tts, out=self.out)

    def test_workflow_calendar_guard_and_shell_syntax(self):
        import yaml
        workflow = yaml.load((detour.ROOT / '.github/workflows/morning-detour.yml').read_text(), Loader=yaml.BaseLoader)
        steps = workflow['jobs']['generate']['steps']
        guard = next(step['run'] for step in steps if step.get('id') == 'window')
        code = '\n'.join(guard.splitlines()[1:-1])
        for days, expected in ((0, 'true'), (13, 'true'), (14, 'false'), (-1, 'false')):
            start = (dt.datetime.now(shared.JST).date() - dt.timedelta(days=days)).isoformat()
            output = self.root / f'window-{days}.txt'
            env = dict(os.environ, DETOUR_START_DATE=start, GITHUB_OUTPUT=str(output))
            subprocess.run([sys.executable, '-c', code], env=env, check=True)
            self.assertEqual(output.read_text(), f'active={expected}\n')
        for step in steps:
            if 'run' in step:
                subprocess.run(['bash', '-n'], input=step['run'], text=True, check=True)

    def test_calendar_window_and_disabled(self):
        for day, expected in [(dt.date(2026,10,31),None),(dt.date(2026,11,1),1),
                              (dt.date(2026,11,14),14),(dt.date(2026,11,15),None)]:
            self.assertEqual(detour.experiment_day('2026-11-01',day),expected)
        self.assertIsNone(detour.experiment_day(None, self.today.date()))
        with patch.object(detour, 'collect_story') as collect:
            self.assertEqual(detour.run(self.cfg,None,self.today,out=self.out),0)
            collect.assert_not_called()
        with self.assertRaises(ValueError):
            detour.experiment_day('not-a-date', self.today.date())

    def test_publish_isolated_feed_page_and_public_script(self):
        news = self.root / 'docs/feed.xml'
        news.write_text('news sentinel')
        self.invoke()
        self.assertEqual(news.read_text(), 'news sentinel')
        feed = ET.parse(self.out / 'feed.xml').getroot()
        item = feed.find('channel/item')
        self.assertEqual(item.findtext('guid'), 'detour-2026-11-01.mp3')
        self.assertIn('/detour/episodes/', item.find('enclosure').attrib['url'])
        self.assertEqual(item.findtext('title'), self.episode['title'])
        page = (self.out / 'index.html').read_text()
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script>alert', page)
        script = json.loads((self.out / 'episodes/2026-11-01.json').read_text())
        self.assertNotIn('text', script['sources'][0])
        self.assertNotIn('claims', script)
        meta = detour.read_meta(self.out)
        self.assertEqual(meta[0]['day'], 1)
        self.assertEqual(meta[0]['topic_id'], 'test')

    def test_failed_audio_never_publishes(self):
        marker = self.out / 'feed.xml'
        marker.write_text('old feed')
        for duration in (0,329,511):
            with self.assertRaises(ValueError):
                self.invoke(duration=duration)
            self.assertEqual(marker.read_text(), 'old feed')
            self.assertFalse((self.out / 'episodes.json').exists())
            self.assertFalse((self.out / 'episodes').exists())

    def test_preview_never_writes_public_output(self):
        before = list(self.out.rglob('*'))
        self.invoke(skip_tts=True)
        self.assertEqual(list(self.out.rglob('*')), before)
        self.assertTrue((self.root / 'work/detour-preview/2026-11-01.json').exists())

    def test_duplicate_no_api_or_output_mutation(self):
        self.invoke()
        before = {str(p):p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        with patch.object(detour,'collect_story',side_effect=AssertionError('API must not run')):
            detour.run(self.cfg,'2026-11-01',self.today,out=self.out)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.out.rglob('*') if p.is_file()})

    def test_start_date_cannot_reuse_old_experiment(self):
        self.invoke()
        with self.assertRaises(ValueError):
            detour.run(self.cfg,'2026-10-31',self.today+dt.timedelta(days=1),out=self.out)

    def test_topic_rotation_and_source_failure(self):
        topics = [dict(id='used',category='自然',sources=[{}]),
                  dict(id='same',category='自然',sources=[{}]),
                  dict(id='fresh',category='ことば',sources=[{}])]
        with patch.object(detour,'fetch_source',return_value=self.sources[0]):
            topic,_ = detour.collect_story(topics,[dict(topic_id='used',category='自然')])
            self.assertEqual(topic['id'],'fresh')
        with patch.object(detour,'fetch_source',side_effect=ValueError('unavailable')):
            with self.assertRaises(RuntimeError):
                detour.collect_story([dict(id='x',category='a',sources=[{'url':'https://example.org'}])],[])

    def valid_episode(self):
        sentence = '小さな手がかりをどう読み解くか。これは私たちAIホストの見方ですが、別の見方もできそうですね。'
        episode = dict(title='発見',description='概要',
                       script='\n'.join(('Risa' if i%2==0 else 'Kenta')+': '+sentence*2 for i in range(20)),
                       claims=[dict(fact='発見の要約',source=0,evidence='An actual source sentence explains the discovery and its evidence.') for _ in range(4)])
        return episode

    def test_script_labels_length_balance_and_evidence(self):
        episode = self.valid_episode()
        detour.validate_episode(episode,self.cfg,self.sources)
        for mutate in [lambda e:e.update(script=e['script'].replace('Risa:','Other:')),
                       lambda e:e.update(script='Risa: 短い\nKenta: 短い'),
                       lambda e:e['claims'][0].update(evidence='Fabricated sentence not in the source.'),
                       lambda e:e['claims'][0].update(source=99)]:
            bad = copy.deepcopy(episode); mutate(bad)
            with self.assertRaises(ValueError):
                detour.validate_episode(bad,self.cfg,self.sources)

    def test_editor_rejection_stops_before_audio(self):
        for field in ('grounded','natural_dialogue','no_fomo','no_fake_experience'):
            audit = dict.fromkeys(('grounded','natural_dialogue','no_fomo','no_fake_experience'), True)
            audit[field] = False
            with patch.object(detour,'ask_json',side_effect=[self.valid_episode(),audit]):
                with self.assertRaises(ValueError):
                    detour.build_episode(self.cfg,dict(hook='入口'),self.sources,'fake')

    def test_html_parser_excludes_navigation_and_scripts(self):
        parser = detour.ArticleText()
        parser.feed('<nav><p>nav</p></nav><main><script>bad()</script><p>Real <b>source</b>.</p><footer><p>footer</p></footer></main>')
        self.assertEqual(parser.text(), 'Real source.')
        for url in ('http://www.nobelprize.org/x','https://example.org/x','https://localhost/x'):
            with self.assertRaises(ValueError): detour.fetch_source(dict(url=url))


class NewsRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Compare actual upstream implementation, not a hand-written approximation.
        source = subprocess.check_output(['git','show','29aa14d2f077710bc5289c700c96690197ae2ffe:src/generate_podcast.py'],text=True)
        cls.upstream = types.ModuleType('upstream_news')
        cls.upstream.__file__ = shared.__file__
        exec(compile(source,'upstream_news.py','exec'),cls.upstream.__dict__)

    def test_existing_feed_and_page_are_identical(self):
        # サムネイル未設定時は、基準コミットと完全に同じ出力であること
        cfg = copy.deepcopy(shared.load_config())
        cfg['podcast'].pop('image', None)
        meta = [dict(title='News & test',description='Description',file='2026-11-01.mp3',
                     pub_date='2026-11-01T07:00:00+09:00',size=123,duration=480)]
        class FixedDateTime(dt.datetime):
            @classmethod
            def now(cls,tz=None): return cls(2026,11,1,0,0,tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b,patch.object(dt,'datetime',FixedDateTime):
            with patch.object(self.upstream,'DOCS_DIR',Path(a)),patch.object(shared,'DOCS_DIR',Path(b)):
                self.upstream.build_feed(cfg,meta)
                shared.build_feed(cfg,meta)
            for file in ('index.html','feed.xml'):
                self.assertEqual((Path(a)/file).read_bytes(),(Path(b)/file).read_bytes())

    def test_existing_gemini_tts_request_is_identical(self):
        cfg = shared.load_config()
        response = {'candidates':[{'content':{'parts':[{'inlineData':{'data':base64.b64encode(b'pcm').decode()}}]}}]}
        calls = []
        for module in (self.upstream,shared):
            with patch.object(module,'gemini_with_fallback',return_value=response) as api:
                self.assertEqual(module._synthesize_gemini(cfg,'Risa: hi\nKenta: hi','fake'),b'pcm')
                calls.append(api.call_args)
        self.assertEqual(calls[0].args,calls[1].args)
        self.assertEqual(calls[0].kwargs["validate"].__name__, calls[1].kwargs["validate"].__name__)


if __name__ == '__main__': unittest.main()
