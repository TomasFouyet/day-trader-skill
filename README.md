# The Day Trader skill

11 Claude skills for day trading: scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a dollar.

Free, MIT, no signup, no API key. The tools are plain Python that read free
public data: Yahoo Finance charts, Nasdaq Trader's symbol lists, SEC EDGAR
and the House Clerk.

One scans thousands of charts for setups. Another tracks breaking news on
every stock you watch. One watches a setup bar by bar and tells you the moment
to get in, and another sets your stop and your target before you do. And the
craziest one is the Time Machine: before you risk a dollar, it replays the
rest of the session 10,000 times from the stock's own recent bars, plus every
past day the same setup fired, and tells you the honest odds.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

> **Looking for the original Trader skill?** Copy an investor's 13F, the
> stock screener, tax lots and rebalancing live in
> [github.com/Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill).
> This repo is its day-trading sequel: the Trader skill v2.

## Install

Paste this into Claude:

```
https://github.com/Jakeschincariol/day-trader-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add Jakeschincariol/day-trader-skill
/plugin install day-trader-skill@day-trader-skill
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/day-trader-skill:trader-scanner` and so on. Copy the folders instead if you want
plain `/trader-scanner`:

```bash
git clone https://github.com/Jakeschincariol/day-trader-skill.git
cp -r day-trader-skill/trader-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. No Claude Code at all? Paste any single `SKILL.md` at the
top of a chat and it runs as a mode. You lose the Python tools, but the method
works.

The tools need Python 3.8 or newer. Nothing to pip install. The SEC tools
(news filings, the analyst) ask you to identify yourself: put your name and
email in `trader/config.json` (`"sec_user_agent": "Your Name you@example.com"`)
or in the `SEC_USER_AGENT` environment variable.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans thousands of charts for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Tracks breaking news on every stock you watch and flags what moves it. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter, so you know when to get out. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a dollar: 10,000 simulated sessions plus every past time the setup fired. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade. |
| `/trader-broker` | Order Executor | Sends approved orders to your broker, paper trading by default, never without your yes. |
| `/trader-alerts` | Price Alerts | Rings when a price, a stop or the opening bell hits. |
| `/trader-analyst` | Earnings Analyst | Reads earnings reports and sums up what changed. |
| `/trader-pelosi` | Pelosi Tracker | Reads Congress's stock-trade disclosures the minute they are filed. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by the close                                       (while it is open)
news / analyst / pelosi                                                        (on a schedule)
```

An example, the trade in the reel: Tesla, Monday October 19 2026, real data.

1. **`/trader-scanner`** at 10:15 AM ET: of the 231 charts in the reel's
   scan, 42 had closed above their 15-minute opening range. TSLA is one: range 354.47 to 362.18,
   the 10:10 candle closed at 364.18, 0.55 % over the top.
2. **`/trader-news`**: the morning's TSLA headlines, in New York time
   ("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", 8:04,
   tagged earnings and deal). Tesla reports this week: a day trade is out
   before the close, so it is not exposed to the report.
3. **`/trader-entry`** watched TSLA bar by bar. At 10:12 it said waiting (the
   10:10 bar was still forming). At 10:15 it said triggered: the first
   5-minute close above the range, 364.18.
4. **`/trader-exit`** set the plan before the entry: stop at the range's
   midpoint, 358.32, so 5.86 a share at risk; target one times that risk,
   370.04; flat by 3:55 PM either way.
5. **`/trader-timemachine`** replayed the rest of the day 10,000 times, each
   5-minute candle drawn from TSLA's own candles at that time of day over the
   previous 59 sessions (seed 19, so anyone can repeat it): target first
   29.4 %, stop first 25.5 %, still open at the close 45.1 %. It also re-ran
   the same rule on those 59 real sessions: it fired 33 times, 13 hit the
   target, 15 the stop, 5 neither. Its verdict: close to a coin flip. That is
   the point of looking before you risk a dollar.
6. **`/trader-budget`** with a $5,000 account and a $50 max loss per trade:
   50 / 5.86 = 8 shares ($2,913.44). If the stop fills at 358.32, the loss is
   $46.88. It writes one bracket order: buy 8 TSLA, stop-loss 358.32,
   take-profit 370.04.
7. **`/trader-risk`** checks it against your limits: the max loss, the daily
   loss limit, trades per day, reward to risk, the entry window, position
   size and cash. PASS or BLOCK, with the reason.
8. **`/trader-broker`** shows you the ticket in plain words. You type yes for
   that exact order. Claude calls your brokerage connector (Alpaca's MCP
   server, paper account by default), and Claude's own "Allow once / Deny"
   card asks you one more time.
9. **`/trader-alerts`** rings on the stop, the target and at 3:55 PM to
   flatten whatever is still open.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 trader-scanner/scan.py universe                                # ~5,900 US stocks, Nasdaq Trader
python3 trader-scanner/scan.py daily                                   # pre-market setups, whole universe
python3 trader-scanner/scan.py orb --at 10:15                          # opening-range breakouts so far
python3 trader-news/news.py --tickers TSLA --hours 12                  # headlines, 8-Ks, today's move
python3 trader-entry/entry.py check TSLA --setup orb                   # waiting / triggered / expired
python3 trader-entry/entry.py watch TSLA --every 30                    # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by close
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-analyst/facts.py TSLA                                   # quarters, growth, EPS
python3 trader-pelosi/ptr.py index --member pelosi                     # her PTRs this year
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real data: TSLA's 5-minute bars
for the 60 days to October 19 2026, the 10:15 AM opening-range scan, Yahoo's
TSLA headlines that morning, the House Clerk's 2026 index, real PTR text and
real XBRL facts. They reproduce every number in the example above.

## Modo swing (este fork, en español)

Swing de 5 a 7 días en acciones de EE. UU. que compras **a mano** (Fintual u
otra app). Cada día hábil **en la mañana, antes de la apertura** (≈08:50 en
Nueva York), el pipeline busca setups en el cierre del día anterior, descarta
los que tienen resultados trimestrales dentro del horizonte, fija stop,
objetivo y stop de tiempo, simula 10.000 trayectorias, calcula el tamaño,
revisa tus límites, **mira el precio de pre-mercado** de cada uno y te manda
los avisos por Telegram para operar en la apertura.

Pre-mercado (`trader-alerts/premarket.py`): ✅ sigue válido, ⚠ válido pero
con gap mayor a `gap_warn_pct` (3 %), ❌ el precio ya pasó el stop o el
objetivo, o la recompensa:riesgo desde ese precio quedó bajo tu mínimo (esos
no se envían: aparecen en el resumen como descartados), ❔ sin dato (vale el
plan con el cierre). Si es válido, el aviso usa el precio de pre-mercado como
entrada y achica el tamaño si hace falta para no pasar tu pérdida máxima
(nunca lo agranda). Stop, objetivo y stop de tiempo no se mueven. Las
probabilidades del Time Machine son las del plan con el cierre anterior. El
pre-mercado de Yahoo es gratis, atrasado y con poco volumen.

**Nada en este modo coloca órdenes.** Los tickets van a
`trader/swing_orders.json`, un archivo que trader-broker nunca lee, y el
pipeline no carga trader-broker (un test lo verifica). Tú decides y compras a
mano. Es educación y herramientas, no asesoría financiera. Una simulación no
es una predicción.

### 1. Instalación paso a paso

1. Python 3.8 o más nuevo (`python3 --version`). No hay nada que instalar con pip.
2. Clona tu fork y entra a la carpeta:

   ```bash
   git clone https://github.com/TU_USUARIO/day-trader-skill.git
   cd day-trader-skill
   git checkout swing-mode
   ```

3. Crea tu carpeta privada `trader/` (está en `.gitignore`: nunca se sube) y
   copia la config de ejemplo:

   ```bash
   mkdir -p trader
   cp trader-budget/config.swing.example.json trader/config.json
   ```

   En `trader/config.json` revisa el bloque `"swing"`: `horizon_days` (7),
   `stop` (`atr:2`), `target` (`2R`), `time_stop_days`,
   `max_risk_per_trade_pct` (1 % de la cuenta por trade), `max_positions`,
   `max_exposure_pct`, los setups y `block_on_earnings`. El tamaño de la cuenta
   puede ir en `"budget"` o, mejor, en la variable `TRADER_BUDGET`. Si tienes
   posiciones abiertas, anótalas en `trader/portfolio.json`
   (`{"cash": 3000, "positions": [{"ticker": "AAPL", "shares": 3, "price": 340, "swing": true}]}`).

4. El SEC pide identificarse (nombre y email) para leer las fechas de
   resultados. Ponlo en una variable de entorno, no en un archivo del repo:

   ```bash
   export SEC_USER_AGENT="Tu Nombre tu@email.com"
   ```

5. Comprueba que todo anda (sin red):

   ```bash
   python3 -m unittest discover -s tests
   ```

### 2. La watchlist (las 250 más negociadas)

```bash
python3 trader-scanner/watchlist_top.py            # -> trader/watchlist.txt
```

Toma el universo de Nasdaq Trader, saca ETFs, warrants, preferentes y
similares, descarta las acciones bajo US$5 y las de poca liquidez, y escribe
las 250 con mayor volumen en dólares promedio de 3 meses (velas diarias de
Yahoo). Tarda unos minutos: unas 300 consultas rápidas y hasta 750 gráficos.
`trader-scanner/watchlist.example.txt` es un ejemplo real generado así.

Las que Fintual no permita: bórralas de `trader/watchlist.txt` o, mejor,
ponlas en `trader/watchlist_exclude.txt` (un ticker por línea) para que no
vuelvan cuando regeneres la lista. Con `trader/watchlist.txt` presente, el
scanner swing revisa **solo** esos tickers.

### 3. El flujo diario a mano

```bash
python3 trader-scanner/scan.py swing                          # -> trader/swing.json
python3 trader-news/news.py --watchlist trader/swing.json --json > trader/news.json
python3 trader-analyst/earnings.py check --news trader/news.json
python3 trader-exit/exit.py plan --from trader/swing.json --pick TICKER --write
python3 trader-timemachine/timemachine.py run --write
python3 trader-budget/size.py --write
python3 trader-risk/check.py
python3 trader-alerts/alerts.py add --from-plan
```

O todo junto, mostrando los mensajes en vez de enviarlos:

```bash
python3 trader-alerts/swing_pipeline.py --dry-run --force-time
```

`--force-time` es solo para probar a cualquier hora: sin él, el pipeline corre
únicamente en un día hábil NYSE, entre las 08:15 y las 11:59 de Nueva York, y
una vez por día. `--session evening` lo corre en cambio después del cierre
(desde las 16:15), sin chequeo de pre-mercado.

### 4. Telegram

1. En Telegram habla con **@BotFather**, envía `/newbot`, elige nombre y
   usuario. Te da un token `123456789:AA...`. Es una contraseña: no lo pegues
   en ningún archivo del repo ni en el chat.
2. Abre tu bot y envíale `/start`.
3. Tu chat id: abre en el navegador
   `https://api.telegram.org/bot<TOKEN>/getUpdates` y busca `"chat":{"id":...}`.
   **No** es el número que va antes de los dos puntos en el token: ese es el id
   del bot, y Telegram responde "the bot can't send messages to the bot". Si
   `getUpdates` sale vacío (`"result":[]`), envíale otra vez `/start` al bot y
   recarga. Tu id personal suele ser un número positivo; el de un grupo empieza
   con `-`.
4. Exporta las dos variables y manda **un** mensaje de prueba:

   ```bash
   export TELEGRAM_BOT_TOKEN="123456789:AA..."
   export TELEGRAM_CHAT_ID="123456789"
   python3 trader-alerts/notify_telegram.py --test
   ```

Cada setup que pasa risk llega una sola vez (el estado queda en
`trader/state/telegram.json`) con símbolo, entrada, stop, objetivo, tamaño
sugerido, stop de tiempo, probabilidades del Time Machine, casos del
historial real y las advertencias de resultados. `--dry-run` muestra los
mensajes sin enviar ni recordar nada.

### 5. GitHub Actions (automático)

El workflow `.github/workflows/swing-daily.yml` corre el pipeline completo de
lunes a viernes con cuatro disparos (12:17, 12:47, 13:17 y 13:47 UTC). GitHub no
sabe de horario de verano y puede atrasar un disparo programado incluso horas,
así que el script mira la hora de Nueva York: corre solo entre las 08:15 y las
11:59 de Nueva York y una vez al día; el primer disparo que cae en la ventana
envía y los demás se saltan. En verano (EDT) envía desde las 08:17 de Nueva
York; en invierno (EST) desde las 08:17 también (los de las 12:xx UTC caen a las
07:xx, antes de la ventana). En Chile llega normalmente entre las 09:20 y las
10:50. Si GitHub lo atrasa hasta después de la apertura (09:30), el aviso usa
el precio de la sesión en vez del pre-mercado.

1. **Los workflows programados solo corren desde la rama por defecto.** Une
   `swing-mode` a `main` (o haz `swing-mode` la rama por defecto en
   Settings > Branches).
2. En tu repo: **Settings > Secrets and variables > Actions > New repository
   secret**. Crea:

   | Secret | qué | obligatorio |
   | --- | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | el token de @BotFather | sí |
   | `TELEGRAM_CHAT_ID` | tu chat id | sí |
   | `SEC_USER_AGENT` | `Tu Nombre tu@email.com` | sí, para las fechas de resultados |
   | `TRADER_BUDGET` | el tamaño de tu cuenta en dólares, p. ej. `5000` | sí (si no, usa el 5000 del ejemplo) |
   | `TRADER_CONFIG_JSON` | tu `trader/config.json` completo | no (si no, la config de ejemplo) |
   | `TRADER_PORTFOLIO_JSON` | tu `trader/portfolio.json` (caja y posiciones abiertas) | no |
   | `TRADER_WATCHLIST` | tu watchlist ya limpia, un ticker por línea | no (si no, se genera cada semana) |
   | `TRADER_WATCHLIST_EXCLUDE` | tickers que tu app no permite | no |

3. **Actions > Swing diario > Run workflow** con `test_message` marcado: llega
   el mensaje de prueba. Después, con `dry_run` y `force_time` marcados, para
   ver que el pipeline corre de punta a punta.

**Privacidad (el repo es público).** El tamaño de cuenta, la cartera, los
tickets, los planes y el estado nunca se commitean: `trader/` está en
`.gitignore`, los datos entran por Secrets, y el estado (avisos ya enviados,
última corrida) se guarda en el **cache** de Actions, que solo leen los
workflows de tu repo. No se suben artifacts (en un repo público cualquiera
podría descargarlos). Los logs de Actions de un repo público son públicos: por
eso el pipeline corre con `--quiet` y solo imprime pasos y conteos, sin
tickers, tamaños ni montos. Los detalles llegan solo a tu Telegram.

### 6. Si Yahoo bloquea las IP de GitHub

Yahoo Finance **no es una API oficial**: es gratis, puede atrasarse y puede
limitar o bloquear las IP de los servidores de GitHub. Señales: en el log del
workflow aparecen pasos con `exit 1` en el scanner, o el pipeline termina con
0 candidatos todos los días. Entonces córrelo en tu computador:

1. Deja tus variables en un archivo `.env` en la carpeta del repo (está en
   `.gitignore`):

   ```bash
   export TELEGRAM_BOT_TOKEN="..."
   export TELEGRAM_CHAT_ID="..."
   export SEC_USER_AGENT="Tu Nombre tu@email.com"
   export TRADER_BUDGET="5000"
   ```

2. Programa el pipeline varias veces en la mañana con `crontab -e` (macOS o
   Linux). La hora es la de tu computador; el script revisa la de Nueva York
   (corre entre las 08:15 y las 11:59) y una sola vez por día. En Chile, por
   ejemplo, a las 9:20, 10:20 y 11:20 cubre el año completo:

   ```bash
   20 9,10,11 * * 1-5 cd "$HOME/day-trader-skill" && . ./.env && python3 trader-alerts/swing_pipeline.py >> trader/state/cron.log 2>&1
   ```

   En Windows: Programador de tareas, una tarea diaria que ejecute
   `python trader-alerts\swing_pipeline.py` en la carpeta del repo, con las
   variables de entorno definidas en tu usuario.

3. Apaga el workflow programado en GitHub (Actions > Swing diario > `...` >
   Disable workflow) para que no corran los dos.

Dominios que usa: `query1.finance.yahoo.com`, `query2.finance.yahoo.com`,
`feeds.finance.yahoo.com`, `www.nasdaqtrader.com`, `www.sec.gov`,
`data.sec.gov` y `api.telegram.org`.

### 7. Long o short, Fintual y Bitunix (apalancamiento)

El scanner busca setups en las dos direcciones (`"sides": ["long", "short"]`
en el bloque `"swing"`). Los short son las mismas tres reglas, al revés:
quiebre del mínimo de 20 días, rebote a la EMA20 en tendencia bajista y RSI(2)
sobrecomprado bajo la media de 200 días. Cada aviso dice arriba
**🟢 LONG** o **🔴 SHORT**.

Tu cuenta se reparte entre venues en `"swing": {"venues": {...}}`:

| venue | `capital_pct` | `max_leverage` | `short` | para qué |
| --- | --- | --- | --- | --- |
| `fintual` | 70 | 1 | no | longs sin apalancamiento, compras fraccionadas |
| `bitunix` | 30 | 5 | sí | shorts, y longs apalancados si prefieres |

El tamaño de cuenta total sale de `TRADER_BUDGET` (o de `portfolio.json`).
Para cada setup el aviso calcula los dos venues:

- **El riesgo es siempre el mismo:** `max_risk_per_trade_pct` (1 %) de la
  cuenta **total**. Si el stop se cumple, pierdes eso, con o sin
  apalancamiento. El apalancamiento solo cambia cuánto margen inmovilizas.
- **Fintual (1x):** acciones (fraccionadas) = riesgo / distancia al stop,
  con tope de exposición, posición y caja sobre el 70 % de la cuenta.
- **Bitunix:** nocional = riesgo / % de stop. El margen máximo por trade es
  el 25 % del capital de Bitunix (`max_margin_per_trade_pct`). Usa el
  **menor apalancamiento** que hace caber ese nocional en ese margen. Nunca
  pasa de `max_leverage` y nunca deja la **liquidación** antes del stop: el
  precio de liquidación tiene que quedar a más de 1,5 veces la distancia del
  stop (`liq_buffer`). Si no cabe, achica la posición (menos riesgo, nunca
  más). Muestra nocional, margen, % de tu cuenta, % del capital del venue,
  liquidación aproximada y comisiones estimadas (`fee_pct`).
- trader-risk bloquea: short en un venue sin short, apalancamiento sobre el
  máximo, liquidación antes del stop, margen sobre el capital libre, y setups
  con expectativa del Time Machine bajo `min_expectancy_R` (0 en el ejemplo).

**Antes de usar Bitunix:**
- Verifica que Bitunix tenga el contrato de cada acción. No sé qué acciones de
  EE. UU. ofrece, ni si las tiene todas. Puedes limitar con
  `"symbols": ["AAPL", "TSLA", ...]` en su venue.
- Un perpetuo no es la acción: paga o cobra *funding* cada pocas horas, puede
  cotizar distinto que la acción y opera cuando la bolsa está cerrada. La
  liquidación que calcula el aviso es aproximada (margen aislado); la real
  depende de las reglas de Bitunix. Revisa ambas en la plataforma.
- Pon el stop en la plataforma apenas abras la posición.

### 8. Winrate y comodines (materias primas)

- **Winrate estimado** en cada aviso: el % de las 10.000 trayectorias que
  terminan en ganancia (tocan el objetivo, o siguen arriba del precio de
  entrada al cierre por tiempo), y el % de los casos del historial real del
  mismo setup que terminaron en ganancia. No es lo mismo que "objetivo
  primero": muchas operaciones ganan algo sin llegar al objetivo.
- **Comodines:** `swing.extra_symbols` agrega cada día, además de tu
  watchlist, ETF de materias primas: GLD (oro), SLV (plata), USO (petróleo
  WTI), BNO (petróleo Brent), UNG (gas natural), CPER (cobre). Se escanean con
  los mismos setups (long y short), no pasan por el filtro de volumen, no
  tienen resultados trimestrales y llevan 🛢 en el aviso. Son ETF: replican el
  precio del futuro, con costos y diferencias (el roll de los contratos). En
  Bitunix el instrumento equivalente (oro, plata, petróleo) puede tener otro
  ticker y otro precio. `scan.py swing --no-extras` los saca.

### 9. Precios desde Alpaca (opcional, recomendado)

Con los Secrets `ALPACA_API_KEY_ID` y `ALPACA_API_SECRET_KEY` (de una cuenta
gratis de Alpaca, basta la de paper trading) los precios salen de la **API de
datos de Alpaca** (`data.alpaca.markets`) en vez de Yahoo: velas diarias del
feed consolidado (gratis con 15 minutos de atraso, suficiente para una vela
diaria) y minutos de pre-mercado del feed IEX (en tiempo real, pero con poco
volumen a esa hora). Si Alpaca falla para una acción, esa consulta usa Yahoo.
Sin las keys, todo sigue con Yahoo. El log del workflow dice al final cuántas
consultas respondió cada uno.

Solo se usa la API de **datos**. La de órdenes (`paper-api.alpaca.markets` o
`api.alpaca.markets`) nunca se llama, y un test lo verifica. Para correrlo en
tu computador, exporta las mismas dos variables en tu `.env`.

### 10. Paper trading en Alpaca (historial real, dinero simulado)

Con el Secret `PAPER_TRADING` = `1` (además de las keys de Alpaca), cada
mañana, después de enviar los avisos, el pipeline opera cada setup que pasó
risk en tu cuenta **paper** de Alpaca (simulada, gratis, sin dinero real):

- **Una orden bracket por setup:** entrada a mercado en la apertura, objetivo
  (límite) y stop ya puestos, válidas hasta cancelarlas. Usa el tamaño del
  aviso (el de pre-mercado si lo hay) en **acciones enteras**: Alpaca no
  acepta fracciones en un bracket, así que en acciones caras puede quedar en
  0 y se omite. Los shorts son ventas en corto de la acción (no replican el
  apalancamiento de Bitunix, pero el resultado en R es el mismo). Se omiten
  los descartados en el pre-mercado.
- **Cada mañana revisa** qué se ejecutó: entrada, objetivo o stop. Si pasa la
  fecha del stop de tiempo y sigue abierta, cancela sus dos órdenes y la
  cierra a mercado en la apertura siguiente (el Time Machine simula el cierre
  de ese día: hay un pequeño desfase).
- **Los lunes llega a Telegram el historial:** operaciones cerradas, winrate
  real comparado con el estimado, R promedio real comparado con la
  expectativa, total en R y patrimonio de la cuenta paper.

Todo pasa por `trader-alerts/paper_trader.py`, que solo habla con
`paper-api.alpaca.markets` (fijo en el código, cualquier otra dirección se
rechaza, y un test lo verifica). Nunca usa trader-broker ni una cuenta real.
Conviene dejar la cuenta paper con el mismo monto que `TRADER_BUDGET` (en el
panel de Alpaca: reset de la cuenta paper). Ojo con la regla de day trading
(PDT) bajo US$25.000: si una entrada y su stop se ejecutan el mismo día, cuenta
como day trade.

### 11. Límites que quedan

- Las fechas de resultados son **estimaciones** a partir de los 8-K 2.02 del
  SEC (el calendario de Yahoo pide un token de sesión). Confírmalas en la web
  de la empresa o anótalas en `trader/earnings_dates.json`
  (`{"AAPL": "2026-10-29"}`). Los ETF y los emisores extranjeros quedan como
  "fecha desconocida" (aviso, no bloqueo, salvo `block_on_unknown_earnings`).
- El calendario de feriados NYSE incluido llega a 2027.
- La entrada sugerida es el último cierre: compras al día siguiente y el
  precio puede abrir con gap. El stop es un plan, no una garantía.
- El historial real de un setup suele tener pocos casos por acción: con menos
  de 30, el aviso lo dice.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money. You can lose money. Decide for
yourself, and talk to a licensed professional if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. Set your brokerage tool to "Needs approval" in Claude
(Customize, Connectors, tool permissions), never "Always allow", so Claude's
own "Allow once / Deny" card is a second check. No options, margin, short
selling or leverage unless you turn them on.

**Paper first.** `"paper": true` is the default in `config.example.json`, and
Alpaca's MCP server defaults to its paper account. Keys live in environment
variables, never in the chat.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: an earnings report, a halt, a headline. Treat its odds as a
reality check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market or across a gap, a
stop order can fill well past its price. The max loss shown is the loss at
the stop price.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API: it can lag, change or rate-limit you. The scanner and the entry
timer read 1-minute or 5-minute bars, not every tick. Check anything that
matters against your broker before you act on it.

**Day-trading rules.** FINRA removed the $25,000 pattern-day-trader minimum
on June 4, 2026 (Regulatory Notice 26-10), and brokers may phase the change
in until October 20, 2027, so yours may still apply the old rule. If it does,
set `"pdt_guard": true` and `/trader-risk` counts your day trades.

**Congress's trades are public, not inside information.** Members must disclose
trades over $1,000 within 45 days (the STOCK Act). The Pelosi skill reads those
public filings the minute they post.

**Taxes.** Day-trading gains are short-term gains, and frequent trading makes
wash sales likely. Nothing here is tax advice.

## Files

```
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
trader-pelosi/      SKILL.md, ptr.py
tests/              unittest suite + offline fixtures
```

MIT. Made by [Jake Schincariol](https://opusjake.ai).
