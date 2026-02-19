# Arquitetura

## Visão geral do fluxo

```
Cliente HTTP
    │
    │  POST /separate  (multipart/form-data)
    ▼
FastAPI (main.py)
    │
    ├─ 1. validate_audio_file()   → rejeita se não for áudio
    ├─ 2. Salva em tempfile       → arquivo temporário com sufixo correto
    ├─ 3. convert_to_wav()        → converte via ffmpeg (se não for .wav)
    ├─ 4. replicate.Client.files.create()  → upload para a Files API do Replicate
    ├─ 5. replicate.Client.run(wait=False) → dispara o modelo e faz polling
    ├─ 6. parse_stems()           → normaliza a saída do modelo
    ├─ 7. save_stem()             → baixa cada stem via httpx
    └─ 8. Retorna JSON com request_id, stems e paths locais
```

---

## Decisões técnicas

### Por que um único arquivo (`main.py`)?

O requisito original pedia simplicidade e reaproveitamento. Um único arquivo é suficiente para a complexidade atual e facilita a leitura e o onboarding.

### Por que converter para `.wav` antes de enviar?

O modelo MDX23 no Replicate salva o arquivo enviado sem extensão (ex: `/tmp/tmpXXXX`), e seu script de inferência (`inference.py`) usa a extensão para detectar o codec. Arquivos `.m4a` e outros formatos com extensão desconhecida causam falha interna com exit code 1. A conversão para `.wav` via ffmpeg garante compatibilidade universal.

### Por que usar a Files API do Replicate?

O arquivo `.wav` convertido de uma música de 5 minutos tem ~50–120 MB. Enviar esse arquivo diretamente no corpo da requisição `predictions.create()` causava timeout de leitura, pois o cliente httpx tem timeout de write de 30s por padrão. A Files API separa o upload (tolerante a latência) da chamada de predição (apenas JSON).

### Por que `wait=False` no `replicate.run()`?

Por padrão, `replicate.run()` envia o header `Prefer: wait` que faz o servidor manter a conexão HTTP aberta até o modelo terminar. O Replicate impõe um limite de **60 segundos** nesse modo. Músicas comuns levam 5–10 minutos para processar, então a conexão é encerrada pelo servidor e o cliente interpreta como `ReadTimeout`.

Com `wait=False`, o `predictions.create()` retorna imediatamente com o ID da predição. O cliente então faz **polling** via `prediction.wait()`, que realiza GETs periódicos sem manter conexão longa. O timeout configurado (`read=600s`) é aplicado por requisição de polling, o que é seguro.

### Por que `httpx.Timeout(connect=30, read=600, write=600, pool=30)`?

| Parâmetro | Valor | Motivo |
|-----------|-------|--------|
| `connect` | 30s | Tempo razoável para abrir conexão com a API |
| `read` | 600s | Polling aguarda resposta do servidor; cada GET deve completar em menos de 10 min |
| `write` | 600s | Upload de arquivos grandes (~120 MB wav) |
| `pool` | 30s | Tempo para adquirir conexão do pool |

### Por que `parse_stems()` trata tanto `dict` quanto `list`?

A saída do modelo Replicate não é garantida em formato fixo entre versões. O modelo pode retornar:
- `dict`: `{"vocals": "https://...", ...}`
- `list`: `["https://.../vocals.wav", ...]`

O parser normaliza ambos os casos, tornando o código resiliente a mudanças de versão.

---

## Modelo utilizado

```
lucataco/mvsep-mdx23-music-separation
Versão: 510b9b91aec1bfa7d634e6c06ee80c18492fb0fc06aa1474533fbda90dd3dba4
```

O version hash é fixado para garantir reprodutibilidade. Para atualizar:

```python
from dotenv import load_dotenv
load_dotenv()
import replicate
model = replicate.models.get("lucataco/mvsep-mdx23-music-separation")
versions = model.versions.list()
print(versions[0].id)  # versão mais recente
```

---

## Estrutura de arquivos de saída

Cada requisição gera um diretório único:

```
output/
└── {request_id}/          # UUID v4
    ├── vocals.wav
    ├── drums.wav
    ├── bass.wav
    └── other.wav
```

Os arquivos **não são limpos automaticamente**. Em produção, considere uma rotina de limpeza por TTL ou mover para object storage (S3, GCS).

---

## Dependências principais

| Pacote | Função |
|--------|--------|
| `fastapi` | Framework HTTP |
| `uvicorn` | Servidor ASGI |
| `replicate` | Cliente oficial da API Replicate |
| `httpx` | Download das stems geradas |
| `python-dotenv` | Carregamento do `.env` |
| `python-multipart` | Parse de `multipart/form-data` |
| `ffmpeg` (sistema) | Conversão de áudio para `.wav` |
