# Lingofy Video Dub MVP — Design (2026-11-16)

## Intent and why

User uploads a video and after a short 5–10s buffer it plays with fully dubbed audio in the other language, feeling realtime. Picture stays untouched, only the audio track is replaced and kept in sync.

This is the video extension of the Lingofy audio pipeline already described in `README.md` / `docs/plan.md` / `docs/architecture.md`. Those docs target audio-only with a 3s per-chunk SLO. This design keeps that pipeline and adds: video ingest + audio extract, open-source speaker diarization, per-speaker steady Piper voices, and video mux-back for synced playback with progressive start.

Why now: prove end-to-end video dub for English <-> Hindi with speaker consistency at near-zero cost before investing in premium voices, lip-sync, or scale.

## Goals

- Accept mp4 plus basics (mov, webm, mkv, plus wav/mp3 audio fallback) up to ~10 min. Demo uses trimmed clip from `samples/` (provided 540M Lex Fridman #503 file trimmed to ~10 min for demo).
- Both directions: English -> Hindi and Hindi -> English via explicit toggle at upload (human call 2026-11-16). No auto-detect in v1. No Spanish in this MVP (human call — overrides `plan.md` es,hi).
- Up to 3 speakers: detect as person 1, 2, 3 and keep the same Piper voice per person for the whole session.
- First dubbed playback in 5–10s, rest continues dubbing in background while user watches start (progressive).
- Audio stays synced with picture (reuse existing `atempo` + drift logic, target ≤250ms).
- Near-zero cost: Piper only for TTS in this version.

## Non-goals

- No lip-sync, no voice cloning, no voice character matching.
- No more than 3 speakers in v1.
- No premium TTS (ElevenLabs, Sarvam Bulbul) in default path; may be noted as swap-in.
- No long-file perfection over fast watchable result.
- No new dashboards, alerts, autoscaling changes beyond what `docs/plan.md` already builds.

## Anti-goals (failure even if it technically dubs)

- Voices swapping mid-video (person 1 becomes person 2 voice later).
- Playback blocked until full 10 min is done.
- Drift that makes audio feel detached from picture.
- Paid API required to run the demo.

## Constraints

- Piper for TTS (user decision). Hindi `hi_IN-pratham-medium`-class + English voice, plus simple pitch/tempo variant if 3 distinct voices needed from 2 base models.
- Open-source diarization / embeddings only (user decision). No paid speaker service. Compute envelope stays `t3.micro` / 500m/512Mi / $5 cap (human call 2026-11-16, tightened from $50) — no HuggingFace-gated large models, pick small CPU embedding + clustering that fits worker image. Demo path must stay $0 with free tiers + Piper; no paid burst.
- Build with what is in the repo: Python 3.12, FastAPI gateway, ffmpeg, SQS `dub-tasks` + DLQ pattern, `dub-worker` doing ASR -> Translate -> TTS sequentially per chunk, assembler to playlist/manifest.
- 8s silence-snapped chunks (existing chunker) to keep per-chunk SLO logic.
- 10 min ≈ 75 chunks. Must stay chunk-parallel. Ingest cap 10 min / 500MB, reject over with clear error (builder call).
- Full-file diarization before any output would blow the 5–10s buffer, so must do per-chunk early labels + smoothing.

## What was decided (human's calls 2026-11-16)

- Video in, synced video out (not audio-only).
- Piper only.
- 3 speakers max (started at 2, raised to 3).
- OSS model/repo for audio profiling / diarization.
- mp4 + basics.
- ~10 min target length. Demo sample provided in `samples/`, trim it for demo.
- 5–10s to first playback is acceptable, rest dubs in background (confirmed yes to progressive).
- Tech choices handed to builder (use repo + simplest free pieces).
- Languages: English to Hindi and reverse only. No Spanish in this MVP.
- Language selection: explicit toggle at upload, no auto-detect in v1.

## Builder calls made on reviewer pass (check while reading)

- Compute envelope stays fixed at $5 — small CPU OSS only, no gated models, demo stays free.
- Playback is watch-from-start live-style only in v1; seeking beyond dubbed frontier shows loading, no placeholder silence logic.
- Video handling prefers `-c:v copy` mux-back; reuse assembler `playlist.m3u8` / `manifest.json` if cheaper, else progressive mp4. Must keep first-playable ≤10s either way.

## What is left to the builder

- Exact OSS pick: pyannote.audio v3-style diarization + ECAPA embeddings vs SpeechBrain vs NeMo. Choose one that runs CPU in worker image with smallest friction. Document why.
- Voice map: which 3 Piper voices / pitch variants map to person 1/2/3 for EN and HI. Keep table in config.
- Diarization strategy to hit buffer: per-chunk embedding + global clustering refinement as more chunks arrive. Define label-smoothing rule and what happens when a 4th speaker appears (map to nearest or mark `unavailable` voice, never block).
- Video handling: ffmpeg extract to 16kHz mono wav for pipeline; mux-back via `-c:v copy` + dubbed audio; progressive output choice (HLS per-language like existing assembler vs progressive mp4). Prefer reuse of assembler + `playlist.m3u8` / `manifest.json` unless mp4 mux is simpler for video. Builder may choose, must keep first-playable ≤10s.
- Session API delta: `source_lang` explicit toggle (`en`|`hi`), `target_lang` is the other language for v1, video upload field + dubbed video URL in `manifest`.
- ASR / Translate providers for v1: keep Deepgram ASR + DeepL from plan, note Sarvam / Whisper as fallback only if keys missing. Builder may stub with local mocks for offline dev but must keep envelope `{session_id, chunk_id, seq, start_ms, end_ms, speaker_label}`.
- Verification: trimmed ~10-min clip from provided sample -> other language video plays in ≤10s to first audio, mapping steady by human listen, drift ≤250ms, DLQ empty. Same reverse via toggle.

## Open guesses resolved

- Toggle is enough (confirmed). No mixed-language-in-chunk handling beyond best-effort.
- Two base Piper voices + variant is acceptable for 3 speakers.
- HLS reuse preferred if it saves work, else progressive mp4 — builder picks.
