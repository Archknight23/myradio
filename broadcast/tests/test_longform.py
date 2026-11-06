import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

spec = importlib.util.spec_from_file_location('broadcast_longform', Path(__file__).resolve().parents[1] / 'broadcast.py')
assert spec is not None and spec.loader is not None
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def fixture():
    line = {'say': 'Synthetic test line.', 'show': 'Synthetic test line.', 'kind': 'report'}
    edition = {'title': 'Synthetic fixture', 'opening': [line.copy()], 'closing': [line.copy()],
               'stories': [{'id': 'one', 'source': 'scope', 'label': 'Scope', 'lines': [line.copy()]}]}
    return {'date': '2026-11-05', 'coverage': 'Synthetic tests, never news', 'languages': list(b.LANGS),
            'sources': {'scope': {'path': 'docs/broadcast.md', 'checked_at': '2026-11-05T00:00:00Z', 'label': 'Scope'}},
            'editions': {lang: copy.deepcopy(edition) for lang in b.LANGS}}


class LongFormTests(unittest.TestCase):
    def test_explicit_five_languages_enforced(self):
        data = fixture()
        self.assertEqual(len(b.validate(data)['editions']), 5)
        del data['editions']['ru']
        with self.assertRaises(ValueError): b.validate(data)

    def test_ten_unique_cves_required(self):
        data = fixture(); data['requirements'] = {'min_cves': 10}
        with self.assertRaises(ValueError): b.validate(data)
        for edition in data['editions'].values():
            edition['stories'] = [{'id': f'cve-{i}', 'source': 'scope', 'label': f'Test {i}', 'desk': 'security',
                                   'cve_id': f'CVE-2026-{10000+i}', 'lines': [
                                       {'say': 'Synthetic report.', 'show': 'Synthetic report.', 'kind': 'report'},
                                       {'say': 'Synthetic commentary.', 'show': 'Synthetic commentary.', 'kind': 'commentary'}]}
                                  for i in range(10)]
        b.validate(data)
        data['editions']['zh']['stories'][0]['lines'].pop()
        with self.assertRaises(ValueError): b.validate(data)

    def test_three_news_items_and_separate_commentary_required(self):
        data = fixture()
        data['requirements'] = {'min_news': 3}
        for edition in data['editions'].values():
            edition['stories'] = [
                {'id': f'news-{i}', 'source': 'scope', 'label': f'Synthetic news {i}', 'desk': 'news',
                 'lines': [{'say': 'Synthetic report.', 'show': 'Synthetic report.', 'kind': 'report'},
                           {'say': 'Synthetic commentary.', 'show': 'Synthetic commentary.', 'kind': 'commentary'}]}
                for i in range(3)]
        b.validate(data)
        missing = copy.deepcopy(data)
        for edition in missing['editions'].values():
            edition['stories'].pop()
        with self.assertRaises(ValueError):
            b.validate(missing)
        data['editions']['ru']['stories'][0]['lines'].pop()
        with self.assertRaises(ValueError):
            b.validate(data)

    def test_collapsed_tail_timestamps_rejected(self):
        text = [{'say': 'A', 'show': 'A'}, {'say': 'B', 'show': 'B'}]
        alignment = {'characters': list('A\nB'),
                     'character_start_times_seconds': [0, .2, .2],
                     'character_end_times_seconds': [.1, .2, .2]}
        with self.assertRaises(ValueError):
            b.align_lines(text, alignment)

    def test_cjk_uses_smaller_request_budget(self):
        for language, budget in [('ja', 1000), ('zh', 1000), ('ru', 4500)]:
            with patch.object(b, 'chunk_lines', return_value=[[{'say': 'A', 'show': 'A'}]]) as partition, \
                 patch.object(b, 'speech_chunk', return_value=[]):
                b.speech({'opening': [], 'stories': [], 'closing': []}, language, Path('synthetic-test-only.mp3'))
                self.assertEqual(partition.call_args.kwargs['limit'], budget)

    def test_chunk_limit_preserves_every_line(self):
        text = [{'say': 'x' * 800, 'show': 'synthetic'} for _ in range(20)]
        chunks = b.chunk_lines(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual([l for chunk in chunks for l in chunk], text)
        self.assertTrue(all(len('\n'.join(l['say'] for l in chunk)) <= 4500 for chunk in chunks))
        with self.assertRaises(ValueError): b.chunk_lines([{'say': 'x' * 4501}])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_actual_pcm_concat_uses_measured_offsets(self):
        # Explicitly synthetic silent audio fixtures test timing mechanics, not real narration.
        chunks = [[{'say': 'A', 'show': 'A'}], [{'say': 'B', 'show': 'B'}]]
        def make_chunk(edition, lang, path, narration):
            wav = path.with_suffix('.wav')
            with wave.open(str(wav), 'wb') as out:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(44100)
                out.writeframes(b'\0\0' * 8820)
            subprocess.run(['ffmpeg','-v','error','-y','-i',str(wav),'-c:a','libmp3lame',str(path)],check=True)
            alignment = {'characters': [edition['opening'][0]['say']],
                         'character_start_times_seconds': [0.02], 'character_end_times_seconds': [0.1]}
            path.with_suffix('.alignment.json').write_text(json.dumps(alignment))
            return [{'start': 0.02, 'end': 0.1, 'show': edition['opening'][0]['show']}]
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'test.mp3'
            with patch.object(b,'chunk_lines',return_value=chunks), patch.object(b,'speech_chunk',side_effect=make_chunk):
                cues = b.speech({'opening': [], 'stories': [], 'closing': []}, 'ru', target)
            self.assertTrue(target.is_file())
            record = json.loads(target.with_suffix('.alignment.json').read_text())
            self.assertEqual(record['format'], 'cf-chunked-alignment/v1')
            self.assertEqual(len(record['chunks']), 2)
            measured = record['chunks'][1]['offset_samples'] / 44100
            self.assertAlmostEqual(cues[1]['start'], measured + .02)
            self.assertGreater(cues[1]['start'], cues[0]['end'])


if __name__ == '__main__':
    unittest.main()
