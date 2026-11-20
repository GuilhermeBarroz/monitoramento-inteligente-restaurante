"""
Gerenciamento, validação e persistência atômica de múltiplas câmeras e fontes de vídeo.
Suporta câmeras IP (RTSP/HTTP), webcams e arquivos de vídeo em `data/videos/`,
garantindo sanitização estrita de caminhos/URLs e criação automática da câmera padrão.
"""

import ipaddress
import json
import os
import re
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse
import cv2

from io_atomico import escrever_imagem_atomica, escrever_json_atomico

DATA_DIR = Path("data")
VIDEOS_DIR = DATA_DIR / "videos"
CAMERAS_DIR = DATA_DIR / "cameras"
CAMERAS_CONFIG_PATH = DATA_DIR / "cameras_config.json"

EXTENSOES_VIDEO = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_CONTROLE_REGEX = re.compile(r"[\x00-\x1f\x7f]")


def sanitizar_id_camera(texto: str) -> str:
    """Converte um nome em ID seguro `[a-z0-9_-]` para uso em arquivos."""
    limpo = re.sub(r"[^a-zA-Z0-9_-]+", "_", (texto or "").strip().lower()).strip("_")
    limpo = limpo[:32]
    return limpo or "cam"


def validar_id_camera(cam_id: str) -> str:
    """Garante que o ID da câmera não contenha travessia de caminho nem caracteres inválidos."""
    cid = str(cam_id or "").strip()
    if not _ID_REGEX.match(cid):
        raise ValueError(f"ID de câmera inválido: '{cam_id}'. Use apenas letras, números, '_' ou '-'.")
    return cid


def _eh_ip_bloqueado_ssrf(hostname: str) -> bool:
    """Bloqueia faixas link-local / metadata na nuvem (ex: 169.254.169.254), permitindo CFTV em LAN privada."""
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_link_local or ip.is_unspecified or ip.is_multicast
    except ValueError:
        return False


def sanitizar_fonte_camera(fonte: Any) -> str:
    """
    Valida e normaliza a fonte da câmera (arquivo em `data/`, webcam `0..10` ou URL RTSP/HTTP).
    Previne path traversal fora de `data/` e protocolos inseguros.
    """
    if isinstance(fonte, int) or (isinstance(fonte, str) and fonte.strip().isdigit()):
        idx = int(str(fonte).strip())
        if idx < 0 or idx > 10:
            raise ValueError("Índice de webcam deve estar entre 0 e 10.")
        return str(idx)

    fonte_str = _CONTROLE_REGEX.sub("", str(fonte or "")).strip()
    if not fonte_str:
        raise ValueError("Informe o caminho do vídeo ou a URL RTSP/HTTP da câmera.")

    parsed = urlparse(fonte_str)
    if parsed.scheme and parsed.scheme.lower() not in {"c", "d", "e"}:
        scheme = parsed.scheme.lower()
        if scheme not in {"rtsp", "rtsps", "http", "https"}:
            raise ValueError(f"Protocolo '{parsed.scheme}' não permitido. Use RTSP, RTSPS, HTTP, HTTPS, webcam ou vídeo local.")
        if not parsed.hostname:
            raise ValueError("URL de câmera inválida: informe o IP ou hostname.")
        try:
            parsed.port  # levanta ValueError para porta não numérica ou fora de 0..65535
        except ValueError as exc:
            raise ValueError("URL de câmera inválida: porta deve ser um número entre 1 e 65535.") from exc
        if _eh_ip_bloqueado_ssrf(parsed.hostname):
            raise ValueError("Endereço IP reservado/link-local não permitido por política de segurança.")
        return fonte_str

    # Caminho de arquivo local: exige que resida dentro da pasta `data/` configurada
    base_data = DATA_DIR.resolve()
    candidato = Path(fonte_str)
    alvo = candidato.resolve() if candidato.is_absolute() else (base_data.parent / candidato).resolve()

    try:
        relativo = alvo.relative_to(base_data)
    except ValueError as exc:
        raise ValueError("Arquivos de vídeo locais devem ficar dentro da pasta 'data/'.") from exc

    if not alvo.is_file():
        raise ValueError(f"Arquivo de vídeo não encontrado em 'data/{relativo.as_posix()}'.")

    return f"data/{relativo.as_posix()}"


def mascarar_fonte_segura(fonte: str) -> str:
    """Oculta senhas em URLs RTSP/HTTP para exibição segura. Nunca levanta exceção."""
    s = str(fonte or "")
    try:
        parsed = urlparse(s)
        if parsed.scheme in {"rtsp", "rtsps", "http", "https"} and parsed.password:
            user = parsed.username or ""
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            netloc = f"{user}:****@{host}{port}"
            return urlunparse(parsed._replace(netloc=netloc))
        return s
    except ValueError:
        # URL malformada (ex.: porta inválida): não expõe o texto, que pode conter senha
        return "<URL de câmera inválida>"


def caminhos_derivados_camera(cam_id: str) -> Tuple[str, str, str]:
    """Retorna caminhos canônicos `(config_mesas_path, frame_limpo_path, frame_vivo_path)` para uma câmera."""
    cid = validar_id_camera(cam_id)
    if cid == "cam_1":
        return (
            "data/mesas_config.json",
            "data/videos/frame_limpo.jpg",
            "data/videos/frame_ao_vivo.jpg",
        )
    return (
        f"data/cameras/{cid}_mesas.json",
        f"data/videos/frame_limpo_{cid}.jpg",
        f"data/videos/frame_ao_vivo_{cid}.jpg",
    )


def _garantir_arquivo_mesas_inicial(config_mesas_path: Path, frame_limpo_path: Path):
    """Cria arquivo vazio de mesas para novas câmeras se ainda não existir."""
    if config_mesas_path.exists():
        return
    largura, altura = 1280, 720
    if frame_limpo_path.exists():
        img = cv2.imread(str(frame_limpo_path))
        if img is not None:
            altura, largura = img.shape[:2]

    config_mesas_path.parent.mkdir(parents=True, exist_ok=True)
    dados = {
        "imagem_referencia": frame_limpo_path.name,
        "resolucao": {"largura": largura, "altura": altura},
        "total_mesas": 0,
        "mesas": [],
    }
    escrever_json_atomico(config_mesas_path, dados, duravel=True)


def salvar_cameras_config(cameras_raw: List[dict], config_path: Path = CAMERAS_CONFIG_PATH) -> List[dict]:
    """Valida, normaliza e persiste `data/cameras_config.json` atomicamente."""
    if not isinstance(cameras_raw, list) or len(cameras_raw) == 0:
        raise ValueError("É necessário manter pelo menos 1 câmera configurada.")
    if len(cameras_raw) > 32:
        raise ValueError("Limite máximo de 32 câmeras excedido.")

    ids_vistos = set()
    normalizadas: List[dict] = []

    for idx, item in enumerate(cameras_raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Câmera #{idx} possui formato inválido.")

        cid = validar_id_camera(item.get("id", f"cam_{idx}"))
        if cid in ids_vistos:
            raise ValueError(f"ID de câmera duplicado: '{cid}'.")
        ids_vistos.add(cid)

        nome = _CONTROLE_REGEX.sub("", str(item.get("nome", ""))).strip()[:50]
        if not nome:
            raise ValueError(f"A câmera '{cid}' está sem nome.")

        fonte = sanitizar_fonte_camera(item.get("fonte", ""))
        ativa = bool(item.get("ativa", True))
        fps_alvo = max(0.5, min(10.0, float(item.get("fps_alvo", 2.5))))

        cfg_mesas, frame_limpo, frame_vivo = caminhos_derivados_camera(cid)
        _garantir_arquivo_mesas_inicial(Path(cfg_mesas), Path(frame_limpo))

        normalizadas.append({
            "id": cid,
            "nome": nome,
            "fonte": fonte,
            "ativa": ativa,
            "fps_alvo": round(fps_alvo, 2),
            "config_mesas_path": cfg_mesas,
            "frame_limpo_path": frame_limpo,
            "frame_vivo_path": frame_vivo,
        })

    escrever_json_atomico(Path(config_path), {"cameras": normalizadas}, duravel=True)
    return normalizadas


# Última configuração lida com sucesso, por caminho. Usada quando uma leitura
# falha (arquivo sendo substituído, JSON corrompido) para nunca perder câmeras.
_ultima_config_valida: dict = {}


def _ler_cameras_do_arquivo(config_path: Path) -> List[dict]:
    with open(config_path, "r", encoding="utf-8") as f:
        dados = json.load(f)
    cams = dados.get("cameras") if isinstance(dados, dict) else None
    if not isinstance(cams, list) or len(cams) == 0:
        raise ValueError("lista 'cameras' ausente ou vazia")
    return cams


def carregar_cameras_config(config_path: Path = CAMERAS_CONFIG_PATH) -> List[dict]:
    """
    Lê `data/cameras_config.json`. Cria a configuração padrão SOMENTE se o arquivo
    não existir. Se existir mas não puder ser lido (corrida com uma escrita, JSON
    corrompido, edição manual errada), o arquivo NUNCA é sobrescrito: tenta de novo
    brevemente e, persistindo o erro, devolve a última configuração válida em
    memória; sem nenhuma, levanta ValueError para o chamador tratar.
    """
    config_path = Path(config_path)
    chave = str(config_path.resolve())

    if not config_path.exists():
        cams = salvar_cameras_config(_camera_padrao(), config_path=config_path)
        _ultima_config_valida[chave] = cams
        return cams

    ultimo_erro: Optional[Exception] = None
    for tentativa in range(3):
        try:
            cams = _ler_cameras_do_arquivo(config_path)
            _ultima_config_valida[chave] = cams
            return cams
        except (OSError, ValueError) as e:  # JSONDecodeError é subclasse de ValueError
            ultimo_erro = e
            time.sleep(0.05 * (tentativa + 1))

    if chave in _ultima_config_valida:
        print(f"⚠️ cameras_config.json ilegível ({ultimo_erro}); mantendo a última configuração válida.")
        return _ultima_config_valida[chave]
    raise ValueError(
        f"Não foi possível ler {config_path} ({ultimo_erro}). "
        "O arquivo foi preservado; corrija-o ou restaure um backup."
    )


VIDEO_EXEMPLO = "data/videos/sample_restaurante.mp4"


def _camera_padrao() -> List[dict]:
    """
    Câmera inicial de uma instalação nova.

    Se o vídeo de exemplo existir (ambiente de desenvolvimento), usa-o ativo.
    Numa instalação limpa (o vídeo não vai para o git), cria a câmera PAUSADA
    apontando para a webcam 0, que passa na validação sem exigir arquivo, para
    o motor e o dashboard subirem e o técnico configurar a fonte RTSP real
    na aba de Câmeras.
    """
    if (DATA_DIR.parent / VIDEO_EXEMPLO).is_file():
        return [{
            "id": "cam_1",
            "nome": "Salão Principal",
            "fonte": VIDEO_EXEMPLO,
            "ativa": True,
            "fps_alvo": 2.5,
        }]
    return [{
        "id": "cam_1",
        "nome": "Salão Principal (configure a fonte)",
        "fonte": "0",
        "ativa": False,
        "fps_alvo": 2.5,
    }]


def listar_videos_disponiveis() -> List[str]:
    """Lista vídeos disponíveis em `data/videos/` excluindo renderizações de saída."""
    if not VIDEOS_DIR.exists():
        return []
    arquivos = []
    for p in sorted(VIDEOS_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in EXTENSOES_VIDEO and not p.name.startswith("output_"):
            arquivos.append(f"data/videos/{p.name}")
    return arquivos


def salvar_upload_video(nome_original: str, conteudo: bytes) -> str:
    """Salva vídeo enviado pelo navegador com nome sanitizado dentro de `data/videos/`."""
    base_name = Path(nome_original).name
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(base_name).stem).strip("_") or "video"
    ext = Path(base_name).suffix.lower()
    if ext not in EXTENSOES_VIDEO:
        raise ValueError(f"Formato '{ext}' não suportado. Envie .mp4, .avi, .mov, .mkv ou .webm.")

    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    destino = VIDEOS_DIR / f"{stem}{ext}"
    tmp = VIDEOS_DIR / f".tmp_{stem}{ext}"
    tmp.write_bytes(conteudo)

    cap = cv2.VideoCapture(str(tmp))
    valido = cap.isOpened()
    if valido:
        ret, _ = cap.read()
        valido = bool(ret)
    cap.release()

    if not valido:
        tmp.unlink(missing_ok=True)
        raise ValueError("O arquivo enviado não é um vídeo válido legível pelo OpenCV.")

    os.replace(tmp, destino)
    return f"data/videos/{destino.name}"


def extrair_frame_limpo_fonte(fonte: str, destino_jpg: Path) -> Optional[Tuple[int, int]]:
    """Captura o primeiro frame de um vídeo/webcam/RTSP para calibração imediata no Editor."""
    fonte_norm = sanitizar_fonte_camera(fonte)
    src: Any = int(fonte_norm) if fonte_norm.isdigit() else fonte_norm

    if isinstance(src, str) and urlparse(src).scheme in {"rtsp", "rtsps", "http", "https"}:
        # Timeouts: uma câmera inacessível não pode travar a página do painel
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000,
        ])
    else:
        cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        cap.release()
        return None

    sucesso, frame = cap.read()
    cap.release()
    if not sucesso or frame is None:
        return None

    escrever_imagem_atomica(destino_jpg, frame)
    h, w = frame.shape[:2]
    return w, h
