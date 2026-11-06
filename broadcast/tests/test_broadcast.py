import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('broadcast', Path(__file__).resolve().parents[1] / 'broadcast.py')
assert spec is not None and spec.loader is not None
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def fixture():
    line = {'say': 'Test statement.', 'show': 'Test statement.'}
    edition = {'title': 'Test', 'opening': [line.copy()], 'closing': [line.copy()],
               'stories': [{'id': 'one', 'source': 'scope', 'label': 'Scope', 'lines': [line.copy()]}]}
    return {'date': '2026-11-05', 'coverage': 'Synthetic unit test only',
            'sources': {'scope': {'path': 'docs/broadcast.md', 'checked_at': '2026-11-05T00:00:00Z', 'label': 'Scope'}},
            'editions': {lang: copy.deepcopy(edition) for lang in b.LANGS}}


class BroadcastTests(unittest.TestCase):
    def test_three_editions_validate(self):
        self.assertEqual(b.validate(fixture())['date'], '2026-11-05')

    def test_missing_language_rejected(self):
        data = fixture(); del data['editions']['de']
        with self.assertRaises(ValueError): b.validate(data)

    def test_source_escape_rejected(self):
        data = fixture(); data['sources']['scope']['path'] = '../not-allowed'
        with self.assertRaises(ValueError): b.validate(data)

    def test_changed_source_rejected(self):
        data = fixture(); data['sources']['scope']['sha256'] = '0' * 64
        with self.assertRaises(ValueError): b.validate(data)

    def test_translation_story_mismatch_rejected(self):
        data = fixture(); data['editions']['ja']['stories'][0]['id'] = 'other'
        with self.assertRaises(ValueError): b.validate(data)

    def test_blank_story_rejected(self):
        data = fixture(); data['editions']['en']['stories'][0]['lines'] = []
        with self.assertRaises(ValueError): b.validate(data)

    def test_exact_alignment(self):
        # Synthetic alignment, not a claim about real generated audio.
        text_lines = [{'say': 'Ab', 'show': 'AB'}, {'say': 'Cd', 'show': 'CD'}]
        alignment = {'characters': list('Ab\nCd'), 'character_start_times_seconds': [0,.1,.2,.3,.4],
                     'character_end_times_seconds': [.1,.2,.3,.4,.5]}
        self.assertEqual(b.align_lines(text_lines, alignment), [
            {'start': 0, 'end': .2, 'show': 'AB'}, {'start': .3, 'end': .5, 'show': 'CD'}])

    def test_alignment_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            b.align_lines([{'say': 'Ab', 'show': 'AB'}], {'characters': list('bad')})

    def test_audio_requires_review(self):
        with patch.object(b, 'read_json', return_value=fixture()), patch.object(b, 'speech') as speech:
            with self.assertRaises(ValueError): b.render(Path('unused'), audio=True)
            speech.assert_not_called()

    def test_v3_profile_and_caption_tag_exclusion(self):
        data = fixture()
        edition = data['editions']['en']
        edition['opening'][0]['say'] = '[happy] Hello.'
        payload = b.voice_payload(edition, 'en', {'model_id': 'eleven_v3', 'stability': 0.0, 'style': 1.0})
        self.assertEqual(payload['model_id'], 'eleven_v3')
        self.assertEqual(payload['voice_settings']['stability'], 0.0)
        self.assertEqual(payload['language_code'], 'en')
        text = '[happy] Hello.'
        alignment = {'characters': list(text),
                     'character_start_times_seconds': [i / 10 for i in range(len(text))],
                     'character_end_times_seconds': [(i + 1) / 10 for i in range(len(text))]}
        timed = b.align_lines([{'say': text, 'show': 'Hello.'}], alignment)
        self.assertEqual(timed[0]['start'], 0.8)
        self.assertEqual(timed[0]['show'], 'Hello.')

    def test_tags_rejected_on_plain_model(self):
        edition = fixture()['editions']['en']
        edition['opening'][0]['say'] = '[happy] Hello.'
        with self.assertRaises(ValueError): b.voice_payload(edition, 'en')

    def test_invalid_voice_setting_rejected(self):
        with self.assertRaises(ValueError):
            b.voice_payload(fixture()['editions']['en'], 'en', {'model_id': 'eleven_v3', 'stability': 2})

    def test_demo_requires_public_review(self):
        with patch.object(b, 'read_json', return_value=fixture()):
            with self.assertRaises(ValueError): b.render(Path('unused'), channel='demo')

    def test_demo_manifest_is_separate(self):
        data = fixture(); data['public_script_reviewed'] = True
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            with patch.object(b, 'OUT', out), patch.object(b, 'read_json', return_value=data), patch.object(Path, 'read_bytes', return_value=b'fixture'):
                b.render(Path('unused'), channel='demo')
                self.assertTrue((out / 'demo' / 'latest.json').is_file())
                self.assertFalse((out / 'latest.json').exists())

    def test_text_render_and_overwrite_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            with patch.object(b, 'OUT', out), patch.object(b, 'read_json', return_value=fixture()), patch.object(Path, 'read_bytes', return_value=b'fixture'):
                b.render(Path('unused'))
                self.assertTrue((out / '2026-11-05' / 'bulletin.json').is_file())
                with self.assertRaises(ValueError): b.render(Path('unused'))


if __name__ == '__main__':
    unittest.main()
