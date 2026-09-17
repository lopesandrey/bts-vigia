# bts-vigia

Vigia os ingressos do BTS (*Arirang*, Estádio do Morumbis, 28/30/31 de outubro
de 2026) no buyticketbrasil e avisa num grupo do Telegram quando o preço cai
para um nível honesto.

Um processo, uma dependência. Checa a cada 30 segundos.

**Ubuntu 24.04:** `sudo ./install.sh` e pronto — [detalhes
abaixo](#rodar-no-ubuntu-2404-o-jeito-de-verdade).

## Por que "ágio de 36%"

A produção anunciou estes valores de face (G1, 27/03/2026):

| Setor | Meia | Inteira |
|---|---|---|
| Arquibancada | R$ 340 | R$ 680 |
| Cadeira Superior | R$ 490 | R$ 980 |
| Cadeira Inferior | R$ 540 | R$ 1.080 |
| Pista | R$ 625 | R$ 1.250 |

Quem comprou na Ticketmaster pagou face **+ ~15% de taxa de conveniência**.
Numa Pista, R$ 1.437. Um ágio honesto de +20% sobre o que a pessoa realmente
gastou dá **R$ 1.725** — ou seja, **face × 1,36**.

Daí o padrão `MAX_MARKUP=36`, que gera estes tetos:

| Setor | Teto |
|---|---|
| Arquibancada | R$ 924,80 |
| Cadeira Superior | R$ 1.332,80 |
| Cadeira Inferior | R$ 1.468,80 |
| **Pista** | **R$ 1.700,00** |

Um teto por setor, e a meia responde ao mesmo — [pelo
assento](#meia-entrada-é-julgada-pelo-assento-não-pelo-desconto).

Para comparação, em 17/09/2026 o mercado pedia de **+120% a +1.194%** sobre a
face. O bot fica calado até isso mudar — e é justamente esse o ponto.

Uma observação que vale mais que a tabela: durante os testes apareceu uma Pista
de 28/10 a **R$ 2.750 (+120%), 25 unidades** — e ela **sumiu do site em cerca de
15 minutos**, com a Pista voltando aos R$ 4.178,90 de antes. Horas depois voltou,
com 24 unidades e um `c_anuncio` **diferente**. Numa terceira vez, sumiu em **2
minutos**. Não dá pra conferir duas vezes: é por isso que o bot checa a cada 30
segundos, que o alerta leva um link direto para o anúncio em vez da página do
evento, e que vale deixar o navegador logado.

Camarotes (Ídolos, Stadium, Enter Experience...) não têm face divulgada, então
não têm teto — e sem referência não há como dizer se o preço é justo. Ficam de
fora do `/status` e nunca disparam alerta.

## Meia-entrada é julgada pelo assento, não pelo desconto

Uma versão anterior comparava a meia com a **meia face** (R$ 340 numa
Arquibancada de R$ 680). Isso mede o quanto o vendedor está sendo ganancioso em
cima do que *ele* pagou — pergunta errada. Quem compra não está comprando o
custo do vendedor, está comprando **um assento**, e a meia senta você
exatamente no mesmo lugar que a inteira.

Na prática o modelo antigo tornava a meia inalertável: a +36%, o teto da
Arquibancada meia dava R$ 462,40, e ninguém revende a R$ 462. Ter
`Meia Estudante` no `CATEGORIES` só gerava ruído 📉 e nunca um 🎯.

Caso real de 31/10/2026, mesmo setor:

| Oferta | Preço | Julgada contra | Quanto é |
|---|---|---|---|
| Cadeira Superior **Meia Estudante** | R$ 2.970 | R$ 980 | **3,0×** |
| Cadeira Superior **Inteira** | R$ 5.059,99 | R$ 980 | **5,2×** |

Mesmo assento, R$ 2.000 de diferença. A meia é claramente a que se compra — e só
esse modelo consegue dizer isso. Por isso `config.face_value()` recebe só o
setor: a categoria não muda o que o assento vale.

O risco é real e está tratado **na mensagem, não na conta**: meia sem
comprovante na catraca é entrada negada, não desconto. Todo alerta de categoria
que não seja `Inteira` carrega esse aviso, e o `CATEGORIES` no `.env` deixa você
tirá-las de vez se ninguém do grupo tiver carteirinha. O padrão é
`Inteira,Meia Estudante`; o site também vende `Meia PCD`, `Meia Jovem Baixa
Renda` e `Acompanhante PCD`.

## Instalação

No Ubuntu, pule para [`install.sh`](#rodar-no-ubuntu-2404-o-jeito-de-verdade).
Isto aqui é para rodar à mão, no Mac ou para desenvolver:

```bash
git clone https://github.com/lopesandrey/bts-vigia.git
cd bts-vigia
./start.sh
```

O `start.sh` cria o venv, instala as dependências, e no Mac já embrulha tudo em
`caffeinate -s` — se a máquina dormir, o bot para e **ninguém do grupo recebe
nada**. Na primeira vez ele cria o `.env` e para, pedindo para você preencher:

1. Crie o bot no [@BotFather](https://t.me/BotFather) → copie o token para
   `BOT_TOKEN` no `.env`.
2. Crie um grupo no Telegram, adicione o bot e seus amigos.
3. Mande qualquer mensagem no grupo e rode `./start.sh --chat-id` →
   copie o `CHAT_ID` para o `.env` (grupo tem id negativo).
4. `./start.sh`

O `.env.example` já vem com `MAX_MARKUP=36`, o valor certo — e, portanto, o bot
começa calado. Para ver um alerta chegar na primeira checagem, ponha `240` por
uma rodada e depois mande `/agio 36` no grupo. O passo a passo completo do
Telegram está no [SETUP-TELEGRAM.md](SETUP-TELEGRAM.md).

## Comandos

| Comando | O que faz |
|---|---|
| `/status` | como está o preço agora, com link direto |
| `/agio 36` | muda o ágio máximo aceitável |
| `/checar` | força uma checagem agora |
| `/pausar`, `/voltar` | liga e desliga o monitoramento |
| `/ajuda` | lista os comandos |

Só respondem no grupo do `CHAT_ID` — ninguém de fora mexe na configuração.

O `/status` é escrito para as pessoas do grupo, não para quem mantém o bot:
o alvo aparece **em reais**, e o quanto um anúncio está caro aparece como
múltiplo (`3,1×`) em vez de percentual (`+208%`) — mesma informação, sem a conta
de cabeça. Ele escolhe pelo múltiplo, não pelo preço: o anúncio mais barato não é
o melhor negócio. E traz duas visões, porque só a primeira mostrava Pista três
vezes e nunca dizia quanto estava custando uma Arquibancada.

```
📊 Como está agora

🔕 Nada no preço ainda. Eu aviso na hora que chegar.
Meu alvo: Pista até R$ 2.125,00 (ela custava R$ 1.250,00).

MAIS BARATO DE CADA NOITE

28/10 qua — R$ 3.850,00 · Pista meia · 3,1× · 23 un.
https://buyticketbrasil.com/r?event=...&c_anuncio=1780326918818x...

30/10 sex — R$ 4.290,00 · Pista meia · 3,4× · 16 un.
https://buyticketbrasil.com/r?event=...&c_anuncio=1788903711752x...

31/10 sáb — R$ 4.400,00 · Pista meia · 3,5× · 8 un.
https://buyticketbrasil.com/r?event=...&c_anuncio=1789655932585x...

MAIS BARATO DE CADA SETOR (nas 3 noites)
Pista             R$ 3.850,00 · 3,1× · 28/10 qua  🎓
Cadeira Inferior  R$ 3.850,00 · 3,6× · 30/10 sex  🎓
Cadeira Superior  R$ 3.932,50 · 4,0× · 28/10 qua
Arquibancada      R$ 3.630,00 · 5,3× · 28/10 qua  🎓

🎓 = meia-entrada, leve o comprovante ou barram na catraca.
"3,1×" quer dizer: está custando 3,1 vezes o valor original.
Confiro a cada 30 s · /agio muda o alvo.
```

Camarote não aparece em nenhuma das duas: sem face divulgada não há referência,
então ele nunca dispara alerta.

## Três tipos de mensagem

**🎯 Preço justo** — algum setor caiu até o teto. É o alerta que importa.
Não repete: só volta a avisar se ficar ainda mais barato, ou se sair e voltar
para baixo do teto.

```
🎯 PREÇO JUSTO — Pista

R$ 1.690,00 · Inteira
📅 28/10/2026 (quarta)
📍 Estádio MorumBIS, São Paulo
🎫 2 disponíveis neste preço
1,4× o valor original de R$ 1.250,00.

https://buyticketbrasil.com/r?event=...&c_anuncio=1789585150454x...

⚡ O link abre esse anúncio. Fique logado no site para cair direto no checkout.
```

**📉 Novo mínimo histórico** — o mais barato de uma noite bateu recorde. Esse
traz a comparação com o valor original: "menor preço até agora" sem contexto
soa como oportunidade mesmo quando ainda é um assalto.

**💓 Batimento diário** — uma linha por noite, uma vez por dia. Existe porque um
bot que fica seis meses calado é indistinguível de um bot morto.

**🐢 Freio** — o site pediu pra ir mais devagar (HTTP 403/429/503). O bot para de
checar por 10 minutos sozinho e avisa. Volta ao normal depois, e se acontecer de
novo em outro dia ele avisa de novo.

E se ele ficar **10 minutos sem conseguir ler** o site, reclama no grupo. Bots de
scraping morrem em silêncio; esse não.

## Vigia de tempo real (a cada 30 segundos)

Não existe WebSocket nem API pública no buyticketbrasil, e a página vem com
`cache-control: no-store` sem ETag nem `Last-Modified` — ou seja, **não há canal
de push e nem GET condicional**. "Tempo real" aqui só pode ser polling curto.

Então é polling curto, feito com cuidado:

| | |
|---|---|
| **Intervalo** | 30 s (`INTERVAL_SECONDS` no `.env`) |
| **Jitter** | +0 a 6 s aleatórios, pra não bater no site num metrônomo perfeito |
| **Por rodada** | 3 requisições (uma por noite), em paralelo, ~0,6 s no total |
| **Payload** | o RSC do Next.js, 18,7 KB em vez dos 31 KB do HTML — 40% menos |
| **Freio** | 403/429/503 → 10 minutos parado, automático |
| **Teto de rodada** | 20 s; passou disso, a rodada é abortada em vez de travar |

Por que 30 s e não 5: medi o site em 17/09/2026 com 60 requisições em 7 minutos
a cada 20 s — **60/60 deram HTTP 200**, média de 0,52 s, pior caso 0,73 s, sem
nenhuma degradação e sem cabeçalho de rate limit. 30 s é metade desse ritmo já
comprovado. Dá pra descer até 15 s se quiser; o mínimo aceito é 10, porque **IP
bloqueado é cegueira permanente** — muito pior do que chegar 30 segundos atrasado.

A janela de perda caiu de 10 minutos para ~30 segundos. Não é zero: se um
anúncio bom aparecer e sumir em 20 segundos, o bot perde. Mas o caso real que
vimos três vezes durou de 2 a 15 minutos.

## O link é o que importa

Cada oferta na matriz do site vem com um `id_ref`, que é exatamente o
`c_anuncio` do botão "Comprar" da página. Então o alerta leva ao **anúncio
específico** que disparou o aviso, não à página do evento onde você teria que
escolher setor e categoria de novo. Custa zero requisição extra: o dado já vem
na mesma leitura.

Uma ressalva prática: se você não estiver logado, o link passa primeiro pela
tela de login. **Deixe o navegador do celular logado no buyticketbrasil** — são
30 segundos que decidem quem leva o ingresso.

Se por acaso o `id_ref` vier vazio, o link cai na página da sessão. Melhor um
link genérico que um link quebrado.

## Arquivos

| Arquivo | Papel |
|---|---|
| `monitor.py` | **único** arquivo que conhece o buyticketbrasil. Só biblioteca padrão. |
| `config.py` | `.env`, sessões, valores de face, cálculo do teto |
| `bot.py` | Telegram, agendamento, regras de alerta |
| `chat_id.py` | utilitário de configuração, roda uma vez |
| `start.sh` | roda na sua máquina: cria o venv, instala e liga (com `caffeinate` no Mac) |
| `install.sh` | instala como serviço no Ubuntu 24.04; idempotente |
| `state.json` | recordes e alertas já enviados (criado sozinho) |

O código é todo em inglês — identificadores, comentários e docstrings. O
português fica só onde uma pessoa lê: as mensagens do Telegram, os erros de
configuração e estes `.md`.

## Monitorar outro site

Escreva um módulo com a mesma assinatura de `monitor.fetch`:

```python
def fetch(url: str, session: str = "") -> list[Offer]: ...
```

`bot.py` não conhece HTML nem o buyticketbrasil — só `Offer`. Trocar a fonte
é trocar um import.

## Rodar no Ubuntu 24.04 (o jeito de verdade)

No Mac, se a máquina dormir o bot para e ninguém do grupo recebe nada. No Linux
ele vira um serviço que sobe no boot e se reergue sozinho.

```bash
git clone https://github.com/lopesandrey/bts-vigia.git
cd bts-vigia
sudo ./install.sh
```

O script é idempotente — pode rodar de novo pra atualizar, que ele não duplica
nada nem sobrescreve o seu `.env`. Ele:

1. instala `python3-venv` (no 24.04 o `pip` se recusa a mexer no Python do
   sistema — [PEP 668](https://peps.python.org/pep-0668/) — então o venv não é
   opcional, e vem em pacote separado do `python3`);
2. cria o usuário de serviço `bts`, sem senha e sem shell de login;
3. copia o código para `/opt/bts-vigia`, cria o venv e instala as dependências;
4. instala o `.env` em `0600` (o token não pode ser legível por outros
   usuários) — se você já preencheu um `.env` na pasta do projeto, ele usa
   **esse**; um `.env` que já exista em `/opt/bts-vigia` nunca é sobrescrito;
5. instala e habilita o serviço `bts-vigia`.

Se você já tinha preenchido o `.env` antes de rodar o script, ele sobe e começa
a vigiar direto. Se não, ele para antes de ligar e pede:

```bash
sudo nano /opt/bts-vigia/.env        # BOT_TOKEN e CHAT_ID
sudo -u bts /opt/bts-vigia/.venv/bin/python /opt/bts-vigia/chat_id.py   # acha o CHAT_ID
sudo systemctl start bts-vigia
journalctl -u bts-vigia -f
```

### Três detalhes que fazem diferença

O `install.sh` já cuida de todos, mas vale saber por que estão lá:

**`RestartPreventExitStatus=78`** — com `Restart=always` puro, um `.env` errado
faz o bot morrer, systemd reiniciar 30 s depois, morrer de novo, para sempre, e
o `journalctl` vira uma parede da mesma mensagem. O `config.py` sai com o código
78 (`EX_CONFIG`) quando a configuração está inválida, e essa linha diz ao systemd
que 78 significa "não adianta tentar de novo". Aí o serviço **para**, com o erro
como última linha do log — que é onde você vai procurar.

**`Wants=network-online.target`** — `After=` sozinho só ordena os serviços *se*
alguém mais tiver puxado o `network-online.target`. Sem o `Wants=`, no boot o bot
sobe antes da rede existir.

**O token não vai para o journal** — se o Telegram recusa o token, a biblioteca
do Telegram monta a mensagem ``The token `<seu token>` was rejected`` e loga o
traceback inteiro. Sob systemd isso vai parar no journal, em disco, a cada
reinício. O `bot.py` instala um formatter que apaga o token de qualquer linha de
log antes dela ser escrita — no lugar dele sai `«TOKEN OCULTO»`.

### Atualizar depois

```bash
cd bts-vigia && git pull && sudo ./install.sh
```

O `.env` e o `state.json` (recordes e alertas já enviados) ficam em
`/opt/bts-vigia` e não são tocados.

## Limites, ditos com clareza

- Lê a página pública a cada 30 segundos, 3 requisições por rodada. Não compra,
  não reserva, não fura fila, não burla proteção nenhuma. É um alerta de preço.
- Raspar o site contraria os termos de uso do buyticketbrasil. A consequência
  prática de exagerar é ter o IP bloqueado. Não baixe o `INTERVAL_SECONDS`
  abaixo de 15 — e o mínimo aceito é 10.
- Os valores de face estão fixos em `config.py`, tirados do anúncio da
  produção. Se estiverem errados, todos os tetos saem errados.
- Sem histórico e sem gráfico: guarda só o mínimo por noite. Era o suficiente.
