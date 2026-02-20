# rc-audio-spleeter

API REST + interface web para separação de stems de áudio. Suporta dois backends:

| Backend | Como funciona | Tempo (~5 min de música) |
|---------|--------------|--------------------------|
| **Replicate** (padrão) | Envia o áudio para o modelo [lucataco/mvsep-mdx23](https://replicate.com/lucataco/mvsep-mdx23-music-separation) via GPU na nuvem | ~9 min |
| **Local CPU** | Roda [Demucs htdemucs](https://github.com/facebookresearch/demucs) localmente na CPU | ~30–60 min |

Dado um arquivo de áudio, o serviço retorna quatro stems em MP3:

| Stem | Descrição |
|------|-----------|
| `vocals` | Voz principal e backing vocals |
| `drums` | Bateria e percussão |
| `bass` | Baixo |
| `other` | Guitarras, teclados, etc. |

---

## Opção 1 — Docker (recomendado)

```bash
git clone https://github.com/rodrigocostacamargos/rc-audio-spleeter.git
cd rc-audio-spleeter
cp .env.example .env          # preencha REPLICATE_API_TOKEN
docker compose up -d --build
```

Acesse em `http://localhost:8001`.

As stems ficam num volume Docker persistente (`stems_data`).
Os logs ficam em `logs_data` e também acessíveis via `GET /logs`.

---

## Opção 2 — Execução local

**Requisitos:** Python 3.11+, ffmpeg no PATH, conta no [Replicate](https://replicate.com/account/api-tokens) (para backend Replicate).

```bash
# 1. Clone e entre na pasta
git clone https://github.com/rodrigocostacamargos/rc-audio-spleeter.git
cd rc-audio-spleeter

# 2. Crie e ative virtualenv
python -m venv .venv && source .venv/bin/activate

# 3. Instale dependências
#    PyTorch CPU-only (menor download; GPU não é necessária)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

# 4. Configure o token
cp .env.example .env          # edite e preencha REPLICATE_API_TOKEN

# 5. Suba o servidor
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

Acesse em `http://localhost:8001`.

---

## Uso

### Interface web

Abra `http://localhost:8001` no navegador, selecione o backend, faça upload do arquivo e aguarde o processamento. Os stems aparecem com player embutido para reprodução e download.

### API

#### `POST /separate`

| Campo (form-data) | Tipo | Obrigatório | Padrão |
|-------------------|------|-------------|--------|
| `file` | arquivo de áudio (`.wav`, `.mp3`, `.m4a`) | sim | — |
| `backend` | `"replicate"` ou `"local"` | não | `"replicate"` |

**Exemplo com curl:**

```bash
# Backend Replicate (padrão)
curl -X POST http://localhost:8001/separate \
  -F "file=@musica.mp3"

# Backend local (Demucs CPU)
curl -X POST http://localhost:8001/separate \
  -F "file=@musica.mp3" \
  -F "backend=local"
```

**Resposta de sucesso (200):**

```json
{
  "stems": ["vocals", "drums", "bass", "other"],
  "paths": {
    "vocals": "stems/vocals_musica.mp3",
    "drums":  "stems/drums_musica.mp3",
    "bass":   "stems/bass_musica.mp3",
    "other":  "stems/other_musica.mp3"
  }
}
```

#### `GET /stems/{filename}`

Baixa ou faz streaming de um stem gerado.

#### `GET /logs?n=100`

Retorna as últimas `n` linhas do log de execução (útil para diagnóstico em produção).

---

## Estrutura do projeto

```
rc-audio-spleeter/
├── main.py                  # Aplicação FastAPI (único arquivo)
├── requirements.txt         # Dependências Python
├── Dockerfile               # Imagem Docker (PyTorch CPU-only + ffmpeg)
├── docker-compose.yml       # Serviço com volumes persistentes
├── .dockerignore
├── .env.example             # Template de configuração
├── static/
│   └── index.html           # Interface web (HTML puro, sem framework)
├── tests/
│   ├── test_unit.py         # Testes unitários (~28, sem chamadas reais)
│   └── test_integration.py  # Teste de integração real com o Replicate (~10 min)
├── stems/                   # Stems geradas (criado automaticamente)
└── logs/                    # Logs persistentes (criado automaticamente)
```

---

## Testes

```bash
# Unitários (rápidos, sem API)
pytest tests/test_unit.py -v

# Integração (requer REPLICATE_API_TOKEN, ~10 min)
pytest tests/test_integration.py -v -s
```

---

## Arquitetura — fluxo do `POST /separate`

1. Validação do arquivo (extensão + content-type)
2. Salva o upload em arquivo temporário
3. Converte para `.wav` via ffmpeg (se necessário)
4. **Replicate:** faz upload via Files API → chama o modelo com `wait=False` (polling) → retry automático em rate limit 429 (backoff linear 15s × tentativa)
5. **Local:** executa `demucs --mp3 -d cpu -n htdemucs` via subprocess
6. Normaliza o output (`dict` ou `list` de URLs, dependendo da versão do modelo)
7. Converte stems WAV → MP3 via ffmpeg e salva em `stems/{instrumento}_{nome_original}.mp3`
