# Passo a passo: a parte do Telegram

Do zero até o primeiro alerta. Faça na ordem — o passo 4 é o que mais quebra.

---

## 1. Criar o bot

No Telegram, procure **@BotFather** e comece a conversa.

Mande:

```
/newbot
```

Ele vai pedir duas coisas, nesta ordem:

| Ele pergunta | Você responde | Regra |
|---|---|---|
| *"Alright, a new bot. How are we going to call it?"* | `BTS Vigia` | nome que aparece na tela, livre |
| *"Now let's choose a username"* | `bts_vigia_bot` | **tem que terminar em `bot`** e ser único no mundo |

Se o username já existir, ele reclama e você tenta outro (`bts_vigia_2026_bot`,
`vigia_ingresso_bts_bot`...).

No fim ele responde com uma linha assim:

```
Use this token to access the HTTP API:
8123456789:AAxx-EXEMPLO-troque-pelo-seu-token-xx
```

**Esse é o `BOT_TOKEN`.** Copie inteiro, com os dois-pontos.

> ⚠️ Esse token é a senha do bot. Quem tiver ele controla o bot. Não mande em
> print, não commite. O `.gitignore` já protege o `.env`.

---

## 2. Guardar o token

```bash
cd bts-vigia
cp .env.example .env
```

(Se preferir, é só rodar `./start.sh` — ele cria o `.env` sozinho e pede pra
você preencher.)

Abra o `.env` e cole na linha do `BOT_TOKEN`:

```
BOT_TOKEN=8123456789:AAxx-EXEMPLO-troque-pelo-seu-token-xx
```

Deixe o `CHAT_ID` como está — vamos preencher no passo 5.

---

## 3. Criar o grupo e colocar o bot dentro

1. Telegram → menu → **Novo Grupo**
2. Adicione seus amigos (precisa de pelo menos 1 pessoa pra criar)
3. Nome do grupo: `BTS Vigia` (ou o que quiser)
4. Grupo criado → toque no **nome do grupo** no topo → **Adicionar membros**
   → procure `bts_vigia_bot` → adicione

Agora o bot está no grupo, mas ainda não sabe que existe.

---

## 4. Deixar o bot ver as mensagens ← *o passo que quebra*

Por padrão, um bot em grupo é **cego**: ele só recebe mensagens que sejam
comandos endereçados a ele. Se você mandar "oi" no grupo, o bot não vê nada — e
o `chat_id.py` não acha nada.

**Escolha um dos dois:**

### Opção A — mais rápida (recomendada)

Mande **no grupo** esta mensagem, com o `@` e o username do seu bot:

```
/start@bts_vigia_bot
```

Isso é um comando endereçado, então atravessa a cegueira. Pronto.

### Opção B — desligar o modo privacidade de vez

No **@BotFather**:

```
/setprivacy
```

→ escolha `@bts_vigia_bot` → clique em **Disable**

O bot passa a ver todas as mensagens do grupo. Funciona, mas é mais permissão
do que ele precisa. A opção A basta.

---

## 5. Descobrir o CHAT_ID

No terminal:

```bash
cd bts-vigia
./start.sh --chat-id
```

Saída esperada:

```
Chats encontrados — copie o id do seu grupo para o .env:

  CHAT_ID=-1002345678901    (supergroup · BTS Vigia)
```

Cole no `.env`:

```
CHAT_ID=-1002345678901
```

**Grupo tem id negativo.** Se vier positivo, você pegou a sua conversa privada
com o bot, não o grupo.

### Se der "Nenhuma mensagem encontrada"

Volte ao passo 4 — é sempre isso. Mande `/start@seu_bot` **dentro do grupo** e
rode de novo.

### Se der "O Telegram recusou"

O token está errado ou incompleto. Confira que copiou até o último caractere.

---

## 6. Botar o menu de comandos (opcional, mas fica bonito)

No **@BotFather**:

```
/setcommands
```

→ escolha o bot → cole este bloco inteiro, de uma vez:

```
status - Como está o preço agora
agio - Define o ágio máximo aceitável (%)
checar - Força uma checagem agora
pausar - Para de monitorar
voltar - Volta a monitorar
ajuda - Lista os comandos
```

Agora quem digitar `/` no grupo vê a lista pronta.

---

## 7. Ligar (em modo teste)

O `.env.example` já vem com o valor **certo** (`MAX_MARKUP=36`), e com ele o bot
começa calado — que é o correto, mas não prova nada. Só para esta primeira vez,
troque no `.env`:

```
MAX_MARKUP=240
```

Isso é folgado o bastante para pegar a oferta mais barata de hoje, então o
alerta dispara na primeira checagem. Agora rode:

```bash
cd bts-vigia
./start.sh
```

No grupo deve chegar, em segundos:

> 🤖 No ar. Vigiando as três noites do BTS no Morumbis.
> Ágio máximo: +240% · checagem a cada 30 s.
>
> /ajuda para os comandos.

E cinco segundos depois, o alerta de teste — algo assim (os números vão ser
outros; o estoque muda de hora em hora):

> 🎯 PREÇO JUSTO — Pista
>
> R$ 2.750,00 · Inteira
> 📅 28/10/2026 (quarta)
> 📍 Estádio MorumBIS, São Paulo
> 🎫 24 disponíveis neste preço
>
> https://buyticketbrasil.com/r?event=...&c_anuncio=1789654191825x...
>
> ⚡ O link abre esse anúncio. Fique logado no site para cair direto no checkout.

Se chegaram os dois, **está funcionando de ponta a ponta.**

Toque no link: ele abre **aquele anúncio específico**, não a página do evento.

No terminal você vê o log; `Ctrl-C` para parar.

---

## 7.1 Deixe o navegador do celular logado ← *faz diferença na hora H*

O link do alerta é o mesmo que o botão "Comprar" do site usa. Se você **já
estiver logado** no buyticketbrasil, ele cai direto no ingresso. Se não
estiver, passa primeiro pela tela de login — e são uns 30 segundos que podem
custar o ingresso.

Então, uma vez só: abra o buyticketbrasil no navegador do celular, faça login e
marque "Lembrar de mim". Deixe assim.

---

## 8. Testar os comandos

No grupo, mande:

| Comando | O que esperar |
|---|---|
| `/status` | como está o preço agora: o mais barato de cada noite e de cada setor, com link |
| `/checar` | "Checando as três noites..." e depois "Pronto." |
| `/agio` | o ágio atual e os tetos que ele gera, inteira e meia lado a lado |

---

## 9. Trocar o teste pelo valor real

Isso é o que faltava. No `.env`:

```
MAX_MARKUP=36
```

Reinicie o bot (`Ctrl-C` e rodar de novo). Ou, sem reiniciar, mande no grupo:

```
/agio 36
```

A partir daí os tetos passam a ser:

| Setor | Teto |
|---|---|
| Arquibancada | R$ 924,80 |
| Cadeira Superior | R$ 1.332,80 |
| Cadeira Inferior | R$ 1.468,80 |
| **Pista** | **R$ 1.700,00** |

Um teto por setor. A meia responde ao **mesmo** teto da inteira — ela te põe no
mesmo assento, então vale o mesmo.

O bot vai **ficar calado** — o mercado hoje pede de +120% a +1.194%. Isso é o
comportamento correto. Você só vai ouvir dele quando:

- 🎯 algum setor cair até o teto (o alerta que importa),
- 📉 alguma noite bater um novo mínimo histórico,
- 💓 passar um dia — aí vem uma linha por noite, só pra provar que está vivo, ou
- 🐢/⚠️ o site reclamar do ritmo, ou ele ficar 10 minutos sem conseguir ler.

### De quanto em quanto tempo ele checa

A cada **30 segundos** — anúncios bons somem em minutos, então isso é um vigia,
não um relatório. Se quiser mudar, no `.env`:

```
INTERVAL_SECONDS=30
```

Dá pra descer até `15`. **Não desça abaixo de 10** (o bot nem sobe): IP
bloqueado é cegueira permanente, muito pior do que chegar meio minuto atrasado.
Se mesmo assim o site reclamar, o bot freia sozinho por 10 minutos e avisa com
um 🐢 — você não precisa fazer nada.

### Quais categorias ele vigia

Também no `.env`:

```
CATEGORIES=Inteira,Meia Estudante
```

**Só liste o que você consegue comprovar na catraca** — sem carteirinha na mão,
meia é entrada negada, não desconto. Todo alerta de meia vem com esse aviso, mas
o filtro de verdade é aqui. O site também vende `Meia PCD`, `Meia Jovem Baixa
Renda` e `Acompanhante PCD`.

---

## Problemas comuns

| Sintoma | Causa |
|---|---|
| `Configuração inválida: ...` e o bot nem sobe | é a própria mensagem dizendo qual linha do `.env` está errada. Corrija e rode de novo |
| Bot não manda nada ao iniciar | `CHAT_ID` errado, ou o bot foi removido do grupo |
| `Chat not found` no terminal | `CHAT_ID` positivo (conversa privada) ou digitado errado |
| Comandos ignorados no grupo | você está mandando de outro chat — só o `CHAT_ID` configurado é aceito |
| `Unauthorized` | token errado, ou você revogou ele no BotFather |
| `Conflict: terminated by other getUpdates` | tem **dois bots rodando com o mesmo token**. Um token só serve um. Se o servidor está ligado, `sudo systemctl stop bts-vigia` antes de rodar aqui |
| Funcionava e parou | seu Mac dormiu. Use `./start.sh`, que já embrulha em `caffeinate -s` |
| Chegou um 🐢 no grupo | o site pediu pra ir mais devagar. O bot já freou sozinho por 10 min e volta sozinho — não mexa no `.env` |
| Mudei o `.env` e nada mudou | você tinha usado `/agio` antes. Editar o `MAX_MARKUP` no `.env` derruba o override — mas só depois de reiniciar |
| `CHAT_ID` mudou sozinho | o grupo virou supergrupo (acontece ao adicionar gente ou mudar permissões). Rode `chat_id.py` de novo |

---

## Antes de convidar os amigos

Duas coisas honestas pra avisar no grupo:

1. **Enquanto rodar no seu Mac, se ele dormir ninguém recebe nada.** O alerta é
   tão confiável quanto a máquina que o hospeda. Por isso o plano de mover pro
   Linux (`README.md` tem o serviço systemd pronto).
2. **Qualquer um no grupo pode mandar `/agio 500` e desregular o teto pra
   todos.** Não há níveis de permissão — é um grupo de confiança. Se virar
   problema, dá pra restringir por `user_id`.
