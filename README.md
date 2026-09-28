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
| `/status` | o melhor de cada setor, noite por noite, com link |
| `/agio 36` | muda o ágio máximo aceitável |
| `/agio env` | descarta esse ajuste e volta ao `MAX_MARKUP` do `.env` |
| `/checar` | força uma checagem agora |
| `/comprar` | reserva agora o melhor que estiver dentro do teto |
| `/desarmar`, `/armar` | para e volta a comprar sozinho |
| `/pausar`, `/voltar` | liga e desliga o monitoramento |
| `/ajuda` | lista os comandos |

Só respondem no grupo do `CHAT_ID` — ninguém de fora mexe na configuração.

Para registrar isso no menu do Telegram, mande `/setcommands` para o
[@BotFather](https://t.me/BotFather), escolha o bot e cole:

```
status - o melhor de cada setor, noite por noite, com link
agio - muda o ágio máximo que eu aceito. ex: /agio 36 ou /agio env
checar - checa as três noites agora
comprar - reserva agora o melhor que estiver no teto
desarmar - para de comprar sozinho
armar - volta a comprar sozinho
pausar - para de vigiar e de avisar
voltar - volta a vigiar
ajuda - o que eu faço e como eu aviso
```

### `/agio` manda mais que o `.env`, e a mensagem diz isso

Um `/agio 50` mora no `state.json` e **sobrevive a todo restart**: o override só
é descartado quando o valor do `.env` *muda*, então editar o `.env` para o valor
que ele já tinha não faz nada. Isso custou uma manhã de confusão — o `.env` dizia
36 (Pista a R$ 1.700) e o grupo recebia alertas até R$ 1.875, que é 50. Os dois
números estavam certos; o que faltava era alguém dizer qual estava valendo.

Agora todo lugar que mostra o alvo (boot, `/status`, `/agio`) diz de onde veio o
número quando ele não é o do `.env`, `/agio env` desfaz, e o boot registra no
journal `in force this boot: ... ágio 50% (.env says 36%)`.

Vale para o intervalo também, de outra forma: a mensagem de boot mostra o
`INTERVAL_SECONDS` **daquele** boot e fica parada no chat para sempre. Se você
editar o `.env` depois, ela não muda — `/status` é a resposta de agora.

## Comprar é o normal, não um modo

O bot **compra sozinho**, sem ninguém pedir e sem ninguém estar olhando o
celular: assim que um anúncio bate o teto ele reserva e manda o Pix no grupo.
Isso é o comportamento padrão e é o ponto do projeto — a corrida por um anúncio
barato é ganha na reserva, e uma mensagem que espera resposta perde a corrida.

**Ele nunca paga.** Quem paga é você, com o código que ele manda no grupo.

Ele para de comprar em quatro casos, e só volta com `/armar`:

| Para porque | Por que insistir seria pior |
|---|---|
| gerou um Pix | o site aceita um pedido em aberto por conta; um segundo Pix é o erro que um comprador automático não pode cometer |
| já tem reserva ou pedido na conta (`PENDING`) | toda tentativa nova bateria na mesma regra |
| falta coisa no `.env` (`SETUP`) | tentar de novo a cada 30 s não conserta configuração |
| alguém mandou `/desarmar` | é uma pessoa decidindo |

Perder uma corrida **não** é motivo para parar: ele risca aquele anúncio e segue
caçando os outros. Cada anúncio tem três tentativas (`bot.STRIKES_OUT`), o que
é o que impede o bot de ficar repetindo a mesma compra impossível — e um anúncio
sem comprovante ou já vendido gasta as três de uma vez, porque isso não muda.

Antes de reservar qualquer coisa ele confere, nessa ordem
(`checkout._vet_listing`):

| Trava | Por que |
|---|---|
| preço lido do site, não do alerta | o alerta é a lembrança de uma página que já pode ter mudado |
| o preço não subiu desde o alerta | se subiu, para sem reservar |
| `status == "Active"` | "LockedForSale" significa que alguém já está com ele |
| **tem comprovante do vendedor** | sem ele ninguém consegue conferir de quem é o ingresso |
| teto do setor e teto absoluto | conferidos de novo aqui, não só no alerta |

A trava do comprovante existe porque **anúncio sem comprovante é coisa que
existe de verdade**: dos 16 no ar em 20/09/2026, dois não tinham nenhum — um
deles Pista Inteira. A API não entrega o **nome** do vendedor para o comprador,
em endpoint nenhum: só um `sellerId` opaco e o `proofUrl`, que é o comprovante
de compra que o vendedor subiu e o que o site mostra na página do anúncio. Então
o que dá para exigir é o comprovante, e é o que o bot exige. Ele recusa, avisa no
grupo com o link para você mesmo olhar, e continua caçando os outros.

As mesmas duas checagens acontecem **antes do alerta**, não só antes da compra
(`checkout.inspect`, chamada por `bot.worth_alerting`). O motivo tem data: em
20/09/2026 um Pista Inteira apareceu a R$ 650 — **abaixo** do valor original de
R$ 1.250 — e o grupo recebeu o alerta; quando alguém abriu o link o anúncio
estava `LockedForSale`, travado no mesmo segundo, com `proofUrl: null` e criado
dezoito dias antes. Da lista pública, que só traz preço e id, esse anúncio é
indistinguível da pechincha do ano. Um alerta em que ninguém consegue agir gasta
a única coisa que o bot tem, que é a pessoa acreditar no alerta seguinte.

### Sempre logado

Um job separado (`bot.keep_login`, a cada 10 min) mantém a sessão do site viva
com uma leitura autenticada. Duas razões, e a segunda é a que importa: a compra
não gasta o primeiro segundo fazendo login, e uma senha trocada ou uma conta
bloqueada aparece **enquanto nada está acontecendo**, num aviso 🔑 no grupo, em
vez de aparecer no meio de uma corrida disfarçada de corrida perdida. É dito uma
vez por mudança de estado, não uma vez por rodada.

Todas as mensagens de compra carregam o **id do anúncio**, em bloco de copiar
com um toque: o link 404 depois que o anúncio sai, o preço muda, o setor o site
pode renomear — o id é a única coisa com que o suporte, a página do pedido e os
registros do site concordam.

O `/status` é escrito para as pessoas do grupo, não para quem mantém o bot.
Ele é agrupado **por noite**, e dentro de cada noite traz **o melhor de cada
setor, com o link daquele anúncio** — essa é a ordem em que a decisão é tomada:
primeiro a data em que dá para ir, depois o melhor assento que cabe no bolso.

Duas versões anteriores erravam nisso. A primeira ranqueava o mercado inteiro e
mostrava Pista três vezes, uma por noite, sem nunca dizer quanto estava custando
uma Arquibancada. A segunda colou por cima um bloco por setor cobrindo as três
noites juntas: respondia "como está o mercado", mas não "o que eu compro para o
dia 30", e não tinha link nenhum — a única linha que valia agir era a única em
que não dava para clicar.

O alvo aparece **em reais e para os quatro setores**. O bot sempre vigiou os
quatro, cada um contra o teto dele (`is_fair()` lê `price_cap(o.sector)`), mas um
status que anunciava só a Pista dava a entender que os outros três eram
ignorados. E o quanto um anúncio está caro aparece como múltiplo (`3,1×`) em vez
de percentual (`+208%`): mesma informação, sem a conta de cabeça.

```
📊 Como está agora

🔕 Nada no preço ainda. Aviso na hora que chegar.

🎯 MEU ALVO — em qualquer um dos 4 setores:
Arquibancada       R$   925
Cadeira Superior   R$ 1.333
Cadeira Inferior   R$ 1.469
Pista              R$ 1.700

🗓 28/10 qua
Pista · R$ 3.850 · 3,1× · 23un 🎓 · comprar
Cadeira Superior · R$ 3.932 · 4,0× · 16un · comprar
Arquibancada · R$ 3.190 · 4,7× · 9un 🎓 · comprar
Cadeira Inferior · R$ 5.500 · 5,1× · 1un 🎓 · comprar

🗓 30/10 sex
Pista · R$ 4.290 · 3,4× · 16un 🎓 · comprar
Cadeira Inferior · R$ 3.850 · 3,6× · 1un 🎓 · comprar
Cadeira Superior · R$ 4.125 · 4,2× · 8un · comprar
Arquibancada · R$ 4.400 · 6,5× · 4un 🎓 · comprar

🗓 31/10 sáb
Pista · R$ 4.400 · 3,5× · 8un 🎓 · comprar
Cadeira Superior · R$ 5.060 · 5,2× · 9un · comprar
Cadeira Inferior · R$ 6.050 · 5,6× · 1un · comprar
Arquibancada · R$ 4.400 · 6,5× · 1un · comprar

🎓 = meia-entrada: leve o comprovante ou barram na catraca.
3,1× = está custando 3,1 vezes o valor original.
Confiro a cada 30 s · /agio muda o alvo.
```

Cada **comprar** é um link para aquele anúncio. São doze links: em URL crua a
mensagem vira uma parede de endereço percent-encoded que esconde os preços a que
pertencem, então eles vão como link clicável (HTML do Telegram). Os centavos
saem: aqui os preços são comparados entre si, não pagos, e a quatro mil reais os
centavos são dois caracteres de ruído por linha. Os alertas mantêm os centavos —
lá o número é o que você vai gastar.

Camarote não aparece: sem face divulgada não há referência, então ele nunca
dispara alerta.

## As mensagens

Um glifo por tipo de mensagem, sempre o mesmo, sempre na primeira linha, e um
título em negrito logo em seguida. Nada de emoji como rótulo de campo: as linhas
`📅 data` / `📍 local` / `🎫 quantidade` saíram, porque disputavam atenção com o
glifo que diz de que mensagem se trata. O único emoji que sobrou dentro do corpo
é o 🎓, e ele não é rótulo — é aviso.

Toda mensagem termina dizendo o que fazer, ou o que vai acontecer sozinho.

**🎯 Bateu o alvo** — o único alerta que pede ação. Não repete: só volta a avisar
se ficar ainda mais barato, ou se sair e voltar para baixo do teto.

```
🎯 ESSE É O PREÇO — Pista

R$ 1.690,00 · Meia Estudante
28/10/2026 (quarta) · 2 disponíveis neste preço
1,4× o valor original de R$ 1.250,00 — eu avisaria até R$ 1.700,00.

🎓 Meia Estudante: leve o comprovante, senão a entrada é negada.

https://buyticketbrasil.com/r?event=...&c_anuncio=1789585150454x...

⚡ O link abre esse anúncio. Fique logado no site para cair direto no checkout.
```

**📉 Caiu, mas ainda não é o preço alvo** — o mais barato de uma noite bateu recorde.
O título carrega a correção inteira. `MENOR PREÇO ATÉ AGORA` era verdade e ainda
assim enganava: tem a forma de boa notícia, então um anúncio a 4,7× da face lia
como oportunidade quando era um assalto que ficou marginalmente mais barato. O
título agora diz as duas metades de uma vez — caiu, e não é o alvo —, o corpo
nomeia a distância **em reais** (porque "4,7× o original" só significa algo ao
lado do número que o bot está esperando), e o link vai por último rotulado como
olhar, não comprar.

```
📉 Caiu de preço — mas ainda não é o preço alvo

Arquibancada · R$ 3.190,00 · Meia Estudante
31/10/2026 (sábado) · 3 disponíveis
Era R$ 3.300,00. É a mínima que já vi para essa noite.

Ainda está 4,7× o valor original de R$ 680,00, e eu só considero justo
até R$ 1.224,00.
Nada bateu o alvo ainda. Aviso na hora que bater.

🎓 Meia Estudante: leve o comprovante, senão a entrada é negada.

ver o anúncio — sem pressa, ainda não é o preço alvo.
```

**💓 Batimento diário** — uma linha por noite, uma vez por dia. Existe porque um
bot que fica seis meses calado é indistinguível de um bot morto. Ordena pelo
múltiplo, não pelos reais: por preço absoluto ele nomearia sempre a
Arquibancada, que tem a face mais baixa e por isso ganha em reais sendo a pior
compra do lote.

**⚠️ Estou cego** — 10 minutos sem conseguir ler o site. Diz explicitamente que
**o silêncio não é confiável agora**, o que é o ponto todo: sem essa frase a
mensagem informa uma falha e deixa o leitor concluir a coisa errada. Bots de
scraping morrem em silêncio; esse não.

**🐢 Indo mais devagar** — o site pediu calma (HTTP 403/429/503). O bot espaça as
checagens por 10 minutos sozinho e explica por quê: insistir é o jeito mais
rápido de tomar bloqueio de IP, e aí a cegueira é permanente.

**✅ Voltei a ver o site** — fecha o ⚠️. Um aviso de falha sem o aviso de volta
deixa o grupo achando que o bot ainda está quebrado.

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
| `envfile.py` | como uma linha do `.env` é lida — um lugar só, para o bot e os scripts |
| `start.sh` | roda na sua máquina: cria o venv, instala e liga (com `caffeinate` no Mac) |
| `install.sh` | instala como serviço no Ubuntu 24.04, **sempre do zero** |
| `stop.sh` | para o serviço e apaga a instalação; no Mac, só para o processo |
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

**Toda vez que roda, ele instala do zero.** Não existe "atualizar" separado,
então não tem atualização pela metade. Ele:

1. instala `python3-venv` (no 24.04 o `pip` se recusa a mexer no Python do
   sistema — [PEP 668](https://peps.python.org/pep-0668/) — então o venv não é
   opcional, e vem em pacote separado do `python3`);
2. confere o `.env` da pasta do projeto **antes de parar qualquer coisa**: se
   tiver erro, ele para ali e o bot que já estava rodando continua rodando;
3. roda o `stop.sh`: para o serviço, derruba qualquer bot deste projeto que tenha
   ficado rodando e apaga `/opt/bts-vigia` inteiro;
4. copia o código para `/opt/bts-vigia`, cria um venv novo e instala as
   dependências;
5. copia o `.env` da pasta do projeto para lá, em `0600` (o token não pode ser
   legível por outros usuários);
6. instala, habilita e liga o serviço `bts-vigia`, e no fim diz se ele subiu, com
   que configuração, e se a compra está pronta.

Se não tiver `.env` na pasta do projeto, ele cria um a partir do exemplo e para
antes de mexer em qualquer coisa, dizendo o que preencher.

### O `.env` é um só: o da pasta do projeto

Esse é o arquivo que você edita. O `/opt/bts-vigia/.env` é uma cópia que o
`install.sh` refaz a cada rodada. Editar a cópia não adianta: a próxima
instalação apaga, e a primeira linha dela avisa isso.

```bash
nano ~/bts-vigia/.env     # mudar qualquer coisa...
sudo ./install.sh         # ...e aplicar (reiniciar sozinho não relê a pasta do projeto)
```

Isso existe porque já deu errado. O `install.sh` antigo mantinha o
`/opt/bts-vigia/.env` para sempre. Quando o bot passou a comprar, `CPF`, `PASS` e
o resto foram preenchidos no `.env` da pasta do projeto e nunca chegaram ao
serviço. Toda compra respondia "falta preencher" sobre valores que estavam lá. Na
primeira rodada do `install.sh` novo, o que só existia no `.env` antigo é passado
para o da pasta do projeto antes de ele ser apagado. Nada se perde, e ele diz
quais chaves passou (só os nomes, nunca os valores).

As mensagens do bot também apontam o arquivo certo: `falta preencher CPF, PASS:
edite /home/voce/bts-vigia/.env e rode sudo ./install.sh`.

### Parar

```bash
sudo ./stop.sh
```

Para de vez, não é pausa: o serviço sai do boot, e `/opt/bts-vigia` é apagado,
com a cópia do `.env`, o login salvo, o `state.json` e o venv. O seu `.env` da
pasta do projeto fica. Para voltar: `sudo ./install.sh`. No Mac, o `./stop.sh`
só derruba o bot que o `./start.sh` ligou.

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

### Um token, um bot

Se o serviço estiver no ar no servidor e você rodar `./start.sh` no seu
computador, os dois brigam pelo mesmo token e o Telegram rejeita um deles com
`Conflict: terminated by other getUpdates request`. Pare um dos dois:

```bash
sudo systemctl stop bts-vigia     # no servidor, para testar no seu computador
sudo systemctl start bts-vigia    # e religar quando terminar
```

O `install.sh` e o `stop.sh` já derrubam, sozinhos, um bot deste projeto que
tenha ficado rodando no servidor, um `./start.sh` esquecido num terminal, por
exemplo.

O bot avisa isso em **uma linha** e continua tentando — assim que o outro parar,
ele assume sozinho, sem precisar reiniciar.

### Atualizar depois

```bash
cd bts-vigia && git pull && sudo ./install.sh
```

É a mesma instalação do zero. O `state.json` começa vazio, e isso é de
propósito: some junto qualquer `/agio` antigo e qualquer "parei de comprar"
gravado, então não precisa de `/armar` depois. O primeiro ciclo não manda
recorde nem "estou vivo", só alerta de verdade. O login é refeito sozinho em
20 segundos.

## Limites, ditos com clareza

- Lê a API pública a cada `INTERVAL_SECONDS` (30 por padrão), 12 requisições
  pequenas por rodada. Quando o preço bate o teto, reserva e gera o Pix, mas
  **nunca paga**: quem paga é você. Não fura fila e não burla proteção nenhuma.
- Raspar o site contraria os termos de uso do buyticketbrasil. A consequência
  prática de exagerar é ter o IP bloqueado. Não baixe o `INTERVAL_SECONDS`
  abaixo de 15 — e o mínimo aceito é 10.
- Os valores de face estão fixos em `config.py`, tirados do anúncio da
  produção. Se estiverem errados, todos os tetos saem errados.
- Sem histórico e sem gráfico: guarda só o mínimo por noite. Era o suficiente.
