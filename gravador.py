"""
gravador.py - Etapa 1 do projeto de cortes automáticos.

Grava uma live em blocos de 20 minutos, sem esperar a live acabar.
Uso:
    python gravador.py nomedocanal                          (Twitch)
    python gravador.py https://www.youtube.com/@canal/live  (link completo)
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ---------------- CONFIGURAÇÕES ----------------
QUALIDADE = "1080p60,1080p,best"  # tenta 1080p60; se não tiver, 1080p; senão a melhor disponível
DURACAO_BLOCO = 1200              # tamanho de cada bloco em segundos (1200 = 20 min)
# ------------------------------------------------


def montar_url(entrada: str) -> tuple[str, str]:
    """Recebe o nome do canal ou um link e devolve (url, nome_para_pasta)."""
    if entrada.startswith("http"):
        url = entrada                                # já é um link completo (YouTube, Kick...)
        nome = url.rstrip("/").split("/")[-1]        # usa o final do link como nome da pasta
    else:
        nome = entrada.lower()                       # só o nome: assume que é Twitch
        url = f"https://twitch.tv/{nome}"
    return url, nome


def gravar(entrada: str) -> None:
    url, nome = montar_url(entrada)

    # Cria uma pasta só para essa live, ex: blocos/gaules_2026-11-19_14-30
    inicio = datetime.now()
    pasta = Path("blocos") / f"{nome}_{inicio:%Y-%m-%d_%H-%M}"
    pasta.mkdir(parents=True, exist_ok=True)

    print(f"[{inicio:%H:%M:%S}] Conectando em {url} ...")

    # 1) streamlink puxa a live e joga o vídeo na "saída" (stdout)
    streamlink = subprocess.Popen(
        ["streamlink", url, QUALIDADE, "-O"],
        stdout=subprocess.PIPE,
    )

    # 2) ffmpeg lê essa saída e vai salvando em blocos .ts
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg",
            "-loglevel", "error",          # só mostra mensagens se der erro
            "-i", "pipe:0",                # lê o vídeo que vem do streamlink
            "-c", "copy",                  # copia sem recodificar (leve pro notebook)
            "-f", "segment",               # modo "dividir em pedaços"
            "-segment_time", str(DURACAO_BLOCO),
            "-reset_timestamps", "1",      # cada bloco começa no tempo 0
            str(pasta / "bloco_%03d.ts"),  # bloco_000.ts, bloco_001.ts, ...
        ],
        stdin=streamlink.stdout,
    )
    streamlink.stdout.close()  # o ffmpeg agora é o único dono desse "cano"

    # Guarda a hora de início: depois vamos usar isso para alinhar com o chat
    (pasta / "inicio.txt").write_text(inicio.isoformat(), encoding="utf-8")

    print(f"Gravando em: {pasta}")
    print("Aperte Ctrl+C para parar.\n")

    try:
        ffmpeg.wait()  # fica esperando até a live acabar
        print("\nA live terminou (ou o canal está offline).")
    except KeyboardInterrupt:
        print("\nParando a gravação...")
        streamlink.terminate()
        ffmpeg.wait()

    blocos = sorted(pasta.glob("bloco_*.ts"))
    print(f"Pronto! {len(blocos)} bloco(s) salvo(s) em {pasta}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python gravador.py nomedocanal   ou   python gravador.py LINK_DA_LIVE")
        sys.exit(1)
    gravar(sys.argv[1])