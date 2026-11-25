"""
blocos.py - as gravações brutas (pastas em blocos/) e quando é seguro apagá-las.

Com APAGAR_BLOCOS_VIGIA = False os blocos ficam no disco até você mandar apagar
pelo Telegram (/blocos ou o botão da mensagem de "live encerrada"). Este módulo
lista as lives que ainda têm blocos e decide se uma delas pode ser apagada.

Proteção: uma live fica "protegida" (não dá para apagar) quando
    1. algum processo marcou a pasta como ocupada: o gravador enquanto grava, o
       main.py até terminar os cortes e as transcrições, e qualquer extração de
       vídeo dos blocos (cortes, extensões de corte, correções de sincronia);
    2. existe um main.py (ou gravador.py) rodando para aquele canal desde antes
       da pasta ser criada (pega também gravações iniciadas por uma versão antiga
       do programa, que ainda não fazia a marcação);
    3. algum bloco .ts mudou no último minuto (alguém está gravando nele).

A marcação é um arquivo .ocupado_* dentro da pasta da sessão, com o número do
processo (PID). Se o processo morrer sem apagar a marca, ela deixa de valer
sozinha: o PID não existe mais (ou é de outro processo, mais novo).
"""

import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

import config
from gravador import montar_url

# canal_AAAA-MM-DD_HH-MM, o nome que o gravador dá às pastas (o canal pode ter "_")
REGEX_SESSAO = re.compile(r"^(?P<canal>\w+?)_(?P<data>\d{4}-\d{2}-\d{2}_\d{2}-\d{2})$")
PREFIXO_OCUPADO = ".ocupado_"
PREFIXO_APAGANDO = ".apagando_"
SEGUNDOS_ARQUIVO_RECENTE = 60          # arquivo mudou há menos que isso = alguém escrevendo
PROGRAMAS_QUE_GRAVAM = ("main.py", "gravador.py")


class LiveProtegida(Exception):
    """A live está gravando ou em processamento: os blocos não podem ser apagados."""


@dataclass
class Live:
    """Uma pasta de blocos/ que ainda tem vídeo."""
    nome: str                  # nome da pasta, ex.: alanzoka_2026-11-25_14-17
    pasta: Path
    canal: str
    inicio: datetime
    tamanho: int               # bytes da pasta inteira (é o que libera ao apagar)
    blocos: int                # quantos bloco_*.ts
    cortes: int = 0            # cortes salvos a partir desta live
    sem_avaliacao: int = 0     # desses, quantos você ainda não marcou no Telegram
    protecao: str | None = None   # motivo para não poder apagar (None = pode)


# ---------------------------------------------------------------- tamanhos

def formatar_tamanho(numero_bytes: float) -> str:
    """15,8 GB / 512 MB, com vírgula como no Brasil."""
    if numero_bytes >= 1024**3:
        texto = f"{numero_bytes / 1024**3:.1f} GB"
    else:
        texto = f"{numero_bytes / 1024**2:.0f} MB"
    return texto.replace(".", ",")


def espaco_livre() -> int:
    """Bytes livres no disco onde ficam os blocos."""
    pasta = config.PASTA_BLOCOS if config.PASTA_BLOCOS.exists() else config.PASTA_PROJETO
    return shutil.disk_usage(pasta).free


def tamanho_pasta(pasta: Path) -> int:
    return sum(a.stat().st_size for a in pasta.rglob("*") if a.is_file())


# ---------------------------------------------------------------- marcação de "ocupada"

def _processo_vivo(pid: int, criado: float) -> bool:
    """O processo existe e é o mesmo (o Windows reaproveita números de PID)."""
    try:
        return abs(psutil.Process(pid).create_time() - criado) < 1
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


def marcar_ocupada(pasta: Path, motivo: str) -> Path | None:
    """
    Marca a pasta da sessão como ocupada por este processo. Devolve a marca, para
    liberar() depois. Use de preferência o `with ocupada(...)`.
    """
    if not pasta.is_dir():
        return None
    pid = os.getpid()
    marca = pasta / f"{PREFIXO_OCUPADO}{pid}_{time.monotonic_ns()}"
    marca.write_text(json.dumps({
        "pid": pid,
        "criado": psutil.Process(pid).create_time(),
        "motivo": motivo,
        "desde": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    return marca


def liberar(marca: Path | None) -> None:
    if marca:
        marca.unlink(missing_ok=True)


@contextmanager
def ocupada(pasta: Path, motivo: str):
    """with blocos.ocupada(pasta_sessao, "corte em andamento"): ... (a marca some no fim)."""
    marca = marcar_ocupada(pasta, motivo)
    try:
        yield
    finally:
        liberar(marca)


def ocupacoes(pasta: Path) -> list[str]:
    """Motivos das marcas que ainda valem. Marcas de processos mortos são apagadas."""
    motivos = []
    for marca in pasta.glob(f"{PREFIXO_OCUPADO}*"):
        try:
            dados = json.loads(marca.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Marca sendo escrita agora (ou estragada): na dúvida, protege
            motivos.append("em processamento")
            continue
        if _processo_vivo(dados["pid"], dados["criado"]):
            motivos.append(dados["motivo"])
        else:
            marca.unlink(missing_ok=True)       # sobrou de um processo que já morreu
    return motivos


# ---------------------------------------------------------------- gravações rodando

def gravacoes_rodando() -> list[tuple[str, float]]:
    """(canal, hora em que o processo começou) de cada main.py/gravador.py rodando."""
    rodando = []
    for processo in psutil.process_iter(["pid", "cmdline", "create_time"]):
        if processo.info["pid"] == os.getpid():
            continue
        argumentos = processo.info["cmdline"] or []
        for i, argumento in enumerate(argumentos[:-1]):
            if Path(argumento).name.lower() in PROGRAMAS_QUE_GRAVAM:
                canal = montar_url(argumentos[i + 1])[1]
                rodando.append((canal, processo.info["create_time"]))
                break
    return rodando


def _inicio_da_pasta(nome: str) -> datetime | None:
    m = REGEX_SESSAO.match(nome)
    return datetime.strptime(m["data"], "%Y-%m-%d_%H-%M") if m else None


def motivo_protecao(pasta: Path, rodando: list[tuple[str, float]] | None = None) -> str | None:
    """Por que essa live não pode ser apagada agora? None = pode apagar."""
    motivos = ocupacoes(pasta)
    if motivos:
        return ", ".join(dict.fromkeys(motivos))   # sem repetir, na ordem
    m = REGEX_SESSAO.match(pasta.name)
    inicio = _inicio_da_pasta(pasta.name)
    if m and inicio:
        for canal, comecou in (gravacoes_rodando() if rodando is None else rodando):
            # A pasta tem a hora só em minutos: 2 min de folga
            if canal == m["canal"].lower() and inicio.timestamp() >= comecou - 120:
                return "gravação em andamento"
    agora = time.time()
    if any(agora - a.stat().st_mtime < SEGUNDOS_ARQUIVO_RECENTE for a in pasta.glob("bloco_*.ts")):
        return "gravação em andamento (bloco crescendo)"
    return None


# ---------------------------------------------------------------- listar

def contar_cortes() -> dict[str, tuple[int, int]]:
    """{pasta da sessão: (cortes, sem avaliação)} lendo todos os cortes.json."""
    contagem: dict[str, list[int]] = {}
    for arquivo in config.PASTA_CORTES.rglob("cortes.json"):
        if "backup_cortes_antigos" in arquivo.parts:
            continue
        try:
            registro = json.loads(arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for corte in registro:
            if corte.get("descarte_apagado"):
                continue
            total = contagem.setdefault(corte.get("sessao", ""), [0, 0])
            total[0] += 1
            total[1] += corte.get("avaliacao") is None
    return {sessao: (t[0], t[1]) for sessao, t in contagem.items()}


def _montar(pasta: Path, cortes: dict, rodando: list) -> Live | None:
    m = REGEX_SESSAO.match(pasta.name)
    quantos_blocos = len(list(pasta.glob("bloco_*.ts")))
    if not m or not quantos_blocos:
        return None
    total, sem = cortes.get(pasta.name, (0, 0))
    return Live(nome=pasta.name, pasta=pasta, canal=m["canal"], inicio=_inicio_da_pasta(pasta.name),
                tamanho=tamanho_pasta(pasta), blocos=quantos_blocos, cortes=total, sem_avaliacao=sem,
                protecao=motivo_protecao(pasta, rodando))


def listar() -> list[Live]:
    """Lives que ainda têm blocos, da mais antiga para a mais nova."""
    if not config.PASTA_BLOCOS.exists():
        return []
    cortes, rodando = contar_cortes(), gravacoes_rodando()
    lives = [_montar(p, cortes, rodando) for p in config.PASTA_BLOCOS.iterdir() if p.is_dir()]
    return sorted((l for l in lives if l), key=lambda l: l.inicio)


def pasta_da_live(nome: str) -> Path:
    """
    A pasta de blocos/ com esse nome. Recusa qualquer outro caminho (com "..",
    barras etc.): o nome chega de um botão do Telegram e só pode apontar para
    uma pasta de sessão dentro de blocos/.
    """
    if not REGEX_SESSAO.match(nome):
        raise ValueError(f"nome de live inválido: {nome!r}")
    pasta = config.PASTA_BLOCOS / nome
    if pasta.resolve().parent != config.PASTA_BLOCOS.resolve():
        raise ValueError(f"fora da pasta blocos/: {nome!r}")
    return pasta


def info(nome: str) -> Live | None:
    """A live com esse nome (None se não existe ou já não tem blocos)."""
    pasta = pasta_da_live(nome)
    if not pasta.is_dir() or pasta.is_symlink():
        return None
    return _montar(pasta, contar_cortes(), gravacoes_rodando())


# ---------------------------------------------------------------- apagar

def apagar(nome: str) -> int:
    """
    Apaga a pasta de blocos dessa live (e só ela). Devolve quantos bytes liberou.
    A proteção é conferida de novo aqui, na hora de apagar: entre o botão
    aparecer e você clicar em "Sim", alguma coisa pode ter começado a usar a live.
    """
    pasta = pasta_da_live(nome)
    if not pasta.is_dir() or pasta.is_symlink():
        raise FileNotFoundError("esses blocos já foram apagados")
    motivo = motivo_protecao(pasta)
    if motivo:
        raise LiveProtegida(motivo)
    tamanho = tamanho_pasta(pasta)
    # Renomeia antes de apagar: no Windows isso falha se algum programa estiver com
    # um arquivo da pasta aberto (o ffmpeg, por exemplo), e aí nada é apagado.
    # Depois de renomeada, nenhum programa novo encontra a pasta para usar.
    temporaria = pasta.with_name(PREFIXO_APAGANDO + nome)
    try:
        pasta.rename(temporaria)
    except OSError as e:
        raise LiveProtegida(f"algum programa está usando os arquivos ({e.strerror})") from None
    shutil.rmtree(temporaria)
    return tamanho


def limpar_restos() -> None:
    """Termina de apagar uma pasta .apagando_* que ficou pela metade (ex.: queda de energia)."""
    if config.PASTA_BLOCOS.exists():
        for pasta in config.PASTA_BLOCOS.glob(f"{PREFIXO_APAGANDO}*"):
            shutil.rmtree(pasta, ignore_errors=True)
