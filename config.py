"""
config.py - Todas as configurações do projeto num lugar só.

Os outros módulos fazem `import config` e leem daqui, assim dá para
ajustar o comportamento do programa sem mexer no código.
"""

from pathlib import Path

# ---------------- PASTAS ----------------
PASTA_PROJETO = Path(__file__).resolve().parent
PASTA_BLOCOS = PASTA_PROJETO / "blocos"    # gravações da live (temporárias)
PASTA_CORTES = PASTA_PROJETO / "cortes"    # cortes salvos, uma pasta por live
PASTA_DADOS = PASTA_PROJETO / "dados"      # banco chat.db

# ---------------- GRAVAÇÃO (etapa 1) ----------------
QUALIDADE = "1080p60,1080p,best"   # melhor qualidade; gravar com -c copy não pesa no notebook
DURACAO_BLOCO = 1200               # segundos por bloco (configurável; menor = candidatos mais rápidos)

ESPACO_MINIMO_GB = 10              # avisar quando o disco estiver acabando
ESPACO_CRITICO_GB = 2              # abaixo disso a gravação para, para não lotar o disco
INTERVALO_CHECAGEM_DISCO = 60      # de quantos em quantos segundos verificar o disco

INTERVALO_RECONEXAO = 10           # segundos de espera antes de tentar reconectar
TEMPO_MAXIMO_SEM_DADOS = 45        # sem receber vídeo por esse tempo (fora de anúncio) = conexão travada
TEMPO_ESPERA_FIM_LIVE = 180        # canal offline por esse tempo depois de cair = a live acabou

# ---------------- CHAT (etapa 2) ----------------
ARQUIVO_BANCO = PASTA_DADOS / "chat.db"
CHAT_SERVIDOR = "irc.chat.twitch.tv"
CHAT_PORTA = 6667
INTERVALO_RESUMO_CHAT = 60         # de quantos em quantos segundos mostrar mensagens/min
CHAT_TEMPO_SEM_DADOS = 60          # sem nada do servidor por esse tempo: mandamos um PING de teste

# ---------------- CORTES (etapas 3 e 4) ----------------
NOTA_MINIMA_AUTOCORTE = 7.0        # só salva corte automático acima dessa nota
MARGEM_ANTES_PICO = 45             # segundos antes do pico (o chat reage com atraso)
DURACAO_MINIMA_CORTE = 60          # exigência de monetização do TikTok
DURACAO_ALVO_CORTE = (60, 90)
PALAVRAS_REACAO = ["KKKK", "KEKW", "LUL", "OMEGALUL", "CLIPA", "CLIP",
                   "NÃO ACREDITO", "MDS", "PogChamp", "Pog"]

# ---------------- TRANSCRIÇÃO (etapa 5) ----------------
TRANSCREVER = True                 # False = não transcreve nada, só o chat decide os cortes

# ---------------- FIM DA LIVE (etapa 6) ----------------
APAGAR_BLOCOS_NO_FIM = True        # apaga a gravação inteira no fim e mantém só os cortes
