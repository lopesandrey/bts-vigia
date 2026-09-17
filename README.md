# bts-vigia

Vigia os ingressos do BTS (*Arirang*, Estádio do Morumbis, 28/30/31 de outubro
de 2026) no buyticketbrasil e avisa num grupo do Telegram quando o preço cai
para um nível honesto.

Um processo, seis arquivos, uma dependência. Checa a cada 30 segundos.

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

| Setor | Inteira | Meia |
|---|---|---|
| Arquibancada | R$ 924,80 | R$ 462,40 |
| Cadeira Superior | R$ 1.332,80 | R$ 666,40 |
| Cadeira Inferior | R$ 1.468,80 | R$ 734,40 |
| **Pista** | **R$ 1.700,00** | **R$ 850,00** |

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
não têm teto: aparecem no `/status` mas nunca disparam alerta de preço justo.

## Meia-entrada tem teto próprio

A vitrine do site mostra o menor preço do setor, e esse menor preço costuma ser
uma meia. Comparar uma meia com a face da inteira dobraria o teto — e o bot
alertaria a pior oferta achando que é a melhor. Caso real de 31/10/2026:

| Oferta | Preço | Face certa | Ágio |
|---|---|---|---|
| Cadeira Superior **Meia Estudante** | R$ 2.970 | R$ 490 | **+506%** |
| Cadeira Superior **Inteira** | R$ 5.059,99 | R$ 980 | **+416%** |

A meia é o número menor e o negócio pior. Por isso `config.face_value()` usa
metade da face para toda categoria que não seja `Inteira` — a razão é exata em
todos os setores, segundo a tabela da produção.

Quais categorias vigiar fica em `CATEGORIES` no `.env`. **Só liste o que você
consegue comprovar na catraca** — meia sem comprovante é entrada negada, não
desconto. O padrão é `Inteira,Meia Estudante`; o site também vende `Meia PCD`,
`Meia Jovem Baixa Renda` e `Acompanhante PCD`.

## Instalação

No Ubuntu, pule para [`install.sh`](#rodar-no-ubuntu-2404-o-jeito-de-verdade).
Isto aqui é para rodar à mão, no Mac ou para desenvolver:

```bash
git clone https://github.com/lopesandrey/bts-vigia.git
cd bts-vigia
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

1. Crie o bot no [@BotFather](https://t.me/BotFather) → copie o token para
   `BOT_TOKEN` no `.env`.
2. Crie um grupo no Telegram, adicione o bot e seus amigos.
3. Mande qualquer mensagem no grupo e rode `.venv/bin/python chat_id.py` →
   copie o `CHAT_ID` para o `.env` (grupo tem id negativo).
4. `caffeinate -s .venv/bin/python bot.py`

O `caffeinate -s` é obrigatório no Mac: se a máquina dormir, o bot para e
**ninguém do grupo recebe nada**. É justamente por isso que o lugar dele é num
Linux ligado direto.

O `.env.example` já vem com `MAX_MARKUP=36`, o valor certo — e, portanto, o bot
começa calado. Para ver um alerta chegar na primeira checagem, ponha `240` por
uma rodada e depois mande `/agio 36` no grupo. O passo a passo completo do
Telegram está no [SETUP-TELEGRAM.md](SETUP-TELEGRAM.md).

## Comandos

| Comando | O que faz |
|---|---|
| `/status` | o melhor negócio de cada noite, com link direto |
| `/agio 36` | muda o ágio máximo aceitável |
| `/checar` | força uma checagem agora |
| `/pausar`, `/voltar` | liga e desliga o monitoramento |
| `/ajuda` | lista os comandos |

Só respondem no grupo do `CHAT_ID` — ninguém de fora mexe na configuração.

O `/status` mostra **uma linha por noite**, não a lista inteira, e escolhe pelo
**ágio**, não pelo preço. A diferença não é cosmética: em 28/10 o anúncio mais
barato era uma Arquibancada meia a R$ 3.630 (**+968%**) enquanto a Pista estava
a R$ 4.178,90 (**+234%**). O ingresso mais caro era o negócio muito melhor — e é
o que vai bater o teto primeiro. Camarote não aparece: sem face divulgada não há
ágio, então ele nunca dispara alerta.

```
📊 Melhor de cada noite · teto +240% (Pista R$ 4.250,00)

28/10 qua — R$ 4.178,90 · Pista · +234% · 22 un.
https://buyticketbrasil.com/r?event=...&c_anuncio=1789585150454x...

30/10 sex — R$ 4.290,00 · Pista · +243% · 21 un.
https://buyticketbrasil.com/r?event=...&c_anuncio=1777993308535x...

31/10 sáb — R$ 4.840,00 · Pista · +287% · 10 un.
https://buyticketbrasil.com/r?event=...&c_anuncio=1787514967110x...

Checando a cada 30 s · /agio muda o teto.
```

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

https://buyticketbrasil.com/r?event=...&c_anuncio=1789585150454x...

⚡ O link abre esse anúncio. Fique logado no site para cair direto no checkout.
```

**📉 Novo mínimo histórico** — o mais barato de uma noite bateu recorde. Esse
traz uma linha de ágio: "menor preço até agora" sem contexto soa como
oportunidade mesmo quando ainda é um assalto.

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
