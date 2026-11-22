"""
main.py - Etapa 6 do projeto de cortes automáticos: roda tudo junto.

    python main.py nomedocanal

Durante a live:
    - grava em blocos (gravador.py) e salva o chat (monitor_chat.py) ao mesmo tempo;
    - roda o detector a cada 5 min e sempre que um bloco fecha (detector.py);
    - quando um bloco fecha, salva os cortes automáticos dele (cortador.py) e
      transcreve esses cortes num processo separado de prioridade baixa (transcritor.py);
    - avisa com uma notificação do Windows a cada corte novo;
    - impede o notebook de entrar em suspensão.

Ctrl+C ou o fim da live encerram tudo de forma limpa. No fim mostra um resumo
e, se APAGAR_BLOCOS_NO_FIM = True, pergunta se pode apagar a gravação.
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape

import config
import cortador
import detector
import gravador
from gravador import log
from monitor_chat import MonitorChat, nome_do_canal

PASTA_PROJETO = Path(__file__).resolve().parent

# Valores da API do Windows (SetThreadExecutionState)
ES_CONTINUOUS = 0x80000000        # o pedido vale até ser cancelado
ES_SYSTEM_REQUIRED = 0x00000001   # o sistema não pode entrar em suspensão (a tela pode apagar)

# O transcritor roda com prioridade baixa e num grupo próprio, para o Ctrl+C
# não interromper uma transcrição no meio (o main espera ela terminar).
FLAGS_TRANSCRITOR = (getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


# ---------------------------------------------------------------- Windows

def manter_acordado(ligar: bool) -> None:
    """Impede (ou volta a permitir) que o notebook entre em suspensão."""
    if sys.platform != "win32":
        return
    estado = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if ligar else ES_CONTINUOUS
    # A função devolve o estado anterior; 0 quer dizer que o Windows recusou
    if not ctypes.windll.kernel32.SetThreadExecutionState(estado):
        log("AVISO: não foi possível impedir a suspensão do notebook.")
    elif ligar:
        log("Suspensão do notebook bloqueada enquanto o programa roda.")


def notificar(titulo: str, texto: str) -> None:
    """
    Mostra uma notificação do Windows (aquele aviso no canto da tela).

    Usa o sistema de notificações do próprio Windows pelo PowerShell, sem
    instalar nada. Roda em segundo plano para não atrasar o programa.
    """
    if sys.platform != "win32" or not config.NOTIFICAR_CORTES:
        return
    xml = (f"<toast><visual><binding template='ToastGeneric'>"
           f"<text>{escape(titulo)}</text><text>{escape(texto)}</text>"
           f"</binding></visual></toast>")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:TOAST_XML);"
        # Usa o "remetente" do PowerShell, que todo Windows já tem registrado
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app)"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "TOAST_XML": xml},   # o texto vai por variável: sem problema com aspas
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# ---------------------------------------------------------------- orquestrador

class Orquestrador:
    """Junta gravador, chat, detector, cortador e transcritor numa live só."""

    def __init__(self, canal: str):
        self.canal = canal
        self.parar = threading.Event()        # liga quando Ctrl+C: avisa o gravador
        self.pasta: Path | None = None        # pasta da sessão em blocos/
        self.monitor: MonitorChat | None = None
        self.thread_chat: threading.Thread | None = None
        self.inicio = time.monotonic()

        self.blocos_fechados = 0
        self.ultimo_detector = 0.0
        self.candidatos: list[dict] = []
        self.transcritor: subprocess.Popen | None = None
        self.transcrever_de_novo = False      # chegou corte novo enquanto o transcritor rodava

    # ---- início

    def ao_criar_sessao(self, sessao: gravador.Sessao) -> None:
        """O gravador criou a pasta: o chat começa com o mesmo id de sessão."""
        self.pasta = sessao.pasta
        self.monitor = MonitorChat(self.canal, sessao.pasta.name)
        self.thread_chat = threading.Thread(target=self.monitor.rodar, daemon=True)
        self.thread_chat.start()

    def rodar(self) -> None:
        log(f"Cortes automáticos: #{self.canal} | nota mínima {config.NOTA_MINIMA_AUTOCORTE} "
            f"| transcrição {'ligada' if config.TRANSCREVER else 'desligada'}")
        manter_acordado(True)
        thread_gravador = threading.Thread(
            target=gravador.gravar, args=(self.canal, self.parar, self.ao_criar_sessao))
        thread_gravador.start()
        try:
            # A thread principal só vigia: o Ctrl+C sempre chega nela
            while thread_gravador.is_alive():
                thread_gravador.join(timeout=config.INTERVALO_VERIFICACAO)
                self.verificar()
        except KeyboardInterrupt:
            print()
            log("Ctrl+C: encerrando tudo...")
            self.parar.set()
            thread_gravador.join()
        finally:
            self.encerrar()
            manter_acordado(False)

    # ---- durante a live

    def verificar(self) -> None:
        """Roda o detector se um bloco fechou ou se passou o intervalo."""
        if not self.pasta or not (self.pasta / "sessao.json").exists():
            return
        fechados = sum(b.fechado for b in detector.linha_do_tempo_video(self.pasta))
        bloco_novo = fechados > self.blocos_fechados
        if bloco_novo:
            log(f"Bloco {fechados - 1:03d} fechado.")
        if bloco_novo or time.monotonic() - self.ultimo_detector > config.INTERVALO_DETECTOR:
            self.blocos_fechados = fechados
            self.rodar_detector()
        # Transcritor terminou e há corte esperando: roda de novo
        if self.transcrever_de_novo and self.transcritor.poll() is not None:
            self.transcrever_de_novo = False
            self.iniciar_transcricao()

    def rodar_detector(self) -> None:
        self.ultimo_detector = time.monotonic()
        self.candidatos = detector.detectar(self.pasta)
        detector.salvar_candidatos(self.pasta, self.candidatos)
        if self.candidatos:
            melhor = max(c["nota"] for c in self.candidatos)
            log(f"[detector] {len(self.candidatos)} candidato(s) até agora, maior nota {melhor}")

        novos = cortador.cortar_automaticos(self.pasta, self.candidatos)
        for corte in novos:
            notificar("Corte novo salvo", f"{corte['arquivo']} (nota {corte['nota']})")
        if novos:
            self.iniciar_transcricao()

    def iniciar_transcricao(self) -> None:
        """Roda o transcritor como outro processo (prioridade abaixo do normal)."""
        if not config.TRANSCREVER:
            return
        if self.transcritor and self.transcritor.poll() is None:
            self.transcrever_de_novo = True      # um de cada vez; depois pega os novos
            return
        pasta_cortes = cortador.pasta_dos_cortes(self.pasta)
        self.transcritor = subprocess.Popen(
            [sys.executable, str(PASTA_PROJETO / "transcritor.py"), str(pasta_cortes)],
            cwd=PASTA_PROJETO, creationflags=FLAGS_TRANSCRITOR,
        )

    # ---- fim

    def encerrar(self) -> None:
        if self.monitor:
            self.monitor.parar()
            self.thread_chat.join(timeout=10)
        if not self.pasta or not (self.pasta / "sessao.json").exists():
            return  # nada foi gravado (canal offline)

        # Último bloco acabou de fechar: última rodada de cortes
        log("Rodando o detector uma última vez...")
        self.rodar_detector()
        self.esperar_transcricao()
        self.resumo()
        if config.APAGAR_BLOCOS_NO_FIM:
            self.perguntar_apagar_blocos()

    def esperar_transcricao(self) -> None:
        while self.transcritor and self.transcritor.poll() is None:
            log("Esperando a transcrição terminar...")
            try:
                self.transcritor.wait()
            except KeyboardInterrupt:
                log("Transcrição interrompida (rode transcritor.py depois para terminar).")
                self.transcritor.kill()
                return
            if self.transcrever_de_novo:
                self.transcrever_de_novo = False
                self.iniciar_transcricao()

    def cortes_da_sessao(self) -> tuple[Path, list[dict]]:
        pasta_cortes = cortador.pasta_dos_cortes(self.pasta)
        cortes = [c for c in cortador.carregar_registro(pasta_cortes) if c["sessao"] == self.pasta.name]
        if not any(pasta_cortes.iterdir()):
            pasta_cortes.rmdir()                 # não deixa pasta vazia se não houve corte
        return pasta_cortes, cortes

    def resumo(self) -> None:
        pasta_cortes, cortes = self.cortes_da_sessao()
        minutos = (time.monotonic() - self.inicio) / 60
        print("\n" + "=" * 60)
        print(f" Resumo da live #{self.canal}")
        print(f"   Duração:         {minutos // 60:.0f} h {minutos % 60:02.0f} min")
        print(f"   Mensagens:       {self.monitor.total if self.monitor else 0}")
        print(f"   Picos (candidatos): {len(self.candidatos)}")
        print(f"   Cortes salvos:   {len(cortes)}")
        if cortes:
            print(f"   Onde:            {pasta_cortes}")
            print(f"   Resumo p/ Claude: {pasta_cortes / 'lista.md'}")
        print("=" * 60 + "\n")

    def perguntar_apagar_blocos(self) -> None:
        """Apaga só os vídeos .ts. Os arquivos pequenos (sessão, candidatos) ficam."""
        blocos = sorted(self.pasta.glob("bloco_*.ts"))
        if not blocos:
            return
        gb = sum(b.stat().st_size for b in blocos) / 1024**3
        try:
            resposta = input(f"Apagar os {len(blocos)} bloco(s) da gravação ({gb:.1f} GB)? "
                             "Os cortes ficam. [s/N] ")
        except (EOFError, KeyboardInterrupt):
            resposta = ""
        if resposta.strip().lower() in ("s", "sim"):
            apagar_blocos(blocos)
            log(f"{len(blocos)} bloco(s) apagado(s), {gb:.1f} GB liberados.")
        else:
            log(f"Blocos mantidos em {self.pasta}")


def apagar_blocos(blocos: list[Path]) -> None:
    for bloco in blocos:
        bloco.unlink()


def main() -> None:
    if len(sys.argv) != 2:
        print("Uso: python main.py nomedocanal   (ou o link da live na Twitch)")
        sys.exit(1)
    Orquestrador(nome_do_canal(sys.argv[1])).rodar()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
