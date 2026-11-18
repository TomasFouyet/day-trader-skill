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

Swing de 15 días (y 60 en el momentum de resultados) en acciones de EE. UU. que compras **a mano** (Fintual u
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

   En `trader/config.json` revisa el bloque `"swing"`: `horizon_days` (15),
   `stop` (`atr:2`), `target` (`10R`: lejano, casi nunca se toca; la salida
   real es el stop o el tiempo, lo mejor del estudio), `time_stop_days` (15),
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

**Los short vienen apagados** (`"sides": ["long"]`): en el estudio de 5 años
perdieron todos los años (−0,08 a −0,13R por operación). Para volver a
probarlos: `"sides": ["long", "short"]` en el bloque `"swing"`. Los short son
las mismas tres reglas, al revés:
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

- **Historial en cada aviso** (en vez del winrate simulado): lo que hizo el
  setup en datos reales con la salida de la config de ejemplo. Setups
  técnicos: +0,17R por operación y winrate de 47 %, lo mismo que entrar al
  azar (+0,15R), así que **sin ventaja propia**. Momentum de resultados:
  +0,60R, winrate de 52 % y ventaja de +0,27R. También muestra los casos
  de esa acción. La Time Machine queda solo para el riesgo (stop o abierta
  al final): en el estudio predijo 63 % de ganadoras y fueron 52 %, y sus
  probabilidades no ordenaron mejor las operaciones (correlación de rangos
  ≈ 0,02). Risk usa esas mismas cifras del estudio para
  `min_expectancy_R`.
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

Con la **Variable** de repositorio `PAPER_TRADING` = `true` (Settings >
Secrets and variables > Actions > pestaña Variables; no hace falta que sea un
Secret, y un Secret con valor `1` hace que GitHub tape todos los "1" del log)
y las keys de Alpaca, cada
mañana, después de enviar los avisos, el pipeline opera cada setup que pasó
risk en tu cuenta **paper** de Alpaca (simulada, gratis, sin dinero real):

- **Una orden bracket por setup:** entrada **límite** al precio de "Entrada"
  del aviso (el de pre-mercado si el chequeo lo movió), objetivo (límite) y
  stop ya puestos, válidas hasta cancelarlas. Si el precio no llega al límite
  durante el día, la entrada se cancela al cierre ("no entró"). Usa el tamaño del
  aviso (el de pre-mercado si lo hay) en **acciones enteras**: Alpaca no
  acepta fracciones en un bracket, así que en acciones caras puede quedar en
  0 y se omite. Los shorts son ventas en corto de la acción (no replican el
  apalancamiento de Bitunix, pero el resultado en R es el mismo). Se omiten
  los descartados en el pre-mercado.
- **Cada mañana revisa** qué se ejecutó: entrada, objetivo o stop. Si pasa la
  fecha del stop de tiempo y sigue abierta, cancela sus dos órdenes y la
  cierra a mercado en la apertura siguiente (el Time Machine simula el cierre
  de ese día: hay un pequeño desfase).
- **Cada día, después del cierre** (workflow "Resumen del dia (paper)",
  16:17 a 18:47 de Nueva York), llega a Telegram el estado de tus operaciones:
  las abiertas con su precio al cierre, % y R desde la entrada, P&L, distancia
  al stop y al objetivo y días hasta la salida por tiempo; las que se cerraron
  hoy (objetivo, stop o tiempo) con su R y P&L; y las que no entraron.
- **Los lunes llega a Telegram el historial:** operaciones cerradas, winrate
  y R promedio reales comparados con el estudio, total en R, **contra el
  S&P 500** (lo que hizo cada operación y lo que hizo el SPY en los mismos
  días, desde la apertura del día de entrada hasta el cierre del de salida:
  promedio, diferencia y en cuántas le ganó) y patrimonio de la cuenta paper.
  Los precios del SPY salen de Alpaca.
- **Máximo 6 operaciones vivas** (`PAPER_MAX_POSITIONS` lo cambia), y una por
  acción. Con 6, una señal nueva solo entra si su setup tiene una ventaja
  medida que la más débil no tiene (hoy, el momentum de resultados frente a
  un setup técnico). La más débil es la de menor ventaja y, entre esas, la de
  peor R abierto. Se cierra a mercado y queda como "reemplazada". Las
  probabilidades de la Time Machine no se usan para esto porque no
  predijeron resultados.

Todo pasa por `trader-alerts/paper_trader.py`, que solo habla con
`paper-api.alpaca.markets` (fijo en el código, cualquier otra dirección se
rechaza, y un test lo verifica). Nunca usa trader-broker ni una cuenta real.
Conviene dejar la cuenta paper con el mismo monto que `TRADER_BUDGET` (en el
panel de Alpaca: reset de la cuenta paper). Ojo con la regla de day trading
(PDT) bajo US$25.000: si una entrada y su stop se ejecutan el mismo día, cuenta
como day trade.

### 11. Momentum de resultados (`earnings_gap`)

El único setup con una ventaja medida en datos reales. Cuando una acción
sube **≥ 12 % más que el S&P 500 en un día, con ≥ 3 veces su volumen
promedio de 50 días**, y ese salto vino de un **reporte de resultados**
(un 8-K ítem 2.02 en el SEC ese día o hasta 5 días antes), suele seguir
ganándole al mercado durante semanas.

- **Scanner**: busca el salto en la última vela o en la anterior (por si
  GitHub se salta una corrida de la mañana).
- **Resultados** (`earnings.py check`): confirma el 8-K en el SEC. Sin 8-K
  queda bloqueado ("el salto no vino de un reporte").
- **Salida propia**: 60 días hábiles, stop **3 × ATR**, objetivo lejano
  (10R, casi nunca se toca: la salida real es el stop o el tiempo). El
  stop de tiempo se recorta para salir **antes de los próximos
  resultados**. Se cambia en la config:
  `"swing": {"setup_exits": {"earnings_gap": {"horizon_days": 40, "stop": "atr:2.5"}}}`.
- **Risk** lo juzga por el estudio (+0,27R), no por la Time Machine. La
  simulación no tiene la deriva post-resultados, así que solo sirve para ver
  el riesgo. Telegram lo dice así.
- En paper, una sola operación viva por acción.

El estudio está en `trader-analyst/drift_study.py` y se corre a mano en
Actions ("Investigacion (resultados)", usa el secret `SEC_USER_AGENT`). Solo
imprime tablas agregadas. Resultado de 2005 a 2026, con 17.024 reportes de
232 acciones de la watchlist:

| Grupo | R a 60 días | Ventaja vs. entrada al azar |
|---|---|---|
| Subida ≥ +12 % con 8-K | +0,60R | **+0,27R ± 0,08** |
| Caída ≤ −12 % con 8-K (control) | +0,23R | −0,09R |
| Subida ≥ +12 % sin 8-K (control) | +0,43R | +0,10R |

Por época la ventaja fue 2005-09 ≈ 0, 2010-14 +0,17R, 2015-19 +0,50R,
**2020-22 −0,22R** y 2023-26 +0,46R: hay años en que no funciona. Las
acciones son las grandes de hoy (sesgo de supervivencia), así que los números
absolutos están inflados. La comparación contra el azar en las mismas
acciones es la cifra honesta. Para medirlo de verdad está el paper trading.

#### Pruebas de robustez (walk-forward, sensibilidad, bootstrap)

`trader-analyst/robustness.py`. La parte técnica se corre en local con 20
años de velas diarias; el momentum de resultados, en el workflow
"Investigacion (resultados)" con la opción `robustness`. Todo es neto de
0,1 % de costo por operación y "azar" son entradas al azar en las mismas
acciones con la misma salida.

| Prueba | Momentum de resultados (60d, 3 ATR, ≥ +12 %) | Setups técnicos (15d, 2 ATR) |
|---|---|---|
| Sensibilidad: configuraciones con ventaja sobre el azar | 213 de 216, y crece con el umbral y el plazo | 12 de 100, todas entre −0,02R y +0,003R |
| Sensibilidad de los parámetros del setup | no aplica | todas las variantes entre −0,006R y +0,004R |
| Walk-forward 2010-2026, configuración en uso | +0,62R por operación, ventaja +0,30R | ventaja −0,005R |
| Walk-forward eligiendo cada año con los 5 anteriores | ventaja +0,77R (casi siempre elige 60d, 2 ATR, umbral 15-20 %) | ventaja +0,006R |
| Bootstrap por meses: ventaja (95 %) | +0,25R [+0,08 a +0,42], P(≤ 0) = 0,3 % | 0,00R [−0,016 a +0,016], P(≤ 0) = 50 % |
| Bootstrap: años negativos | 10 % | 16 % |
| Bootstrap: caída máxima (R sumados) | mediana 20R, peor 5 % 32R | no aplica |

El momentum de resultados pasa las tres pruebas. Los setups técnicos no
tienen ventaja propia con ninguna salida ni con ningún parámetro: lo que ganan
lo gana también el azar. La salvedad de siempre: las acciones son las grandes
de hoy, y una acción que dio un gran salto por resultados tiene más
probabilidad de estar hoy en la lista. Por eso el paper trading es la prueba
que falta.

### 12. Sweep scanner (velas de 30 minutos, 1 hora o 4 horas)

`trader-scanner/sweep.py` replica el indicador de TradingView "Trading
Scanner" (barrido de liquidez → MSS → entrada). El código del indicador es
cerrado: las reglas se ajustaron contra sus gráficos de acciones al
13-11-2026. Reproducen al centavo tres gráficos (NOW, APP y SPCX) y 19 de las
20 filas de su tabla con la lista de 20 acciones (META no cuadra). En cripto
solo se pudo contrastar con precios de Yahoo, no de Binance, y coincide en
parte.

1. **Liquidez:** el último máximo y mínimo de swing confirmados (8 velas a
   cada lado).
2. **Barrido:** una vela pasa el nivel y cierra de vuelta adentro. La
   estructura que tendrá que romper debe ser interna (no el swing mayor del
   otro lado). Mientras un barrido espera, los siguientes se ignoran; solo un
   *cierre* más allá de la vela del barrido lo cancela. Cada nivel se barre
   una sola vez.
3. **MSS:** el cierre rompe el último swing interno (3 velas) contra el
   barrido, en 20 velas como máximo.
4. **Entrada:** límite en el borde cercano del hueco (FVG) que dejó la
   ruptura. Si no dejó hueco, en el nivel roto (el retesteo). Tiene 30 velas
   para llenarse.
5. **Stop** en el extremo de la vela del barrido (mínimo 0,2 × ATR),
   **objetivo** 2R, salida al cierre tras 200 velas.

```bash
python3 trader-scanner/sweep.py scan                  # las 250, solo las que tienen algo
python3 trader-scanner/sweep.py scan --interval 1h    # o 4h
python3 trader-scanner/sweep.py scan --all            # todas las filas
python3 trader-scanner/sweep.py backtest --interval 1h
python3 trader-scanner/sweep.py backtest --flat-eod   # variante day trading: nada pasa la noche
```

Por defecto escanea `trader/watchlist.txt` (tus 250) o, si no existe, la
lista de ejemplo. El indicador de TradingView solo tiene 20 casillas: la idea
es que este script mire las 250 y tú cargues en el indicador las que estén
en setup o barridas. `trader-scanner/sweep_watchlist.txt` trae 20 fijas por
liquidez y movimiento, en formato `BOLSA:TICKER`. Los parámetros se cambian
en `"sweep": {...}` de la config (`swing_len`, `internal_len`,
`take_profit_r`, `min_fvg_atr`, `symbols`, ...). Las velas de 4 horas son
las dos de TradingView por sesión (09:30-13:30 y 13:30-16:00).

**En TradingView:** `trader-scanner/sweep_strategy.pine` es la misma réplica
como estrategia de Pine Script (v6). Copia el archivo, ábrelo en el Pine
Editor, pulsa "Add to chart" y mira el "Strategy Tester". Úsalo junto al
indicador en el mismo gráfico: deberían marcar los mismos barridos (triángulos
naranjos), la misma línea de MSS (celeste) y la misma entrada, stop y objetivo.
La tabla abajo a la derecha da el resultado de la réplica en R antes de
costos; el Strategy Tester, en dinero con comisión (0,05 % por lado) y
deslizamiento. Opciones: largos y cortos por separado ("Allow longs", "Allow shorts"; los cortos vienen apagados), riesgo por
operación, "flat at the regular session close" para day trading en acciones y
fecha de inicio. En una vela que toca stop y objetivo, TradingView decide el
orden con su propio supuesto, así que puede diferir algo de `sweep.py`.

Dos cosas son propias y no del indicador: el hueco mínimo que cuenta
(0,1 × ATR) y la nota de calidad (0 a 100).

Backtest sobre 248 acciones, sin comisiones ("azar" = entradas al azar con
stop a 1,5 ATR, el mismo objetivo y el mismo límite de tiempo):

| Velas | Período | Operaciones | Ganadoras | Promedio | Al azar |
|---|---|---|---|---|---|
| 30 min | 60 sesiones | 738 | 36 % | −0,02R ± 0,06 | −0,10R |
| 1 hora | las mismas 60 | 342 | 33 % | −0,09R ± 0,08 | −0,06R |
| 4 horas | las mismas 60 | 79 | 29 % | −0,16R ± 0,16 | −0,01R |
| 1 hora | 2 años y 11 meses | 4.698 | 34 % | −0,06R ± 0,02 | −0,05R |
| 4 horas | 2 años y 11 meses | 1.421 | 33 % | −0,07R ± 0,04 | −0,02R |
| 30 min, sin pasar la noche | 60 sesiones | 398 | 44 % | −0,09R ± 0,03 | 0,00R |
| 1 hora, sin pasar la noche | 2 años y 11 meses | 868 | 47 % | −0,01R ± 0,02 | 0,00R |

Ningún timeframe se distingue del azar. En 30 minutos queda algo por encima
(+0,08R ± 0,06), pero con solo 60 sesiones esa diferencia cabe en el ruido, y
en casi tres años de velas de 1 hora no aparece. Los largos salen algo
positivos (+0,08R en 1 hora, +0,03R en 4 horas) y los cortos negativos
(−0,20R y −0,16R), pero comprar al azar en el mismo período dio +0,03R y
+0,10R: es el mercado subiendo, no la señal. Con objetivo a 2R hay que ganar
más del 33 % de las veces, y las reglas ganan entre 33 % y 36 %. Cerrando al
final del día casi nada llega al stop ni al objetivo. El script no coloca
órdenes ni avisa en tiempo real.

**La estrategia de Pine tal como está (solo largos, objetivo 1,5R, 30 min),
en 5 años y 246 acciones:** `python3 trader-analyst/sweep_study.py` (o el
workflow "Investigacion" con `study: sweep`) corre la réplica sobre cada
acción con velas de 30 minutos de Alpaca desde noviembre de 2021, la compara
con compras al azar con el mismo stop mediano, y busca patrones: separa las
operaciones por tipo de entrada, tendencia, SPY, tamaño del stop y del
barrido, velas hasta el MSS, tamaño del hueco, volatilidad, volumen, hora,
día y calidad, elige en la primera mitad del período los cortes que
destacan y los mide en la segunda, que no participó en elegirlos.

| | Operaciones | Ganadoras | Promedio | Al azar |
|---|---|---|---|---|
| Todas | 8.544 | 42 % | −0,002R ± 0,014 | +0,005R |
| 2022 | 1.672 | 40 % | −0,04R | −0,10R |
| 2023 | 1.693 | 44 % | +0,06R | +0,07R |
| 2024 | 1.744 | 43 % | +0,01R | +0,08R |
| 2025 | 1.713 | 43 % | +0,03R | +0,01R |
| 2026 | 1.541 | 41 % | −0,03R | −0,04R |

Con objetivo a 1,5R hay que ganar más del 40 % para empatar; la réplica gana
42 %, pero los stops que se pasan con gap dejan el promedio en cero, igual
que el azar, y la comisión (~0,03R) lo deja negativo. El único corte que
destacó en la primera mitad (barridos de jueves, +0,15R) dio 0,00R en la
segunda. Las 25 mejores acciones de la primera mitad dieron +0,01R en la
segunda: no hay acciones donde funcione mejor de forma estable. Lo más
parecido a un patrón, sin pasar la prueba: volatilidad alta (ATR de 30 min
≥ 1,5 % del precio) +0,06R y +0,12R en cada mitad, stop ancho (≥ 3,5 ATR)
+0,04R y +0,05R, SPY bajo su media de 20 días +0,02R y +0,05R; con unas 50
casillas mirando a la vez, se esperan 2 o 3 así por azar.

En cripto (el uso original del indicador) se midió aparte con velas exactas
de Binance de 30 minutos: las 20 monedas de su lista, noviembre de 2023 a
noviembre de 2026, 4.440 operaciones. La réplica coincide ahí con casi todas
las filas de dos capturas del indicador (TRX y QNT al centavo; UNI, la
entrada de TAO y un barrido de HBAR no cuadran). Resultado: +0,02R ± 0,02 por
operación antes de comisiones (+0,03R ± 0,01 con objetivo a 1R). Frente a
entradas al azar en la misma dirección queda unos +0,04R arriba, positivo en
5 de 7 semestres: una señal débil, menor que la comisión de ida y vuelta
(≈0,06R con un stop mediano de 1,7 % del precio). Ningún corte por tipo de
entrada, tamaño del barrido, hora o tendencia se sostiene en las dos mitades
del período. Lo que no se pudo probar es el filtro de calidad "Q" del
indicador, cuya fórmula no se conoce.

### 13. Caja 09:00-10:30 (ruptura, velas de 5 minutos)

Reglas: la caja es el máximo y el mínimo de las velas de 5 minutos de 09:00
a 10:30 de Nueva York (pre-mercado incluido). Después de las 10:30, una vela
que **cierra** sobre la caja es un largo y una que cierra bajo ella, un corto.
Entrada a ese cierre, stop en el otro borde de la caja, objetivo a 3R. Filtro:
largos solo con la EMA 50 sobre la EMA 200 en velas de 15 minutos, cortos al
revés. Una operación al día; si no tocó stop ni objetivo, se cierra a las
16:00.

- `trader-scanner/orb.py`: las reglas (y variantes: sin filtro, solo la
  primera ruptura, otro objetivo, mantener varios días).
- `trader-scanner/orb_strategy.pine`: la misma estrategia para el Strategy
  Tester de TradingView, con la caja de 09:00-09:30 por defecto (para la de
  10:30: "Caja" 0900-1030, "Ventana" 1030-1600, "Velas mínimas" 9). Úsala
  en velas de 5 minutos; en ETF y acciones activa el horario extendido. Ahí puedes probarla
  sobre los futuros reales (NQ, ES, MNQ, MES) con todo su historial. Dibuja la caja y sus
  bordes hasta las 16:00, y una tabla dice cuántos días tuvieron caja, ruptura, ruptura
  descartada por el filtro y entrada. Va con `margin_long/short = 0`: con el margen por
  defecto (100 %), TradingView rechaza en silencio las órdenes que valen más que el capital,
  y con una caja chica (o un contrato de NQ) eso deja casi sin operaciones. "Tamaño" permite
  una cantidad fija de contratos en vez del riesgo en %.
- `python3 trader-analyst/orb_study.py --symbols QQQ,SPY` (o el workflow
  "Investigacion" con `study: caja`): el estudio. Compara con entradas al
  azar en la dirección del filtro, con la caja como stop, y busca patrones
  en la primera mitad del período para medirlos en la segunda.

Futuros: Alpaca no tiene futuros y Yahoo solo da 60 días de velas de 5
minutos de NQ=F y ES=F. En esos 60 días, QQQ y SPY dieron casi las mismas
operaciones que NQ y ES (mismo día, hora, dirección y resultado), así que
se usaron QQQ y SPY para los 5 años. (Yahoo trajo una vela falsa en NQ y ES
el 30 de octubre a las 15:00, 4 % arriba de todo lo demás; `orb.py` limpia
esas velas aisladas.)

Resultados (5 años, noviembre de 2021 a noviembre de 2026; "al azar" =
entrar a cualquier hora después de las 10:30 en la dirección del filtro, con
el alto de la caja como stop, el mismo objetivo y el mismo cierre; costo por
lado 0,01 % en índices, 0,05 % en acciones):

| Mercado | Operaciones | Ganadoras | Promedio | Con costos | Al azar |
|---|---|---|---|---|---|
| QQQ + SPY, las reglas tal cual | 1.676 | 53 % | +0,05R ± 0,02 | +0,02R | +0,03R |
| QQQ + SPY, solo largos | 980 | 57 % | +0,10R ± 0,02 | +0,06R | +0,02R |
| QQQ + SPY, solo cortos | 726 | 48 % | −0,02R ± 0,03 | −0,04R | +0,04R |
| NQ + ES (futuros, 60 días) | 64 | 55 % | +0,07R ± 0,07 | +0,03R | +0,05R |
| 248 acciones | 160.001 | 48 % | +0,00R ± 0,001 | −0,06R | −0,00R |

- El objetivo de 3R casi nunca llega en el día (12 de 1.676 operaciones en
  los índices): el stop está al otro lado de la caja, así que el riesgo es
  la caja entera (mediana 0,8 % del precio en QQQ/SPY) y 3 cajas más no se
  recorren en una tarde. En la práctica es "entrar en la ruptura y salir a
  las 16:00". Por eso 1R, 1,5R, 2R y 3R dan casi lo mismo.
- Mantenerla varios días hasta tocar stop u objetivo empeora: 0,00R en
  índices y −0,03R en acciones. Solo los largos de índices suben (+0,12R),
  y eso es la subida del mercado en esos años, no la ruptura.
- El filtro de medias ayuda algo en los índices (+0,05R con él, +0,04R sin
  él, +0,03R al revés), pero la diferencia cabe en el ruido.
- Lo único que se distingue del azar son los **largos en QQQ y SPY**:
  +0,10R contra +0,02R de entrar al azar, positivos antes de costos en los
  6 años del período. Pero se eligieron después de ver el resultado y en la
  segunda mitad bajan a +0,04R ± 0,03 (eran +0,15R en la primera); 2025 dio
  −0,01R con costos. Con 1 % de riesgo por operación son unos +12R al año
  entre los dos, con una peor caída de 15R.
- En acciones no hay nada: 48 % de ganadoras, cero antes de costos, y el
  costo (0,05R) lo deja negativo. Ningún corte por hora, tamaño de la caja,
  distancia del cierre a la caja, separación de las medias o día de la
  semana se sostiene en la segunda mitad, y las 25 mejores acciones de la
  primera mitad dieron +0,00R en la segunda.

**Variante: caja de 09:00 a 09:30** (se opera desde las 09:30;
`--box 09:00-09:30`, y es la que trae por defecto el Pine). Índices: QQQ
(Nasdaq 100), SPY (S&P 500), IWM (Russell 2000) y DIA (Dow), 5 años, costo
0,01 % por lado:

| Caja | Operaciones | Ganadoras | Promedio | Con costos | Al azar | Stop mediano |
|---|---|---|---|---|---|---|
| 09:00-09:30, tal cual | 4.415 | 34 % | +0,01R ± 0,02 | −0,07R | +0,05R | 0,30 % |
| 09:00-09:30, solo largos | 2.410 | 37 % | +0,07R ± 0,03 | −0,02R | +0,05R | |
| 09:00-09:30, solo cortos | 2.005 | 31 % | −0,06R ± 0,03 | −0,12R | +0,04R | |
| 09:00-10:30, tal cual | 3.268 | 51 % | +0,03R ± 0,01 | +0,00R | +0,02R | 0,82 % |
| 09:00-10:30, solo largos | 1.795 | 55 % | +0,07R ± 0,02 | +0,04R | +0,01R | |

Con la caja corta el stop es casi tres veces más chico, el objetivo de 3R
sí llega (638 veces de 4.415), pero el 59 % de las operaciones toca el
stop: dos tercios entran entre 09:30 y 10:00, en el ruido de la apertura.
Queda por debajo de entrar al azar, el costo pesa el triple en R, el filtro
de medias resta (+0,06R sin él) y por índice solo QQQ sale positivo (+0,10R
antes de costos, +0,04R después; SPY, IWM y DIA negativos). Ningún corte se
sostiene en la segunda mitad. Con la caja de 09:00-10:30, en los cuatro
índices, los largos siguen siendo lo mejor (+0,07R contra +0,01R al azar),
pero IWM y DIA quedan en cero: lo positivo viene de QQQ y SPY.

En futuros (60 días de Yahoo, NQ, ES, RTY y YM), la caja de 09:00-09:30
dio +0,06R ± 0,11 y la de 09:00-10:30 +0,04R ± 0,05: demasiado poco tiempo
para distinguir una de otra.

### 14. Límites que quedan

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
