"""
telegram_bot.py - o bot do Telegram do vigia (só mensagens de texto).

Roda dentro do vigia.py, em três threads próprias, para nunca atrasar a
gravação, o chat ou os cortes:

    envio    manda as mensagens da fila, em ordem. Sem internet, elas esperam
             (a fila fica salva em dados/telegram_fila.json e sobrevive a um
             reinício do vigia).
    receber  espera comandos e cliques nos botões (getUpdates). Só responde ao
             TELEGRAM_CHAT_ID; qualquer outra pessoa é ignorada.
    cortes   procura cortes novos nos cortes.json e avisa quando a transcrição
             fica pronta; de hora em hora apaga os descartados antigos.

Usa a API do Telegram direto (urllib, da biblioteca padrão): nenhuma
dependência nova. O token nunca é escrito na tela nem no log.
"""

import html
import http.client
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

import config
import cortador
import estado_vigia
from cortador import TEXTO_AVALIACAO, formatar_duracao

log = logging.getLogger("vigia")              # escreve no mesmo dados/vigia.log

AJUDA = (
    "<b>Comandos do vigia</b>\n"
    "/status - o que está acontecendo agora\n"
    "/pausar - pausa o vigia (encerra a gravação atual de forma limpa)\n"
    "/retomar - volta a vigiar os canais\n"
    "/parar - encerra o vigia (pede confirmação)\n"
    "/cortes - cortes de hoje e suas avaliações\n"
    "/ajuda - esta lista\n\n"
    "Em cada corte: 👍 Bom marca como aprovado, 👎 Descartar move para descartados/ "
    f"(apagado depois de {config.DIAS_MANTER_DESCARTADOS} dias) e ↩️ Desfazer devolve."
)


class _ConexaoIPv4(http.client.HTTPSConnection):
    """
    Conexão HTTPS que usa só IPv4. Em algumas redes o IPv6 até tem endereço, mas
    não funciona: cada chamada ficava 21 s tentando o IPv6 antes de cair no IPv4.
    """

    def connect(self):
        try:
            ip = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        except OSError:
            return super().connect()              # sem IPv4: deixa o Python escolher
        conexao = socket.create_connection((ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(conexao, server_hostname=self.host)


class _AbridorIPv4(urllib.request.HTTPSHandler):
    def https_open(self, pedido):
        return self.do_open(_ConexaoIPv4, pedido, context=self._context)


ABRIDOR = urllib.request.build_opener(_AbridorIPv4())


class ErroRede(Exception):
    """Sem internet (ou o Telegram fora do ar): vale a pena tentar de novo."""


class ErroTelegram(Exception):
    """O Telegram respondeu com erro."""

    def __init__(self, mensagem: str, codigo: int, esperar: int = 0):
        super().__init__(mensagem)
        self.codigo = codigo
        self.esperar = esperar                  # 429: o Telegram pede para esperar N s

    @property
    def permanente(self) -> bool:
        """Erro na própria mensagem (texto inválido, etc.): tentar de novo não adianta."""
        return 400 <= self.codigo < 500 and self.codigo != 429


def ler_json(arquivo: Path, padrao):
    try:
        return json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def salvar_json(arquivo: Path, dados) -> None:
    temporario = arquivo.with_suffix(".tmp")
    temporario.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporario, arquivo)


def resumir(texto: str, limite: int) -> str:
    """Corta o texto no último espaço antes do limite."""
    if len(texto) <= limite:
        return texto
    return texto[:limite].rsplit(" ", 1)[0] + "…"


def canal_da_sessao(sessao: str) -> str:
    """'loud_coringa_2026-11-24_18-23' -> 'loud_coringa'."""
    return sessao.rsplit("_", 2)[0]


class BotTelegram:
    def __init__(self, estado: estado_vigia.EstadoVigia):
        self.estado = estado
        self.token = config.TELEGRAM_TOKEN
        self.chat_id = str(config.TELEGRAM_CHAT_ID)
        self.trava = threading.Lock()
        self.parar_evento = threading.Event()
        self.tem_mensagem = threading.Event()

        config.PASTA_DADOS.mkdir(exist_ok=True)
        self.fila: list[dict] = ler_json(config.ARQUIVO_FILA_TELEGRAM, [])
        # Cortes já avisados: id curto (vai no botão) -> pasta e arquivo do corte
        self.avisados = ler_json(config.ARQUIVO_CORTES_TELEGRAM, {"proximo_id": 1, "cortes": {}})
        self.conhecidos: set[str] = set()        # "pasta/arquivo" que já vimos
        self.vendo_desde: dict[str, float] = {}  # quando vimos um corte sem transcrição
        self.offset: int | None = None           # próximo update que queremos do Telegram
        self.threads = [
            threading.Thread(target=self._laco_envio, name="telegram-envio", daemon=True),
            threading.Thread(target=self._laco_receber, name="telegram-receber", daemon=True),
            threading.Thread(target=self._laco_cortes, name="telegram-cortes", daemon=True),
        ]

    @staticmethod
    def configurado() -> bool:
        return bool(config.TELEGRAM_ATIVO and config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID)

    def iniciar(self) -> None:
        for thread in self.threads:
            thread.start()
        log.info("Bot do Telegram ligado.")

    def parar(self, espera: float = 15) -> None:
        """Tenta mandar o que está na fila (até `espera` s); o resto fica salvo para depois."""
        self.parar_evento.set()
        self.tem_mensagem.set()
        self.threads[0].join(timeout=espera)
        with self.trava:
            if self.fila:
                log.info(f"Telegram: {len(self.fila)} mensagem(ns) ficaram na fila para o próximo início.")

    # ---------------------------------------------------------------- API do Telegram

    def _chamar(self, metodo: str, espera: int = 20, **parametros) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{metodo}"
        pedido = urllib.request.Request(url, data=json.dumps(parametros).encode("utf-8"),
                                        headers={"Content-Type": "application/json"})
        try:
            with ABRIDOR.open(pedido, timeout=espera) as resposta:
                dados = json.load(resposta)
        except urllib.error.HTTPError as e:
            try:
                corpo = json.load(e)
            except ValueError:
                corpo = {}
            esperar = (corpo.get("parameters") or {}).get("retry_after", 0)
            raise ErroTelegram(self._sem_token(corpo.get("description", str(e))), e.code, esperar) from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise ErroRede(self._sem_token(e)) from None
        return dados["result"]

    def _sem_token(self, texto) -> str:
        """Garante que o token nunca vá parar no log."""
        return str(texto).replace(self.token, "***") if self.token else str(texto)

    # ---------------------------------------------------------------- envio (fila)

    def enviar(self, texto: str, botoes: list | None = None, corte_id: str | None = None) -> None:
        """Põe a mensagem na fila e volta na hora: quem chama nunca espera a internet."""
        with self.trava:
            self.fila.append({"texto": texto, "botoes": botoes, "corte_id": corte_id})
            salvar_json(config.ARQUIVO_FILA_TELEGRAM, self.fila)
        self.tem_mensagem.set()

    def _laco_envio(self) -> None:
        espera = 5
        sem_internet_avisado = False
        while True:
            with self.trava:
                item = self.fila[0] if self.fila else None
            if item is None:
                if self.parar_evento.is_set():
                    return
                self.tem_mensagem.wait(5)
                self.tem_mensagem.clear()
                continue
            try:
                parametros = {"chat_id": self.chat_id, "text": item["texto"], "parse_mode": "HTML"}
                if item["botoes"]:
                    parametros["reply_markup"] = {"inline_keyboard": item["botoes"]}
                enviada = self._chamar("sendMessage", **parametros)
            except ErroRede as e:
                if not sem_internet_avisado:
                    log.warning(f"Telegram sem conexão ({e}). As mensagens esperam na fila.")
                    sem_internet_avisado = True
                if self.parar_evento.is_set():
                    return                          # sem internet no fim: a fila fica salva
                self.parar_evento.wait(espera)
                espera = min(espera * 2, 60)
                continue
            except ErroTelegram as e:
                if e.esperar:
                    time.sleep(e.esperar)
                    continue
                if not e.permanente:
                    self.parar_evento.wait(espera)
                    continue
                log.error(f"Telegram recusou uma mensagem ({e}). Ela foi descartada.")
                enviada = None
            if sem_internet_avisado:
                log.info("Telegram voltou a conectar.")
            espera, sem_internet_avisado = 5, False
            if enviada and item["corte_id"]:
                with self.trava:
                    self.avisados["cortes"][item["corte_id"]]["mensagem"] = enviada["message_id"]
                    salvar_json(config.ARQUIVO_CORTES_TELEGRAM, self.avisados)
            with self.trava:
                self.fila.pop(0)
                salvar_json(config.ARQUIVO_FILA_TELEGRAM, self.fila)

    # ---------------------------------------------------------------- receber

    def _laco_receber(self) -> None:
        espera = 5
        # Ignora o que chegou enquanto o vigia estava desligado: um /parar antigo
        # não pode encerrar o vigia que acabou de ligar
        while self.offset is None and not self.parar_evento.is_set():
            try:
                ultimos = self._chamar("getUpdates", offset=-1, timeout=0)
                self.offset = ultimos[-1]["update_id"] + 1 if ultimos else 0
            except (ErroRede, ErroTelegram):
                self.parar_evento.wait(espera)
        while not self.parar_evento.is_set():
            try:
                atualizacoes = self._chamar("getUpdates", espera=40, offset=self.offset, timeout=30,
                                            allowed_updates=["message", "callback_query"])
                espera = 5
            except (ErroRede, ErroTelegram):
                self.parar_evento.wait(espera)
                espera = min(espera * 2, 60)
                continue
            for atualizacao in atualizacoes:
                self.offset = atualizacao["update_id"] + 1
                try:
                    self._tratar(atualizacao)
                except Exception:
                    log.exception("Erro tratando uma mensagem do Telegram")

    def _tratar(self, atualizacao: dict) -> None:
        if "message" in atualizacao:
            mensagem = atualizacao["message"]
            if str(mensagem["chat"]["id"]) != self.chat_id:
                log.warning(f"Telegram: mensagem ignorada de outro chat ({mensagem['chat']['id']}).")
                return
            self._comando((mensagem.get("text") or "").strip())
        elif "callback_query" in atualizacao:
            clique = atualizacao["callback_query"]
            if str(clique["from"]["id"]) != self.chat_id:
                log.warning(f"Telegram: clique ignorado de outra pessoa ({clique['from']['id']}).")
                self._responder_clique(clique, "Sem permissão.")
                return
            self._clique(clique)

    def _responder(self, texto: str, botoes: list | None = None) -> dict:
        """Resposta direta a um comando (estamos online: acabamos de receber)."""
        parametros = {"chat_id": self.chat_id, "text": texto, "parse_mode": "HTML"}
        if botoes:
            parametros["reply_markup"] = {"inline_keyboard": botoes}
        return self._chamar("sendMessage", **parametros)

    def _responder_clique(self, clique: dict, texto: str = "", alerta: bool = False) -> None:
        try:
            self._chamar("answerCallbackQuery", callback_query_id=clique["id"], text=texto, show_alert=alerta)
        except (ErroRede, ErroTelegram):
            pass

    def _editar(self, clique: dict, texto: str, botoes: list | None) -> None:
        self._chamar("editMessageText", chat_id=self.chat_id, message_id=clique["message"]["message_id"],
                     text=texto, parse_mode="HTML", reply_markup={"inline_keyboard": botoes or []})

    # ---------------------------------------------------------------- comandos

    def _comando(self, texto: str) -> None:
        comando = texto.split()[0].split("@")[0].lower() if texto.startswith("/") else ""
        modo = self.estado.modo
        if comando in ("/start", "/ajuda", "/help"):
            self._responder(AJUDA)
        elif comando == "/status":
            self._responder(self.texto_status())
        elif comando == "/pausar":
            if modo == estado_vigia.PAUSADO:
                self._responder("O vigia já está pausado. Use /retomar para voltar.")
            else:
                self.estado.pedir("pausar")
                self._responder("⏸️ Pausando" + (" (encerrando a gravação de forma limpa)..."
                                                 if modo == estado_vigia.GRAVANDO else "..."))
        elif comando == "/retomar":
            if modo != estado_vigia.PAUSADO:
                self._responder("O vigia não está pausado.")
            else:
                self.estado.pedir("retomar")
                self._responder("▶️ Retomando...")
        elif comando == "/parar":
            self._responder("Encerrar o vigia? Ele termina os cortes e transcrições em andamento antes "
                            "de fechar.\n\nPara ligar de novo depois, use o Agendador de Tarefas.",
                            [[{"text": "Sim", "callback_data": "parar:sim"},
                              {"text": "Não", "callback_data": "parar:nao"}]])
        elif comando == "/cortes":
            self._responder(self.texto_cortes_de_hoje())
        else:
            self._responder("Não conheço esse comando. Veja a lista em /ajuda.")

    def texto_status(self) -> str:
        foto = self.estado.foto()
        emoji = {"gravando": "🔴", "pausado": "🟡"}.get(foto["modo"], "⚪")
        alvo = config.CATEGORIA_ALVO or "qualquer categoria"
        return (f"{emoji} <b>{html.escape(self.estado.descricao())}</b>\n\n"
                f"Canais: {html.escape(', '.join(config.CANAIS_VIGIADOS))}\n"
                f"Categoria: {html.escape(alvo)}")

    def texto_cortes_de_hoje(self) -> str:
        hoje = date.today().isoformat()
        linhas = []
        for arquivo in sorted(config.PASTA_CORTES.glob(f"*_{hoje}/cortes.json")):
            for c in ler_json(arquivo, []):
                hora = datetime.fromisoformat(c["horario_pico"])
                linhas.append(f"{c['numero']:02d} · {html.escape(canal_da_sessao(c['sessao']))} · "
                              f"{hora:%Hh%M} · nota {c['nota']} · {TEXTO_AVALIACAO[c.get('avaliacao')]}")
        if not linhas:
            return "Nenhum corte hoje ainda."
        texto = f"<b>Cortes de hoje ({len(linhas)})</b>\n" + "\n".join(linhas)
        return resumir(texto, 4000)               # limite do Telegram: 4096 caracteres

    # ---------------------------------------------------------------- botões

    def _clique(self, clique: dict) -> None:
        acao, _, corte_id = (clique.get("data") or "").partition(":")
        if acao == "parar":
            if corte_id == "sim":
                self._editar(clique, "⏹️ Encerrando o vigia (terminando os cortes em andamento)...", None)
                self.estado.pedir("sair")
            else:
                self._editar(clique, "Ok, o vigia continua.", None)
            self._responder_clique(clique)
            return

        avaliacao = {"bom": "aprovado", "desc": "descartado", "desf": None}.get(acao, "?")
        info = self.avisados["cortes"].get(corte_id)
        if avaliacao == "?" or info is None:
            self._responder_clique(clique, "Botão desconhecido.", alerta=True)
            return
        pasta = config.PASTA_CORTES / info["pasta"]
        try:
            corte = cortador.avaliar_corte(pasta, info["arquivo"], avaliacao)
        except (FileNotFoundError, StopIteration) as e:
            self._responder_clique(clique, f"Não deu: {e or 'corte não encontrado'}.", alerta=True)
            return
        log.info(f"Telegram: {info['pasta']}/{info['arquivo']} -> {TEXTO_AVALIACAO[avaliacao]}")
        self._editar(clique, self.texto_corte(pasta, corte), self.botoes_corte(corte, corte_id))
        self._responder_clique(clique, {"aprovado": "Marcado como bom 👍",
                                        "descartado": "Movido para descartados/ 👎",
                                        None: "Devolvido para a pasta ↩️"}[avaliacao])

    @staticmethod
    def botoes_corte(corte: dict, corte_id: str) -> list:
        bom = {"text": "👍 Bom", "callback_data": f"bom:{corte_id}"}
        descartar = {"text": "👎 Descartar", "callback_data": f"desc:{corte_id}"}
        desfazer = {"text": "↩️ Desfazer", "callback_data": f"desf:{corte_id}"}
        return {"aprovado": [[descartar, desfazer]], "descartado": [[desfazer]]}.get(
            corte.get("avaliacao"), [[bom, descartar]])

    @staticmethod
    def texto_corte(pasta: Path, corte: dict) -> str:
        hora = datetime.fromisoformat(corte["horario_pico"])
        avaliacao = corte.get("avaliacao")
        caminho = pasta / ("descartados" if avaliacao == "descartado" else "") / corte["arquivo"]
        transcricao = corte.get("transcricao")
        transcricao = (f"“{html.escape(resumir(transcricao, config.TAMANHO_RESUMO_TRANSCRICAO))}”"
                       if transcricao else "<i>(sem transcrição)</i>")
        texto = (f"🎬 <b>Corte novo: {html.escape(canal_da_sessao(corte['sessao']))}</b>\n"
                 f"Nota <b>{corte['nota']}</b> · pico às {hora:%Hh%M} · {formatar_duracao(corte['duracao'])}\n\n"
                 f"{transcricao}\n\n"
                 f"📁 <code>{html.escape(str(caminho))}</code>")
        if avaliacao:
            texto += f"\n\nSua escolha: <b>{TEXTO_AVALIACAO[avaliacao]}</b>"
        return texto

    # ---------------------------------------------------------------- cortes novos

    def _laco_cortes(self) -> None:
        primeira_vez = True
        ultima_limpeza = 0.0
        while not self.parar_evento.is_set():
            try:
                self._procurar_cortes_novos(primeira_vez)
                primeira_vez = False
                if time.monotonic() - ultima_limpeza > 3600:
                    ultima_limpeza = time.monotonic()
                    apagados = cortador.limpar_descartados(config.DIAS_MANTER_DESCARTADOS)
                    if apagados:
                        log.info(f"Telegram: {apagados} corte(s) descartado(s) há mais de "
                                 f"{config.DIAS_MANTER_DESCARTADOS} dias foram apagados.")
            except Exception:
                log.exception("Erro procurando cortes novos para o Telegram")
            self.parar_evento.wait(15)

    def _procurar_cortes_novos(self, primeira_vez: bool) -> None:
        """
        Avisa cada corte novo uma vez, quando a transcrição fica pronta (ou depois
        de ESPERA_MAXIMA_TRANSCRICAO, sem ela). Na primeira passada só anota os
        cortes que já existiam, para não repetir avisos antigos ao ligar o vigia.
        """
        for arquivo in config.PASTA_CORTES.glob("*/cortes.json"):
            pasta = arquivo.parent
            for corte in ler_json(arquivo, []):
                chave = f"{pasta.name}/{corte['arquivo']}"
                if chave in self.conhecidos:
                    continue
                if primeira_vez:
                    self.conhecidos.add(chave)
                    continue
                visto = self.vendo_desde.setdefault(chave, time.monotonic())
                pronto = (corte.get("transcricao") or not config.TRANSCREVER
                          or time.monotonic() - visto > config.ESPERA_MAXIMA_TRANSCRICAO)
                if not pronto:
                    continue
                self.conhecidos.add(chave)
                self._avisar_corte(pasta, corte)

    def _avisar_corte(self, pasta: Path, corte: dict) -> None:
        with self.trava:
            corte_id = str(self.avisados["proximo_id"])
            self.avisados["proximo_id"] += 1
            self.avisados["cortes"][corte_id] = {"pasta": pasta.name, "arquivo": corte["arquivo"]}
            salvar_json(config.ARQUIVO_CORTES_TELEGRAM, self.avisados)
        self.enviar(self.texto_corte(pasta, corte), self.botoes_corte(corte, corte_id), corte_id)


# ---------------------------------------------------------------- configuração

def descobrir_chat_id() -> None:
    """
    python telegram_bot.py --descobrir-chat-id
    Lê quem mandou mensagem para o bot e grava o TELEGRAM_CHAT_ID no .env.
    (Rode com o vigia desligado: ele também lê as mensagens do bot.)
    """
    if not config.TELEGRAM_TOKEN:
        print("Falta o TELEGRAM_TOKEN no .env (veja o .env.exemplo).")
        return
    bot = BotTelegram(estado_vigia.EstadoVigia())
    try:
        nome = bot._chamar("getMe")["username"]
        atualizacoes = bot._chamar("getUpdates", timeout=0)
    except (ErroRede, ErroTelegram) as e:
        print(f"Não consegui falar com o Telegram: {e}")
        return
    particulares = {str(a["message"]["chat"]["id"]): a["message"]["chat"].get("first_name", "")
                    for a in atualizacoes if a.get("message", {}).get("chat", {}).get("type") == "private"}
    if len(particulares) != 1:
        print(f"Encontrei {len(particulares)} conversa(s) particular(es) com @{nome}. Mande uma mensagem "
              "para o bot (só você) e rode de novo.")
        return
    chat_id, nome_pessoa = particulares.popitem()
    linhas = config.ARQUIVO_ENV.read_text(encoding="utf-8-sig").splitlines()
    if not any(l.split("=", 1)[0].strip() == "TELEGRAM_CHAT_ID" for l in linhas):
        linhas.append("TELEGRAM_CHAT_ID=")
    linhas = [f"TELEGRAM_CHAT_ID={chat_id}" if l.split("=", 1)[0].strip() == "TELEGRAM_CHAT_ID" else l
              for l in linhas]
    config.ARQUIVO_ENV.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"Pronto: TELEGRAM_CHAT_ID de {nome_pessoa} gravado no .env.")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "--descobrir-chat-id" in sys.argv:
        descobrir_chat_id()
    else:
        print("Uso: python telegram_bot.py --descobrir-chat-id   (o bot em si roda dentro do vigia.py)")
