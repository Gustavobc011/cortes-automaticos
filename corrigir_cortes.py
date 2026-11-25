"""
corrigir_cortes.py - corrige a sincronia de cortes feitos com o cortador antigo (-c copy).

    python corrigir_cortes.py                     todas as pastas de cortes
    python corrigir_cortes.py cortes\\cellbit_2026-11-20 [outras pastas...]
    python corrigir_cortes.py --sem-transcrever   não refaz as legendas

Os cortes antigos tinham o vídeo começando até 2 s antes do áudio (o -c copy
só corta no keyframe), escondido por uma "lista de edição" do MP4 que muitos
programas ignoram: neles o áudio tocava atrasado. Para cada corte:

    1. o original (.mp4 e .srt) vai para cortes/backup_cortes_antigos/;
    2. se os blocos da gravação ainda existem, o corte é refeito a partir deles;
       se não, o próprio corte é recodificado a partir do ponto em que o vídeo
       começa (onde a lista de edição dizia), com áudio e vídeo juntos;
    3. a sincronia é medida de novo e o cortes.json/lista.md são atualizados;
    4. a transcrição é refeita, para o .srt bater com a fala do arquivo novo.

Não mexe nos blocos da gravação. Roda em prioridade baixa.
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import config
import cortador
import detector
import transcritor

PASTA_BACKUP = config.PASTA_CORTES / "backup_cortes_antigos"


def inicio_das_faixas(arquivo: Path) -> dict[str, float]:
    """Onde o áudio e o vídeo começam, respeitando a lista de edição do MP4."""
    saida = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time", "-of", "json", str(arquivo)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    return {s["codec_type"]: float(s["start_time"]) for s in json.loads(saida)["streams"]
            if s["codec_type"] in ("audio", "video")}


def realinhar_corte(origem: Path, destino: Path) -> float:
    """
    Para quando os blocos já foram apagados: recodifica o próprio corte a partir
    do ponto em que o vídeo começa. A busca na entrada (-ss antes do -i) respeita
    a lista de edição e corta as duas faixas no mesmo instante. Devolve quantos
    segundos saíram do começo (era áudio sem imagem).
    """
    inicios = inicio_das_faixas(origem)
    deslocamento = max(0.0, inicios["video"] - inicios["audio"])
    cortador.rodar_ffmpeg([
        "-ss", f"{deslocamento:.3f}", "-i", str(origem),
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", config.PRESET_CORTE, "-crf", str(config.QUALIDADE_CORTE),
        "-pix_fmt", "yuv420p", "-threads", str(config.THREADS_TRANSCRICAO),
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(destino),
    ], prioridade_baixa=True)
    return deslocamento


def refazer_dos_blocos(corte: dict, destino: Path) -> bool:
    """Refaz o corte com o cortador novo, se todos os blocos de origem ainda existem."""
    pasta_sessao = config.PASTA_BLOCOS / corte["sessao"]
    if not all((pasta_sessao / b).exists() for b in corte.get("blocos", [])) or not corte.get("blocos"):
        return False
    margem = config.MARGEM_KEYFRAME
    inicio = datetime.fromisoformat(corte["inicio"]).timestamp() - margem
    fim = datetime.fromisoformat(corte["fim"]).timestamp() + margem
    partes = detector.localizar_no_video(inicio, fim, detector.linha_do_tempo_video(pasta_sessao))
    if not partes:
        return False
    cortador.extrair_trechos(pasta_sessao, partes, destino)
    return True


def corrigir_pasta(pasta: Path) -> list[dict]:
    """Corrige os cortes dessincronizados de uma pasta. Devolve um resumo de cada um."""
    resultados = []
    registro = cortador.carregar_registro(pasta)
    for corte in registro:
        if corte.get("descarte_apagado"):
            continue
        local = pasta / ("descartados" if corte.get("avaliacao") == "descartado" else "") / corte["arquivo"]
        if not local.exists():
            resultados.append({"arquivo": corte["arquivo"], "metodo": "arquivo não encontrado"})
            continue
        antes = round(cortador.medir_sincronia(local), 3)
        if antes < config.MAX_DIFERENCA_SINCRONIA:
            resultados.append({"arquivo": corte["arquivo"], "metodo": "já estava sincronizado", "antes": antes})
            continue

        # 1. Backup do original (o .mp4 e o .srt), na mesma estrutura de pastas
        backup = PASTA_BACKUP / pasta.relative_to(config.PASTA_CORTES)
        backup.mkdir(parents=True, exist_ok=True)
        srt = local.with_suffix(".srt")
        if not (backup / local.name).exists():
            shutil.copy2(local, backup / local.name)
            if srt.exists():
                shutil.copy2(srt, backup / srt.name)

        # 2. Corte novo num arquivo temporário; só troca o original se der certo
        novo = local.with_name(local.stem + ".novo.mp4")
        try:
            if refazer_dos_blocos(corte, novo):
                metodo, deslocamento = "refeito dos blocos", None
            else:
                deslocamento = realinhar_corte(backup / local.name, novo)
                metodo = "realinhado no próprio corte (blocos já apagados)"
            depois = round(cortador.medir_sincronia(novo), 3)
        except Exception as e:
            novo.unlink(missing_ok=True)
            resultados.append({"arquivo": corte["arquivo"], "metodo": f"FALHOU: {e}", "antes": antes})
            continue
        novo.replace(local)
        srt.unlink(missing_ok=True)                 # a legenda velha não bate mais: refeita no passo 4

        # 3. Registro atualizado (lista.md e cortes.json)
        corte.update({
            "duracao": round(cortador.duracao_video(local), 1),
            "sincronia": depois,
            "recodificado": True,
            "transcricao": None,
            "correcao_sincronia": {"metodo": metodo, "antes": antes, "depois": depois,
                                   "segundos_tirados_do_inicio": deslocamento,
                                   "em": datetime.now().isoformat(timespec="seconds")},
        })
        cortador.salvar_registro(pasta, registro)
        cortador.gerar_lista_md(pasta, registro)
        resultados.append({"arquivo": corte["arquivo"], "metodo": metodo, "antes": antes, "depois": depois})
        print(f"  {corte['arquivo']}: {antes:.3f} s -> {depois:.3f} s ({metodo})")
    return resultados


def pastas_de_cortes() -> list[Path]:
    return sorted(p.parent for p in config.PASTA_CORTES.rglob("cortes.json")
                  if PASTA_BACKUP not in p.parents)


def main() -> None:
    parser = argparse.ArgumentParser(description="Corrige a sincronia de cortes antigos.")
    parser.add_argument("pastas", nargs="*", help="pastas de cortes (padrão: todas)")
    parser.add_argument("--sem-transcrever", action="store_true", help="não refaz as legendas")
    args = parser.parse_args()
    transcritor.baixar_prioridade()               # este processo também fica em prioridade baixa
    pastas = [Path(p).resolve() for p in args.pastas] or pastas_de_cortes()

    total = {}
    for pasta in pastas:
        print(f"\n{pasta}")
        resultados = corrigir_pasta(pasta)
        for r in resultados:
            total[r["metodo"]] = total.get(r["metodo"], 0) + 1
        if not args.sem_transcrever and any(r.get("depois") is not None for r in resultados):
            transcritor.transcrever_pasta(pasta)    # só os que ficaram sem transcrição
    print("\nResumo:", json.dumps(total, ensure_ascii=False))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
