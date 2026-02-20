# Arquitetura — rc-audio-spleeter no Hetzner

## Visao geral

O rc-audio-spleeter roda como um **servico independente** no Hetzner, acessivel via nginx do LMS na rota `/spleeter/`.

```
                         Hetzner (91.99.194.219)
  ┌──────────────────────────────────────────────────────────────────┐
  │                                                                  │
  │   ┌─────────────────────────────────────────────┐                │
  │   │  LMS (Docker Compose)    /opt/lms-music     │                │
  │   │                                             │                │
  │   │  ┌─────────┐    ┌────────┐   ┌────────┐    │                │
  │   │  │  nginx   │───▶│ django │   │ worker │    │                │
  │   │  │  :80     │    │ :8000  │   │  (RQ)  │    │                │
  │   │  └────┬─────┘    └────────┘   └────────┘    │                │
  │   │       │                                     │                │
  │   │       │  /spleeter/* ──▶ host:8001          │                │
  │   │       │  /*          ──▶ django:8000         │                │
  │   └───────┼─────────────────────────────────────┘                │
  │           │                                                      │
  │           │  proxy_pass (via host.docker.internal)               │
  │           ▼                                                      │
  │   ┌─────────────────────────────────────────────┐                │
  │   │  Spleeter (Docker Compose)  /root/spleeter  │                │
  │   │                                             │                │
  │   │  ┌──────────────────┐                       │                │
  │   │  │  spleeter (uvicorn)                      │                │
  │   │  │  FastAPI :8001   │                       │                │
  │   │  │                  │                       │                │
  │   │  │  volumes:        │                       │                │
  │   │  │   stems_data     │                       │                │
  │   │  │   model_cache    │                       │                │
  │   │  │   logs_data      │                       │                │
  │   │  └──────────────────┘                       │                │
  │   └─────────────────────────────────────────────┘                │
  │                                                                  │
  └──────────────────────────────────────────────────────────────────┘
```

## Fluxo de uma requisicao (celular ou PC)

```
Navegador                    nginx :80                 spleeter :8001
   │                            │                            │
   │  GET /spleeter/            │                            │
   │───────────────────────────▶│                            │
   │                            │  GET /                     │
   │                            │───────────────────────────▶│
   │                            │     index.html             │
   │                            │◀───────────────────────────│
   │        index.html          │                            │
   │◀───────────────────────────│                            │
   │                            │                            │
   │  POST /spleeter/upload     │                            │
   │  (nginx bufferiza o        │                            │
   │   arquivo inteiro antes    │                            │
   │   de encaminhar)           │                            │
   │───────────────────────────▶│                            │
   │                            │  POST /upload              │
   │                            │───────────────────────────▶│
   │                            │     303 → job/{id}         │
   │                            │◀───────────────────────────│
   │   303 → /spleeter/job/{id} │                            │
   │◀───────────────────────────│                            │
   │                            │                            │
   │  GET /spleeter/job/{id}    │                            │
   │───────────────────────────▶│  GET /job/{id}             │
   │                            │───────────────────────────▶│
   │       job page (polling)   │                            │
   │◀───────────────────────────│◀───────────────────────────│
   │                            │                            │
   │  GET /spleeter/status/{id} │  GET /status/{id}          │
   │───────────────────────────▶│───────────────────────────▶│
   │         {status: done}     │                            │
   │◀───────────────────────────│◀───────────────────────────│
```

## Por que o nginx resolve o problema no celular?

O upload direto para `uvicorn:8001` falhava no Android Chrome porque:

1. **Porta nao-padrao (8001)**: alguns navegadores mobile tratam conexoes em portas nao-padrao com restricoes adicionais
2. **Sem buffering**: uvicorn processa o upload em streaming, e o Chrome Android cancelava a conexao antes de completar
3. **Timeout curto**: sem proxy intermediario, qualquer instabilidade de rede cortava o upload

O nginx resolve todos esses problemas:
- Porta 80 (padrao HTTP, sem restricoes)
- `proxy_request_buffering on` — recebe o arquivo **inteiro** antes de encaminhar ao backend
- `client_max_body_size 500M` — aceita arquivos grandes
- Timeouts generosos (`proxy_read_timeout 600s`)

## Dois projetos, dois deploys

| Projeto | Diretorio local | Diretorio remoto | Deploy |
|---------|----------------|-----------------|--------|
| **LMS** | `/home/rodrigo/lms-music` | `/opt/lms-music` | `scripts/update_hetzner_b2.sh` |
| **Spleeter** | `/home/rodrigo/rc-audio-spleeter` | `/root/spleeter` | `deploy.sh` |

### Quando fazer deploy de cada um?

- **Mudou codigo do spleeter** (main.py, index.html, etc) → roda `deploy.sh` do spleeter
- **Mudou config nginx ou docker-compose do LMS** → roda `update_hetzner_b2.sh` do LMS
- **Mudou ambos** → roda os dois

### O que cada deploy faz

**`rc-audio-spleeter/deploy.sh`:**
```
1. Verifica conexao SSH
2. Empacota codigo (tar.gz, exclui .git, stems, musicas, .env)
3. Envia via SCP para /tmp/
4. Extrai em /root/spleeter
5. docker compose build && up -d
6. Health check em http://IP:8001/
```

**`lms-music/scripts/update_hetzner_b2.sh`:**
```
1. Validacoes locais (SSH key, manage.py)
2. Empacota codigo (tar.gz)
3. Envia via SCP
4. Opcional: backup do DB
5. Extrai em /opt/lms-music
6. docker compose build && up -d
7. migrate + collectstatic
8. Health check
```

## Config nginx relevante (lms.conf)

```nginx
# Audio Spleeter — proxy para container independente
location /spleeter/ {
    proxy_pass http://host.docker.internal:8001/;  # trailing slash strip /spleeter/
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    client_max_body_size 500M;
    proxy_request_buffering on;

    proxy_connect_timeout 300s;
    proxy_send_timeout 300s;
    proxy_read_timeout 600s;
}
```

**`proxy_pass` com trailing slash** (`http://host:8001/`): nginx remove o prefixo `/spleeter/` antes de encaminhar. O spleeter recebe as rotas normais (`/`, `/upload`, `/job/{id}`, etc).

**`host.docker.internal`**: resolvido via `extra_hosts` no docker-compose do LMS, aponta para o host (onde o container do spleeter escuta na porta 8001).

## URLs relativas no spleeter

O spleeter usa URLs **relativas** para funcionar tanto direto (`:8001`) quanto via proxy (`/spleeter/`):

| Pagina | URL no codigo | Resolve direto (:8001) | Resolve via proxy (/spleeter/) |
|--------|---------------|------------------------|-------------------------------|
| index.html | `action="upload"` | `/upload` | `/spleeter/upload` |
| /job/{id} | `../status/{id}` | `/status/{id}` | `/spleeter/status/{id}` |
| /job/{id} | `../stems/file` | `/stems/file` | `/spleeter/stems/file` |
| /job/{id} | `href=".."` | `/` | `/spleeter/` |

## Estrutura de arquivos no servidor

```
Hetzner (91.99.194.219)
├── /opt/lms-music/              ← LMS (django + nginx + postgres + redis)
│   ├── docker-compose.yml       ← extra_hosts para host.docker.internal
│   ├── nginx/lms.conf           ← inclui location /spleeter/
│   ├── courses/templates/...    ← link para /spleeter/ no dashboard
│   └── ...
│
└── /root/spleeter/              ← Spleeter (fastapi + uvicorn)
    ├── docker-compose.yml       ← expoe porta 8001 no host
    ├── main.py                  ← app FastAPI (arquivo unico)
    ├── static/index.html        ← interface web
    ├── Dockerfile
    └── ...
```

## Decisoes tecnicas

### Por que converter para `.wav` antes de enviar?

O modelo MDX23 no Replicate salva o arquivo enviado sem extensao, e seu script de inferencia usa a extensao para detectar o codec. Arquivos `.m4a` e outros formatos causam falha. A conversao para `.wav` via ffmpeg garante compatibilidade.

### Por que usar a Files API do Replicate?

O arquivo `.wav` convertido tem ~50-120 MB. Enviar diretamente causava timeout de write. A Files API separa o upload da chamada de predicao.

### Por que `wait=False` no `replicate.run()`?

O Replicate impoe limite de **60 segundos** no modo sincrono. Musicas levam 5-10 minutos. Com `wait=False`, o cliente faz **polling** via GETs periodicos.

### Por que o spleeter nao esta dentro do docker-compose do LMS?

Sao projetos independentes com ciclos de deploy distintos. O spleeter pode ser atualizado sem reiniciar o LMS e vice-versa. A comunicacao entre eles e via rede do host (porta 8001).
