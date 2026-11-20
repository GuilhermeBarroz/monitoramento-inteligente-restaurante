"""
Escrita atômica de arquivos compartilhados entre o motor de visão e o dashboard.

Grava num arquivo temporário exclusivo na mesma pasta e troca com `os.replace`,
de modo que um leitor nunca veja um arquivo pela metade. Diferenças em relação
às versões antigas espalhadas pelo código:
- nome temporário único por escrita (dois escritores não colidem no mesmo `.tmp`);
- nova tentativa curta quando o Windows recusa o `os.replace` porque o dashboard
  está com o arquivo de destino aberto (`PermissionError`);
- o temporário é apagado se algo falhar;
- `cv2.imwrite` tem o retorno verificado (antes falhava em silêncio).
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

_TENTATIVAS_REPLACE = 5
_ESPERA_INICIAL_S = 0.02


def _substituir_com_retentativa(origem: Path, destino: Path) -> None:
    espera = _ESPERA_INICIAL_S
    for tentativa in range(_TENTATIVAS_REPLACE):
        try:
            os.replace(origem, destino)
            return
        except PermissionError:
            if tentativa == _TENTATIVAS_REPLACE - 1:
                raise
            time.sleep(espera)
            espera *= 2


_SUFIXOS_IMAGEM = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _temporario_na_pasta(destino: Path, sufixo: str) -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, nome = tempfile.mkstemp(prefix=f".{destino.stem}_", suffix=sufixo, dir=destino.parent)
    os.close(fd)
    # mkstemp cria com 0600; o arquivo final precisa ser legível pelo outro
    # processo/container (dashboard x motor) e por backups no host.
    try:
        os.chmod(nome, 0o644)
    except OSError:
        pass
    return Path(nome)


def escrever_json_atomico(destino: Path, dados: Any, indent: int = 2, duravel: bool = False) -> None:
    """
    Grava `dados` como JSON em `destino` de forma atômica.
    `duravel=True` faz fsync antes da troca: use para configurações (câmeras,
    mesas, regras), que não podem virar arquivo vazio após queda de energia.
    Estado ao vivo, regravado a cada segundo, dispensa (poupa o SSD).
    """
    destino = Path(destino)
    tmp = _temporario_na_pasta(destino, ".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=indent, ensure_ascii=False)
            if duravel:
                f.flush()
                os.fsync(f.fileno())
        _substituir_com_retentativa(tmp, destino)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def assinatura_arquivo(caminho: Path):
    """
    Identifica uma versão de arquivo para hot-reload: (mtime_ns, tamanho, inode).
    Só o mtime falha em sistemas de arquivos com resolução de 1-2 s (bind mounts,
    SMB, FAT): duas gravações no mesmo tique pareciam iguais. Como toda escrita
    atômica cria um inode novo, o inode distingue as versões no Linux.
    Retorna None se o arquivo não existir.
    """
    try:
        st = Path(caminho).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def limpar_temporarios_orfaos(pasta: Path, idade_min_s: float = 3600.0) -> int:
    """
    Remove temporários de escrita atômica deixados por um processo morto no meio
    da gravação (SIGKILL, queda de energia). Só apaga arquivos ocultos com o
    padrão destes escritores e mais antigos que `idade_min_s`. Retorna quantos.
    """
    pasta = Path(pasta)
    if not pasta.is_dir():
        return 0
    limite = time.time() - idade_min_s
    removidos = 0
    for p in pasta.iterdir():
        nome = p.name
        eh_tmp = (
            (nome.startswith(".") and (nome.endswith(".json.tmp") or p.suffix.lower() in _SUFIXOS_IMAGEM))
            or nome.endswith(".tmp.json")  # padrão antigo (antes do io_atomico)
        )
        if not eh_tmp or not p.is_file():
            continue
        try:
            if p.stat().st_mtime < limite:
                p.unlink()
                removidos += 1
        except OSError:
            pass
    return removidos


def escrever_imagem_atomica(destino: Path, img: np.ndarray) -> None:
    """Grava a imagem em `destino` (formato pela extensão) de forma atômica."""
    destino = Path(destino)
    tmp = _temporario_na_pasta(destino, destino.suffix or ".jpg")
    try:
        if not cv2.imwrite(str(tmp), img):
            raise OSError(f"cv2.imwrite falhou ao gravar {destino.name}")
        _substituir_com_retentativa(tmp, destino)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
