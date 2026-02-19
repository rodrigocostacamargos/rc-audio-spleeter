# rc-audio-spleeter

API REST em Python para separação de stems de áudio usando o modelo [lucataco/mvsep-mdx23-music-separation](https://replicate.com/lucataco/mvsep-mdx23-music-separation) via [Replicate](https://replicate.com).

Dado um arquivo de áudio, o serviço retorna quatro stems separadas:

| Stem | Descrição |
|------|-----------|
| `vocals` | Voz principal e backing vocals |
| `drums` | Bateria e percussão |
| `bass` | Baixo |
| `other` | Tudo o que não se encaixa nas anteriores (guitarras, teclados, etc.) |

---

## Requisitos

| Dependência | Versão mínima | Observação |
|-------------|--------------|------------|
| Python | 3.11+ | |
| ffmpeg | qualquer | Deve estar no `PATH` |
| Conta Replicate | — | Token em [replicate.com/account/api-tokens](https://replicate.com/account/api-tokens) |

---

## Instalação

```bash
# 1. Clone o repositório
git clone https://github.com/rodrigocostacamargos/rc-audio-spleeter.git
cd rc-audio-spleeter

# 2. (Opcional) crie e ative um virtualenv
python -m venv .venv && source .venv/bin/activate

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Instale o ffmpeg (se ainda não tiver)
# Ubuntu/Debian:
sudo apt install ffmpeg
# macOS:
brew install ffmpeg
```

---

## Configuração

Copie o arquivo de exemplo e preencha o token:

```bash
cp .env.example .env
```

Edite `.env`:

```env
REPLICATE_API_TOKEN=r8_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> O token começa sempre com `r8_`. Nunca commite o arquivo `.env`.

---

## Execução

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Acesse a documentação interativa em: `http://localhost:8000/docs`

---

## Uso da API

### `POST /separate`

Recebe um arquivo de áudio e retorna as stems separadas.

**Formatos aceitos:** `.wav`, `.mp3`, `.m4a`

**Exemplo com curl:**

```bash
curl -X POST http://localhost:8000/separate \
  -F "file=@/caminho/para/musica.wav"
```

**Resposta de sucesso (200):**

```json
{
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "stems": ["bass", "drums", "other", "vocals"],
  "paths": {
    "bass":   "output/550e8400-.../bass.wav",
    "drums":  "output/550e8400-.../drums.wav",
    "other":  "output/550e8400-.../other.wav",
    "vocals": "output/550e8400-.../vocals.wav"
  }
}
```

**Respostas de erro:**

| Código | Situação |
|--------|----------|
| `400` | Arquivo inválido ou formato não suportado |
| `500` | Falha na conversão, upload ou no modelo Replicate |

---

## Estrutura do projeto

```
rc-audio-spleeter/
├── main.py                  # Aplicação FastAPI (único arquivo)
├── requirements.txt         # Dependências de produção
├── .env.example             # Template de configuração
├── .gitignore
├── tests/
│   ├── __init__.py
│   ├── test_unit.py         # 20 testes unitários (sem chamadas reais)
│   └── test_integration.py  # Teste de integração real com o Replicate
├── output/                  # Stems geradas (criado automaticamente)
└── docs/
    ├── ARCHITECTURE.md      # Decisões técnicas e arquitetura
    └── TROUBLESHOOTING.md   # Problemas encontrados e soluções
```

---

## Testes

### Unitários (rápidos, sem API)

```bash
pip install pytest pytest-asyncio
pytest tests/test_unit.py -v
```

Saída esperada: **20 passed**

### Integração (requer token + ~10 min)

```bash
pytest tests/test_integration.py -v -s
```

O teste envia o arquivo em `musicas/` para o Replicate e valida que os 4 arquivos `.wav` são salvos em disco.

---

## Observações de tempo de processamento

O modelo MDX23 é pesado. Tempos aproximados observados:

| Duração da música | Tempo de processamento |
|---|---|
| ~5 min | ~9 minutos |

O processamento ocorre nos servidores do Replicate (GPU). O serviço aguarda de forma síncrona (polling).
