# Broadcast production

Report of Work by Cognito Engine — X-106-S61

This companion renderer is separate from the upstream myradio player. It validates a reviewed JSON rundown, produces text editions, and can generate ElevenLabs narration and aligned captions when explicitly enabled. It serves its preview only on loopback. It does not automatically collect news, write scripts, translate, publish, or deploy. The Foundry's private ingestion adapters and configuration are not included.

## Try the example without an API key

From the repository root:

    python3 broadcast/broadcast.py validate examples/2026-11-05-five-languages.json
    python3 broadcast/broadcast.py render examples/2026-11-05-five-languages.json
    python3 broadcast/broadcast.py serve

Open http://127.0.0.1:8766/. This render is text-only. The downloadable showcase is a separate static player with pre-rendered audio under `showcase/`.

## Create audio

Provide `ELEVENLABS_API_KEY` and `CF_BROADCAST_VOICE_ID` in your process environment. Never commit keys or private source data. Install FFmpeg for long-form audio concatenation. An audio render sends your full script to ElevenLabs and may incur charges. Review the exact script, provider terms and voice permissions first, then set `cloud_tts_approved` to `true` in your own copy. Run:

    python3 broadcast/broadcast.py render /path/to/your-reviewed-bulletin.json --audio

Inputs have coverage, checked local sources, and matching ordered editions. Sources with SHA-256 values are rechecked before rendering. ElevenLabs timestamps are tied to the exact audio generated; long Japanese and Mandarin scripts are split into shorter requests to avoid collapsed timing data. The cache is stored under `broadcast/output/`, which must remain private and ignored by Git. A successful API request does not guarantee a particular voice's license or pronunciation.

The renderer preview is bound to `127.0.0.1`, is not an authenticated web application, and should not be tunneled or deployed publicly as-is.

## Test

    python3 -m unittest discover -s broadcast/tests -v

The static demo uses a distinct bulletin layout and does not modify upstream `index.html`. Additional adapters or exporters need their own configuration and security review before publication.
