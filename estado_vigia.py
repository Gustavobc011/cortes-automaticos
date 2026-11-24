"""
estado_vigia.py - o estado do vigia num lugar só.

Quem mostra ou controla o vigia (o ícone da bandeja e, no futuro, o bot do
Telegram) faz duas coisas com este objeto:
    - LÊ o estado:      estado.modo, estado.descricao(), estado.foto()
    - FAZ PEDIDOS:      estado.pedir("pausar" | "retomar" | "sair")

Quem cumpre os pedidos é só o laço do vigia (vigia.py). Assim duas threads
nunca mexem na gravação ao mesmo tempo: o ícone só deixa um recado e acorda
o vigia, que faz a ação com calma, no ritmo dele.
"""

import threading
from datetime import datetime

VIGIANDO = "vigiando"      # ninguém da lista ao vivo (ou fora da categoria)
GRAVANDO = "gravando"
PAUSADO = "pausado"
ENCERRANDO = "encerrando"  # pediram para sair: terminando os cortes

PEDIDOS = ("pausar", "retomar", "sair")


def tempo_desde(inicio: datetime) -> str:
    """'15 min' ou '2h15'."""
    minutos = int((datetime.now() - inicio).total_seconds() // 60)
    return f"{minutos // 60}h{minutos % 60:02d}" if minutos >= 60 else f"{minutos} min"


class EstadoVigia:
    def __init__(self):
        self._trava = threading.Lock()
        self.modo = VIGIANDO
        self.canal: str | None = None              # canal gravando agora
        self.gravando_desde: datetime | None = None
        self.finalizando: list[str] = []           # canais terminando os cortes
        self._pedido: str | None = None
        self._ouvintes = []                        # funções chamadas quando algo muda
        self.acordar = threading.Event()           # tira o vigia da espera na hora

    # ---- leitura (qualquer thread)

    def descricao(self) -> str:
        """Uma linha para mostrar a uma pessoa. Ex.: 'Gravando cellbit há 2h15'."""
        with self._trava:
            if self.modo == GRAVANDO:
                texto = f"Gravando {self.canal} há {tempo_desde(self.gravando_desde)}"
            elif self.modo == PAUSADO:
                texto = "Pausado"
            elif self.modo == ENCERRANDO:
                texto = "Encerrando"
            else:
                texto = "Vigiando (ninguém da lista ao vivo)"
            if self.finalizando:
                texto += f" | terminando cortes de {', '.join(self.finalizando)}"
            return texto

    def foto(self) -> dict:
        """O estado inteiro como dicionário (para o /status do Telegram, por exemplo)."""
        with self._trava:
            return {
                "modo": self.modo,
                "canal": self.canal,
                "gravando_desde": self.gravando_desde.isoformat() if self.gravando_desde else None,
                "finalizando": list(self.finalizando),
            }

    # ---- pedidos (qualquer thread)

    def pedir(self, pedido: str) -> None:
        if pedido not in PEDIDOS:
            raise ValueError(f"pedido desconhecido: {pedido}")
        with self._trava:
            if self._pedido != "sair":             # "sair" nunca é trocado por outro pedido
                self._pedido = pedido
        self.acordar.set()

    # ---- usado só pelo vigia

    def pegar_pedido(self) -> str | None:
        with self._trava:
            pedido, self._pedido = self._pedido, None
            return pedido

    def atualizar(self, modo: str, canal: str | None = None,
                  gravando_desde: datetime | None = None, finalizando: list[str] | None = None) -> None:
        with self._trava:
            self.modo = modo
            self.canal = canal
            self.gravando_desde = gravando_desde
            self.finalizando = list(finalizando or [])
        for ouvinte in self._ouvintes:
            ouvinte()

    def ao_mudar(self, funcao) -> None:
        """Registra uma função para ser chamada a cada atualização (ex.: o ícone)."""
        self._ouvintes.append(funcao)
