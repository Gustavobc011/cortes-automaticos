"""
monitor_chat.py - Etapa 2 do projeto de cortes automáticos.

Conecta no chat da Twitch de forma anônima (só leitura, sem login) e salva
cada mensagem no SQLite (dados/chat.db). No terminal mostra só um resumo
por minuto, não cada mensagem.

Uso:
    python monitor_chat.py nomedocanal
    python monitor_chat.py nomedocanal --sessao gaules_2026-11-19_15-57

O "--sessao" liga as mensagens a uma gravação (o nome da pasta em blocos/).
Se não for informado, é criado um id no mesmo formato que o gravador usa.
"""

import argparse
import random
import socket
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import config
from gravador import agora, log, montar_url

TAMANHO_LEITURA = 4096
COMMIT_A_CADA = 2.0   # segundos entre gravações no banco (gravar em lote é bem mais rápido)


# ---------------------------------------------------------------- banco

def abrir_banco(caminho: Path = config.ARQUIVO_BANCO) -> sqlite3.Connection:
    """Abre (ou cria) o banco e a tabela de mensagens."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    banco = sqlite3.connect(caminho, check_same_thread=False)
    # WAL deixa o detector (etapa 3) ler o banco enquanto o monitor escreve
    banco.execute("PRAGMA journal_mode=WAL")
    banco.execute("""
        CREATE TABLE IF NOT EXISTS mensagens (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sessao      TEXT NOT NULL,  -- id da gravação (nome da pasta em blocos/)
            canal       TEXT NOT NULL,
            horario     TEXT NOT NULL,  -- hora local em ISO, legível
            horario_ts  REAL NOT NULL,  -- mesma hora em segundos (epoch), para contas rápidas
            enviado_ts  REAL,           -- hora em que o servidor da Twitch recebeu a mensagem
            usuario     TEXT NOT NULL,
            texto       TEXT NOT NULL
        )
    """)
    # Índice: o detector sempre busca "mensagens da sessão X entre os horários A e B"
    banco.execute("CREATE INDEX IF NOT EXISTS idx_sessao_horario "
                  "ON mensagens (sessao, horario_ts)")
    banco.commit()
    return banco


# ---------------------------------------------------------------- IRC

def interpretar_linha(linha: str) -> tuple[dict, str, str, list[str], str]:
    """
    Separa uma linha do IRC nas suas partes.

    Exemplo de linha (com tags):
      @display-name=Fulano;tmi-sent-ts=1700000000000 :fulano!fulano@fulano.tmi.twitch.tv PRIVMSG #gaules :KKKKK
    Devolve: (tags, prefixo, comando, parâmetros, texto)
    """
    tags = {}
    if linha.startswith("@"):
        bruto, linha = linha[1:].split(" ", 1)
        for par in bruto.split(";"):
            chave, _, valor = par.partition("=")
            tags[chave] = valor

    prefixo = ""
    if linha.startswith(":"):
        prefixo, linha = linha[1:].split(" ", 1)

    # O texto da mensagem vem depois de " :" (pode ter espaços)
    linha, _, texto = linha.partition(" :")
    partes = linha.split()
    comando, parametros = partes[0], partes[1:]
    return tags, prefixo, comando, parametros, texto


class MonitorChat:
    """
    Lê o chat de um canal e salva as mensagens no banco.

    Roda até alguém chamar parar() (ou Ctrl+C quando usado pelo terminal).
    Pensado para ser usado também pelo main.py (etapa 6), numa thread.
    """

    def __init__(self, canal: str, sessao: str):
        self.canal = canal.lower()
        self.sessao = sessao
        self.banco = abrir_banco()
        self.parar_evento = threading.Event()
        self.sock: socket.socket | None = None

        self.pendentes: list[tuple] = []        # mensagens esperando ir para o banco
        self.ultimo_commit = time.monotonic()
        self.total = 0
        self.no_minuto = 0
        self.reconexoes = 0

    # ---- conexão

    def conectar(self) -> None:
        """Conecta anonimamente: NICK justinfan<número> não precisa de senha."""
        nick = f"justinfan{random.randint(10000, 99999)}"
        self.sock = socket.create_connection((config.CHAT_SERVIDOR, config.CHAT_PORTA), timeout=15)
        # Espera no máximo 5 s por dados, assim o laço "acorda" para mostrar o resumo
        # e conferir se a conexão está viva mesmo quando o chat está parado
        self.sock.settimeout(5)
        # tags: traz o nome de exibição e a hora do servidor; commands: avisos como RECONNECT
        self.enviar("CAP REQ :twitch.tv/tags twitch.tv/commands")
        self.enviar(f"NICK {nick}")
        self.enviar(f"JOIN #{self.canal}")

    def enviar(self, comando: str) -> None:
        self.sock.sendall(f"{comando}\r\n".encode("utf-8"))

    def fechar_conexao(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # ---- laço principal

    def rodar(self) -> None:
        """Conecta e fica lendo. Se cair, espera um pouco e reconecta (2, 4, 8... até 60 s)."""
        espera = 2
        while not self.parar_evento.is_set():
            try:
                self.conectar()
                self._ler_ate_cair()
                espera = 2  # a conexão funcionou: a próxima espera volta a ser curta
            except OSError as erro:
                if not self.parar_evento.is_set():  # ao parar, fechar o socket gera erro: ignorar
                    log(f"[chat] Erro de conexão: {erro}")
            finally:
                self.fechar_conexao()
                self._salvar_pendentes()

            if self.parar_evento.is_set():
                break
            self.reconexoes += 1
            log(f"[chat] Conexão perdida. Reconectando em {espera} s...")
            self.parar_evento.wait(espera)
            espera = min(espera * 2, 60)

        self._salvar_pendentes()
        self.banco.close()

    def parar(self) -> None:
        self.parar_evento.set()
        self.fechar_conexao()  # destrava o recv() que estiver esperando

    def _ler_ate_cair(self) -> None:
        """Lê do socket e trata linha por linha. Sai quando a conexão cai."""
        sobra = ""                          # pedaço de linha incompleta da leitura anterior
        ultimo_dado = time.monotonic()      # quando o servidor mandou algo pela última vez
        mandou_ping = False
        proximo_resumo = time.monotonic() + config.INTERVALO_RESUMO_CHAT

        while not self.parar_evento.is_set():
            try:
                dados = self.sock.recv(TAMANHO_LEITURA)
                if not dados:
                    return  # o servidor fechou a conexão
                ultimo_dado = time.monotonic()
                mandou_ping = False
            except socket.timeout:
                dados = b""

            # Silêncio demais: mandamos um PING. Se nem assim vier resposta, a conexão morreu.
            silencio = time.monotonic() - ultimo_dado
            if silencio > 2 * config.CHAT_TEMPO_SEM_DADOS:
                log("[chat] O servidor não responde.")
                return
            if silencio > config.CHAT_TEMPO_SEM_DADOS and not mandou_ping:
                self.enviar("PING :tmi.twitch.tv")
                mandou_ping = True

            texto = sobra + dados.decode("utf-8", errors="replace")
            *linhas, sobra = texto.split("\r\n")
            for linha in linhas:
                if linha and not self._tratar_linha(linha):
                    return  # o servidor pediu para reconectar

            if time.monotonic() - self.ultimo_commit > COMMIT_A_CADA:
                self._salvar_pendentes()
            if time.monotonic() >= proximo_resumo:
                proximo_resumo += config.INTERVALO_RESUMO_CHAT
                self._mostrar_resumo()

    def _tratar_linha(self, linha: str) -> bool:
        """Trata uma linha do IRC. Devolve False se for preciso reconectar."""
        tags, prefixo, comando, parametros, texto = interpretar_linha(linha)

        if comando == "PING":
            self.enviar(f"PONG :{texto}")       # responde para não ser desconectado
        elif comando == "PRIVMSG":
            self._guardar_mensagem(tags, prefixo, texto)
        elif comando == "JOIN" and prefixo.startswith("justinfan"):
            log(f"[chat] Conectado ao chat de #{self.canal} (anônimo, só leitura).")
        elif comando == "RECONNECT":
            log("[chat] A Twitch pediu para reconectar (manutenção do servidor).")
            return False
        elif comando == "NOTICE":
            log(f"[chat] Aviso da Twitch: {texto}")
        return True

    def _guardar_mensagem(self, tags: dict, prefixo: str, texto: str) -> None:
        # Usamos a hora local de chegada para alinhar com o gravador, que também
        # usa o relógio do notebook. A hora do servidor fica guardada como referência.
        chegada = agora()
        enviado = tags.get("tmi-sent-ts")
        usuario = tags.get("display-name") or prefixo.split("!")[0]
        self.pendentes.append((
            self.sessao, self.canal,
            chegada.isoformat(timespec="milliseconds"), chegada.timestamp(),
            int(enviado) / 1000 if enviado and enviado.isdigit() else None,
            usuario, texto,
        ))
        self.total += 1
        self.no_minuto += 1

    def _salvar_pendentes(self) -> None:
        """Grava no banco, de uma vez, as mensagens acumuladas."""
        if self.pendentes:
            self.banco.executemany(
                "INSERT INTO mensagens (sessao, canal, horario, horario_ts, enviado_ts, usuario, texto) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                self.pendentes,
            )
            self.banco.commit()
            self.pendentes.clear()
        self.ultimo_commit = time.monotonic()

    def _mostrar_resumo(self) -> None:
        por_minuto = self.no_minuto * 60 / config.INTERVALO_RESUMO_CHAT
        log(f"[chat] {por_minuto:.0f} msg/min | total: {self.total} | reconexões: {self.reconexoes}")
        self.no_minuto = 0


# ---------------------------------------------------------------- terminal

def nome_do_canal(entrada: str) -> str:
    """Aceita o nome do canal ou um link da Twitch. O chat só funciona com Twitch."""
    url, nome = montar_url(entrada)
    if "twitch.tv" not in url:
        raise SystemExit("O monitor de chat só funciona com canais da Twitch.")
    return nome


def main() -> None:
    parser = argparse.ArgumentParser(description="Salva o chat de um canal da Twitch no SQLite.")
    parser.add_argument("canal", help="nome do canal ou link da Twitch")
    parser.add_argument("--sessao", help="id da gravação (nome da pasta em blocos/)")
    args = parser.parse_args()

    canal = nome_do_canal(args.canal)
    sessao = args.sessao or f"{canal}_{datetime.now():%Y-%m-%d_%H-%M}"

    monitor = MonitorChat(canal, sessao)
    log(f"[chat] Conectando ao chat de #{canal} | sessão: {sessao}")
    log(f"[chat] Salvando em {config.ARQUIVO_BANCO}. Aperte Ctrl+C para parar.")

    # O laço roda numa thread; a principal só espera o Ctrl+C
    thread = threading.Thread(target=monitor.rodar)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print()
        log("[chat] Parando...")
        monitor.parar()
        thread.join()
    log(f"[chat] Pronto! {monitor.total} mensagens salvas.")


if __name__ == "__main__":
    main()
