#!/usr/bin/env python3
"""Evidence-backed bulletin renderer and allowlisted loopback preview."""
import argparse
import base64
from datetime import date
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'broadcast'
OUT = HERE / 'output'
LANGS = ('en', 'ja', 'de', 'ru', 'zh')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def validate(data):
    date.fromisoformat(data['date'])
    if data['date'] != date.fromisoformat(data['date']).isoformat():
        raise ValueError('Use an ISO YYYY-MM-DD date')
    actual_languages = set(data['editions'])
    requested_languages = data.get('languages')
    if not {'en', 'ja', 'de'} <= actual_languages or not actual_languages <= set(LANGS):
        raise ValueError('EN/JA/DE required; supported editions are en, ja, de, ru, zh')
    if requested_languages is not None and (len(requested_languages) != len(set(requested_languages)) or set(requested_languages) != actual_languages):
        raise ValueError('Every explicitly requested language must be present exactly once')
    if not data.get('coverage') or not data.get('sources'):
        raise ValueError('Coverage and sources are mandatory')
    for key, src in data['sources'].items():
        path = (ROOT / src['path']).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError(f'Invalid source: {key}')
        if not src.get('checked_at'):
            raise ValueError('Source check timestamp required')
        if src.get('sha256') and hashlib.sha256(path.read_bytes()).hexdigest() != src['sha256']:
            raise ValueError(f'Source changed since editorial review: {key}')
    ids = None
    for lang, edition in data['editions'].items():
        if not edition.get('title') or not edition.get('stories'):
            raise ValueError(f'Missing title/stories: {lang}')
        if not edition.get('opening') or not edition.get('closing'):
            raise ValueError('Opening and closing must contain lines')
        story_ids = [(s['id'], s['source'], s.get('desk'), s.get('cve_id')) for s in edition['stories']]
        if len(story_ids) != len({s['id'] for s in edition['stories']}):
            raise ValueError('Duplicate story ids')
        if ids is not None and story_ids != ids:
            raise ValueError('All editions must cover the same stories in order')
        ids = story_ids
        for story in edition['stories']:
            if story['source'] not in data['sources'] or not story.get('label') or not story.get('lines'):
                raise ValueError('Every story needs a known source, label, and lines')
        requirements = data.get('requirements', {})
        cves = [s for s in edition['stories'] if s.get('desk') == 'security']
        if requirements.get('min_cves', 0):
            identifiers = [s.get('cve_id', '') for s in cves]
            if len(set(identifiers)) < requirements['min_cves'] or any(not re.fullmatch(r'CVE-\d{4}-\d{4,}', cve) for cve in identifiers):
                raise ValueError('Required minimum of distinct, valid CVE stories not met')
        for story in cves + [s for s in edition['stories'] if s.get('desk') == 'news']:
            if not {'report', 'commentary'} <= {l.get('kind') for l in story['lines']}:
                raise ValueError('Each CVE/news item requires sourced report and separate commentary')
        if sum(s.get('desk') == 'news' for s in edition['stories']) < requirements.get('min_news', 0):
            raise ValueError('Required news story minimum not met')
        for line in lines(edition):
            if not all(isinstance(line.get(k), str) and line[k].strip() for k in ('say', 'show')):
                raise ValueError('Every line needs nonempty say/show strings')
    return data


def lines(edition):
    return edition['opening'] + [l for s in edition['stories'] for l in s['lines']] + edition['closing']


def align_lines(text_lines, alignment):
    text = '\n'.join(l['say'] for l in text_lines)
    if ''.join(alignment['characters']) != text:
        raise ValueError('Alignment text differs from submitted script; refusing fabricated captions')
    starts, ends = alignment['character_start_times_seconds'], alignment['character_end_times_seconds']
    if len(starts) != len(text) or len(ends) != len(text):
        raise ValueError('Incomplete alignment')
    result, cursor = [], 0
    for line in text_lines:
        last = cursor + len(line['say']) - 1
        # Delivery tags are performed, not caption text. Seek to spoken characters.
        tagged = {i for m in re.finditer(r'\[[^\]\n]+\]', line['say']) for i in range(m.start(), m.end())}
        spoken = [i for i, c in enumerate(line['say']) if i not in tagged and not c.isspace()]
        if not spoken:
            raise ValueError('Narration line contains no spoken text')
        start, end = starts[cursor + spoken[0]], ends[cursor + spoken[-1]]
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start or (result and start < result[-1]['end']):
            raise ValueError('Invalid/nonmonotonic alignment')
        result.append({'start': start, 'end': end, 'show': line['show']})
        cursor = last + 2
    return result


def voice_payload(edition, lang, narration=None):
    profile = narration or {}
    model = profile.get('model_id', 'eleven_multilingual_v2')
    if model not in ('eleven_multilingual_v2', 'eleven_v3'):
        raise ValueError('Unsupported broadcast speech model')
    settings = {'stability': profile.get('stability', 0.5),
                'similarity_boost': profile.get('similarity_boost', 0.75)}
    for name in ('style', 'use_speaker_boost'):
        if name in profile:
            settings[name] = profile[name]
    for name in ('stability', 'similarity_boost', 'style'):
        if name in settings and (not isinstance(settings[name], (int, float)) or not 0 <= settings[name] <= 1):
            raise ValueError(f'Invalid narration setting: {name}')
    payload = {'text': '\n'.join(l['say'] for l in lines(edition)), 'model_id': model, 'voice_settings': settings}
    if model == 'eleven_v3':
        payload['language_code'] = lang
    elif '[' in payload['text']:
        raise ValueError('Delivery tags require eleven_v3')
    return payload


def speech_chunk(edition, lang, path, narration=None):
    # Credentials are supplied by the caller, never stored in the repository.
    key = os.environ.get('ELEVENLABS_API_KEY')
    if not key:
        raise ValueError('Set ELEVENLABS_API_KEY in the process environment')
    voice = os.environ.get('CF_BROADCAST_VOICE_ID')
    if not voice or not voice.isalnum():
        raise ValueError('Configure a valid ElevenLabs voice_id')
    payload = voice_payload(edition, lang, narration)
    cache = OUT / '.speech-cache'
    cache.mkdir(parents=True, exist_ok=True)
    cache_id = hashlib.sha256(json.dumps({'voice': voice, 'payload': payload}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    cached_audio, cached_alignment = cache / (cache_id + '.mp3'), cache / (cache_id + '.json')
    if cached_audio.is_file() and cached_alignment.is_file():
        audio = cached_audio.read_bytes()
        alignment = read_json(cached_alignment)
        timed = align_lines(lines(edition), alignment)
    else:
        req = Request(f'https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps',
                      data=json.dumps(payload).encode(), headers={'xi-api-key': key, 'Content-Type': 'application/json'})
        with urlopen(req, timeout=240) as response:
            result = json.load(response)
        alignment = result['alignment']
        timed = align_lines(lines(edition), alignment)
        audio = base64.b64decode(result['audio_base64'], validate=True)
        if len(audio) < 100:
            raise ValueError('Speech response is empty')
        cached_audio.write_bytes(audio)
        cached_alignment.write_text(json.dumps(alignment, ensure_ascii=False), encoding='utf-8')
        cached_audio.chmod(0o600)
        cached_alignment.chmod(0o600)
    path.write_bytes(audio)
    path.with_suffix('.alignment.json').write_text(json.dumps(alignment, ensure_ascii=False), encoding='utf-8')
    return timed


def chunk_lines(text_lines, limit=4500):
    chunks, current, length = [], [], 0
    for line in text_lines:
        size = len(line['say'])
        if size > limit:
            raise ValueError('A single narration line exceeds the speech limit; split it into sentences')
        if current and length + 1 + size > limit:
            chunks.append(current)
            current, length = [], 0
        if current:
            length += 1
        current.append(line)
        length += size
    if current:
        chunks.append(current)
    return chunks


def speech(edition, lang, path, narration=None):
    # Long CJK requests can return collapsed tail timings despite API success.
    # Bound native-script requests more conservatively and reject zero-length cues.
    chunks = chunk_lines(lines(edition), limit=1000 if lang in ('ja', 'zh') else 4500)
    if len(chunks) == 1:
        return speech_chunk(edition, lang, path, narration)
    if not shutil.which('ffmpeg'):
        raise ValueError('ffmpeg required for long-form narration')
    timed, records, offset_samples = [], [], 0
    with tempfile.TemporaryDirectory(prefix='.speech-', dir=path.parent) as folder:
        folder = Path(folder)
        combined = folder / 'complete.wav'
        with wave.open(str(combined), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(44100)
            for index, chunk in enumerate(chunks):
                part = folder / f'{index:03}.mp3'
                chunk_edition = {'opening': chunk, 'stories': [], 'closing': []}
                cues = speech_chunk(chunk_edition, lang, part, narration)
                decoded = folder / f'{index:03}.wav'
                subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(part), '-ar', '44100', '-ac', '1', '-c:a', 'pcm_s16le', str(decoded)], check=True)
                with wave.open(str(decoded), 'rb') as audio:
                    frames = audio.getnframes()
                    output.writeframes(audio.readframes(frames))
                duration = frames / 44100
                offset = offset_samples / 44100
                for cue in cues:
                    if cue['start'] >= duration or cue['end'] > duration + 0.05:
                        raise ValueError('Provider alignment exceeds decoded audio boundary')
                    timed.append(dict(cue, start=offset + min(cue['start'], duration), end=offset + min(cue['end'], duration)))
                records.append({'offset_samples': offset_samples, 'decoded_samples': frames,
                                'input_audio_sha256': hashlib.sha256(part.read_bytes()).hexdigest(),
                                'alignment': read_json(part.with_suffix('.alignment.json'))})
                offset_samples += frames
                print(f'{lang}: chunk {index + 1}/{len(chunks)} synthesized and measured', flush=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(combined), '-c:a', 'libmp3lame', '-q:a', '2', str(path)], check=True)
    path.with_suffix('.alignment.json').write_text(json.dumps({'format': 'cf-chunked-alignment/v1', 'sample_rate': 44100,
        'total_decoded_samples': offset_samples, 'chunks': records,
        'output_audio_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}, ensure_ascii=False), encoding='utf-8')
    return timed


def render(source, audio=False, replace=False, channel='internal'):
    if channel not in ('internal', 'demo'):
        raise ValueError('Unknown broadcast channel')
    data = validate(read_json(source))
    if channel == 'demo' and (data.get('public_script_reviewed') is not True or data.get('source_health')):
        raise ValueError('Demo requires a reviewed public script and no raw internal source-health records')
    if audio and data.get('cloud_tts_approved') is not True:
        raise ValueError('Set cloud_tts_approved=true only after reviewing the exact scripts for cloud speech')
    output_root = OUT / 'demo' if channel == 'demo' else OUT
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / data['date']
    if target.exists() and not replace:
        raise ValueError('Edition already exists; review and use --replace explicitly')
    with tempfile.TemporaryDirectory(prefix='.staging-', dir=HERE) as temp:
        stage = Path(temp)
        public = {'date': data['date'], 'coverage': data['coverage'], 'editions': {},
                  'input_sha256': hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                  'source_health': data.get('source_health', []),
                  'narration': data.get('narration', {'model_id': 'eleven_multilingual_v2'})}
        for lang, edition in data['editions'].items():
            entry = dict(edition)
            entry['lines_timed'] = speech(edition, lang, stage / f'{lang}.mp3', data.get('narration')) if audio else []
            entry['audio'] = (('demo/' if channel == 'demo' else '') + f'{data["date"]}/{lang}.mp3') if audio else None
            # Source references remain local labels, never raw source files or private paths.
            for story in entry['stories']:
                story['source_label'] = data['sources'][story['source']]['label']
            public['editions'][lang] = entry
            print(f'{lang}: {"audio + aligned captions" if audio else "text only"}', flush=True)
        (stage / 'bulletin.json').write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding='utf-8')
        if target.exists():
            # Replacement is explicit; preserve old output until the new set is complete.
            backup = output_root / (data['date'] + '.previous')
            if backup.exists():
                shutil.rmtree(backup)
            target.rename(backup)
        shutil.move(str(stage), target)
    latest_temp = output_root / '.latest.json.tmp'
    latest_temp.write_text(json.dumps({'date': data['date']}), encoding='utf-8')
    latest_temp.replace(output_root / 'latest.json')
    print(f'Rendered {target}')


def serve(port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = unquote(urlsplit(self.path).path)
            if route in ('/', '/index.html'):
                path, mime = HERE / 'player.html', 'text/html; charset=utf-8'
            elif route in ('/demo', '/demo/', '/demo/index.html'):
                path, mime = HERE / 'demo.html', 'text/html; charset=utf-8'
            elif route == '/demo/latest.json':
                path, mime = OUT / 'demo/latest.json', 'application/json; charset=utf-8'
            elif route == '/latest.json':
                path, mime = OUT / 'latest.json', 'application/json; charset=utf-8'
            else:
                parts = route.strip('/').split('/')
                is_demo = parts[0] == 'demo'
                if is_demo:
                    parts = parts[1:]
                try:
                    valid_date = len(parts) == 2 and date.fromisoformat(parts[0]).isoformat() == parts[0]
                except ValueError:
                    valid_date = False
                if not valid_date or parts[1] not in ('bulletin.json', *(lang + '.mp3' for lang in LANGS)):
                    self.send_error(404)
                    return
                path = (OUT / 'demo' if is_demo else OUT) / parts[0] / parts[1]
                mime = 'audio/mpeg' if path.suffix == '.mp3' else 'application/json; charset=utf-8'
            if not path.resolve().is_relative_to(HERE) or path.is_symlink() or not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            size = len(body)
            status, start, end = 200, 0, size - 1
            range_header = self.headers.get('Range')
            if range_header and mime == 'audio/mpeg':
                import re
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', range_header)
                if not match:
                    self.send_error(416)
                    return
                start = int(match[1])
                end = min(int(match[2]) if match[2] else size - 1, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
                status = 206
                body = body[start:end + 1]
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            if mime == 'audio/mpeg':
                self.send_header('Accept-Ranges', 'bytes')
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'none'; frame-src 'none'; connect-src 'self'; media-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
    print(f'Internal CF preview: http://127.0.0.1:{port} (Ctrl-C to stop)', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('validate').add_argument('source', type=Path)
    r = sub.add_parser('render')
    r.add_argument('source', type=Path)
    r.add_argument('--audio', action='store_true')
    r.add_argument('--replace', action='store_true')
    r.add_argument('--channel', choices=['internal', 'demo'], default='internal')
    s = sub.add_parser('serve')
    s.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    try:
        if args.command == 'validate':
            validate(read_json(args.source))
            print('Valid: ' + ', '.join(read_json(args.source)['editions']) + '; matching story ids; local sources present')
        elif args.command == 'render':
            render(args.source, args.audio, args.replace, args.channel)
        else:
            serve(args.port)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f'Broadcast failed: {exc}\n')


if __name__ == '__main__':
    main()
