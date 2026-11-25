"""
Teste da sincronia entre áudio e vídeo nos cortes.

    python -m unittest discover tests

Cria um vídeo sintético com "verdade conhecida": um clarão branco na tela e um
bipe no áudio no MESMO instante, a cada 5 s, com keyframe a cada 2 s (como a
Twitch). Grava em blocos .ts como o gravador, corta com o cortador e confere:
    - o corte novo (recodificado) passa na checagem de sincronia;
    - em todos os momentos do corte o clarão e o bipe continuam juntos, até em
      programas que ignoram a "lista de edição" do MP4;
    - a checagem pega um corte feito do jeito antigo (-c copy).
Precisa do ffmpeg instalado. Leva uns 20 s.
"""

import contextlib
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config      # noqa: E402
import cortador    # noqa: E402

FFMPEG = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
# Corte que começa fora de um keyframe (7,3 s) e cruza a junção dos blocos 0 e 1
PARTES = [{"bloco": "bloco_000.ts", "de": 7.3, "ate": 20.0}, {"bloco": "bloco_001.ts", "de": 0.0, "ate": 18.0}]


def criar_blocos(pasta: Path) -> None:
    fonte = pasta / "fonte.ts"
    subprocess.run(FFMPEG + [
        "-f", "lavfi", "-i",
        "color=c=black:s=320x180:r=30:d=45,drawbox=c=white:t=fill:enable='lt(mod(t\\,5)\\,0.034)'",
        "-f", "lavfi", "-i", "aevalsrc='if(lt(mod(t\\,5)\\,0.05)\\,sin(2*PI*1000*t)\\,0)':s=48000:d=45",
        "-c:v", "libx264", "-g", "60", "-bf", "2", "-c:a", "aac", "-f", "mpegts", str(fonte)], check=True)
    # Os mesmos parâmetros do gravador: blocos de 20 s, -c copy, tempos zerados em cada bloco
    subprocess.run(FFMPEG + ["-i", str(fonte), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                             "-f", "segment", "-segment_time", "20", "-reset_timestamps", "1",
                             str(pasta / "bloco_%03d.ts")], check=True)


def desvios_bipe_clarao(arquivo: Path, ignorar_edicao: bool) -> list[float]:
    """Para cada clarão do vídeo, quanto o bipe mais próximo está atrasado (s)."""
    entrada = (["-ignore_editlist", "1"] if ignorar_edicao else []) + ["-i", str(arquivo)]
    saida = subprocess.run(["ffmpeg", "-hide_banner", *entrada, "-vf", "blackdetect=d=0.01:pix_th=0.5",
                            "-af", "silencedetect=n=-30dB:d=0.02", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
    claroes = [float(x) for x in re.findall(r"black_end:([\d.]+)", saida)]
    bipes = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", saida)]
    duracao = cortador.duracao_video(arquivo)
    # Ignora o último segundo: o fim do corte corta o silêncio no meio
    return [min(bipes, key=lambda b: abs(b - c)) - c for c in claroes if c < duracao - 1 and bipes]


class TesteSincronia(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporaria = tempfile.TemporaryDirectory()
        cls.pasta = Path(cls.temporaria.name)
        criar_blocos(cls.pasta)
        cls.recodificar_original = config.RECODIFICAR_CORTES

    @classmethod
    def tearDownClass(cls):
        config.RECODIFICAR_CORTES = cls.recodificar_original
        cls.temporaria.cleanup()

    def cortar(self, recodificar: bool) -> Path:
        config.RECODIFICAR_CORTES = recodificar
        destino = self.pasta / f"corte_{recodificar}.mp4"
        cortador.extrair_trechos(self.pasta, PARTES, destino)
        return destino

    def test_corte_novo_passa_na_checagem(self):
        corte = self.cortar(recodificar=True)
        self.assertLess(cortador.medir_sincronia(corte), config.MAX_DIFERENCA_SINCRONIA)

    def test_clarao_e_bipe_juntos_em_qualquer_programa(self):
        corte = self.cortar(recodificar=True)
        for ignorar in (False, True):
            desvios = desvios_bipe_clarao(corte, ignorar)
            self.assertGreaterEqual(len(desvios), 5, "o corte deveria ter vários clarões")
            for desvio in desvios:
                self.assertLess(abs(desvio), 0.05, f"bipe {desvio:+.3f} s fora do clarão "
                                                   f"({'ignorando' if ignorar else 'com'} lista de edição)")

    def test_checagem_pega_corte_do_jeito_antigo(self):
        corte = self.cortar(recodificar=False)
        self.assertGreater(cortador.medir_sincronia(corte), config.MAX_DIFERENCA_SINCRONIA)
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            cortador.conferir_sincronia(corte)
        self.assertIn("AVISO:", saida.getvalue())


if __name__ == "__main__":
    unittest.main()
