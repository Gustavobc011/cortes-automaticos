"""
vigia.py - modo automático: vigia uma lista de canais e grava sozinho.

    pythonw vigia.py            roda sem janela (é assim que o Windows inicia ele)
    python vigia.py             roda com janela, mostrando o log na tela
    python vigia.py --checar    só mostra quem está ao vivo agora e sai

A cada INTERVALO_VIGIA o vigia pergunta ao streamlink quais canais de
CANAIS_VIGIADOS estão ao vivo na CATEGORIA_ALVO e grava o de maior prioridade,
rodando o main.py como outro processo. Um canal por vez.
"""

import ctypes
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import config
from main import notificar

PASTA_PROJETO = Path(__file__).resolve().parent
log = logging.getLogger("vigia")

# Sem isso, cada processo que o vigia abre (streamlink, main.py) abriria uma
# janela preta de terminal, porque o vigia roda sem janela (pythonw).
SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

REGEX_REPRISE = re.compile(r"\W*(RERUN|REPRISE)\b", re.IGNORECASE)


# ---------------------------------------------------------------- parte 1: quem está ao vivo

@dataclass
class Status:
    """O que o streamlink respondeu sobre um canal."""
    ao_vivo: bool
    categoria: str | None = None
    titulo: str | None = None
    erro: str | None = None          # a checagem falhou (internet, Twitch fora do ar...)

    def descricao(self) -> str:
        if self.erro:
            return f"erro na checagem ({self.erro})"
        if not self.ao_vivo:
            return "offline"
        return f"ao vivo em {self.categoria or 'categoria desconhecida'}"


def checar_canal(canal: str) -> Status:
    """
    Pergunta ao streamlink se o canal está ao vivo. Não precisa de conta nem de
    API da Twitch: o --json devolve a categoria e o título sem baixar o vídeo.
    """
    try:
        resultado = subprocess.run(
            ["streamlink", "--json", f"https://www.twitch.tv/{canal}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=SEM_JANELA,
        )
        dados = json.loads(resultado.stdout)
    except subprocess.TimeoutExpired:
        return Status(False, erro="o streamlink não respondeu em 60 s")
    except (OSError, ValueError) as e:
        return Status(False, erro=str(e)[:100])

    if "error" in dados:
        if "No playable streams" in dados["error"]:
            return Status(False)                      # offline: resposta normal
        return Status(False, erro=dados["error"][:100])
    meta = dados.get("metadata") or {}
    return Status(True, categoria=meta.get("category"), titulo=meta.get("title"))


def categoria_ok(canal: str, status: Status) -> bool:
    """A live está na categoria certa (ou o canal está liberado para qualquer uma)?"""
    if config.CATEGORIA_ALVO is None or canal in config.CANAIS_QUALQUER_CATEGORIA:
        return True
    return (status.categoria or "").casefold() == config.CATEGORIA_ALVO.casefold()


def disponivel(canal: str, status: Status) -> tuple[bool, str]:
    """Dá para gravar esse canal agora? Devolve (sim/não, motivo)."""
    if not status.ao_vivo:
        return False, status.descricao()
    if config.IGNORAR_REPRISES and REGEX_REPRISE.match(status.titulo or ""):
        return False, "ao vivo, mas é reprise (RERUN)"
    if not categoria_ok(canal, status):
        return False, f"ao vivo, mas em {status.categoria}"
    return True, f"ao vivo em {status.categoria}"


# ---------------------------------------------------------------- parte 2: uma gravação

def comando_gravacao(canal: str) -> list[str]:
    """
    O main.py roda com o python.exe do mesmo venv (não o pythonw), porque ele
    escreve no terminal; o SEM_JANELA esconde esse terminal.
    """
    python = Path(sys.executable).with_name("python.exe")
    return [str(python), str(PASTA_PROJETO / "main.py"), canal, "--vigia"]


def formatar_duracao(segundos: float) -> str:
    minutos = round(segundos / 60)
    return f"{minutos // 60} h {minutos % 60:02d} min"


class Gravacao:
    """Um main.py --vigia gravando um canal, rodando como outro processo."""

    def __init__(self, canal: str, prioridade: int):
        self.canal = canal
        self.prioridade = prioridade                 # 1 = a maior
        self.inicio = datetime.now()
        self.pasta: Path | None = None               # pasta da sessão em blocos/
        self.motivo_fim: str | None = None           # por que o vigia pediu para parar
        self.fora_da_categoria_desde: float | None = None

        config.PASTA_LOGS_GRAVACOES.mkdir(parents=True, exist_ok=True)
        self.arquivo_log = config.PASTA_LOGS_GRAVACOES / f"{canal}_{self.inicio:%Y-%m-%d_%H-%M-%S}.log"
        self.processo = subprocess.Popen(
            comando_gravacao(canal), cwd=PASTA_PROJETO,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            # Sem buffer e em UTF-8: cada linha do main chega na hora e com acento
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            creationflags=SEM_JANELA,
        )
        threading.Thread(target=self._guardar_saida, daemon=True).start()

    def _guardar_saida(self) -> None:
        """Copia tudo o que o main.py escreve para o log da gravação."""
        with open(self.arquivo_log, "a", encoding="utf-8") as arquivo:
            for linha in self.processo.stdout:
                arquivo.write(linha)
                arquivo.flush()
                if "Pasta da gravação:" in linha:
                    self.pasta = Path(linha.split("Pasta da gravação:", 1)[1].strip())

    def rodando(self) -> bool:
        return self.processo.poll() is None

    def pedir_para_parar(self, motivo: str) -> None:
        """Manda "parar" para o main.py: ele fecha o bloco, corta, transcreve e sai."""
        if self.motivo_fim:
            return
        self.motivo_fim = motivo
        try:
            self.processo.stdin.write("parar\n")
            self.processo.stdin.close()
        except OSError:
            pass                                      # o processo já tinha saído

    def resumo(self) -> dict | None:
        """O resumo.json que o main.py escreve no fim (None se nada foi gravado)."""
        if self.pasta and (self.pasta / "resumo.json").exists():
            return json.loads((self.pasta / "resumo.json").read_text(encoding="utf-8"))
        return None


# ---------------------------------------------------------------- parte 3: o vigia

class Vigia:
    """Decide quem gravar, quando trocar de canal e quando a live acabou."""

    def __init__(self):
        self.atual: Gravacao | None = None
        self.finalizando: list[Gravacao] = []         # receberam "parar", ainda cortando
        self.ultimo_estado: dict[str, str] = {}       # para só registrar o que mudou
        self.proxima_checagem = 0.0

    def rodar(self) -> None:
        # Um parar_vigia que sobrou (criado com o vigia desligado, por exemplo)
        # não pode fazer este vigia fechar logo depois de ligar
        if config.ARQUIVO_PARAR_VIGIA.exists():
            config.ARQUIVO_PARAR_VIGIA.unlink()
            log.info("Apaguei um arquivo parar_vigia antigo que tinha sobrado.")
        canais = ", ".join(config.CANAIS_VIGIADOS)
        alvo = config.CATEGORIA_ALVO or "qualquer categoria"
        log.info(f"Vigia iniciado. Canais (em ordem de prioridade): {canais} | {alvo}")
        notificar("Vigia iniciado", f"{canais} ({alvo})")
        try:
            while True:
                self.conferir_gravacoes()
                if config.ARQUIVO_PARAR_VIGIA.exists():
                    config.ARQUIVO_PARAR_VIGIA.unlink()
                    log.info("Arquivo parar_vigia encontrado: encerrando o vigia.")
                    break
                if time.monotonic() >= self.proxima_checagem:
                    self.proxima_checagem = time.monotonic() + config.INTERVALO_VIGIA
                    try:
                        self.checar()
                    except Exception:
                        log.exception("Erro na checagem (tento de novo na próxima)")
                time.sleep(5)
        except KeyboardInterrupt:
            log.info("Ctrl+C: encerrando o vigia.")
        finally:
            self.encerrar()

    # ---- checagens

    def checar_e_registrar(self, canal: str) -> tuple[Status, bool]:
        """Checa um canal e escreve no log só se algo mudou desde a última vez."""
        status = checar_canal(canal)
        ok, motivo = disponivel(canal, status)
        if self.ultimo_estado.get(canal) != motivo:
            self.ultimo_estado[canal] = motivo
            (log.warning if status.erro else log.info)(f"#{canal}: {motivo}")
        return status, ok

    def checar(self) -> None:
        if self.atual is None:
            # Ninguém gravando: o primeiro disponível da lista é o de maior prioridade
            for prioridade, canal in enumerate(config.CANAIS_VIGIADOS, start=1):
                status, ok = self.checar_e_registrar(canal)
                if ok:
                    self.iniciar(canal, prioridade, status)
                    return
            return

        # Gravando: só um canal de prioridade MAIOR (número menor) pode interromper
        for prioridade, canal in enumerate(config.CANAIS_VIGIADOS[:self.atual.prioridade - 1], start=1):
            status, ok = self.checar_e_registrar(canal)
            if ok:
                self.trocar(canal, prioridade, status)
                return
        self.conferir_categoria_atual()

    def conferir_categoria_atual(self) -> None:
        """
        O streamer saiu do jogo? Isso conta como fim da live, mas com a mesma
        tolerância do offline, para uma troca rápida de categoria não encerrar tudo.
        (Se o canal ficar offline, quem decide é o próprio gravador.)
        """
        atual = self.atual
        status, ok = self.checar_e_registrar(atual.canal)
        if ok:
            if atual.fora_da_categoria_desde:
                log.info(f"#{atual.canal} voltou para {status.categoria}: a gravação continua.")
            atual.fora_da_categoria_desde = None
            return
        if status.erro or not status.ao_vivo:
            return
        if atual.fora_da_categoria_desde is None:
            atual.fora_da_categoria_desde = time.monotonic()
            log.info(f"#{atual.canal} saiu da categoria (agora em {status.categoria}). "
                     f"Espero {config.OFFLINE_TOLERANCIA} s antes de encerrar.")
        elif time.monotonic() - atual.fora_da_categoria_desde >= config.OFFLINE_TOLERANCIA:
            self.encerrar_atual(f"saiu de {config.CATEGORIA_ALVO} (agora em {status.categoria})")

    # ---- ações

    def iniciar(self, canal: str, prioridade: int, status: Status) -> None:
        self.atual = Gravacao(canal, prioridade)
        log.info(f"Gravando #{canal} (prioridade {prioridade}). Saída em {self.atual.arquivo_log}")
        notificar(f"Live começou: #{canal}", f"{status.categoria}. Gravando.")

    def encerrar_atual(self, motivo: str) -> None:
        log.info(f"Encerrando #{self.atual.canal}: {motivo}")
        self.atual.pedir_para_parar(motivo)
        self.finalizando.append(self.atual)
        self.atual = None
        self.proxima_checagem = 0                     # vigia a lista inteira de novo já

    def trocar(self, canal: str, prioridade: int, status: Status) -> None:
        antigo = self.atual
        motivo = f"#{canal} (prioridade {prioridade}) entrou ao vivo em {status.categoria}"
        log.info(f"TROCA: #{antigo.canal} -> #{canal}. Motivo: {motivo}")
        notificar(f"Troca: #{antigo.canal} → #{canal}", motivo)
        # A gravação antiga termina os cortes em segundo plano; a nova já começa
        self.encerrar_atual(f"troca para #{canal}")
        self.iniciar(canal, prioridade, status)

    def conferir_gravacoes(self) -> None:
        """Alguma gravação terminou (a live acabou ou terminou de cortar)?"""
        for gravacao in [g for g in [self.atual, *self.finalizando] if g and not g.rodando()]:
            if gravacao is self.atual:
                self.atual = None
                self.proxima_checagem = 0
            else:
                self.finalizando.remove(gravacao)
            self.gravacao_terminou(gravacao)

    def gravacao_terminou(self, gravacao: Gravacao) -> None:
        motivo = gravacao.motivo_fim or f"a live acabou (offline por mais de {config.OFFLINE_TOLERANCIA} s)"
        codigo = gravacao.processo.returncode
        if codigo != 0:
            log.error(f"#{gravacao.canal}: o main.py saiu com erro (código {codigo}). "
                      f"Veja {gravacao.arquivo_log}")
        resumo = gravacao.resumo()
        if resumo is None:
            log.warning(f"#{gravacao.canal}: nada foi gravado ({motivo}).")
            return
        texto = (f"{formatar_duracao(resumo['duracao_segundos'])}, "
                 f"{resumo['cortes']} corte(s) salvo(s). Motivo: {motivo}")
        log.info(f"Live terminou: #{gravacao.canal}: {texto} | "
                 f"{resumo['mensagens']} mensagens, {resumo['candidatos']} candidatos")
        notificar(f"Live terminou: #{gravacao.canal}", texto)

    def encerrar(self) -> None:
        """Pede para a gravação atual parar e espera os cortes ficarem prontos."""
        if self.atual:
            self.encerrar_atual("o vigia foi encerrado")
        try:
            while self.finalizando:
                log.info(f"Esperando {len(self.finalizando)} gravação(ões) terminar(em) os cortes...")
                time.sleep(15)
                self.conferir_gravacoes()
        except KeyboardInterrupt:
            log.info("Saindo sem esperar (o main.py termina os cortes sozinho).")
        log.info("Vigia encerrado.")


# ---------------------------------------------------------------- início

def configurar_log() -> None:
    """Log em dados/vigia.log (até 4 arquivos de 1 MB) e na tela, se tiver tela."""
    config.PASTA_DADOS.mkdir(exist_ok=True)
    formato = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    arquivo = logging.handlers.RotatingFileHandler(
        config.ARQUIVO_LOG_VIGIA, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    arquivo.setFormatter(formato)
    log.addHandler(arquivo)
    if sys.stdout:                                    # no pythonw não existe tela
        tela = logging.StreamHandler(sys.stdout)
        tela.setFormatter(formato)
        log.addHandler(tela)
    log.setLevel(logging.INFO)


def ja_existe_outro_vigia() -> bool:
    """
    Um "mutex" com nome do Windows: só um processo consegue ser o dono. Evita dois
    vigias gravando a mesma live (por exemplo, o do login e um aberto à mão).
    """
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    global _mutex                                     # precisa viver até o programa fechar
    _mutex = kernel32.CreateMutexW(None, False, "Local\\cortes_automaticos_vigia")
    return ctypes.get_last_error() == 183             # ERROR_ALREADY_EXISTS


def main() -> None:
    configurar_log()
    if ja_existe_outro_vigia():
        log.info("Já existe um vigia rodando. Este vai fechar.")
        return
    try:
        Vigia().rodar()
    except Exception:
        log.exception("Erro inesperado: o vigia caiu")
        sys.exit(1)                                   # código de erro: o Agendador reinicia


def mostrar_quem_esta_ao_vivo(canais: list[str]) -> None:
    for prioridade, canal in enumerate(canais, start=1):
        status = checar_canal(canal)
        ok, motivo = disponivel(canal, status)
        print(f"  {prioridade}. {canal:15} {'GRAVARIA' if ok else '-':9} {motivo}")
        if status.titulo:
            print(f"     título: {status.titulo[:70]}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "--checar" in sys.argv:
        canais = [a for a in sys.argv[1:] if a != "--checar"] or config.CANAIS_VIGIADOS
        print(f"Categoria alvo: {config.CATEGORIA_ALVO}")
        mostrar_quem_esta_ao_vivo(canais)
    else:
        main()
