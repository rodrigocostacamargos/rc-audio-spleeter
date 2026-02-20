# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run the server:**
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

**Run unit tests (fast, no API calls):**
```bash
pytest tests/test_unit.py -v
```

**Run a single test:**
```bash
pytest tests/test_unit.py::TestValidateAudioFile::test_wav_aceito -v
```

**Run integration tests (requires `REPLICATE_API_TOKEN`, takes ~10 min):**
```bash
pytest tests/test_integration.py -v -s
```

**Install dependencies:**
```bash
pip install -r requirements.txt
# Also requires ffmpeg in PATH (system dependency)
```

**Setup:**
```bash
cp .env.example .env  # then fill in REPLICATE_API_TOKEN=r8_...
```

## Architecture

The entire app lives in `main.py` — a single-file FastAPI service. The flow for `POST /separate`:

1. `validate_audio_file()` — rejects non-audio by extension + content-type
2. Save upload to a `tempfile`
3. `convert_to_wav()` — runs `ffmpeg` to convert if not already `.wav` (the Replicate model requires it; extensionless temp files cause inference failure)
4. `replicate_client.files.create()` — uploads via Replicate Files API (avoids httpx write timeout on large files)
5. `run_with_retry()` — calls `client.run(wait=False)` then polls; `wait=False` is required because Replicate's 60s sync limit is far less than the model's ~5–10 min processing time. Auto-retries on 429 with linear backoff (15s × attempt)
6. `parse_stems()` — normalizes output that can be either a `dict` or a `list` of URLs
7. `save_stem()` — downloads each stem WAV, converts to MP3 via ffmpeg, saves to `stems/{instrument}_{song_name}.mp3`

**Serving stems:** `GET /stems/{filename}` serves files from the `stems/` directory. The web UI is at `static/index.html`, mounted at `/static`, and served at `/`.

**Key constants in `main.py`:**
- `REPLICATE_MODEL` — pinned version hash for reproducibility
- `REPLICATE_TIMEOUT` — `connect=30s, read=600s, write=600s` (each polling GET, not total)
- `STEMS_DIR = Path("stems")` — output directory (created automatically)

**Output format:** `stems/{instrument}_{original_filename_stem}.mp3` — e.g. `stems/vocals_mysong.mp3`

**Stem names:** `vocals`, `drums`, `bass`, `other`

## Key design decisions

- **No cleanup:** stems in `stems/` are never deleted automatically.
- **Sync endpoint:** `POST /separate` blocks until Replicate finishes (can take many minutes). There is no async job queue.
- **Integration test note:** `test_integration.py` checks for `"request_id"` in the response, but the current API no longer returns it — the integration test will fail on that assertion.
