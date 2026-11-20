"""
Testes automatizados offline para suporte Multi-Câmeras, sanitização de fontes/upload,
leitura não-bloqueante em StreamCamera e hot-reload no GerenciadorMultiCameras.
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
import cv2
import numpy as np

sys.path.append(str(Path(__file__).parent))
import config_cameras
from camera_stream import StreamCamera
import multi_cameras
from multi_cameras import GerenciadorMultiCameras

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _criar_video_sintetico(caminho: Path, largura: int = 320, altura: int = 240, qtd_frames: int = 6):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(caminho), fourcc, 5.0, (largura, altura))
    assert writer.isOpened(), f"Falha ao criar vídeo sintético em {caminho}"
    for i in range(qtd_frames):
        frame = np.full((altura, largura, 3), 40 + (i * 20) % 150, dtype=np.uint8)
        writer.write(frame)
    writer.release()


class _FakeResult:
    boxes = []


class _FakeYOLO:
    def __init__(self, model_path: str):
        self.model_path = model_path

    def __call__(self, frame, classes=None, conf=0.25, verbose=False):
        return [_FakeResult()]


def testar_seguranca_fontes_e_ids():
    assert config_cameras.sanitizar_id_camera("Varanda Sul!") == "varanda_sul"
    assert config_cameras.validar_id_camera("cam_2") == "cam_2"

    for id_ruim in ["", "../etc", "cam 1", "cam/sub"]:
        try:
            config_cameras.validar_id_camera(id_ruim)
            raise AssertionError(f"Era esperado erro para ID inválido: {id_ruim}")
        except ValueError:
            pass

    assert config_cameras.sanitizar_fonte_camera("0") == "0"
    assert (
        config_cameras.sanitizar_fonte_camera("rtsp://admin:segredo@192.168.1.50:554/stream1")
        == "rtsp://admin:segredo@192.168.1.50:554/stream1"
    )

    mascarada = config_cameras.mascarar_fonte_segura("rtsp://admin:SenhaSecreta123@192.168.1.50:554/stream1")
    assert "SenhaSecreta123" not in mascarada
    assert "admin:****@192.168.1.50:554" in mascarada

    fontes_bloqueadas = [
        "15",
        "../outside.mp4",
        "file:///etc/passwd",
        "ftp://192.168.1.1/video.mp4",
        "rtsp://169.254.169.254/latest/meta-data",
        "rtsp://224.0.0.1/stream",
    ]
    for f_ruim in fontes_bloqueadas:
        try:
            config_cameras.sanitizar_fonte_camera(f_ruim)
            raise AssertionError(f"Fonte insegura não bloqueada: {f_ruim}")
        except ValueError:
            pass

    print("✅ [OK] Teste 1: Sanitização de IDs, path traversal, mascaramento e bloqueio SSRF")


def testar_caminhos_derivados_e_compatibilidade():
    c1 = config_cameras.caminhos_derivados_camera("cam_1")
    assert c1 == (
        "data/mesas_config.json",
        "data/videos/frame_limpo.jpg",
        "data/videos/frame_ao_vivo.jpg",
    )

    c2 = config_cameras.caminhos_derivados_camera("varanda")
    assert c2 == (
        "data/cameras/varanda_mesas.json",
        "data/videos/frame_limpo_varanda.jpg",
        "data/videos/frame_ao_vivo_varanda.jpg",
    )
    print("✅ [OK] Teste 2: Caminhos canônicos e retrocompatibilidade de cam_1")


def testar_persistencia_e_upload_video_isolado():
    orig_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            import os

            os.chdir(tmpdir)
            tmp_data = Path("data")
            tmp_videos = tmp_data / "videos"
            tmp_cfg = tmp_data / "cameras_config.json"

            vid_sint = tmp_videos / "salao_teste.mp4"
            _criar_video_sintetico(vid_sint)
            raw_mp4 = vid_sint.read_bytes()

            rel_up = config_cameras.salvar_upload_video("Clipe Varanda (Novo)!.mp4", raw_mp4)
            assert rel_up == "data/videos/Clipe_Varanda_Novo.mp4"
            assert (tmp_videos / "Clipe_Varanda_Novo.mp4").exists()

            # Rejeita extensão inválida e bytes corrompidos
            try:
                config_cameras.salvar_upload_video("malware.exe", b"MZ...")
                raise AssertionError("Extensão .exe deveria ser rejeitada")
            except ValueError:
                pass

            try:
                config_cameras.salvar_upload_video("falso.mp4", b"nao e um video valido")
                raise AssertionError("Arquivo .mp4 inválido deveria ser rejeitado")
            except ValueError:
                pass

            salvas = config_cameras.salvar_cameras_config(
                [
                    {
                        "id": "cam_1",
                        "nome": "Salão Interno",
                        "fonte": "data/videos/Clipe_Varanda_Novo.mp4",
                        "ativa": True,
                        "fps_alvo": 2.5,
                    }
                ],
                config_path=tmp_cfg,
            )
            assert len(salvas) == 1
            assert Path(salvas[0]["config_mesas_path"]).exists()
            lidas = config_cameras.carregar_cameras_config(config_path=tmp_cfg)
            assert lidas[0]["nome"] == "Salão Interno"

            # Rejeita IDs duplicados
            try:
                config_cameras.salvar_cameras_config([salvas[0], salvas[0]], config_path=tmp_cfg)
                raise AssertionError("IDs duplicados deveriam falhar")
            except ValueError:
                pass

            frame_dest = tmp_videos / "frame_capturado.jpg"
            dims = config_cameras.extrair_frame_limpo_fonte("data/videos/Clipe_Varanda_Novo.mp4", frame_dest)
            assert dims == (320, 240)
            assert frame_dest.exists()
        finally:
            os.chdir(orig_cwd)

    print("✅ [OK] Teste 3: Upload validado por OpenCV, persistência atômica e captura de frame")


def testar_stream_camera_nao_bloqueante():
    with tempfile.TemporaryDirectory() as tmpdir:
        vid = Path(tmpdir) / "stream_teste.mp4"
        _criar_video_sintetico(vid)

        stream = StreamCamera(fonte=str(vid), target_fps=10.0, reconnect_timeout=0.5)
        stream.iniciar()
        try:
            import time

            frame_lido = None
            for _ in range(40):
                ok, frame = stream.ler_se_pronto()
                if ok and frame is not None:
                    frame_lido = frame
                    break
                time.sleep(0.05)

            assert frame_lido is not None, "StreamCamera.ler_se_pronto() não retornou frame a tempo"
            assert stream.conectada is True
        finally:
            stream.parar()

        try:
            StreamCamera(fonte="rtsp://169.254.169.254/latest")
            raise AssertionError("StreamCamera deveria bloquear IP link-local SSRF")
        except ValueError:
            pass

    print("✅ [OK] Teste 4: StreamCamera leitura não-bloqueante e bloqueio SSRF")


def testar_gerenciador_multi_cameras_e_hot_reload():
    orig_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            import os
            import time

            os.chdir(tmpdir)
            tmp_data = Path("data")
            tmp_videos = tmp_data / "videos"
            tmp_cams = tmp_data / "cameras"
            tmp_cfg = tmp_data / "cameras_config.json"
            tmp_regras = tmp_data / "regras_config.json"
            tmp_estado = tmp_data / "estado_atual.json"

            vid1 = tmp_videos / "cam1.mp4"
            vid2 = tmp_videos / "cam2.mp4"
            _criar_video_sintetico(vid1)
            _criar_video_sintetico(vid2)

            mesas1 = tmp_data / "mesas_config.json"
            mesas1.parent.mkdir(parents=True, exist_ok=True)
            mesas1.write_text(
                json.dumps({
                    "resolucao": {"largura": 320, "altura": 240},
                    "mesas": [{"id": "Mesa Principal", "descricao": "Centro", "pontos": [[10, 10], [100, 10], [100, 100], [10, 100]]}],
                }),
                encoding="utf-8",
            )
            mesas2 = tmp_cams / "varanda_mesas.json"
            mesas2.parent.mkdir(parents=True, exist_ok=True)
            mesas2.write_text(
                json.dumps({
                    "resolucao": {"largura": 320, "altura": 240},
                    "mesas": [{"id": "Varanda 1", "descricao": "Deck", "pontos": [[20, 20], [150, 20], [150, 150], [20, 150]]}],
                }),
                encoding="utf-8",
            )

            with patch.object(multi_cameras, "YOLO", _FakeYOLO):
                config_cameras.salvar_cameras_config(
                    [
                        {"id": "cam_1", "nome": "Salão A", "fonte": "data/videos/cam1.mp4", "ativa": True, "fps_alvo": 5.0},
                        {"id": "varanda", "nome": "Varanda B", "fonte": "data/videos/cam2.mp4", "ativa": True, "fps_alvo": 5.0},
                    ],
                    config_path=tmp_cfg,
                )

                ger = GerenciadorMultiCameras(
                    cameras_config_path=tmp_cfg,
                    regras_path=tmp_regras,
                    model_path="fake.pt",
                )
                try:
                    assert len(ger.runners) == 2
                    assert ger.runners["cam_1"]["motor"].model is ger.runners["varanda"]["motor"].model

                    ger.exportar_estado_consolidado(tmp_estado)
                    snap = json.loads(tmp_estado.read_text(encoding="utf-8"))
                    assert snap["total_cameras"] == 2
                    assert snap["total_mesas"] == 2
                    assert "Mesa Principal" in snap["cameras"]["cam_1"]["mesas"]
                    assert "Varanda 1" in snap["cameras"]["varanda"]["mesas"]
                    assert "Mesa Principal" in snap["mesas"]
                    assert "Varanda 1" in snap["mesas"]

                    time.sleep(0.05)
                    config_cameras.salvar_cameras_config(
                        [
                            {"id": "cam_1", "nome": "Salão A Editado", "fonte": "data/videos/cam1.mp4", "ativa": True, "fps_alvo": 5.0},
                            {"id": "varanda", "nome": "Varanda B", "fonte": "data/videos/cam2.mp4", "ativa": False, "fps_alvo": 5.0},
                        ],
                        config_path=tmp_cfg,
                    )
                    houve_mudanca = ger._recarregar_cameras()
                    assert houve_mudanca is True
                    assert "cam_1" in ger.runners
                    assert "varanda" not in ger.runners
                    assert ger.runners["cam_1"]["motor"].camera_nome == "Salão A Editado"
                finally:
                    for cid in list(ger.runners.keys()):
                        ger._parar_runner(cid)
        finally:
            os.chdir(orig_cwd)

    print("✅ [OK] Teste 5: GerenciadorMultiCameras (modelo compartilhado, snapshot consolidado e hot-reload)")


def testar_camera_padrao_instalacao_limpa():
    """Instalação nova sem o vídeo de exemplo não pode quebrar: cam_1 nasce pausada."""
    import os

    orig_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            os.chdir(tmpdir)
            cfg = Path("data") / "cameras_config.json"

            # Sem vídeo de exemplo: cria cam_1 pausada (webcam 0), sem exceção
            cams = config_cameras.carregar_cameras_config(config_path=cfg)
            assert len(cams) == 1
            assert cams[0]["id"] == "cam_1"
            assert cams[0]["ativa"] is False
            assert cams[0]["fonte"] == "0"
            assert cfg.exists()

            # Com o vídeo de exemplo presente: cam_1 ativa apontando para ele
            cfg.unlink()
            _criar_video_sintetico(Path(config_cameras.VIDEO_EXEMPLO))
            cams = config_cameras.carregar_cameras_config(config_path=cfg)
            assert cams[0]["ativa"] is True
            assert cams[0]["fonte"] == config_cameras.VIDEO_EXEMPLO
        finally:
            os.chdir(orig_cwd)

    print("✅ [OK] Teste 6: Câmera padrão em instalação limpa (pausada sem vídeo de exemplo)")


if __name__ == "__main__":
    testar_seguranca_fontes_e_ids()
    testar_caminhos_derivados_e_compatibilidade()
    testar_persistencia_e_upload_video_isolado()
    testar_stream_camera_nao_bloqueante()
    testar_gerenciador_multi_cameras_e_hot_reload()
    testar_camera_padrao_instalacao_limpa()
    print("\n🎉 Todos os testes Multi-Câmeras passaram com sucesso!")
