"""
cortador.py - Etapa 4 do projeto de cortes automáticos.

Salva os cortes dos melhores momentos (os candidatos do detector) em
cortes/<canal>_<data>/, do jeito que vieram da live: horizontais, sem
recodificar e sem legenda. Também mantém um lista.md com o resumo de
cada corte, pronto para colar no Claude.

Uso:
    python cortador.py                  corta automático: candidatos com nota >= NOTA_MINIMA_AUTOCORTE
    python cortador.py 3 7              corta os candidatos 3 e 7, mesmo abaixo da nota mínima
    python cortador.py 3 --sessao cellbit_2026-11-20_23-44

Antes de cortar, o detector roda de novo para usar os dados mais recentes.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import config
import detector


# ---------------------------------------------------------------- pastas e registro

def pasta_dos_cortes(pasta_sessao: Path) -> Path:
    """cortes/<canal>_<data>/, ex.: cortes/cellbit_2026-11-20."""
    sessao = json.loads((pasta_sessao / "sessao.json").read_text(encoding="utf-8"))
    data = datetime.fromisoformat(sessao["trechos"][0]["inicio"]).date() if sessao["trechos"] else None
    pasta = config.PASTA_CORTES / f"{sessao['canal']}_{data or 'sem_data'}"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def carregar_registro(pasta_cortes: Path) -> list[dict]:
    """O registro (cortes.json) lembra o que já foi cortado, para não cortar duas vezes."""
    arquivo = pasta_cortes / "cortes.json"
    return json.loads(arquivo.read_text(encoding="utf-8")) if arquivo.exists() else []


def salvar_registro(pasta_cortes: Path, registro: list[dict]) -> None:
    # Escreve num arquivo temporário e troca de uma vez: quem estiver lendo
    # (o bot do Telegram, por exemplo) nunca pega o arquivo pela metade
    texto = json.dumps(registro, indent=2, ensure_ascii=False)
    temporario = pasta_cortes / "cortes.json.tmp"
    temporario.write_text(texto, encoding="utf-8")
    os.replace(temporario, pasta_cortes / "cortes.json")


# ---------------------------------------------------------------- avaliação (Telegram)

def _mover_corte(origem: Path, destino: Path, arquivo: str) -> None:
    """Move o .mp4 e o .srt de um corte de uma pasta para outra."""
    destino.mkdir(exist_ok=True)
    for caminho in (origem / arquivo, (origem / arquivo).with_suffix(".srt")):
        if caminho.exists():
            shutil.move(caminho, destino / caminho.name)
            os.utime(destino / caminho.name)   # a data do arquivo vira a do descarte


def avaliar_corte(pasta_cortes: Path, arquivo: str, avaliacao: str | None) -> dict:
    """
    Marca um corte como "aprovado", "descartado" ou None (sem avaliação, o "desfazer").
    Descartado não é apagado: vai para descartados/ dentro da pasta da live, e
    limpar_descartados() apaga depois de DIAS_MANTER_DESCARTADOS. Os blocos da
    gravação nunca são tocados aqui.
    """
    registro = carregar_registro(pasta_cortes)
    corte = next(c for c in registro if c["arquivo"] == arquivo)
    descartados = pasta_cortes / "descartados"
    estava_descartado = corte.get("avaliacao") == "descartado"

    if avaliacao == "descartado" and not estava_descartado:
        _mover_corte(pasta_cortes, descartados, arquivo)
    elif estava_descartado and avaliacao != "descartado":
        if not (descartados / arquivo).exists():
            raise FileNotFoundError("esse corte descartado já foi apagado")
        _mover_corte(descartados, pasta_cortes, arquivo)

    corte["avaliacao"] = avaliacao
    corte["avaliado_em"] = datetime.now().isoformat(timespec="seconds")
    salvar_registro(pasta_cortes, registro)
    gerar_lista_md(pasta_cortes, registro)
    _marcar_candidato(corte, avaliacao)
    return corte


def _marcar_candidato(corte: dict, avaliacao: str | None) -> None:
    """Anota a avaliação também no candidatos.json da gravação (se ainda existir)."""
    arquivo = config.PASTA_BLOCOS / corte["sessao"] / "candidatos.json"
    if not arquivo.exists():
        return
    candidatos = json.loads(arquivo.read_text(encoding="utf-8"))
    for c in candidatos:
        if c.get("numero") == corte["candidato"]:
            c["avaliacao"] = avaliacao
    detector.salvar_candidatos(arquivo.parent, candidatos)


def limpar_descartados(dias: int) -> int:
    """Apaga de vez os cortes descartados há mais de `dias` dias. Devolve quantos apagou."""
    limite = time.time() - dias * 86400
    apagados = 0
    for descartados in config.PASTA_CORTES.glob("*/descartados"):
        velhos = [a for a in descartados.iterdir() if a.is_file() and a.stat().st_mtime < limite]
        if not velhos:
            continue
        for arquivo in velhos:
            arquivo.unlink()
        apagados += sum(a.suffix == ".mp4" for a in velhos)
        pasta_cortes = descartados.parent
        registro = carregar_registro(pasta_cortes)
        nomes = {a.name for a in velhos}
        for c in registro:
            if c["arquivo"] in nomes:
                c["descarte_apagado"] = True
        salvar_registro(pasta_cortes, registro)
        gerar_lista_md(pasta_cortes, registro)
    return apagados


def corte_existente(registro: list[dict], sessao: str, inicio: str, fim: str) -> dict | None:
    """
    Procura um corte já salvo que se sobreponha a esse intervalo na mesma sessão.
    Usamos sobreposição (e não o número do candidato) porque um momento pode
    crescer um pouco entre uma rodada do detector e outra.
    """
    for corte in registro:
        if corte["sessao"] == sessao and corte["inicio"] < fim and inicio < corte["fim"]:
            return corte
    return None


# ---------------------------------------------------------------- ffmpeg

def rodar_ffmpeg(argumentos: list[str]) -> None:
    """
    Roda o ffmpeg e só mostra as mensagens dele se der erro de verdade.
    (Ao cortar sem recodificar, ele reclama dos quadros antes do keyframe,
    o que é normal e não afeta o resultado.)
    """
    resultado = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *argumentos],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL,   # o ffmpeg lê atalhos de teclado da entrada: não herda a do main
    )
    if resultado.returncode != 0:
        print(resultado.stderr)
        raise RuntimeError("O ffmpeg falhou ao salvar o corte.")


def extrair_trechos(pasta_sessao: Path, partes: list[dict], destino: Path) -> None:
    """
    Recorta o momento do(s) bloco(s) com -c copy e salva num .mp4.

    Usamos o "concat" do ffmpeg: um .txt lista os blocos e, para cada um, de
    que segundo (inpoint) até que segundo (outpoint) ler. Com um bloco só ou
    com dois, é o mesmo comando, e o ffmpeg junta os pedaços sem recodificar.
    """
    linhas = []
    for i, parte in enumerate(partes):
        linhas.append(f"file '{(pasta_sessao / parte['bloco']).resolve().as_posix()}'")
        if parte["de"] > 0.05:                # começa no meio do bloco
            linhas.append(f"inpoint {parte['de']:.3f}")
        if i == len(partes) - 1:              # o último pedaço termina no meio do bloco;
            linhas.append(f"outpoint {parte['ate']:.3f}")  # os outros vão até o fim do arquivo

    with tempfile.TemporaryDirectory() as temporaria:
        arquivo_lista = Path(temporaria) / "lista.txt"
        arquivo_lista.write_text("\n".join(linhas), encoding="utf-8")
        rodar_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(arquivo_lista),
            "-map", "0:v:0", "-map", "0:a:0",  # só vídeo e áudio (ignora metadados da Twitch)
            "-c", "copy",                      # sem recodificar: instantâneo e sem perder qualidade
            "-bsf:a", "aac_adtstoasc",         # ajusta o formato do áudio AAC de .ts para .mp4
            "-movflags", "+faststart",         # o vídeo começa a tocar antes de carregar inteiro
            str(destino),
        ])


def duracao_video(arquivo: Path) -> float:
    saida = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(arquivo)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    return float(saida.stdout.strip() or 0)


# ---------------------------------------------------------------- cortar

def cortar_candidato(pasta_sessao: Path, candidato: dict, motivo: str) -> dict | None:
    """Salva o corte de um candidato. Devolve o registro do corte, ou None se não deu."""
    numero = candidato["numero"]
    if not candidato["blocos_fechados"]:
        print(f"  Candidato {numero}: o bloco ainda está sendo gravado. Tente depois.")
        return None
    if not candidato["video_completo"]:
        print(f"  Candidato {numero}: aviso, parte do momento não tem vídeo (a conexão caiu).")

    pasta_cortes = pasta_dos_cortes(pasta_sessao)
    registro = carregar_registro(pasta_cortes)
    if existente := corte_existente(registro, pasta_sessao.name, candidato["inicio"], candidato["fim"]):
        print(f"  Candidato {numero}: já foi cortado ({existente['arquivo']}).")
        return None

    # Recalcula os pedaços com a margem extra de keyframe no começo e no fim
    margem = config.MARGEM_KEYFRAME
    inicio_ts = datetime.fromisoformat(candidato["inicio"]).timestamp() - margem
    fim_ts = datetime.fromisoformat(candidato["fim"]).timestamp() + margem
    partes = detector.localizar_no_video(inicio_ts, fim_ts, detector.linha_do_tempo_video(pasta_sessao))
    if not partes:
        print(f"  Candidato {numero}: não há vídeo gravado nesse horário.")
        return None

    # Nome: número sequencial na pasta + hora do pico + nota. Ex.: 01_14h32_nota9.2.mp4
    sequencia = len(registro) + 1
    hora = datetime.fromisoformat(candidato["horario_pico"])
    nome = f"{sequencia:02d}_{hora:%Hh%M}_nota{candidato['nota']}.mp4"
    destino = pasta_cortes / nome

    extrair_trechos(pasta_sessao, partes, destino)

    corte = {
        "numero": sequencia,
        "arquivo": nome,
        "sessao": pasta_sessao.name,
        "candidato": numero,
        "motivo": motivo,                     # "automatico" ou "manual"
        "inicio": candidato["inicio"],
        "fim": candidato["fim"],
        "horario_pico": candidato["horario_pico"],
        "duracao": round(duracao_video(destino), 1),
        "nota": candidato["nota"],
        "pico": candidato["pico"],
        "reacoes_pct": candidato["reacoes_pct"],
        "mensagens_exemplo": candidato["mensagens_exemplo"],
        "blocos": [p["bloco"] for p in partes],
        "transcricao": None,                  # preenchido pelo transcritor (etapa 5)
    }
    registro.append(corte)
    salvar_registro(pasta_cortes, registro)
    gerar_lista_md(pasta_cortes, registro)
    print(f"  Corte salvo: {destino}  ({corte['duracao']:.0f} s, blocos: {', '.join(corte['blocos'])})")
    return corte


def cortar_automaticos(pasta_sessao: Path, candidatos: list[dict]) -> list[dict]:
    """Corta todos os candidatos com nota suficiente cujo vídeo já está pronto."""
    novos = []
    for c in candidatos:
        if c["nota"] < config.NOTA_MINIMA_AUTOCORTE or not c["blocos_fechados"]:
            continue
        registro = carregar_registro(pasta_dos_cortes(pasta_sessao))
        if corte_existente(registro, pasta_sessao.name, c["inicio"], c["fim"]):
            continue  # já cortado numa rodada anterior: não avisa de novo
        if corte := cortar_candidato(pasta_sessao, c, "automatico"):
            novos.append(corte)
    return novos


# ---------------------------------------------------------------- lista.md

def formatar_duracao(segundos: float) -> str:
    minutos, segundos = divmod(round(segundos), 60)
    return f"{minutos}min{segundos:02d}" if minutos else f"{segundos}s"


TEXTO_AVALIACAO = {"aprovado": "👍 aprovado", "descartado": "👎 descartado", None: "sem avaliação"}


def gerar_lista_md(pasta_cortes: Path, registro: list[dict]) -> None:
    """Reescreve o lista.md com todos os cortes da pasta."""
    linhas = [
        f"# Cortes: {pasta_cortes.name}",
        "",
        "Momentos em que o chat reagiu forte durante a live. Cole esta lista no Claude e peça "
        "para escolher os melhores cortes e sugerir títulos.",
        "",
    ]
    for c in registro:
        pico = c["pico"]
        hora = datetime.fromisoformat(c["horario_pico"])
        avaliacao = c.get("avaliacao")
        if avaliacao == "descartado":
            arquivo = "(apagado)" if c.get("descarte_apagado") else f"`descartados/{c['arquivo']}`"
        else:
            arquivo = f"`{c['arquivo']}`"
        linhas += [
            f"## {c['numero']:02d} · {hora:%Hh%M} · {formatar_duracao(c['duracao'])} · nota {c['nota']}",
            "",
            f"- Arquivo: {arquivo}",
            f"- Avaliação: {TEXTO_AVALIACAO[avaliacao]}",
            f"- Pico do chat: {pico['maior_janela']} mensagens em 10 s "
            f"({pico['razao']}x o normal), {c['reacoes_pct']}% com reações",
            f"- O chat dizia: {', '.join(c['mensagens_exemplo'])}",
            f"- Transcrição: {c['transcricao'] or '(ainda não transcrito)'}",
            "",
        ]
    (pasta_cortes / "lista.md").write_text("\n".join(linhas), encoding="utf-8")


# ---------------------------------------------------------------- terminal

def main() -> None:
    parser = argparse.ArgumentParser(description="Salva os cortes dos candidatos do detector.")
    parser.add_argument("numeros", nargs="*", type=int,
                        help="números dos candidatos para cortar (vazio = modo automático)")
    parser.add_argument("--sessao", help="nome da pasta em blocos/ (padrão: a mais recente)")
    args = parser.parse_args()

    pasta_sessao = detector.achar_pasta_sessao(args.sessao)
    candidatos = detector.detectar(pasta_sessao)       # dados mais recentes do chat
    detector.salvar_candidatos(pasta_sessao, candidatos)
    print(f"Sessão: {pasta_sessao.name} ({len(candidatos)} candidato(s))")

    if args.numeros:
        por_numero = {c["numero"]: c for c in candidatos}
        for n in args.numeros:
            if n not in por_numero:
                print(f"  Candidato {n} não existe.")
            else:
                cortar_candidato(pasta_sessao, por_numero[n], "manual")
    else:
        print(f"Modo automático: cortando candidatos com nota >= {config.NOTA_MINIMA_AUTOCORTE}")
        novos = cortar_automaticos(pasta_sessao, candidatos)
        if not novos:
            print("  Nenhum corte novo.")

    print(f"Pasta dos cortes: {pasta_dos_cortes(pasta_sessao)}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
