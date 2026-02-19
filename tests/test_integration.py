"""
Teste de integração — faz chamada real ao Replicate.
Requer REPLICATE_API_TOKEN definido no ambiente ou no .env.

Rodar apenas este arquivo:
    pytest tests/test_integration.py -v -s
"""

import os
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

MUSIC_FILE = Path(__file__).parent.parent / "musicas" / "Red Hot Chili Peppers - Snow.m4a"
STEM_NAMES = {"vocals", "drums", "bass", "other"}


@pytest.mark.skipif(
    not os.getenv("REPLICATE_API_TOKEN"),
    reason="REPLICATE_API_TOKEN não definido — pulando teste de integração",
)
def test_separacao_real():
    """Envia o arquivo real ao Replicate e valida a resposta completa."""
    assert MUSIC_FILE.exists(), f"Arquivo não encontrado: {MUSIC_FILE}"

    print(f"\nEnviando: {MUSIC_FILE.name} ({MUSIC_FILE.stat().st_size / 1024 / 1024:.1f} MB)")

    with open(MUSIC_FILE, "rb") as f:
        resp = client.post(
            "/separate",
            files={"file": (MUSIC_FILE.name, f, "audio/mp4")},
            # timeout generoso — o modelo leva ~1-3 min dependendo do tamanho
            timeout=300,
        )

    print(f"Status: {resp.status_code}")
    print(f"Resposta: {resp.json()}")

    assert resp.status_code == 200, f"Erro inesperado: {resp.text}"

    data = resp.json()
    assert "request_id" in data
    assert "stems" in data
    assert "paths" in data

    # Verifica que ao menos algumas stems foram retornadas
    returned = set(data["stems"])
    assert returned.issubset(STEM_NAMES), f"Stems inesperadas: {returned - STEM_NAMES}"
    assert len(returned) > 0, "Nenhuma stem retornada"

    # Verifica que os arquivos foram salvos em disco
    for stem_name, path in data["paths"].items():
        p = Path(path)
        assert p.exists(), f"Arquivo não encontrado em disco: {p}"
        assert p.stat().st_size > 0, f"Arquivo vazio: {p}"
        print(f"  ✓ {stem_name}: {p} ({p.stat().st_size / 1024:.0f} KB)")
