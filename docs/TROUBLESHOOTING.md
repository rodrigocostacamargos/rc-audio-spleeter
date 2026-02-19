# Troubleshooting

Registro dos problemas encontrados durante o desenvolvimento e suas soluções.

---

## 1. Replicate retorna 404 ao chamar o modelo

**Sintoma:**
```
ReplicateError: status: 404 — The requested resource could not be found.
```

**Causa:** O cliente Replicate (`replicate.run("username/model")`) requer um version hash explícito quando o modelo não tem um deployment padrão ativo.

**Solução:** Fixar o hash da versão na constante `REPLICATE_MODEL`:

```python
REPLICATE_MODEL = "lucataco/mvsep-mdx23-music-separation:510b9b91..."
```

Para obter o hash mais recente:

```python
import replicate
model = replicate.models.get("lucataco/mvsep-mdx23-music-separation")
print(model.versions.list()[0].id)
```

---

## 2. Modelo falha internamente com exit code 1

**Sintoma:**
```
ModelError: Command '['python', 'inference.py', '--input_audio', '/tmp/tmpXXXXXdownload', ...]'
returned non-zero exit status 1.
```

**Causa:** O Replicate salva o arquivo enviado como objeto binário sem extensão (ex: `/tmp/tmpXXXXdownload`). O script `inference.py` do modelo usa a extensão para detectar o codec. Arquivos `.m4a` ou outros formatos sem extensão causam falha.

**Solução:** Converter o arquivo para `.wav` antes de enviar, usando ffmpeg:

```python
subprocess.run(["ffmpeg", "-y", "-i", str(src), str(dest)], check=True)
```

O `.wav` é identificado sem depender da extensão, pois o cabeçalho RIFF é autoexplicativo.

---

## 3. ReadTimeout ao enviar arquivo grande

**Sintoma:**
```
httpx.ReadTimeout: The read operation timed out
```
ocorre durante `replicate_client.files.create(audio_file)`.

**Causa:** O cliente Replicate usa por padrão `write=30s`. Um arquivo `.wav` de ~120 MB (música de 5 min) leva mais de 30 segundos para ser enviado.

**Solução:** Instanciar o cliente com timeout customizado:

```python
REPLICATE_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)
replicate_client = replicate.Client(api_token=..., timeout=REPLICATE_TIMEOUT)
```

---

## 4. ReadTimeout ao aguardar o modelo terminar

**Sintoma:**
```
httpx.ReadTimeout: The read operation timed out
```
ocorre durante `replicate_client.run(...)`, na chamada `predictions.create()`.

**Causa:** Por padrão, `replicate.run()` envia o header `Prefer: wait` que faz o servidor manter a conexão HTTP aberta até o modelo concluir. O Replicate impõe um limite de **60 segundos** nesse modo. Músicas de duração normal levam 5–10 minutos para processar, então a conexão é encerrada pelo servidor antes da conclusão.

**Solução:** Passar `wait=False` para forçar modo polling:

```python
output = replicate_client.run(
    REPLICATE_MODEL,
    input={"audio": replicate_file.urls["get"]},
    wait=False,  # desabilita Prefer: wait; usa polling via prediction.wait()
)
```

Com `wait=False`, o `predictions.create()` retorna imediatamente com o ID da predição. O polling faz GETs curtos em intervalos regulares até o modelo terminar, sem manter conexão longa aberta.

---

## 5. Testes unitários falham após adicionar `convert_to_wav`

**Sintoma:** Teste com arquivo `.mp3` chama ffmpeg com conteúdo falso e falha.

**Causa:** O teste enviava `b"ID3 fake mp3"` como conteúdo, que não é um MP3 válido. Com a adição da conversão automática, o ffmpeg tenta processar o arquivo e falha.

**Solução:** Mockar `convert_to_wav` nos testes de endpoint que enviam formatos não-wav:

```python
@patch("main.convert_to_wav")
def test_sucesso_com_saida_lista(self, mock_convert, ...):
    fake_wav = tmp_path / "song.wav"
    fake_wav.write_bytes(b"RIFF fake")
    mock_convert.return_value = fake_wav
    ...
```

---

## 6. Testes unitários falham após migrar de `replicate.run` para `replicate.Client`

**Sintoma:** Mocks de `main.replicate.run` deixam de funcionar.

**Causa:** O endpoint passou a usar `replicate.Client(...)` com `files.create()` e `client.run()`, quebrando os patches que apontavam para `replicate.run` diretamente.

**Solução:** Mockar `main.replicate.Client` e simular o fluxo completo:

```python
@patch("main.replicate.Client")
def test_sucesso(self, mock_client_cls):
    fake_file = MagicMock()
    fake_file.urls = {"get": "https://cdn.replicate.com/fake.wav"}

    mock_instance = MagicMock()
    mock_instance.files.create.return_value = fake_file
    mock_instance.run.return_value = {"vocals": "https://..."}
    mock_client_cls.return_value = mock_instance
    ...
```

---

## 6. Teste de integração demora muito ou falha por timeout de rede

**Sintoma:** `pytest tests/test_integration.py` fica preso por mais de 10 minutos.

**Causa esperada:** O modelo MDX23 é pesado e leva 5–10 minutos para músicas de ~5 minutos. É comportamento normal.

**Como rodar corretamente:**

```bash
# Timeout padrão do pytest é 0 (sem limite), mas o TestClient tem o seu próprio
pytest tests/test_integration.py -v -s
```

Se quiser pular o teste de integração em CI:

```bash
pytest tests/test_unit.py  # roda apenas os unitários
```
