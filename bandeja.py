"""
bandeja.py - o ícone do vigia na bandeja do Windows (perto do relógio).

O ícone roda numa thread própria e nunca mexe na gravação: ele só lê o
EstadoVigia e faz pedidos ("pausar", "retomar", "sair"). Quem cumpre é o vigia.

Cores: cinza = vigiando | vermelho = gravando | amarelo = pausado.
"""

import os
import subprocess
import threading

import pystray
from PIL import Image, ImageDraw

import config
from estado_vigia import ENCERRANDO, GRAVANDO, PAUSADO, EstadoVigia

CORES = {GRAVANDO: (220, 40, 40), PAUSADO: (240, 185, 0)}
CINZA = (130, 130, 130)
LIMITE_DICA = 127          # o Windows corta a dica do ícone em 128 caracteres


def desenhar_icone(cor: tuple[int, int, int]) -> Image.Image:
    """Uma bolinha colorida com borda branca (64x64; o Windows reduz sozinho)."""
    imagem = Image.new("RGBA", (64, 64), (0, 0, 0, 0))       # fundo transparente
    ImageDraw.Draw(imagem).ellipse((4, 4, 60, 60), fill=cor, outline=(255, 255, 255), width=5)
    return imagem


class Bandeja:
    def __init__(self, estado: EstadoVigia):
        self.estado = estado
        self.imagens = {modo: desenhar_icone(cor) for modo, cor in CORES.items()}
        self.imagem_cinza = desenhar_icone(CINZA)
        self.ultima_descricao = ""
        self.icone = pystray.Icon(
            "cortes_automaticos_vigia",
            icon=self.imagem_cinza,
            title="Vigia de cortes",
            menu=pystray.Menu(
                # Os textos são funções: o pystray pergunta de novo a cada update_menu()
                pystray.MenuItem(lambda item: self.estado.descricao(), None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(self._texto_pausar, self._pausar_ou_retomar,
                                 enabled=lambda item: self.estado.modo != ENCERRANDO),
                pystray.MenuItem("Abrir pasta de cortes", self._abrir_cortes),
                pystray.MenuItem("Abrir log", self._abrir_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Sair", self._sair,
                                 enabled=lambda item: self.estado.modo != ENCERRANDO),
            ),
        )
        self.thread = threading.Thread(target=self.icone.run, name="bandeja", daemon=True)
        estado.ao_mudar(self.atualizar)

    def iniciar(self) -> None:
        self.thread.start()

    def parar(self) -> None:
        """Tira o ícone da bandeja (chamado quando o vigia termina de vez)."""
        self.icone.stop()
        self.thread.join(timeout=5)

    def atualizar(self) -> None:
        """Chamado pelo vigia a cada mudança: troca a cor, a dica e o menu."""
        descricao = self.estado.descricao()
        if descricao == self.ultima_descricao:
            return                                    # nada mudou: não pisca o menu
        self.ultima_descricao = descricao
        self.icone.icon = self.imagens.get(self.estado.modo, self.imagem_cinza)
        self.icone.title = descricao[:LIMITE_DICA]
        self.icone.update_menu()

    # ---- ações do menu (rodam na thread do ícone: só pedem, não fazem)

    def _texto_pausar(self, item) -> str:
        return "Retomar vigia" if self.estado.modo == PAUSADO else "Pausar vigia"

    def _pausar_ou_retomar(self, icone, item) -> None:
        self.estado.pedir("retomar" if self.estado.modo == PAUSADO else "pausar")

    def _abrir_cortes(self, icone, item) -> None:
        config.PASTA_CORTES.mkdir(exist_ok=True)
        os.startfile(config.PASTA_CORTES)             # abre no Explorador de Arquivos

    def _abrir_log(self, icone, item) -> None:
        subprocess.Popen(["notepad.exe", str(config.ARQUIVO_LOG_VIGIA)])

    def _sair(self, icone, item) -> None:
        self.estado.pedir("sair")
