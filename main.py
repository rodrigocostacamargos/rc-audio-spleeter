"""
Audio Stem Separation API
Uses Replicate's lucataco/mvsep-mdx23-music-separation model to split audio into:
vocals, drums, bass, other
"""

import os
import logging
import tempfile
import subprocess
import httpx
from pathlib import Path

import replicate
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

load_dotenv()  # carrega variáveis do arquivo .env (se existir)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ALLOWED_CONTENT_TYPES = {"audio/wav", "audio/wave", "audio/mpeg", "audio/mp3", "audio/x-wav", "audio/mp4", "audio/x-m4a", "video/mp4"}
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a"}
STEMS_DIR = Path("stems")

REPLICATE_MODEL = "lucataco/mvsep-mdx23-music-separation:510b9b91aec1bfa7d634e6c06ee80c18492fb0fc06aa1474533fbda90dd3dba4"

# Timeout generoso: upload de arquivos grandes + processamento do modelo podem levar vários minutos
REPLICATE_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Audio Stem Separator", version="1.0.0")

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
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/separate")
async def separate(file: UploadFile = File(...)) -> JSONResponse:
    """
    Receive an audio file, send it to Replicate for stem separation,
    download the results, and return their local paths.
    """
    # 1. Validate input
    validate_audio_file(file)

    original_name = Path(file.filename or "audio").stem  # nome sem extensão
    suffix = Path(file.filename or "audio").suffix.lower() or ".wav"
    log.info("New request — file: %s", file.filename)

    # 2. Save upload to a temp file so Replicate can read it
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)
    except Exception as exc:
        log.exception("Failed to save uploaded file")
        raise HTTPException(status_code=500, detail="Could not save uploaded file.") from exc

    # 3. Converter para .wav se necessário (o modelo exige formato identificável)
    wav_path = tmp_path
    if tmp_path.suffix.lower() != ".wav":
        try:
            wav_path = convert_to_wav(tmp_path)
        except subprocess.CalledProcessError as exc:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail="Falha ao converter áudio para wav.") from exc

    # 4. Call Replicate
    try:
        replicate_client = replicate.Client(
            api_token=os.environ["REPLICATE_API_TOKEN"],
            timeout=REPLICATE_TIMEOUT,
        )

        log.info("Uploading audio to Replicate Files API (%s) …", wav_path.name)
        with open(wav_path, "rb") as audio_file:
            replicate_file = replicate_client.files.create(audio_file)

        log.info("Running model %s …", REPLICATE_MODEL)
        # wait=False: evita o modo blocking (Prefer: wait, limite de 60s no servidor)
        # O cliente faz polling via prediction.wait() com o timeout configurado
        output = replicate_client.run(
            REPLICATE_MODEL,
            input={"audio": replicate_file.urls["get"]},
            wait=False,
        )
        log.info("Replicate finished. Parsing output …")
        stems_urls = parse_stems(output)
    except Exception as exc:
        log.exception("Replicate processing failed")
        raise HTTPException(status_code=500, detail=f"Replicate error: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)
        if wav_path != tmp_path:
            wav_path.unlink(missing_ok=True)

    if not stems_urls:
        raise HTTPException(status_code=500, detail="Model returned no stems.")

    # 5. Salva stems em stems/{instrumento}_{nome_da_musica}.mp3
    STEMS_DIR.mkdir(parents=True, exist_ok=True)

    saved_stems: dict[str, str] = {}
    try:
        for stem_name, url in stems_urls.items():
            dest = STEMS_DIR / f"{stem_name}_{original_name}.mp3"
            save_stem(url, dest)
            saved_stems[stem_name] = str(dest)
    except Exception as exc:
        log.exception("Failed to download stems")
        raise HTTPException(status_code=500, detail=f"Could not download stems: {exc}") from exc

    log.info("Stems salvas: %s", list(saved_stems.values()))

    return JSONResponse(
        status_code=200,
        content={
            "stems": list(saved_stems.keys()),
            "paths": saved_stems,
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
