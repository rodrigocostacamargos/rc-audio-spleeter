"""
Testes unitários — sem chamadas reais à API do Replicate.
Todas as dependências externas são substituídas por mocks.
"""

import pytest
import httpx
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock
import replicate.exceptions

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from main import app, validate_audio_file, parse_stems, save_stem, run_with_retry

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_upload(filename: str, content_type: str, content: bytes = b"fake audio") -> UploadFile:
    """Cria um UploadFile sintético para testes de unidade."""
    return UploadFile(filename=filename, file=BytesIO(content), headers={"content-type": content_type})


# ---------------------------------------------------------------------------
# validate_audio_file
# ---------------------------------------------------------------------------

class TestValidateAudioFile:
    def test_wav_aceito(self):
        validate_audio_file(make_upload("song.wav", "application/octet-stream"))

    def test_mp3_aceito(self):
        validate_audio_file(make_upload("song.mp3", "application/octet-stream"))

    def test_m4a_aceito(self):
        validate_audio_file(make_upload("song.m4a", "application/octet-stream"))

    def test_content_type_audio_aceito(self):
        # Extensão desconhecida, mas content-type válido → deve passar
        validate_audio_file(make_upload("song.bin", "audio/mpeg"))

    def test_txt_rejeitado_400(self):
        with pytest.raises(HTTPException) as exc:
            validate_audio_file(make_upload("doc.txt", "text/plain"))
        assert exc.value.status_code == 400

    def test_pdf_rejeitado_400(self):
        with pytest.raises(HTTPException) as exc:
            validate_audio_file(make_upload("doc.pdf", "application/pdf"))
        assert exc.value.status_code == 400

    def test_sem_extensao_sem_content_type_rejeitado(self):
        with pytest.raises(HTTPException) as exc:
            validate_audio_file(make_upload("noextension", "application/octet-stream"))
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# parse_stems
# ---------------------------------------------------------------------------

class TestParseStems:
    def test_dict_completo(self):
        output = {
            "vocals": "https://cdn.example.com/vocals.wav",
            "drums":  "https://cdn.example.com/drums.wav",
            "bass":   "https://cdn.example.com/bass.wav",
            "other":  "https://cdn.example.com/other.wav",
        }
        assert parse_stems(output) == output

    def test_dict_filtra_chaves_desconhecidas(self):
        output = {
            "vocals":  "https://cdn.example.com/vocals.wav",
            "unknown": "https://cdn.example.com/unknown.wav",
        }
        result = parse_stems(output)
        assert "unknown" not in result
        assert "vocals" in result

    def test_lista_de_urls(self):
        output = [
            "https://cdn.example.com/vocals.wav",
            "https://cdn.example.com/drums.wav",
            "https://cdn.example.com/bass.wav",
            "https://cdn.example.com/other.wav",
        ]
        result = parse_stems(output)
        assert result["vocals"] == "https://cdn.example.com/vocals.wav"
        assert result["drums"]  == "https://cdn.example.com/drums.wav"
        assert result["bass"]   == "https://cdn.example.com/bass.wav"
        assert result["other"]  == "https://cdn.example.com/other.wav"

    def test_lista_sem_nome_conhecido_retorna_vazio(self):
        output = ["https://cdn.example.com/stem1.wav", "https://cdn.example.com/stem2.wav"]
        result = parse_stems(output)
        assert result == {}

    def test_tipo_invalido_levanta_valueerror(self):
        with pytest.raises(ValueError, match="Unexpected output format"):
            parse_stems("string inesperada")

    def test_tupla_funciona_como_lista(self):
        output = ("https://cdn.example.com/vocals.wav",)
        result = parse_stems(output)
        assert "vocals" in result


# ---------------------------------------------------------------------------
# run_with_retry
# ---------------------------------------------------------------------------

class TestRunWithRetry:
    def _make_429(self):
        err = replicate.exceptions.ReplicateError("rate limited")
        err.status = 429
        return err

    @patch("main.time.sleep")
    def test_sucesso_na_primeira_tentativa(self, mock_sleep):
        mock_client = MagicMock()
        mock_client.run.return_value = {"vocals": "https://example.com/v.wav"}

        result = run_with_retry(mock_client, "model", {})

        assert result == {"vocals": "https://example.com/v.wav"}
        mock_client.run.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("main.time.sleep")
    def test_retry_em_429_e_sucesso_na_segunda(self, mock_sleep):
        mock_client = MagicMock()
        mock_client.run.side_effect = [self._make_429(), {"vocals": "https://example.com/v.wav"}]

        result = run_with_retry(mock_client, "model", {})

        assert mock_client.run.call_count == 2
        mock_sleep.assert_called_once_with(15)  # delay da 1ª tentativa

    @patch("main.time.sleep")
    def test_levanta_apos_max_retries(self, mock_sleep):
        mock_client = MagicMock()
        mock_client.run.side_effect = self._make_429()

        with pytest.raises(replicate.exceptions.ReplicateError):
            run_with_retry(mock_client, "model", {}, max_retries=3)

        assert mock_client.run.call_count == 3
        assert mock_sleep.call_count == 2  # sleep entre tentativas, não após a última

    @patch("main.time.sleep")
    def test_backoff_linear(self, mock_sleep):
        mock_client = MagicMock()
        mock_client.run.side_effect = [self._make_429(), self._make_429(), {"ok": True}]

        run_with_retry(mock_client, "model", {}, max_retries=5)

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [15, 30]  # 15*1, 15*2

    @patch("main.time.sleep")
    def test_nao_faz_retry_em_outros_erros(self, mock_sleep):
        mock_client = MagicMock()
        err = replicate.exceptions.ReplicateError("server error")
        err.status = 500
        mock_client.run.side_effect = err

        with pytest.raises(replicate.exceptions.ReplicateError):
            run_with_retry(mock_client, "model", {})

        mock_client.run.assert_called_once()
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# save_stem
# ---------------------------------------------------------------------------

class TestSaveStem:
    def test_converte_e_salva_mp3(self, tmp_path):
        dest = tmp_path / "vocals.mp3"
        fake_wav = b"RIFF fake wav audio data"
        fake_mp3 = b"ID3 fake mp3 data"

        def fake_ffmpeg(cmd, **kwargs):
            # Simula ffmpeg escrevendo o mp3 de destino
            Path(cmd[6]).write_bytes(fake_mp3)  # cmd[6] = dest_path
            return MagicMock(returncode=0)

        with patch("main.httpx.Client") as mock_cls, \
             patch("main.subprocess.run", side_effect=fake_ffmpeg):
            mock_response = MagicMock()
            mock_response.content = fake_wav
            mock_cls.return_value.__enter__.return_value.get.return_value = mock_response

            save_stem("https://cdn.example.com/vocals.wav", dest)

        assert dest.exists()
        assert dest.read_bytes() == fake_mp3

    def test_levanta_excecao_em_http_erro(self, tmp_path):
        dest = tmp_path / "vocals.mp3"

        with patch("main.httpx.Client") as mock_cls:
            mock_response = MagicMock()
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "404 Not Found",
                request=MagicMock(),
                response=MagicMock(),
            )
            mock_cls.return_value.__enter__.return_value.get.return_value = mock_response

            with pytest.raises(httpx.HTTPStatusError):
                save_stem("https://cdn.example.com/missing.wav", dest)


# ---------------------------------------------------------------------------
# Helpers para mockar replicate.Client
# ---------------------------------------------------------------------------

def make_replicate_client_mock(run_return_value):
    """
    Cria um mock de replicate.Client que simula files.create() e client.run().
    """
    fake_file = MagicMock()
    fake_file.urls = {"get": "https://cdn.replicate.com/fake-file.wav"}

    mock_client_instance = MagicMock()
    mock_client_instance.files.create.return_value = fake_file
    mock_client_instance.run.return_value = run_return_value

    mock_client_cls = MagicMock(return_value=mock_client_instance)
    return mock_client_cls, mock_client_instance


# ---------------------------------------------------------------------------
# Endpoint POST /separate
# ---------------------------------------------------------------------------

class TestSeparateEndpoint:
    def test_arquivo_invalido_retorna_400(self):
        resp = client.post(
            "/separate",
            files={"file": ("documento.txt", b"conteudo qualquer", "text/plain")},
        )
        assert resp.status_code == 400

    @patch("main.save_stem")
    @patch("main.replicate.Client")
    def test_sucesso_com_saida_dict(self, mock_client_cls, mock_save):
        stems_dict = {
            "vocals": "https://cdn.example.com/vocals.wav",
            "drums":  "https://cdn.example.com/drums.wav",
            "bass":   "https://cdn.example.com/bass.wav",
            "other":  "https://cdn.example.com/other.wav",
        }
        real_mock, _ = make_replicate_client_mock(stems_dict)
        mock_client_cls.side_effect = real_mock.side_effect
        mock_client_cls.return_value = real_mock.return_value
        mock_save.return_value = None

        resp = client.post(
            "/separate",
            files={"file": ("song.wav", b"RIFF fake wav", "audio/wav")},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "request_id" not in data
        assert set(data["stems"]) == {"vocals", "drums", "bass", "other"}
        assert all(k in data["paths"] for k in ["vocals", "drums", "bass", "other"])
        # paths devem seguir o padrão {instrumento}_{nome_da_musica}.mp3
        for stem, path in data["paths"].items():
            assert path.endswith(f"{stem}_song.mp3")

    @patch("main.save_stem")
    @patch("main.replicate.Client")
    @patch("main.convert_to_wav")
    def test_sucesso_com_saida_lista(self, mock_convert, mock_client_cls, mock_save, tmp_path):
        fake_wav = tmp_path / "song.wav"
        fake_wav.write_bytes(b"RIFF fake")
        mock_convert.return_value = fake_wav

        stems_list = [
            "https://cdn.example.com/vocals.wav",
            "https://cdn.example.com/drums.wav",
            "https://cdn.example.com/bass.wav",
            "https://cdn.example.com/other.wav",
        ]
        real_mock, _ = make_replicate_client_mock(stems_list)
        mock_client_cls.side_effect = real_mock.side_effect
        mock_client_cls.return_value = real_mock.return_value
        mock_save.return_value = None

        resp = client.post(
            "/separate",
            files={"file": ("song.mp3", b"ID3 fake mp3", "audio/mpeg")},
        )

        assert resp.status_code == 200
        assert set(resp.json()["stems"]) == {"vocals", "drums", "bass", "other"}

    @patch("main.replicate.Client")
    def test_erro_replicate_retorna_500(self, mock_client_cls):
        mock_instance = MagicMock()
        mock_instance.files.create.side_effect = Exception("Replicate indisponível")
        mock_client_cls.return_value = mock_instance

        resp = client.post(
            "/separate",
            files={"file": ("song.wav", b"RIFF fake wav", "audio/wav")},
        )

        assert resp.status_code == 500
        assert "Replicate error" in resp.json()["detail"]

    @patch("main.save_stem")
    @patch("main.replicate.Client")
    def test_paths_usam_nome_da_musica(self, mock_client_cls, mock_save):
        real_mock, _ = make_replicate_client_mock({"vocals": "https://cdn.example.com/vocals.wav"})
        mock_client_cls.side_effect = real_mock.side_effect
        mock_client_cls.return_value = real_mock.return_value
        mock_save.return_value = None

        resp = client.post("/separate", files={"file": ("minha musica.wav", b"RIFF", "audio/wav")})

        assert resp.status_code == 200
        paths = resp.json()["paths"]
        assert paths["vocals"].endswith("vocals_minha musica.mp3")
