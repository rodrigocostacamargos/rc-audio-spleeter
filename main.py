"""
Audio Stem Separation API
Uses Replicate's lucataco/mvsep-mdx23-music-separation model to split audio into:
vocals, drums, bass, other
"""

import os
import shutil
import sys
import time
import uuid
import logging
import tempfile
import threading
import subprocess
import httpx
from pathlib import Path

import replicate
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

load_dotenv()  # carrega variáveis do arquivo .env (se existir)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ALLOWED_CONTENT_TYPES = {"audio/wav", "audio/wave", "audio/mpeg", "audio/mp3", "audio/x-wav", "audio/mp4", "audio/x-m4a", "video/mp4"}
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a"}
STEMS_DIR = Path("stems")
LOG_FILE = Path("logs/spleeter.log")

REPLICATE_MODEL = "lucataco/mvsep-mdx23-music-separation:510b9b91aec1bfa7d634e6c06ee80c18492fb0fc06aa1474533fbda90dd3dba4"

# Timeout generoso: upload de arquivos grandes + processamento do modelo podem levar vários minutos
REPLICATE_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),                          # stdout (docker logs)
        logging.FileHandler(LOG_FILE, encoding="utf-8"),  # arquivo persistente
    ],
)
log = logging.getLogger(__name__)

app = FastAPI(title="Audio Stem Separator", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")

# In-memory job store: {job_id: {"status": "processing"|"done"|"error", ...}}
jobs: dict[str, dict] = {}


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = time.time() - start
    log.info("%s %s → %d (%.2fs)", request.method, request.url.path, response.status_code, elapsed)
    return response


# ---------------------------------------------------------------------------
# UI & file serving
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    """Serve a interface web."""
    return FileResponse(
        "static/index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/logs")
def get_logs(n: int = 100):
    """Retorna as últimas n linhas do log (padrão: 100)."""
    if not LOG_FILE.exists():
        return JSONResponse({"lines": []})
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    return JSONResponse({"lines": lines[-n:]})


@app.get("/stems/{filename}")
def download_stem(filename: str):
    """Serve um arquivo de stem gerado para download/streaming."""
    path = STEMS_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Stem não encontrada.")
    return FileResponse(path, media_type="audio/mpeg", filename=filename)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def validate_audio_file(file: UploadFile) -> None:
    """Raise HTTP 400 if the uploaded file is not a recognised audio format."""
    suffix = Path(file.filename or "").suffix.lower()
    content_type = (file.content_type or "").lower()

    if suffix not in ALLOWED_EXTENSIONS and content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file. Accepted formats: {', '.join(ALLOWED_EXTENSIONS)}",
        )


def convert_to_wav(src: Path) -> Path:
    """Converte qualquer áudio para .wav usando ffmpeg. Retorna o novo Path."""
    dest = src.with_suffix(".wav")
    log.info("Convertendo %s → %s", src.name, dest.name)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), str(dest)],
        check=True,
        capture_output=True,
    )
    return dest


def save_stem(url: str, dest_path: Path) -> None:
    """Download a stem (WAV) from a URL, convert to MP3 and save to dest_path."""
    log.info("Downloading stem → %s", dest_path)
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        response = client.get(url)
        response.raise_for_status()

    # Salva o WAV em temp e converte para MP3; descarta o WAV intermediário
    tmp_wav = dest_path.with_suffix(".wav.tmp")
    tmp_wav.write_bytes(response.content)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(tmp_wav), "-q:a", "2", str(dest_path)],
            check=True,
            capture_output=True,
        )
    finally:
        tmp_wav.unlink(missing_ok=True)


def run_with_retry(client, model: str, input_data: dict, max_retries: int = 5) -> object:
    """
    Executa o modelo no Replicate com retry automático em caso de rate limit (429).
    O RetryTransport do cliente só faz retry em GET — POST precisa de tratamento manual.
    Backoff linear: aguarda 15s, 30s, 45s… entre tentativas.
    """
    for attempt in range(max_retries):
        try:
            return client.run(model, input=input_data, wait=False)
        except replicate.exceptions.ReplicateError as exc:
            is_rate_limit = getattr(exc, "status", None) == 429
            if is_rate_limit and attempt < max_retries - 1:
                delay = 15 * (attempt + 1)
                log.warning("Rate limit (429). Aguardando %ds antes da tentativa %d/%d…", delay, attempt + 2, max_retries)
                time.sleep(delay)
            else:
                raise


def parse_stems(output) -> dict[str, str]:
    """
    Normalise the Replicate model output to a dict mapping stem name → URL.

    The model may return:
    - a dict  {"vocals": "https://...", "drums": "https://...", ...}
    - a list  ["https://.../vocals.wav", "https://.../drums.wav", ...]
    """
    stem_names = ["vocals", "drums", "bass", "other"]

    if isinstance(output, dict):
        return {k: v for k, v in output.items() if k in stem_names}

    if isinstance(output, (list, tuple)):
        result: dict[str, str] = {}
        for url in output:
            url_str = str(url)
            for name in stem_names:
                if name in url_str.lower():
                    result[name] = url_str
                    break
        return result

    raise ValueError(f"Unexpected output format from Replicate: {type(output)}")


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

def separate_with_replicate(wav_path: Path, original_name: str) -> dict[str, str]:
    """Envia o wav para o Replicate e retorna dict stem→path_local_mp3."""
    replicate_client = replicate.Client(
        api_token=os.environ["REPLICATE_API_TOKEN"],
        timeout=REPLICATE_TIMEOUT,
    )

    log.info("Uploading audio to Replicate Files API (%s) …", wav_path.name)
    with open(wav_path, "rb") as audio_file:
        replicate_file = replicate_client.files.create(audio_file)

    log.info("Running model %s …", REPLICATE_MODEL)
    output = run_with_retry(
        replicate_client,
        REPLICATE_MODEL,
        {"audio": replicate_file.urls["get"]},
    )
    log.info("Replicate finished. Parsing output …")
    stems_urls = parse_stems(output)

    STEMS_DIR.mkdir(parents=True, exist_ok=True)
    saved: dict[str, str] = {}
    for stem_name, url in stems_urls.items():
        dest = STEMS_DIR / f"{stem_name}_{original_name}.mp3"
        save_stem(url, dest)
        saved[stem_name] = str(dest)

    return saved


def separate_with_local(wav_path: Path, original_name: str) -> dict[str, str]:
    """Roda Demucs via subprocess na CPU e retorna dict stem→path_local_mp3."""
    STEM_NAMES = ["vocals", "drums", "bass", "other"]

    with tempfile.TemporaryDirectory() as tmp_out:
        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "demucs",
                    "--mp3", "--mp3-bitrate", "192",
                    "-d", "cpu",
                    "-n", "htdemucs",
                    "-o", tmp_out,
                    str(wav_path),
                ],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace").strip()
            log.error("Demucs falhou (exit %d):\n%s", exc.returncode, stderr)
            raise RuntimeError(f"Demucs falhou: {stderr[-400:]}") from exc
        # Demucs cria: {tmp_out}/htdemucs/{wav_path.stem}/{stem}.mp3
        source_dir = Path(tmp_out) / "htdemucs" / wav_path.stem
        STEMS_DIR.mkdir(parents=True, exist_ok=True)
        saved: dict[str, str] = {}
        for stem_name in STEM_NAMES:
            src = source_dir / f"{stem_name}.mp3"
            if src.exists():
                dst = STEMS_DIR / f"{stem_name}_{original_name}.mp3"
                shutil.move(str(src), str(dst))
                saved[stem_name] = str(dst)

    return saved


# ---------------------------------------------------------------------------
# Background job processor
# ---------------------------------------------------------------------------

def _process_job(job_id: str, content: bytes, suffix: str, original_name: str, backend: str) -> None:
    """Runs in a background thread: converts, separates and updates jobs dict."""
    tmp_path = None
    wav_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        wav_path = tmp_path
        if tmp_path.suffix.lower() != ".wav":
            wav_path = convert_to_wav(tmp_path)

        if backend == "local":
            saved_stems = separate_with_local(wav_path, original_name)
        else:
            saved_stems = separate_with_replicate(wav_path, original_name)

        if not saved_stems:
            jobs[job_id] = {"status": "error", "detail": "Model returned no stems."}
            return

        log.info("Stems salvas: %s", list(saved_stems.values()))
        jobs[job_id] = {
            "status": "done",
            "stems": list(saved_stems.keys()),
            "paths": saved_stems,
        }

    except Exception as exc:
        log.exception("Processing failed for job %s", job_id)
        prefix = "Replicate error" if backend == "replicate" else "Local processing error"
        jobs[job_id] = {"status": "error", "detail": f"{prefix}: {exc}"}
    finally:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
        if wav_path and wav_path != tmp_path:
            wav_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/separate", status_code=202)
async def separate(
    file: UploadFile = File(...),
    backend: str = Form("replicate"),
) -> JSONResponse:
    """
    Enfileira a separação de stems em background e retorna imediatamente um job_id.
    Use GET /status/{job_id} para acompanhar o progresso.
    """
    validate_audio_file(file)

    if backend not in {"replicate", "local"}:
        raise HTTPException(status_code=400, detail="backend deve ser 'replicate' ou 'local'.")

    original_name = Path(file.filename or "audio").stem
    suffix = Path(file.filename or "audio").suffix.lower() or ".wav"
    log.info("New request — file: %s, backend: %s", file.filename, backend)

    try:
        content = await file.read()
    except Exception as exc:
        log.exception("Failed to read uploaded file")
        raise HTTPException(status_code=500, detail="Could not read uploaded file.") from exc

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "processing"}

    thread = threading.Thread(
        target=_process_job,
        args=(job_id, content, suffix, original_name, backend),
        daemon=True,
    )
    thread.start()

    return JSONResponse(status_code=202, content={"job_id": job_id})


@app.post("/upload")
async def upload_form(
    file: UploadFile = File(...),
    backend: str = Form("replicate"),
):
    """Upload via formulário HTML tradicional — redireciona para /job/{job_id}."""
    validate_audio_file(file)

    if backend not in {"replicate", "local"}:
        raise HTTPException(status_code=400, detail="backend deve ser 'replicate' ou 'local'.")

    original_name = Path(file.filename or "audio").stem
    suffix = Path(file.filename or "audio").suffix.lower() or ".wav"
    log.info("Form upload — file: %s, backend: %s", file.filename, backend)

    content = await file.read()

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "processing"}

    thread = threading.Thread(
        target=_process_job,
        args=(job_id, content, suffix, original_name, backend),
        daemon=True,
    )
    thread.start()

    return RedirectResponse(url=f"job/{job_id}", status_code=303)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_page(job_id: str):
    """Página de acompanhamento do job com polling automático."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return HTMLResponse(
        content=JOB_PAGE_HTML.replace("{{JOB_ID}}", job_id),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/status/{job_id}")
def get_status(job_id: str) -> JSONResponse:
    """Retorna o status de um job: processing | done | error."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return JSONResponse(job)


# ---------------------------------------------------------------------------
# Job status page template (inline HTML)
# ---------------------------------------------------------------------------

JOB_PAGE_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Processando - Audio Spleeter</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, sans-serif;
      background: #0f0f0f;
      color: #e0e0e0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 2rem;
    }
    .card {
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 12px;
      padding: 2.5rem;
      width: 100%;
      max-width: 540px;
      text-align: center;
    }
    h1 { font-size: 1.4rem; font-weight: 600; color: #fff; margin-bottom: 1.5rem; }
    .status { font-size: 0.95rem; color: #888; margin-bottom: 1.5rem; min-height: 1.5em; }
    .status.error { color: #e05a5a; }
    .status.done { color: #4ade80; }
    .progress-wrap {
      background: #2a2a2a; border-radius: 999px; height: 6px;
      overflow: hidden; margin-bottom: 1.5rem;
    }
    .progress-bar {
      height: 100%; background: #6c63ff; width: 5%;
      border-radius: 999px; transition: width 0.5s ease;
    }
    .stem-row {
      display: flex; align-items: center; gap: 0.75rem;
      background: #222; border-radius: 8px; padding: 0.65rem 0.9rem;
      margin-bottom: 0.5rem;
    }
    .stem-label {
      font-size: 0.85rem; font-weight: 600; color: #bbb;
      width: 60px; flex-shrink: 0; text-transform: capitalize; text-align: left;
    }
    .stem-row audio { flex: 1; height: 32px; }
    .stem-row a { font-size: 0.75rem; color: #6c63ff; text-decoration: none; white-space: nowrap; }
    .stem-row a:hover { text-decoration: underline; }
    .results { display: none; text-align: left; }
    .results.visible { display: block; }
    .results h2 {
      font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em;
      color: #555; margin-bottom: 0.6rem;
    }
    .back-link {
      display: inline-block; margin-top: 1.5rem; color: #6c63ff;
      text-decoration: none; font-size: 0.85rem;
    }
    .back-link:hover { text-decoration: underline; }
  </style>
</head>
<body>
<div class="card">
  <h1>Audio Spleeter</h1>
  <div class="progress-wrap"><div class="progress-bar" id="bar"></div></div>
  <p class="status" id="status">Processando...</p>
  <div class="results" id="results">
    <h2>Stems geradas</h2>
    <div id="stemList"></div>
  </div>
  <a class="back-link" href="..">Novo upload</a>
</div>
<script>
  var jobId = "{{JOB_ID}}";
  var statusEl = document.getElementById("status");
  var bar = document.getElementById("bar");
  var resultsEl = document.getElementById("results");
  var stemList = document.getElementById("stemList");
  var pct = 5;
  var dots = 0;

  var timer = setInterval(function() {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "../status/" + jobId);
    xhr.timeout = 10000;
    xhr.onload = function() {
      try { var data = JSON.parse(xhr.responseText); } catch(e) { return; }

      if (data.status === "done") {
        clearInterval(timer);
        bar.style.width = "100%";
        statusEl.textContent = "Concluido!";
        statusEl.className = "status done";
        renderStems(data.stems, data.paths);
      } else if (data.status === "error") {
        clearInterval(timer);
        bar.style.width = "0%";
        statusEl.textContent = data.detail || "Erro no processamento";
        statusEl.className = "status error";
      } else {
        dots = (dots + 1) % 4;
        var step = pct < 80 ? 2 : 0.3;
        pct = Math.min(pct + step, 95);
        bar.style.width = pct + "%";
        statusEl.textContent = "Processando" + "....".substring(0, dots + 1);
      }
    };
    xhr.onerror = function() {
      dots = (dots + 1) % 4;
      statusEl.textContent = "Reconectando" + "....".substring(0, dots + 1);
    };
    xhr.send();
  }, 3000);

  function renderStems(stems, paths) {
    var order = ["vocals", "drums", "bass", "other"];
    for (var i = 0; i < order.length; i++) {
      var stem = order[i];
      if (stems.indexOf(stem) === -1) continue;
      var file = paths[stem].split("/").pop();
      var url = "../stems/" + encodeURIComponent(file);
      var row = document.createElement("div");
      row.className = "stem-row";
      row.innerHTML =
        '<span class="stem-label">' + stem + "</span>" +
        '<audio controls preload="none" src="' + url + '"></audio>' +
        '<a href="' + url + '" download>baixar</a>';
      stemList.appendChild(row);
    }
    resultsEl.className = "results visible";
  }
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
