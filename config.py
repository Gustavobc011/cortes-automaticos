"""
config.py - Todas as configurações do projeto num lugar só.

Os outros módulos fazem `import config` e leem daqui, assim dá para
ajustar o comportamento do programa sem mexer no código.
"""

import os
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

# ---------------- DETECTOR DE PICOS (etapa 3) ----------------
JANELA_SEGUNDOS = 10               # o chat é contado em janelas desse tamanho
JANELA_MEDIA_SEGUNDOS = 300        # média móvel dos últimos 5 min = o "normal" do chat
HISTORICO_MINIMO_SEGUNDOS = 60     # sem pelo menos 1 min de chat ainda não dá para saber o "normal"
FATOR_PICO = 3.0                   # pico = janela com volume >= 3x a média
MINIMO_MENSAGENS_PICO = 15         # e com pelo menos esse tanto de mensagens na janela
JUNTAR_PICOS_SEGUNDOS = 60         # picos mais próximos que isso viram um só momento
MARGEM_DEPOIS_PICO = 15            # segundos extras depois que o chat acalma (a cena fecha com calma)
FATOR_CALMA = 1.5                  # chat "calmo" = janela abaixo de 1,5x a média de antes do pico
CALMA_SEGUNDOS = 30                # o momento acaba quando o chat fica calmo por esse tempo seguido
AJUSTE_TEMPO_VIDEO = 0             # segundos; positivo = o vídeo começou antes da hora anotada

# ---------------- CORTES (etapas 3 e 4) ----------------
NOTA_MINIMA_AUTOCORTE = 4.0        # só salva corte automático acima dessa nota (etapa 7: era 7.0)
MARGEM_ANTES_PICO = 45             # segundos antes do pico (o chat reage com atraso)
DURACAO_MINIMA_CORTE = 60          # exigência de monetização do TikTok
DURACAO_MAXIMA_CORTE = 180         # limite para o fim dinâmico não virar um vídeo longo demais
MARGEM_KEYFRAME = 3                # segundos extras no início e no fim (o -c copy corta no keyframe)
# "KKK" (3 letras) também pega "kkkk..."; "CLAP" foi a palavra que mais subiu nos picos do teste
PALAVRAS_REACAO = ["KKK", "CLAP", "KEKW", "LUL", "OMEGALUL", "CLIPA", "CLIP",
                   "NÃO ACREDITO", "MDS", "PogChamp", "Pog"]

# ---------------- TRANSCRIÇÃO (etapa 5) ----------------
TRANSCREVER = True                 # False = não transcreve nada, só o chat decide os cortes
MODELO_WHISPER = "small"           # bom equilíbrio entre qualidade e velocidade no processador
IDIOMA_WHISPER = "pt"
THREADS_TRANSCRICAO = max(1, (os.cpu_count() or 2) // 2)   # metade dos núcleos: sobra para o CapCut

# ---------------- ORQUESTRADOR (etapa 6) ----------------
INTERVALO_DETECTOR = 300           # roda o detector a cada 5 min (e sempre que um bloco fecha)
INTERVALO_VERIFICACAO = 10         # de quantos em quantos segundos o main confere os blocos
NOTIFICAR_CORTES = True            # notificação do Windows a cada corte novo

# ---------------- FIM DA LIVE (etapa 6) ----------------
APAGAR_BLOCOS_NO_FIM = True        # apaga a gravação inteira no fim e mantém só os cortes
