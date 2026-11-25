"""
Testes da proteção dos blocos (blocos.py): o /blocos do Telegram nunca pode
apagar a live que está gravando nem uma live em processamento (cortes,
transcrições, extensões, correções).

    python -m unittest tests.test_blocos

Tudo roda numa pasta temporária: blocos/ e cortes/ de verdade não são tocados.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import blocos
import config


class TesteProtecaoBlocos(unittest.TestCase):
    def setUp(self):
        self.temporaria = tempfile.TemporaryDirectory()
        raiz = Path(self.temporaria.name)
        self.originais = config.PASTA_BLOCOS, config.PASTA_CORTES
        config.PASTA_BLOCOS = raiz / "blocos"
        config.PASTA_CORTES = raiz / "cortes"
        config.PASTA_BLOCOS.mkdir()
        config.PASTA_CORTES.mkdir()
        self.raiz = raiz

    def tearDown(self):
        config.PASTA_BLOCOS, config.PASTA_CORTES = self.originais
        self.temporaria.cleanup()

    def criar_live(self, canal: str, inicio: datetime, recente: bool = False) -> Path:
        """Uma pasta de sessão com 2 blocos falsos (antigos, a não ser que recente=True)."""
        pasta = config.PASTA_BLOCOS / f"{canal}_{inicio:%Y-%m-%d_%H-%M}"
        pasta.mkdir()
        for i in range(2):
            bloco = pasta / f"bloco_{i:03d}.ts"
            bloco.write_bytes(b"\0" * 1000)
            if not recente:
                antigo = time.time() - 3600
                os.utime(bloco, (antigo, antigo))
        (pasta / "sessao.json").write_text("{}", encoding="utf-8")
        return pasta

    def processo_falso(self, *argumentos: str) -> subprocess.Popen:
        """Um Python dormindo, com a linha de comando que quisermos (ex.: main.py canal)."""
        processo = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argumentos])
        self.addCleanup(processo.wait)
        self.addCleanup(processo.kill)
        time.sleep(0.5)                         # dá tempo de o processo aparecer na lista
        return processo

    # ---------------------------------------------------------------- pode apagar

    def test_live_parada_pode_ser_apagada(self):
        pasta = self.criar_live("fulano", datetime(2026, 11, 20, 10, 0))
        self.assertIsNone(blocos.info(pasta.name).protecao)
        self.assertEqual(blocos.apagar(pasta.name), 2000 + 2)   # 2 blocos + sessao.json
        self.assertFalse(pasta.exists())

    def test_apaga_so_a_pasta_daquela_live(self):
        alvo = self.criar_live("fulano", datetime(2026, 11, 20, 10, 0))
        outra = self.criar_live("ciclano", datetime(2026, 11, 21, 10, 0))
        corte = config.PASTA_CORTES / "fulano_2026-11-20" / "01_10h05_nota5.0.mp4"
        corte.parent.mkdir()
        corte.write_bytes(b"corte")
        blocos.apagar(alvo.name)
        self.assertFalse(alvo.exists())
        self.assertTrue((outra / "bloco_000.ts").exists())
        self.assertTrue(corte.exists())

    def test_nome_fora_de_blocos_e_recusado(self):
        for nome in ("..", "../cortes", "fulano_2026-11-20_10-00/../..", "C:\\Windows", "cortes"):
            with self.assertRaises(ValueError, msg=nome):
                blocos.apagar(nome)
        self.assertTrue(config.PASTA_CORTES.exists())

    # ---------------------------------------------------------------- protegidas

    def test_marca_de_processamento_protege(self):
        pasta = self.criar_live("fulano", datetime(2026, 11, 20, 10, 0))
        for motivo in ("corte em andamento", "transcrição em andamento", "extensão de corte em andamento",
                       "correção de sincronia em andamento"):
            with blocos.ocupada(pasta, motivo):
                live = blocos.info(pasta.name)
                self.assertEqual(live.protecao, motivo)
                with self.assertRaises(blocos.LiveProtegida):
                    blocos.apagar(pasta.name)
                self.assertTrue((pasta / "bloco_000.ts").exists())
        # Terminou o processamento: a marca some e aí pode apagar
        self.assertIsNone(blocos.info(pasta.name).protecao)

    def test_marca_de_outro_processo_vivo_protege(self):
        pasta = self.criar_live("fulano", datetime(2026, 11, 20, 10, 0))
        processo = self.processo_falso()
        import psutil
        (pasta / ".ocupado_teste").write_text(json.dumps({
            "pid": processo.pid, "criado": psutil.Process(processo.pid).create_time(),
            "motivo": "cortes e transcrições em andamento"}), encoding="utf-8")
        with self.assertRaises(blocos.LiveProtegida):
            blocos.apagar(pasta.name)
        # O processo morreu sem apagar a marca: ela deixa de valer sozinha
        processo.kill()
        processo.wait()
        self.assertIsNone(blocos.info(pasta.name).protecao)
        self.assertFalse((pasta / ".ocupado_teste").exists())

    def test_main_py_rodando_para_o_canal_protege(self):
        agora = datetime.now()
        gravando = self.criar_live("fulano", agora)
        antiga = self.criar_live("fulano", agora - timedelta(days=2))
        outro_canal = self.criar_live("ciclano", agora)
        self.processo_falso("main.py", "fulano", "--vigia")
        self.assertEqual(blocos.info(gravando.name).protecao, "gravação em andamento")
        with self.assertRaises(blocos.LiveProtegida):
            blocos.apagar(gravando.name)
        self.assertTrue(gravando.exists())
        # Uma live antiga do mesmo canal e a de outro canal continuam liberadas
        self.assertIsNone(blocos.info(antiga.name).protecao)
        self.assertIsNone(blocos.info(outro_canal.name).protecao)

    def test_bloco_crescendo_protege(self):
        pasta = self.criar_live("fulano", datetime(2026, 11, 20, 10, 0), recente=True)
        with self.assertRaises(blocos.LiveProtegida):
            blocos.apagar(pasta.name)
        self.assertTrue(pasta.exists())

    def test_marca_da_gravacao_do_main(self):
        """O main.py marca a pasta quando a sessão começa e só libera no fim de tudo."""
        import main
        pasta = self.criar_live("fulano", datetime(2026, 11, 20, 10, 0))
        orquestrador = main.Orquestrador("fulano", modo_vigia=True)
        orquestrador.marca = blocos.marcar_ocupada(pasta, "gravação em andamento")
        self.assertEqual(blocos.info(pasta.name).protecao, "gravação em andamento")
        blocos.liberar(orquestrador.marca)
        self.assertIsNone(blocos.info(pasta.name).protecao)

    def test_cortador_marca_a_live_durante_o_corte(self):
        """Cortes, extensões e correções passam por extrair_trechos: a live fica protegida."""
        import cortador
        pasta = self.criar_live("fulano", datetime(2026, 11, 20, 10, 0))
        visto = []
        original = cortador._extrair_trechos
        cortador._extrair_trechos = lambda *a: visto.append(blocos.info(pasta.name).protecao)
        try:
            cortador.extrair_trechos(pasta, [], self.raiz / "corte.mp4")
        finally:
            cortador._extrair_trechos = original
        self.assertEqual(visto, ["corte em andamento"])
        self.assertIsNone(blocos.info(pasta.name).protecao)

    def test_botao_sim_do_telegram_nao_apaga_live_protegida(self):
        import telegram_bot
        pasta = self.criar_live("fulano", datetime(2026, 11, 20, 10, 0))
        bot = telegram_bot.BotTelegram.__new__(telegram_bot.BotTelegram)   # sem conectar no Telegram
        editados, alertas = [], []
        bot._editar = lambda clique, texto, botoes: editados.append(texto)
        bot._responder_clique = lambda clique, texto="", alerta=False: alertas.append(texto)
        bot._responder = lambda *a, **k: self.fail("não devia pedir confirmação de live protegida")
        clique = {"id": "1", "message": {"message_id": 1, "text": ""}}
        with blocos.ocupada(pasta, "transcrição em andamento"):
            bot._clique_blocos(clique, "blap", pasta.name)     # nem chega a pedir confirmação
            bot._clique_blocos(clique, "blsim", pasta.name)    # "Sim" de uma confirmação antiga
        self.assertTrue((pasta / "bloco_000.ts").exists())
        self.assertTrue(all("Protegida" in a for a in alertas))
        # Liberada, o mesmo "Sim" apaga
        bot._clique_blocos(clique, "blsim", pasta.name)
        self.assertFalse(pasta.exists())
        self.assertIn("apagados", editados[-1])

    # ---------------------------------------------------------------- listagem

    def test_listar_conta_cortes_sem_avaliacao(self):
        pasta = self.criar_live("fulano", datetime(2026, 11, 20, 10, 0))
        registro = config.PASTA_CORTES / "fulano_2026-11-20" / "cortes.json"
        registro.parent.mkdir()
        registro.write_text(json.dumps([
            {"sessao": pasta.name, "avaliacao": None},
            {"sessao": pasta.name, "avaliacao": "aprovado"},
            {"sessao": pasta.name, "avaliacao": None},
        ]), encoding="utf-8")
        (live,) = blocos.listar()
        self.assertEqual((live.cortes, live.sem_avaliacao), (3, 2))

    def test_pasta_sem_blocos_nao_aparece(self):
        pasta = self.criar_live("fulano", datetime(2026, 11, 20, 10, 0))
        for bloco in pasta.glob("*.ts"):
            bloco.unlink()
        self.assertEqual(blocos.listar(), [])


if __name__ == "__main__":
    unittest.main()
