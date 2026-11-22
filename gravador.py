"""
gravador.py - Etapa 1 do projeto de cortes automáticos.

Grava uma live em blocos .ts enquanto ela acontece, reconecta sozinho se a
conexão cair e registra a hora real de início de cada trecho gravado, para
depois alinhar o vídeo com o chat.

Uso:
    python gravador.py nomedocanal                          (Twitch)
    python gravador.py https://www.youtube.com/@canal/live  (link completo)
    python gravador.py https://kick.com/canal               (link completo)

Arquivos gerados em blocos/<canal>_<data>_<hora>/:
    bloco_000.ts, bloco_001.ts, ...  vídeo gravado (sem recodificar)
    inicio.txt                       hora real (ISO) do primeiro byte de vídeo
    sessao.json                      trechos (cada reconexão) e anúncios detectados
    trecho_01.csv, trecho_02.csv...  lista de blocos de cada trecho, escrita pelo ffmpeg
"""

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import config

TAMANHO_PEDACO = 64 * 1024  # bytes lidos do streamlink por vez

# No Windows, cada processo filho ganha seu próprio "grupo". Assim o Ctrl+C
# chega só no Python, e nós encerramos o ffmpeg do jeito certo (sem corromper).
GRUPO_SEPARADO = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


# ---------------------------------------------------------------- utilidades

def agora() -> datetime:
    """Hora atual com fuso horário (ex.: 2026-11-19T14:30:00.123-03:00)."""
    return datetime.now().astimezone()


def log(mensagem: str) -> None:
    """Escreve uma mensagem no terminal com o horário na frente."""
    print(f"[{agora():%H:%M:%S}] {mensagem}", flush=True)


def montar_url(entrada: str) -> tuple[str, str]:
    """
    Recebe o nome do canal ou um link e devolve (url, nome_para_pasta).

    Exemplos:
        "Gaules"                                -> twitch.tv/gaules, "gaules"
        "https://www.twitch.tv/gaules"          -> o próprio link,   "gaules"
        "https://www.youtube.com/@canal/live"   -> o próprio link,   "canal"
        "kick.com/canal"                        -> https://kick.com/canal, "canal"
    """
    entrada = entrada.strip()

    # Só o nome (sem ponto e sem barra): assume que é Twitch
    if "/" not in entrada and "." not in entrada:
        nome = entrada.lower()
        return f"https://www.twitch.tv/{nome}", nome

    # Link sem "https://" (ex.: twitch.tv/canal): completa
    url = entrada if entrada.startswith("http") else f"https://{entrada}"

    # O nome da pasta é a primeira parte "útil" do caminho do link,
    # ignorando palavras genéricas como "live", "watch", "c" e "channel"
    partes = [p for p in urlparse(url).path.split("/") if p]
    genericas = {"live", "watch", "c", "channel", "user", "videos"}
    uteis = [p.lstrip("@") for p in partes if p.lower() not in genericas]
    if uteis:
        nome = uteis[0]
    else:
        # Links tipo youtube.com/watch?v=ID: usa o site + o ID do vídeo
        site = urlparse(url).netloc.removeprefix("www.").split(".")[0]
        video_id = re.search(r"v=([\w-]+)", url)
        nome = f"{site}_{video_id.group(1)}" if video_id else site

    # Deixa só caracteres seguros para nome de pasta no Windows
    nome = re.sub(r"[^\w\-]", "_", nome.lower()) or "live"
    return url, nome


def espaco_livre_gb(pasta: Path) -> float:
    """Quantos GB livres existem no disco onde a pasta está."""
    return shutil.disk_usage(pasta).free / 1024**3


def verificar_disco(pasta: Path) -> bool:
    """Avisa se o disco estiver acabando. Devolve False se estiver crítico."""
    livre = espaco_livre_gb(pasta)
    if livre < config.ESPACO_CRITICO_GB:
        log(f"ERRO: só {livre:.1f} GB livres (crítico: {config.ESPACO_CRITICO_GB} GB). "
            "Parando a gravação para não lotar o disco.")
        return False
    if livre < config.ESPACO_MINIMO_GB:
        log(f"AVISO: pouco espaço em disco: {livre:.1f} GB livres "
            f"(mínimo recomendado: {config.ESPACO_MINIMO_GB} GB).")
    return True


def proximo_numero_bloco(pasta: Path) -> int:
    """Número do próximo bloco, para continuar a numeração depois de reconectar."""
    numeros = [int(a.stem.split("_")[1]) for a in pasta.glob("bloco_*.ts")]
    return max(numeros) + 1 if numeros else 0


# ---------------------------------------------------------------- sessão

class Sessao:
    """
    Guarda as informações de uma gravação em sessao.json.

    Cada vez que o programa (re)conecta, nasce um "trecho" novo. Para cada
    trecho guardamos a hora real em que chegou o primeiro byte de vídeo e o
    primeiro bloco dele. Com isso + o CSV do ffmpeg (início/fim de cada bloco
    em segundos), dá para saber a hora real de qualquer ponto do vídeo.
    """

    def __init__(self, url: str, nome: str):
        self.inicio = agora()
        self.pasta = config.PASTA_BLOCOS / f"{nome}_{self.inicio:%Y-%m-%d_%H-%M}"
        self.pasta.mkdir(parents=True, exist_ok=True)
        self.trava = threading.Lock()  # duas threads podem escrever ao mesmo tempo
        self.dados = {
            "canal": nome,
            "url": url,
            "qualidade": config.QUALIDADE,
            "duracao_bloco": config.DURACAO_BLOCO,
            "trechos": [],
            "anuncios": [],
        }
        self.salvar()

    def salvar(self) -> None:
        with self.trava:
            texto = json.dumps(self.dados, indent=2, ensure_ascii=False)
            (self.pasta / "sessao.json").write_text(texto, encoding="utf-8")

    def registrar_trecho(self, numero: int, inicio: datetime, primeiro_bloco: int) -> None:
        # O inicio.txt guarda o começo do primeiro trecho (o "zero" da live)
        if not self.dados["trechos"]:
            (self.pasta / "inicio.txt").write_text(inicio.isoformat(), encoding="utf-8")
        self.dados["trechos"].append({
            "numero": numero,
            "inicio": inicio.isoformat(timespec="milliseconds"),
            "primeiro_bloco": primeiro_bloco,
            "lista_blocos": f"trecho_{numero:02d}.csv",
        })
        self.salvar()

    def registrar_anuncio(self, evento: str, segundos: int | None = None) -> None:
        self.dados["anuncios"].append({
            "hora": agora().isoformat(timespec="milliseconds"),
            "evento": evento,
            "duracao_anunciada": segundos,
        })
        self.salvar()


# ---------------------------------------------------------------- trecho

class Trecho:
    """
    Uma conexão com a live: streamlink -> (Python) -> ffmpeg.

    O Python fica no meio do "cano" repassando o vídeo. Isso permite:
      - anotar a hora exata em que chegou o primeiro byte (alinhamento com o chat);
      - perceber se o vídeo parou de chegar (conexão travada) e reconectar.
    """

    def __init__(self, sessao: Sessao, url: str, numero: int):
        self.sessao = sessao
        self.url = url
        self.numero = numero
        self.primeiro_bloco = proximo_numero_bloco(sessao.pasta)

        self.streamlink: subprocess.Popen | None = None
        self.ffmpeg: subprocess.Popen | None = None
        self.recebeu_dados = False     # chegou pelo menos 1 byte de vídeo?
        self.offline = False           # streamlink disse que o canal está offline
        self.em_anuncio = False        # streamlink está pulando um anúncio agora
        self.ultimo_dado = time.monotonic()
        self.bytes_recebidos = 0

    # ---- início e fim

    def iniciar(self) -> None:
        self.streamlink = subprocess.Popen(
            [
                "streamlink", self.url, config.QUALIDADE,
                "--stdout",                 # manda o vídeo para a saída padrão (o "cano")
                "--loglevel", "info",       # mensagens de anúncio aparecem no nível info
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=GRUPO_SEPARADO,
        )
        # Duas threads: uma repassa o vídeo, outra lê as mensagens do streamlink
        threading.Thread(target=self._repassar_video, daemon=True).start()
        threading.Thread(target=self._ler_mensagens_streamlink, daemon=True).start()

    def _iniciar_ffmpeg(self) -> None:
        """Abre o ffmpeg só quando chega o primeiro byte (não cria arquivo vazio)."""
        pasta = self.sessao.pasta
        self.ffmpeg = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",             # só mostra mensagens se der erro
                "-f", "mpegts",                   # o streamlink entrega MPEG-TS
                "-i", "pipe:0",                   # lê o vídeo que o Python repassa
                "-c", "copy",                     # copia sem recodificar (leve pro notebook)
                "-f", "segment",                  # modo "dividir em blocos"
                "-segment_time", str(config.DURACAO_BLOCO),
                "-segment_start_number", str(self.primeiro_bloco),  # continua a numeração
                "-segment_list", str(pasta / f"trecho_{self.numero:02d}.csv"),
                "-segment_list_type", "csv",      # nome,início,fim de cada bloco fechado
                "-reset_timestamps", "1",         # cada bloco começa no tempo 0
                str(pasta / "bloco_%03d.ts"),
            ],
            stdin=subprocess.PIPE,
            creationflags=GRUPO_SEPARADO,
        )

    def rodando(self) -> bool:
        return self.streamlink is not None and self.streamlink.poll() is None

    def segundos_sem_dados(self) -> float:
        return time.monotonic() - self.ultimo_dado

    def parar(self) -> None:
        """Encerra os dois processos sem corromper o bloco atual."""
        if self.streamlink and self.streamlink.poll() is None:
            self.streamlink.terminate()
        if self.ffmpeg:
            try:
                self.ffmpeg.stdin.close()   # "fim do vídeo": o ffmpeg fecha o bloco e sai
            except OSError:
                pass
            try:
                self.ffmpeg.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.ffmpeg.kill()

    # ---- threads

    def _repassar_video(self) -> None:
        """Lê o vídeo do streamlink e escreve no ffmpeg, pedaço por pedaço."""
        while True:
            pedaco = self.streamlink.stdout.read1(TAMANHO_PEDACO)
            if not pedaco:
                break  # streamlink terminou
            self.ultimo_dado = time.monotonic()
            if not self.recebeu_dados:
                self.recebeu_dados = True
                inicio = agora()
                self._iniciar_ffmpeg()
                self.sessao.registrar_trecho(self.numero, inicio, self.primeiro_bloco)
                log(f"Vídeo chegando! Trecho {self.numero} começou às {inicio:%H:%M:%S}.")
            try:
                self.ffmpeg.stdin.write(pedaco)
                self.bytes_recebidos += len(pedaco)
            except OSError:
                log("ERRO: o ffmpeg parou de aceitar vídeo.")
                break

    def _ler_mensagens_streamlink(self) -> None:
        """Traduz as mensagens importantes do streamlink e anota os anúncios."""
        for linha_bytes in self.streamlink.stderr:
            linha = linha_bytes.decode("utf-8", errors="replace").strip()
            if not linha:
                continue

            if m := re.search(r"advertisement break of (\d+)", linha):
                segundos = int(m.group(1))
                log(f"Anúncio da Twitch detectado ({segundos} s). O streamlink vai pular.")
                self.sessao.registrar_anuncio("anuncio_detectado", segundos)
            elif "pre-roll" in linha.lower():
                log("Esperando o anúncio de entrada (pre-roll) terminar...")
            elif "Filtering out segments" in linha:
                self.em_anuncio = True
                log("Pausando a gravação durante o anúncio.")
                self.sessao.registrar_anuncio("pausa_inicio")
            elif "Resuming stream output" in linha:
                self.em_anuncio = False
                self.ultimo_dado = time.monotonic()
                log("Anúncio acabou, gravação retomada.")
                self.sessao.registrar_anuncio("pausa_fim")
            elif m := re.search(r"Opening stream: (\S+)", linha):
                log(f"Qualidade escolhida: {m.group(1)}")
            elif "No playable streams" in linha or "offline" in linha.lower():
                self.offline = True
            elif "error" in linha.lower() or "warning" in linha.lower():
                log(f"[streamlink] {linha}")


# ---------------------------------------------------------------- gravação

def gravar(entrada: str, parar: threading.Event | None = None,
           ao_criar_sessao=None) -> Path | None:
    """
    Grava a live até ela acabar (ou até Ctrl+C), reconectando quando precisar.
    Devolve a pasta da sessão, ou None se nada foi gravado.

    Usado pelo main.py (etapa 6), que roda a gravação numa thread:
      parar            -> evento que o main liga para encerrar a gravação
      ao_criar_sessao  -> função chamada com a Sessao assim que a pasta existe
                          (o main usa para iniciar o chat com o mesmo id)
    """
    parar = parar or threading.Event()
    url, nome = montar_url(entrada)
    config.PASTA_BLOCOS.mkdir(parents=True, exist_ok=True)
    if not verificar_disco(config.PASTA_BLOCOS):
        return None
    log(f"Espaço livre em disco: {espaco_livre_gb(config.PASTA_BLOCOS):.1f} GB")

    sessao = Sessao(url, nome)
    if ao_criar_sessao:
        ao_criar_sessao(sessao)
    log(f"Conectando em {url} ...")
    log(f"Pasta da gravação: {sessao.pasta}")
    log("Aperte Ctrl+C para parar.\n")

    numero_trecho = 0
    offline_desde: float | None = None   # quando o canal começou a ficar offline
    ultima_checagem_disco = time.monotonic()
    trecho: Trecho | None = None

    try:
        while True:
            numero_trecho += 1
            trecho = Trecho(sessao, url, numero_trecho)
            trecho.iniciar()

            # Fica vigiando enquanto o trecho grava
            while trecho.rodando():
                if parar.wait(1):          # espera 1 s; o main pediu para parar?
                    raise KeyboardInterrupt

                if time.monotonic() - ultima_checagem_disco > config.INTERVALO_CHECAGEM_DISCO:
                    ultima_checagem_disco = time.monotonic()
                    if not verificar_disco(sessao.pasta):
                        raise KeyboardInterrupt  # sai do mesmo jeito que o Ctrl+C

                # Vídeo parou de chegar sem ser anúncio: conexão travada
                if (trecho.recebeu_dados and not trecho.em_anuncio
                        and trecho.segundos_sem_dados() > config.TEMPO_MAXIMO_SEM_DADOS):
                    log(f"Nenhum vídeo há {config.TEMPO_MAXIMO_SEM_DADOS} s. "
                        "Conexão travada, reconectando...")
                    break

            trecho.parar()

            # Decide se reconecta ou se a live acabou
            if trecho.recebeu_dados:
                offline_desde = None
                gb = trecho.bytes_recebidos / 1024**3
                log(f"Trecho {trecho.numero} terminou ({gb:.2f} GB gravados). "
                    "A conexão caiu ou a live parou.")
            elif not sessao.dados["trechos"]:
                # Nunca chegou vídeo: o canal já estava offline (ou o link está errado)
                log("Não foi possível abrir a live. O canal está offline ou o link está errado.")
                break
            else:
                offline_desde = offline_desde or time.monotonic()
                esperando = time.monotonic() - offline_desde
                if esperando > config.TEMPO_ESPERA_FIM_LIVE:
                    log(f"Canal offline há {esperando:.0f} s. Considerando que a live acabou.")
                    break

            log(f"Tentando reconectar em {config.INTERVALO_RECONEXAO} s...")
            if parar.wait(config.INTERVALO_RECONEXAO):
                raise KeyboardInterrupt

    except KeyboardInterrupt:
        print()
        log("Parando a gravação...")
        if trecho:
            trecho.parar()

    # Resumo final
    blocos = sorted(sessao.pasta.glob("bloco_*.ts"))
    if not blocos:
        shutil.rmtree(sessao.pasta, ignore_errors=True)  # não deixa pasta vazia para trás
        log("Nada foi gravado.")
        return None

    total_gb = sum(b.stat().st_size for b in blocos) / 1024**3
    log(f"Pronto! {len(blocos)} bloco(s), {total_gb:.2f} GB, em {sessao.pasta}")
    log(f"Trechos (conexões): {len(sessao.dados['trechos'])} | "
        f"anúncios detectados: {sum(a['evento'] == 'anuncio_detectado' for a in sessao.dados['anuncios'])}")
    return sessao.pasta


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python gravador.py nomedocanal   ou   python gravador.py LINK_DA_LIVE")
        sys.exit(1)
    gravar(sys.argv[1])
