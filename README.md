# Cortes automáticos de lives

Programa em Python que grava uma live da Twitch, acompanha o chat ao mesmo tempo
e, quando o chat explode, salva sozinho o trecho do vídeo daquele momento como um
corte pronto para editar, com transcrição e legenda `.srt`.

Tudo roda no notebook, no processador (sem placa de vídeo), e foi feito para
funcionar **em segundo plano** enquanto eu uso o computador para editar no CapCut.

> O programa **não posta nada** em redes sociais. Ele só encontra e separa os
> momentos; quem decide o que vai ao ar sou eu.

## O problema

Quem faz cortes de live precisa assistir horas de transmissão para achar poucos
minutos que valem a pena. Encontrar o momento, voltar o vídeo, marcar início e
fim e exportar toma mais tempo do que editar o corte.

O chat resolve a parte difícil: quando algo engraçado, assustador ou surpreendente
acontece, centenas de pessoas reagem nos mesmos segundos. O programa mede esse
volume, compara com o "normal" daquela live e trata cada explosão como um
candidato a corte.

## Como funciona

```mermaid
flowchart LR
    T[(Twitch)] -->|vídeo| G[gravador.py<br/>streamlink + ffmpeg]
    T -->|chat IRC| M[monitor_chat.py]
    G -->|blocos .ts de 20 min<br/>+ sessao.json| B[(blocos/)]
    M -->|mensagens com horário| D[(dados/chat.db<br/>SQLite)]
    B --> DET[detector.py<br/>picos + nota]
    D --> DET
    DET -->|candidatos.json| C[cortador.py<br/>ffmpeg -c copy]
    C -->|.mp4 + lista.md| CO[(cortes/)]
    CO --> TR[transcritor.py<br/>faster-whisper<br/>prioridade baixa]
    TR -->|.srt + texto| CO
    O{{main.py<br/>orquestrador}} -.coordena.-> G & M & DET & C & TR
```

| Etapa | Arquivo | O que faz |
|---|---|---|
| 1 | `gravador.py` | Grava a live em blocos `.ts` de 20 min. Reconecta se a conexão cair, anota a hora real de início de cada trecho e os intervalos de anúncio, e para sozinho quando a live acaba ou o disco está quase cheio. |
| 2 | `monitor_chat.py` | Entra no chat da Twitch como visitante anônimo (só leitura) e salva cada mensagem com o horário de chegada no SQLite. |
| 3 | `detector.py` | Conta mensagens em janelas de 10 s. Um pico é uma janela com pelo menos 3x a média dos 5 min anteriores. Picos próximos viram um só momento, com uma nota de 0 a 10. |
| 4 | `cortador.py` | Converte o horário do momento em "qual bloco, de que segundo a que segundo" e extrai o trecho sem recodificar. Gera também uma `lista.md` com os cortes. |
| 5 | `transcritor.py` | Transcreve só os cortes, com o modelo Whisper `small` no processador, e gera a legenda `.srt`. |
| 6 | `main.py` | Roda tudo junto, chama o detector a cada 5 min e sempre que um bloco fecha, mostra uma notificação do Windows a cada corte novo e impede o notebook de suspender. |
| extra | `vigia.py` | Modo automático: vigia uma lista de canais por ordem de prioridade e grava sozinho quem entrar ao vivo no jogo escolhido, trocando de canal quando um de prioridade maior aparece. |
| extra | `estado_vigia.py` | O estado do vigia (vigiando, gravando, pausado) num lugar só: o ícone e o Telegram leem e fazem pedidos, só o vigia executa. |
| extra | `bandeja.py` | Ícone perto do relógio do Windows: cor conforme o estado, pausar, retomar, abrir os cortes e o log, sair. |
| extra | `telegram_bot.py` | Bot do Telegram: avisa cada corte (com botões para aprovar ou descartar), os eventos do vigia e os erros, e aceita comandos. |
| extra | `blocos.py` | Lista as gravações brutas guardadas e decide se dá para apagar: protege a live que está gravando ou em processamento. |

### A nota de cada momento

| Parte | Até | Como é medida |
|---|---|---|
| Volume | 6 | Quantas vezes o chat ficou acima do normal (3x ≈ 3,2 pontos; 8x = 6). |
| Duração | 1,5 | Picos que se mantêm por várias janelas seguidas valem mais. |
| Reações | 2,5 | % das mensagens com palavras como `KKK`, `KEKW`, `CLAP`, `MDS`. |

Com nota a partir de `NOTA_MINIMA_AUTOCORTE` (4.0), o corte é salvo sozinho.

O corte começa 45 s antes do pico, porque o chat reage com atraso. Ele termina
quando o chat volta ao normal por 30 s seguidos (fim dinâmico), com no mínimo 60 s
e no máximo 3 min.

## Tecnologias

- **Python 3.13**, quase só com a biblioteca padrão (`sqlite3`, `socket`, `threading`, `subprocess`, `ctypes`)
- **[Streamlink](https://streamlink.github.io/)**: pega o vídeo da live
- **[FFmpeg](https://ffmpeg.org/)**: grava em blocos e extrai os cortes
- **SQLite** (modo WAL): guarda o chat
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)**: transcrição no processador (`int8`)
- **psutil**: deixa a transcrição em prioridade baixa e descobre quais processos estão gravando
- **pystray + Pillow**: ícone do vigia na bandeja do Windows
- **API do Telegram**: chamada direto com `urllib` (biblioteca padrão), sem biblioteca extra

## Instalação (Windows)

1. Instale o Python 3.13, o FFmpeg e o Streamlink. Depois feche e abra o PowerShell para os comandos aparecerem.

   ```powershell
   winget install Python.Python.3.13
   ```

   ```powershell
   winget install Gyan.FFmpeg
   ```

   ```powershell
   winget install Streamlink.Streamlink
   ```

2. Clone o repositório e crie o ambiente virtual:

   ```powershell
   git clone https://github.com/Gustavobc011/cortes-automaticos.git
   ```

   ```powershell
   cd cortes-automaticos
   ```

   ```powershell
   python -m venv venv
   ```

   ```powershell
   .\venv\Scripts\pip install -r requirements.txt
   ```

   O modelo do Whisper (uns 500 MB) é baixado sozinho na primeira transcrição.

## Uso

Com a live no ar:

```powershell
.\venv\Scripts\python main.py nomedocanal
```

Também aceita o link (`https://www.twitch.tv/nomedocanal`). Ctrl+C encerra tudo
de forma limpa. No fim aparece um resumo, e o programa pergunta se pode apagar a
gravação bruta. Os cortes ficam.

O que fica no disco:

```
blocos/<canal>_<data>/   bloco_000.ts ... + sessao.json + candidatos.json   (gravação bruta)
cortes/<canal>_<data>/   01_19h21_nota4.2.mp4 + .srt + cortes.json + lista.md
dados/chat.db            todas as mensagens do chat
```

Cada etapa também roda sozinha, o que ajuda a testar ou refazer uma parte:

| Comando | O que faz |
|---|---|
| `python gravador.py canal` | só grava |
| `python monitor_chat.py canal` | só salva o chat |
| `python detector.py [sessao]` | mostra a tabela de candidatos de uma gravação |
| `python cortador.py 3 5 --sessao <pasta>` | corta os candidatos 3 e 5 à mão, com qualquer nota |
| `python transcritor.py [pasta] [--todos]` | transcreve os cortes que faltam |

Todas as configurações (nota mínima, margens, palavras de reação, tamanho dos
blocos, modelo do Whisper) ficam comentadas em [`config.py`](config.py).

### Modo vigia (automático)

O `vigia.py` fica rodando sem janela, iniciado pelo Agendador de Tarefas do
Windows no login, e decide sozinho o que gravar:

- **Checagem:** a cada 2 min pergunta ao streamlink (`--json`, sem conta nem API)
  quais canais de `CANAIS_VIGIADOS` estão ao vivo na `CATEGORIA_ALVO`. Reprises
  ("RERUN") são ignoradas.
- **Escolha:** grava o canal de maior prioridade disponível, rodando o
  `main.py --vigia` como outro processo. Um canal por vez.
- **Troca:** se um canal de prioridade maior entra ao vivo, a gravação atual
  termina os cortes em segundo plano e a nova começa na hora. Um canal de
  prioridade menor nunca interrompe um de prioridade maior.
- **Fim da live:** o canal ficou offline ou fora do jogo por mais de
  `OFFLINE_TOLERANCIA` (5 min). Uma queda curta continua na mesma gravação.
- **Disco:** os blocos ficam guardados até você mandar apagar pelo Telegram
  (`APAGAR_BLOCOS_VIGIA = False`). Com `True`, eles são apagados sozinhos no fim,
  depois que os cortes e as transcrições ficam prontos.
- **Avisos:** notificação quando o vigia inicia, quando uma live começa, quando
  há troca (com o motivo) e quando a live termina (duração e cortes salvos).
  Tudo fica registrado em `dados/vigia.log`, e a saída completa de cada
  gravação em `dados/gravacoes/`.

```powershell
.\venv\Scripts\python vigia.py --checar
```

O comando acima só mostra quem está ao vivo agora. Para encerrar o vigia de
forma limpa (esperando os cortes), crie o arquivo `dados\parar_vigia`, use
"Sair" no ícone perto do relógio ou `/parar` no Telegram.

### Bot do Telegram (opcional)

Com o bot, o vigia manda pelo Telegram, só em texto (nenhum vídeo sai do
computador):

- **Cortes:** cada corte salvo, com streamer, nota, horário, duração,
  transcrição resumida e o caminho do arquivo. Embaixo vêm os botões
  **👍 Bom** e **👎 Descartar**. Descartar move o corte para `descartados/`, dentro
  da pasta da live, e ele é apagado depois de `DIAS_MANTER_DESCARTADOS` (7 dias).
  **↩️ Desfazer** devolve. A escolha fica marcada no `lista.md` e no `candidatos.json`.
- **Avisos:** vigia iniciado, live começou, troca de canal (com o motivo), live
  encerrada (duração, cortes e tamanho dos blocos, com os botões **🗑 Apagar
  blocos** e **📦 Manter**) e erros (gravação caiu, disco abaixo de
  `ESPACO_MINIMO_GB`). O aviso de disco sugere as lives mais antigas, com botão
  para apagar.
- **Comandos:** `/status`, `/pausar`, `/retomar`, `/parar` (pede confirmação),
  `/cortes` (os de hoje), `/blocos` e `/ajuda`.
- **Blocos (`/blocos`):** mostra o espaço livre e cada live que ainda tem blocos,
  com streamer, data, tamanho, número de cortes e quantos estão sem avaliação.
  Apagar sempre pede confirmação (Sim/Não), mostra o tamanho e avisa que não dá
  para desfazer nem estender os cortes sem avaliação depois. Só a pasta
  `blocos/<live>` é apagada, nunca os cortes. Cada exclusão fica no `vigia.log`.
- **Proteção:** a live que está gravando, ou que tem cortes, transcrições,
  extensões ou correções em andamento, aparece como 🔒 protegida, sem botão. A
  proteção é conferida de novo na hora do "Sim". Quem usa a pasta deixa nela uma
  marca `.ocupado_*` com o número do processo, que deixa de valer sozinha se o
  processo morrer. Os testes ficam em `tests/test_blocos.py`
  (`python -m unittest discover tests`).

O bot só responde ao seu `chat_id` e ignora qualquer outra pessoa. Se a
internet cair, as mensagens esperam numa fila salva em disco e saem quando ela
voltar. Ele roda em threads próprias, então nunca atrasa a gravação.

**Como configurar:**

1. **Criar o bot:** no Telegram, abra uma conversa com o **@BotFather**, mande
   `/newbot` e escolha um nome e um usuário (terminado em `bot`). Ele responde com
   o **token** do bot. Trate esse token como uma senha: quem tiver ele controla o bot.
2. **Guardar o token:** copie o `.env.exemplo` para `.env` e cole o token em
   `TELEGRAM_TOKEN=`. O `.env` está no `.gitignore` e nunca vai para o GitHub.

   ```powershell
   Copy-Item .env.exemplo .env
   ```

3. **Descobrir o seu `chat_id`:** abra a conversa com o seu bot, toque em
   **Iniciar** e mande qualquer mensagem. Depois, com o vigia desligado, rode o
   comando abaixo. Ele grava o `TELEGRAM_CHAT_ID` no `.env` sozinho.

   ```powershell
   .\venv\Scripts\python telegram_bot.py --descobrir-chat-id
   ```

4. **Ligar o vigia** de novo. A primeira mensagem do bot é "Vigia iniciado".
   Para desligar o bot sem apagar nada, use `TELEGRAM_ATIVO = False` no `config.py`.

## Decisões técnicas

### Por que transcrever só os picos

Sem placa de vídeo, o Whisper é lento e pesado. Transcrever uma live inteira de
4 h deixaria o notebook ocupado por horas, e a maior parte do texto nunca seria
lida. O chat já diz
onde estão os bons momentos, então só os cortes (1 a 3 min cada) são transcritos,
depois de salvos.

### Por que gravar em blocos `.ts`

- **Resiste a falhas:** o formato MPEG-TS é feito para transmissão. Se o programa
  ou o notebook cair, tudo o que já foi gravado continua legível, ao contrário de
  um `.mp4`, que só fica válido quando é fechado.
- **Cortes durante a live:** blocos de 20 min permitem cortar enquanto a live
  ainda está rolando. Quando um bloco fecha, os cortes dele já podem sair.
- **Espaço em disco:** dá para apagar a gravação bruta aos poucos.
- **Horários exatos:** o ffmpeg anota em um CSV o início e o fim de cada bloco.
  Somando isso à hora real do primeiro byte de vídeo, cada segundo do vídeo tem
  um horário real, e é isso que liga o vídeo ao chat.

### Por que `-c copy` na gravação, mas não nos cortes

O `-c copy` copia o vídeo e o áudio como estão, sem recodificar. Na
**gravação** ele é essencial: usa quase nada de CPU, o que permite gravar horas
enquanto o notebook é usado para outra coisa.

Nos **cortes** ele foi usado no começo (um corte saía em menos de 1 s), mas
causava um problema: com `-c copy` o vídeo só pode começar num keyframe, e a
live tem um a cada 2 s. O ffmpeg então levava junto até 2 s de vídeo antes do
ponto de corte e escondia isso com uma "lista de edição" do MP4. Muitos
programas ignoram essa lista, e neles **o áudio tocava até 2 s atrasado**.

Para medir isso com certeza, o teste [`tests/test_sincronia.py`](tests/test_sincronia.py)
cria um vídeo com "verdade conhecida": um clarão na tela e um bipe no áudio no
mesmo instante, a cada 5 s. Ele grava esse vídeo em blocos como o gravador,
corta e confere onde o clarão e o bipe caem. Com `-c copy` o bipe vinha 1,7 s
depois do clarão. Testei também começar no keyframe exato, o `-ss` na saída e a
busca na entrada: este ffmpeg sempre recua um GOP inteiro.

A solução foi **recodificar o corte** (`libx264 -preset veryfast`, em prioridade
baixa e com metade dos núcleos), buscando o ponto de corte na entrada. O ffmpeg
decodifica a partir do keyframe e descarta o que vem antes do ponto nas duas
faixas, que começam juntas, sem lista de edição. Leva mais ou menos o tempo do
próprio corte, em segundo plano. Depois de cada corte, a sincronia é medida com
o ffprobe: acima de 0,1 s, o vigia avisa no log e no Telegram. Os cortes
antigos são corrigidos com o [`corrigir_cortes.py`](corrigir_cortes.py).

### Por que usar o chat como sinal

- **Leve:** analisar o vídeo ou o áudio com IA durante a live pesaria demais num
  processador de notebook. Contar mensagens custa quase nada.
- **Mede o que importa:** o chat é a reação do público, que é justamente o que
  faz um corte funcionar.
- **Compara com o próprio canal:** o "normal" é a média móvel dos últimos 5 min,
  então o mesmo código serve para um canal com 50 ou com 5 000 mensagens por
  minuto e se adapta quando o chat esquenta ao longo da live.
- **Ajustado com uma live real:** no teste de 70 min no canal do cellbit, o
  melhor momento foi de suspense ("MONKA", "MEDO") e teve nota 4.2, enquanto
  momentos com mais emotes de risada ficaram com notas maiores. Por isso a nota
  mínima ficou baixa e o fim do corte passou a seguir o chat. Os detalhes
  estão no histórico de commits.

### Como o programa convive com outros programas

- **Gravação:** só copia bytes (`-c copy`), sem recodificar.
- **Transcrição:** roda num processo separado, com prioridade "abaixo do normal" e
  só metade dos núcleos. O CapCut sempre tem preferência.
- **Suspensão:** enquanto o programa roda, ele pede ao Windows para não suspender
  o notebook (`SetThreadExecutionState`), e esse pedido é retirado no fim. A tela
  pode apagar normalmente.
- **Ctrl+C:** streamlink, ffmpeg e o transcritor rodam em grupos de processo
  próprios, então o Ctrl+C chega só ao Python, que encerra cada um em ordem sem
  corromper o último bloco.
- **Avisos:** cada corte novo aparece como notificação do Windows, sem precisar
  olhar o terminal.

## Limitações conhecidas

- O chat só funciona com a Twitch. O gravador também aceita YouTube e Kick, mas sem o chat não há detecção.
- Momentos que o chat não percebe (uma fala boa num chat calmo) não viram corte.
- A nota foi calibrada com poucas lives. Cada canal reage de um jeito, então a nota mínima e as palavras de reação podem precisar de ajuste.
