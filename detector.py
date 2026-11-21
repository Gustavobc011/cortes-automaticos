"""
detector.py - Etapa 3 do projeto de cortes automáticos.

Lê o chat salvo no SQLite, encontra os picos de reação e transforma cada pico
em um "candidato a corte": hora real, nota e em qual bloco de vídeo (e em que
segundo dele) o momento está. O resultado vai para candidatos.json, na pasta
da sessão.

Uso:
    python detector.py                               (sessão mais recente em blocos/)
    python detector.py cellbit_2026-11-19_23-43      (uma sessão específica)

Como funciona, em resumo:
    1. conta as mensagens em janelas de 10 s;
    2. o "normal" do chat é a média dos últimos 5 min antes de cada janela;
    3. pico = janela com volume >= 3x o normal (e um mínimo absoluto de mensagens);
    4. picos a menos de 60 s um do outro viram um único momento;
    5. o corte começa 45 s antes do pico (o chat reage com atraso);
    6. a hora real é convertida em "arquivo de bloco + segundo dentro do bloco".
"""

import argparse
import csv
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config

# Uma expressão regular que acha qualquer palavra de reação no começo de uma
# palavra da mensagem. Ex.: "KKKK" acha "kkkkkkk"; "Pog" acha "PogU".
# As maiores vêm primeiro para "PogChamp" ganhar de "Pog".
_palavras = sorted(config.PALAVRAS_REACAO, key=len, reverse=True)
REGEX_REACAO = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, _palavras)) + ")", re.IGNORECASE)


# ---------------------------------------------------------------- estruturas

@dataclass
class Bloco:
    """Um arquivo de vídeo e o intervalo de hora real (em segundos epoch) que ele cobre."""
    arquivo: str
    inicio_ts: float
    fim_ts: float
    fechado: bool   # False = o ffmpeg ainda está gravando nele


@dataclass
class Janela:
    """10 segundos de chat."""
    mensagens: int = 0
    reacoes: int = 0                  # mensagens com alguma palavra de reação
    textos: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- utilidades

def hora_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def normalizar_texto(texto: str) -> str:
    """Deixa mensagens parecidas iguais: 'kkkkkkk' e 'KKKK' viram 'KKK'."""
    texto = texto.strip().upper()
    return re.sub(r"(.)\1{3,}", r"\1\1\1", texto)  # 4+ letras repetidas viram 3


def achar_pasta_sessao(nome: str | None) -> Path:
    """Acha a pasta da sessão pelo nome, ou a mais recente se nenhum nome for dado."""
    if nome:
        pasta = Path(nome) if Path(nome).is_dir() else config.PASTA_BLOCOS / nome
        if not (pasta / "sessao.json").exists():
            raise SystemExit(f"Sessão não encontrada: {pasta}")
        return pasta
    sessoes = sorted(config.PASTA_BLOCOS.glob("*/sessao.json"), key=lambda a: a.stat().st_mtime)
    if not sessoes:
        raise SystemExit("Nenhuma sessão gravada em blocos/.")
    return sessoes[-1].parent


# ---------------------------------------------------------------- vídeo

def linha_do_tempo_video(pasta: Path) -> list[Bloco]:
    """
    Monta a lista de blocos com a hora real de início e fim de cada um.

    Para cada trecho (cada conexão do gravador) temos a hora real do primeiro
    byte, e o CSV do ffmpeg diz em que segundo do trecho cada bloco começa e
    termina. Hora real do bloco = início do trecho + segundos do CSV.
    """
    sessao = json.loads((pasta / "sessao.json").read_text(encoding="utf-8"))
    blocos: list[Bloco] = []

    for trecho in sessao["trechos"]:
        t0 = datetime.fromisoformat(trecho["inicio"]).timestamp() - config.AJUSTE_TEMPO_VIDEO
        linhas = []
        arquivo_csv = pasta / trecho["lista_blocos"]
        if arquivo_csv.exists():
            with open(arquivo_csv, newline="", encoding="utf-8") as f:
                linhas = [l for l in csv.reader(f) if l]

        for nome, inicio, fim in linhas:
            blocos.append(Bloco(nome, t0 + float(inicio), t0 + float(fim), fechado=True))

        # Bloco ainda aberto (sendo gravado agora): existe o arquivo, mas não está no CSV.
        # O fim estimado é a última vez que o arquivo foi modificado.
        numero_aberto = trecho["primeiro_bloco"] + len(linhas)
        aberto = pasta / f"bloco_{numero_aberto:03d}.ts"
        if aberto.exists():
            inicio = t0 + (float(linhas[-1][2]) if linhas else 0.0)
            blocos.append(Bloco(aberto.name, inicio, aberto.stat().st_mtime, fechado=False))

    return blocos


def localizar_no_video(inicio_ts: float, fim_ts: float, blocos: list[Bloco]) -> list[dict]:
    """
    Converte um intervalo de hora real em pedaços de blocos.
    Se o momento cruzar dois blocos, devolve dois pedaços.
    """
    partes = []
    for b in blocos:
        de, ate = max(inicio_ts, b.inicio_ts), min(fim_ts, b.fim_ts)
        if ate > de:
            partes.append({
                "bloco": b.arquivo,
                "de": round(de - b.inicio_ts, 2),     # segundo dentro do bloco
                "ate": round(ate - b.inicio_ts, 2),
                "fechado": b.fechado,
            })
    return partes


# ---------------------------------------------------------------- chat

def carregar_mensagens(sessao: str) -> list[tuple[float, str]]:
    """Todas as mensagens da sessão, em ordem: [(hora_ts, texto), ...]."""
    if not config.ARQUIVO_BANCO.exists():
        return []
    banco = sqlite3.connect(config.ARQUIVO_BANCO)
    try:
        return banco.execute(
            "SELECT horario_ts, texto FROM mensagens WHERE sessao = ? ORDER BY horario_ts",
            (sessao,),
        ).fetchall()
    finally:
        banco.close()


def contar_janelas(mensagens: list[tuple[float, str]]) -> tuple[float, list[Janela]]:
    """Agrupa as mensagens em janelas de 10 s. Devolve (hora da 1ª janela, janelas)."""
    tamanho = config.JANELA_SEGUNDOS
    t0 = math.floor(mensagens[0][0] / tamanho) * tamanho   # alinha em múltiplos de 10 s
    total = int((mensagens[-1][0] - t0) // tamanho) + 1
    janelas = [Janela() for _ in range(total)]

    for ts, texto in mensagens:
        j = janelas[int((ts - t0) // tamanho)]
        j.mensagens += 1
        j.textos.append(texto)
        if REGEX_REACAO.search(texto):
            j.reacoes += 1
    return t0, janelas


def encontrar_picos(janelas: list[Janela]) -> dict[int, float]:
    """
    Devolve {índice da janela: média normal naquele momento} das janelas em pico.
    A média é das janelas ANTERIORES (últimos 5 min), sem contar a própria janela.
    """
    tamanho = config.JANELA_SEGUNDOS
    n_media = config.JANELA_MEDIA_SEGUNDOS // tamanho
    n_minimo = config.HISTORICO_MINIMO_SEGUNDOS // tamanho
    picos = {}

    for i, janela in enumerate(janelas):
        if i < n_minimo:
            continue  # ainda não temos histórico para saber o que é "normal"
        anteriores = janelas[max(0, i - n_media):i]
        media = sum(j.mensagens for j in anteriores) / len(anteriores)
        if (janela.mensagens >= config.FATOR_PICO * max(media, 1.0)
                and janela.mensagens >= config.MINIMO_MENSAGENS_PICO):
            picos[i] = media
    return picos


def agrupar_picos(picos: dict[int, float]) -> list[list[int]]:
    """Junta picos que estão a menos de JUNTAR_PICOS_SEGUNDOS um do outro."""
    grupos: list[list[int]] = []
    distancia_max = config.JUNTAR_PICOS_SEGUNDOS / config.JANELA_SEGUNDOS
    for i in sorted(picos):
        # (i - último - 1) = quantas janelas "normais" existem entre os dois picos
        if grupos and (i - grupos[-1][-1] - 1) < distancia_max:
            grupos[-1].append(i)
        else:
            grupos.append([i])
    return grupos


def calcular_nota(razao: float, janelas_em_pico: int, pct_reacoes: float) -> dict:
    """
    Nota de 0 a 10, somando três partes:
      - volume (até 6): quantas vezes o chat ficou acima do normal. 3x = 3,2; 8x = 6.
      - duração (até 1,5): picos que se mantêm por várias janelas valem mais.
      - reações (até 2,5): % das mensagens com palavras de reação. 50% = 2,5.
    """
    volume = min(6.0, 2 * math.log2(max(razao, 1.0)))
    duracao = min(1.5, 0.5 * (janelas_em_pico - 1))
    reacoes = min(2.5, pct_reacoes / 100 * 5)
    return {
        "nota": round(volume + duracao + reacoes, 1),
        "nota_detalhe": {   # de onde veio cada ponto da nota
            "volume": round(volume, 2),
            "duracao_pico": round(duracao, 2),
            "reacoes": round(reacoes, 2),
        },
    }


# ---------------------------------------------------------------- detector

def detectar(pasta: Path) -> list[dict]:
    """Roda o detector numa sessão e devolve a lista de candidatos."""
    mensagens = carregar_mensagens(pasta.name)
    if not mensagens:
        return []

    t0, janelas = contar_janelas(mensagens)
    picos = encontrar_picos(janelas)
    blocos = linha_do_tempo_video(pasta)
    tamanho = config.JANELA_SEGUNDOS
    duracao_min = max(config.DURACAO_MINIMA_CORTE, config.DURACAO_ALVO_CORTE[0])
    duracao_max = config.DURACAO_ALVO_CORTE[1]
    candidatos = []

    for grupo in agrupar_picos(picos):
        janelas_grupo = [janelas[i] for i in grupo]
        maior_i = max(grupo, key=lambda i: janelas[i].mensagens)
        maior = janelas[maior_i]
        media = picos[maior_i]
        razao = maior.mensagens / max(media, 1.0)
        msgs = sum(j.mensagens for j in janelas_grupo)
        pct_reacoes = 100 * sum(j.reacoes for j in janelas_grupo) / msgs

        # Intervalo do corte: começa MARGEM_ANTES_PICO antes do pico e termina um
        # pouco depois dele, respeitando a duração mínima e a máxima
        pico_inicio = t0 + grupo[0] * tamanho
        pico_fim = t0 + (grupo[-1] + 1) * tamanho
        inicio = pico_inicio - config.MARGEM_ANTES_PICO
        fim = pico_fim + config.MARGEM_DEPOIS_PICO
        fim = min(max(fim, inicio + duracao_min), inicio + duracao_max)

        partes = localizar_no_video(inicio, fim, blocos)
        segundos_com_video = sum(p["ate"] - p["de"] for p in partes)

        # As mensagens mais repetidas no pico: ajudam a lembrar o que aconteceu
        repetidas = Counter(normalizar_texto(t) for j in janelas_grupo for t in j.textos)

        candidatos.append({
            "inicio": hora_iso(inicio),
            "fim": hora_iso(fim),
            "horario_pico": hora_iso(t0 + maior_i * tamanho),
            "duracao": round(fim - inicio),
            **calcular_nota(razao, len(grupo), pct_reacoes),
            "pico": {
                "maior_janela": maior.mensagens,       # mensagens nos 10 s mais fortes
                "media_normal": round(media, 1),       # média de mensagens por janela antes
                "razao": round(razao, 1),              # quantas vezes acima do normal
                "janelas_em_pico": len(grupo),
                "mensagens": msgs,
            },
            "reacoes_pct": round(pct_reacoes),
            "mensagens_exemplo": [f"{texto} ({n}x)" for texto, n in repetidas.most_common(3)],
            "partes": partes,
            "blocos_fechados": bool(partes) and all(p["fechado"] for p in partes),
            "video_completo": segundos_com_video >= (fim - inicio) - 1,
        })

    # Numera em ordem de horário: rodando de novo, os números antigos não mudam
    for n, c in enumerate(candidatos, start=1):
        c["numero"] = n
    return candidatos


def salvar_candidatos(pasta: Path, candidatos: list[dict]) -> Path:
    destino = pasta / "candidatos.json"
    destino.write_text(json.dumps(candidatos, indent=2, ensure_ascii=False), encoding="utf-8")
    return destino


def mostrar_tabela(candidatos: list[dict]) -> None:
    print(f"\n{'Nº':>3}  {'pico':8}  {'dur':>4}  {'nota':>4}  {'msgs/10s':>8}  {'x normal':>8}  "
          f"{'reação':>6}  {'blocos':20}  o chat dizia")
    for c in candidatos:
        hora = c["horario_pico"][11:19]
        blocos = "+".join(p["bloco"].removeprefix("bloco_").removesuffix(".ts") for p in c["partes"])
        if not c["video_completo"]:
            blocos += " (incompleto)"
        elif not c["blocos_fechados"]:
            blocos += " (gravando)"
        marca = "*" if c["nota"] >= config.NOTA_MINIMA_AUTOCORTE else " "
        exemplo = c["mensagens_exemplo"][0][:40] if c["mensagens_exemplo"] else ""
        print(f"{c['numero']:>3}{marca} {hora:8}  {c['duracao']:>3}s  {c['nota']:>4}  "
              f"{c['pico']['maior_janela']:>8}  {c['pico']['razao']:>7}x  {c['reacoes_pct']:>5}%  "
              f"{blocos or '-':20}  {exemplo}")
    print(f"\n* = nota >= {config.NOTA_MINIMA_AUTOCORTE} (vira corte automático na etapa 4)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Encontra os picos do chat de uma sessão gravada.")
    parser.add_argument("sessao", nargs="?", help="nome da pasta em blocos/ (padrão: a mais recente)")
    args = parser.parse_args()

    pasta = achar_pasta_sessao(args.sessao)
    candidatos = detectar(pasta)
    destino = salvar_candidatos(pasta, candidatos)

    print(f"Sessão: {pasta.name}")
    if not candidatos:
        print("Nenhum pico encontrado (ou não há chat salvo para essa sessão).")
    else:
        mostrar_tabela(candidatos)
    print(f"Salvo em: {destino}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
