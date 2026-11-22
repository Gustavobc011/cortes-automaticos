"""
transcritor.py - Etapa 5 do projeto de cortes automáticos.

Transcreve só os cortes salvos (nunca a live inteira) com o faster-whisper e
salva um .srt ao lado de cada corte, além de colocar o texto no lista.md.

Roda com prioridade "abaixo do normal" do Windows e usando só metade dos
núcleos, para não travar o CapCut ou outros programas abertos.

Uso:
    python transcritor.py                               pasta de cortes mais recente
    python transcritor.py cortes/cellbit_2026-11-20     uma pasta específica
    python transcritor.py --todos                       transcreve de novo os já transcritos

O main.py (etapa 6) roda este arquivo como um processo separado.
"""

import argparse
import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

import config
from cortador import carregar_registro, gerar_lista_md, salvar_registro


# ---------------------------------------------------------------- prioridade

def baixar_prioridade() -> None:
    """Deixa este processo com prioridade "abaixo do normal": o Windows dá
    preferência aos programas que você está usando."""
    processo = psutil.Process()
    if sys.platform == "win32":
        processo.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    else:
        processo.nice(10)


# ---------------------------------------------------------------- áudio e texto

def extrair_audio(video: Path, destino: Path) -> None:
    """Tira o áudio do corte em mono, 16 kHz: o formato que o Whisper usa."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(video),
         "-vn",                # sem vídeo
         "-ac", "1",           # mono
         "-ar", "16000",       # 16 kHz
         str(destino)],
        check=True,
    )


def tempo_srt(segundos: float) -> str:
    """12.5 -> '00:00:12,500' (formato de tempo das legendas .srt)."""
    milissegundos = round(segundos * 1000)
    horas, resto = divmod(milissegundos, 3_600_000)
    minutos, resto = divmod(resto, 60_000)
    segundos, milissegundos = divmod(resto, 1000)
    return f"{horas:02d}:{minutos:02d}:{segundos:02d},{milissegundos:03d}"


def gerar_srt(trechos: list) -> str:
    """Cada trecho falado vira uma legenda numerada com início --> fim."""
    blocos = []
    for n, t in enumerate(trechos, start=1):
        blocos.append(f"{n}\n{tempo_srt(t.start)} --> {tempo_srt(t.end)}\n{t.text.strip()}\n")
    return "\n".join(blocos)


# ---------------------------------------------------------------- transcrição

def carregar_modelo():
    """Carrega o modelo uma vez só (é a parte mais demorada)."""
    from faster_whisper import WhisperModel   # importa aqui: só quem transcreve precisa dele
    return WhisperModel(
        config.MODELO_WHISPER,
        device="cpu",
        compute_type="int8",                   # números menores: mais rápido no processador
        cpu_threads=config.THREADS_TRANSCRICAO,
    )


def transcrever_corte(modelo, video: Path) -> str:
    """Transcreve um corte, salva o .srt ao lado e devolve o texto corrido."""
    with tempfile.TemporaryDirectory() as temporaria:
        audio = Path(temporaria) / "audio.wav"
        extrair_audio(video, audio)
        trechos, _ = modelo.transcribe(
            str(audio),
            language=config.IDIOMA_WHISPER,
            vad_filter=True,        # pula os silêncios (mais rápido e evita texto inventado)
        )
        trechos = list(trechos)     # a transcrição só acontece de verdade aqui

    video.with_suffix(".srt").write_text(gerar_srt(trechos), encoding="utf-8")
    return " ".join(t.text.strip() for t in trechos) or "(sem fala)"


def transcrever_pasta(pasta_cortes: Path, refazer: bool = False) -> int:
    """Transcreve os cortes da pasta que ainda não têm texto. Devolve quantos fez."""
    pendentes = [c for c in carregar_registro(pasta_cortes)
                 if (refazer or not c["transcricao"]) and (pasta_cortes / c["arquivo"]).exists()]
    if not pendentes:
        print("Nenhum corte para transcrever.")
        return 0

    print(f"Carregando o modelo '{config.MODELO_WHISPER}' "
          f"({config.THREADS_TRANSCRICAO} núcleos, prioridade abaixo do normal)...")
    modelo = carregar_modelo()

    for corte in pendentes:
        inicio = time.monotonic()
        texto = transcrever_corte(modelo, pasta_cortes / corte["arquivo"])

        # Lê o registro de novo antes de salvar: o cortador pode ter adicionado
        # um corte novo enquanto este era transcrito. Só mudamos o texto deste.
        registro = carregar_registro(pasta_cortes)
        for c in registro:
            if c["arquivo"] == corte["arquivo"]:
                c["transcricao"] = texto
        salvar_registro(pasta_cortes, registro)
        gerar_lista_md(pasta_cortes, registro)

        segundos = time.monotonic() - inicio
        print(f"  {corte['arquivo']}: {segundos:.0f} s para {corte['duracao']:.0f} s de vídeo")
        print(f"    \"{texto[:100]}{'...' if len(texto) > 100 else ''}\"")
    return len(pendentes)


# ---------------------------------------------------------------- terminal

def pasta_mais_recente() -> Path:
    pastas = sorted(config.PASTA_CORTES.glob("*/cortes.json"), key=lambda a: a.stat().st_mtime)
    if not pastas:
        raise SystemExit("Nenhuma pasta de cortes encontrada em cortes/.")
    return pastas[-1].parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcreve os cortes salvos de uma live.")
    parser.add_argument("pasta", nargs="?", help="pasta em cortes/ (padrão: a mais recente)")
    parser.add_argument("--todos", action="store_true", help="transcreve de novo os já transcritos")
    args = parser.parse_args()

    if not config.TRANSCREVER:
        print("Transcrição desligada (TRANSCREVER = False no config.py).")
        return

    baixar_prioridade()
    pasta = Path(args.pasta) if args.pasta else pasta_mais_recente()
    if not (pasta / "cortes.json").exists():
        pasta = config.PASTA_CORTES / pasta.name
    print(f"Pasta: {pasta}")
    transcrever_pasta(pasta, refazer=args.todos)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
