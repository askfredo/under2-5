"""
UNDER 2.5 BOT — Railway + Discord Webhook
==========================================
- Cada 5 días: escanea ligas y manda señales Under2.5 a Discord
- Cada día a las 00:30 UTC: verifica resultados y reporta si fue Under o Over
- Al cerrar posiciones: muestra scoreboard acumulado con winrate por nivel

Variables de entorno:
    DISCORD_WEBHOOK_URL  → tu webhook de Discord
    FOOTBALL_API_KEY     → tu key de football-data.org
    DATA_DIR             → opcional, por defecto /data (Railway Volume)

Liga excluida: Bundesliga (señal no funciona — 35% under incluso con señal)
"""

import os, json, time, requests, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ── Configuración ────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
API_KEY             = os.environ.get("FOOTBALL_API_KEY", "da8df76a845d4ae0b4cc8938e9d4a9d6")
HEADERS             = {"X-Auth-Token": API_KEY}
BASE_URL            = "https://api.football-data.org/v4"
TEMPORADA_ACTUAL    = 2024

# Volumen Railway: /data   — sin volumen: directorio actual
_DATA_ENV    = os.environ.get("DATA_DIR", "/data")
DATA_DIR     = _DATA_ENV if os.path.isdir(_DATA_ENV) else "."
ALERTAS_FILE = os.path.join(DATA_DIR, "alertas_pendientes.json")
STATS_FILE   = os.path.join(DATA_DIR, "stats.json")

# Bundesliga excluida
LIGAS = {
    "Premier League": "PL",
    "La Liga":        "PD",
    "Serie A":        "SA",
    "Ligue 1":        "FL1",
    "Eredivisie":     "DED",
    "Primeira Liga":  "PPL",
}

# ── Parámetros visitante bloqueado ───────────────────────────────────────────
RACHA_MAX_ESTRICTO  = 0.20
RACHA_MAX_MEDIO     = 0.40
GA_MAX_ESTRICTO     = 1.10
GA_MAX_MEDIO        = 1.20

# ── Tabla media ──────────────────────────────────────────────────────────────
POS_MID_LO_ESTRICTO = 6
POS_MID_HI_ESTRICTO = 14
POS_MID_LO_AMPLIO   = 5
POS_MID_HI_AMPLIO   = 15

# ── Jornada ──────────────────────────────────────────────────────────────────
JORNADA_INI_MAX = 18
JORNADA_MED_MAX = 25
VENTANA_RACHA   = 5

# ── Probabilidades históricas calibradas (lab v6, sin Bundesliga) ────────────
PROB = {
    "ELITE":  {"under25": 65.6, "label": "🔒 ELITE"},
    "FUERTE": {"under25": 62.2, "label": "⭐ FUERTE"},
    "SÓLIDO": {"under25": 60.0, "label": "✅ SÓLIDO"},
    "SEÑAL":  {"under25": 57.2, "label": "🔵 SEÑAL"},
}
NIVELES_ORDEN = ["ELITE", "FUERTE", "SÓLIDO", "SEÑAL"]

# ── Discord ──────────────────────────────────────────────────────────────────
def send_discord(content: str):
    if not DISCORD_WEBHOOK_URL:
        print(f"[DISCORD-MOCK] {content[:120]}")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": content[:2000]}, timeout=10)
        if r.status_code not in (200, 204):
            print(f"[ERROR Discord] {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[ERROR Discord] {e}")

# ── Stats acumulados ─────────────────────────────────────────────────────────
def _stats_vacios():
    base = {"gana": 0, "pierde": 0}
    return {
        "total":  dict(base),
        "ELITE":  dict(base),
        "FUERTE": dict(base),
        "SÓLIDO": dict(base),
        "SEÑAL":  dict(base),
        "ultima_actualizacion": None,
    }

def cargar_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            return json.load(f)
    return _stats_vacios()

def guardar_stats(stats):
    stats["ultima_actualizacion"] = datetime.utcnow().isoformat()
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

def registrar_resultado_en_stats(nivel: str, es_under: bool):
    stats = cargar_stats()
    campo = "gana" if es_under else "pierde"
    stats["total"][campo] += 1
    if nivel in stats:
        stats[nivel][campo] += 1
    guardar_stats(stats)

def winrate(gana, pierde):
    total = gana + pierde
    return f"{round(gana / total * 100, 1)}%" if total else "—"

def build_scoreboard(stats, contexto=""):
    t      = stats["total"]
    total  = t["gana"] + t["pierde"]
    wr     = winrate(t["gana"], t["pierde"])
    llenos = round(t["gana"] / total * 20) if total else 0
    barra  = "█" * llenos + "░" * (20 - llenos)

    lineas = ["## 📈 SCOREBOARD UNDER2.5 ACUMULADO"]
    if contexto:
        lineas.append(contexto)
    lineas += [
        "```",
        "─" * 50,
        f"  TOTAL     {t['gana']:>4}✅   {t['pierde']:>4}❌   WR: {wr:>7}   ({total} señales)",
        f"  [{barra}]",
        "─" * 50,
    ]
    for nivel in NIVELES_ORDEN:
        n = stats.get(nivel, {"gana": 0, "pierde": 0})
        j = n["gana"] + n["pierde"]
        if j == 0:
            continue
        wr_n  = winrate(n["gana"], n["pierde"])
        badge = PROB[nivel]["label"]
        lineas.append(f"  {badge:<14}  {n['gana']:>4}✅   {n['pierde']:>4}❌   WR: {wr_n:>7}   ({j})")
    lineas.append("```")
    return "\n".join(lineas)

# ── Persistencia alertas ─────────────────────────────────────────────────────
def cargar_alertas():
    if os.path.exists(ALERTAS_FILE):
        with open(ALERTAS_FILE) as f:
            return json.load(f)
    return []

def guardar_alertas(alertas):
    with open(ALERTAS_FILE, "w") as f:
        json.dump(alertas, f, ensure_ascii=False, indent=2)

def agregar_alertas_pendientes(nuevas):
    existentes = cargar_alertas()
    keys = {(a["fecha"], a["home"], a["away"]) for a in existentes}
    for a in nuevas:
        if (a["fecha"], a["home"], a["away"]) not in keys:
            a["resultado"] = None
            existentes.append(a)
    guardar_alertas(existentes)

# ── Lógica fútbol ────────────────────────────────────────────────────────────
def calcular_estado_liga(liga_code):
    url = f"{BASE_URL}/competitions/{liga_code}/matches?season={TEMPORADA_ACTUAL}&status=FINISHED"
    r   = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 429:
        print("  Rate limit — esperando 65s...")
        time.sleep(65)
        r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return None

    partidos = sorted(r.json().get("matches", []), key=lambda x: x["utcDate"])
    stats    = defaultdict(lambda: {
        "pts":0,"gf":0,"ga":0,"jugados":0,
        "historial_over25":[],"historial_under25":[],"nombre":""
    })

    for match in partidos:
        hid = match["homeTeam"]["id"]; aid = match["awayTeam"]["id"]
        sc  = match["score"]["fullTime"]; gh, ga = sc["home"], sc["away"]
        if gh is None or ga is None: continue

        stats[hid]["nombre"] = match["homeTeam"]["name"]
        stats[aid]["nombre"] = match["awayTeam"]["name"]
        total = gh + ga; o25 = 1 if total > 2 else 0; u25 = 1 - o25

        stats[hid]["jugados"] += 1; stats[aid]["jugados"] += 1
        stats[hid]["gf"] += gh;     stats[hid]["ga"] += ga
        stats[aid]["gf"] += ga;     stats[aid]["ga"] += gh
        stats[hid]["historial_over25"].append(o25); stats[aid]["historial_over25"].append(o25)
        stats[hid]["historial_under25"].append(u25); stats[aid]["historial_under25"].append(u25)

        if gh > ga:    stats[hid]["pts"] += 3
        elif gh == ga: stats[hid]["pts"] += 1; stats[aid]["pts"] += 1
        else:          stats[aid]["pts"] += 3

    ranking = sorted(stats.items(), key=lambda x: (-x[1]["pts"], -(x[1]["gf"]-x[1]["ga"]), -x[1]["gf"]))
    equipos = []
    for pos, (eq_id, s) in enumerate(ranking, 1):
        j = s["jugados"]
        if j < 5: continue
        ho = s["historial_over25"][-VENTANA_RACHA:]
        hu = s["historial_under25"][-VENTANA_RACHA:]
        equipos.append({
            "id": eq_id, "nombre": s["nombre"], "pos": pos, "jugados": j,
            "avg_gf":    round(s["gf"]/j, 2),
            "avg_ga":    round(s["ga"]/j, 2),
            "racha_o25": round(sum(ho)/len(ho) if ho else 0, 2),
            "racha_u25": round(sum(hu)/len(hu) if hu else 0, 2),
        })
    return equipos

def get_proximos_dias(liga_code, dias=5):
    hoy = datetime.utcnow().date(); fin = hoy + timedelta(days=dias)
    r   = requests.get(f"{BASE_URL}/competitions/{liga_code}/matches?status=SCHEDULED",
                       headers=HEADERS, timeout=30)
    if r.status_code != 200: return []
    return [m for m in r.json().get("matches", [])
            if hoy <= datetime.fromisoformat(m["utcDate"].replace("Z","")).date() <= fin]

def evaluar_partido(home_id, away_id, fecha_str, jornada, liga, equipos_list):
    eq   = {e["id"]: e for e in equipos_list}
    home = eq.get(home_id); away = eq.get(away_id)
    if not home or not away: return None

    roa      = away["racha_o25"]
    gaa      = away["avg_ga"]
    gfa      = away["avg_gf"]
    roh      = home["racha_o25"]
    gah      = home["avg_ga"]
    pos_home = int(home["pos"])
    pos_away = int(away["pos"])
    dp       = abs(pos_home - pos_away)

    # Condiciones
    vbloq_estricto     = roa <= RACHA_MAX_ESTRICTO and gaa <= GA_MAX_ESTRICTO
    mid_h_estricto     = POS_MID_LO_ESTRICTO <= pos_home <= POS_MID_HI_ESTRICTO
    mid_h_amplio       = POS_MID_LO_AMPLIO   <= pos_home <= POS_MID_HI_AMPLIO
    mid_a_estricto     = POS_MID_LO_ESTRICTO <= pos_away <= POS_MID_HI_ESTRICTO
    ambos_mid_estricto = mid_h_estricto and mid_a_estricto
    jornada_ini        = jornada is not None and 5 <= jornada <= JORNADA_INI_MAX
    jornada_med        = jornada is not None and 5 <= jornada <= JORNADA_MED_MAX
    parejos            = dp <= 5

    if not vbloq_estricto: return None  # condición base siempre necesaria

    # Clasificación
    if   vbloq_estricto and mid_h_estricto and jornada_ini:          nivel = "ELITE"
    elif vbloq_estricto and mid_h_amplio   and jornada_ini:          nivel = "FUERTE"
    elif vbloq_estricto and ambos_mid_estricto:                       nivel = "SÓLIDO"
    elif vbloq_estricto and mid_h_estricto and parejos:               nivel = "SÓLIDO"
    elif vbloq_estricto and mid_h_amplio:                             nivel = "SEÑAL"
    else: return None

    info  = PROB[nivel]
    justo = round(info["under25"] / 100, 2)

    # Señales activas
    señales = [
        f"racha_a={roa:.0%}(≤20%✓)",
        f"ga_a={gaa:.2f}(≤1.10✓)",
        f"pos_local={pos_home}({'mid✓' if mid_h_estricto else 'mid~'})",
    ]
    if ambos_mid_estricto: señales.append(f"pos_visita={pos_away}(mid✓)")
    if jornada_ini:        señales.append(f"jornada={jornada}(ini✓)")
    elif jornada_med:      señales.append(f"jornada={jornada}(med)")
    if parejos:            señales.append(f"diff_pos={dp}(≤5✓)")

    if   nivel == "ELITE":  contexto = f"Visitante muy bloqueado (racha {roa:.0%}, ga {gaa:.2f}) + local pos {pos_home} + jornada temprana {jornada}"
    elif nivel == "FUERTE": contexto = f"Visitante bloqueado (racha {roa:.0%}, ga {gaa:.2f}) + local pos {pos_home} zona media + jornada {jornada}"
    elif nivel == "SÓLIDO": contexto = f"Visitante bloqueado + ambos en tabla media (local {pos_home}, visita {pos_away})"
    else:                   contexto = f"Visitante bloqueado (racha {roa:.0%}, ga {gaa:.2f}) + local zona media (pos {pos_home})"

    return {
        "nivel": nivel, "badge": info["label"], "prob_under25": info["under25"], "poly_justo": justo,
        "fecha": fecha_str, "jornada": jornada, "liga": liga,
        "home": home["nombre"], "away": away["nombre"],
        "pos_home": pos_home, "pos_away": pos_away, "diff_pos": dp,
        "racha_o25_h": roh, "racha_o25_a": roa, "racha_u25_a": away["racha_u25"],
        "avg_ga_h": gah, "avg_ga_a": gaa, "avg_gf_a": gfa,
        "vbloq_estricto": "✓", "mid_h_estricto": "✓" if mid_h_estricto else "✗",
        "ambos_mid": "✓" if ambos_mid_estricto else "✗",
        "jornada_ini": "✓" if jornada_ini else "✗", "parejos": "✓" if parejos else "✗",
        "señales": " | ".join(señales), "contexto": contexto,
        "resultado": None,
    }

# ── JOB 1: Scanner cada 5 días ───────────────────────────────────────────────
def job_scanner():
    print(f"\n[{datetime.utcnow()}] 🔍 Iniciando scanner Under2.5...")
    hoy    = datetime.utcnow().date()
    fin    = hoy + timedelta(days=5)
    nuevas = []

    for nombre, codigo in LIGAS.items():
        print(f"  {nombre}...", end=" ", flush=True)
        equipos = calcular_estado_liga(codigo); time.sleep(7)
        if not equipos: print("sin datos"); continue
        proximos = get_proximos_dias(codigo, 5); time.sleep(7)
        hits = 0
        for m in proximos:
            res = evaluar_partido(
                m["homeTeam"]["id"], m["awayTeam"]["id"],
                m["utcDate"][:10], m.get("matchday"), nombre, equipos
            )
            if res: nuevas.append(res); hits += 1
        print(f"{len(proximos)} próximos → {hits} alertas")

    if not nuevas:
        send_discord(f"📭 **Scanner Under2.5 {hoy} → {fin}**\nSin alertas para este período.")
        return

    agregar_alertas_pendientes(nuevas)
    orden = {"ELITE": 0, "FUERTE": 1, "SÓLIDO": 2, "SEÑAL": 3}
    nuevas.sort(key=lambda x: (orden.get(x["nivel"], 9), x["fecha"]))

    send_discord(
        f"# 🔒 SCANNER UNDER2.5 — {hoy} → {fin}\n"
        f"**{len(nuevas)} alertas** | Lab v6 | sin Bundesliga\n"
        f"{'─'*40}"
    )
    time.sleep(0.5)

    for a in nuevas:
        vent = round(a["poly_justo"] - 0.50, 2)
        send_discord(
            f"## {a['badge']}  `{a['fecha']}`  J{a['jornada']}  [{a['liga']}]\n"
            f"**{a['home']}** (#{a['pos_home']}) vs **{a['away']}** (#{a['pos_away']})\n"
            f"```\n"
            f"Visitante  : racha_O25={a['racha_o25_a']:.0%}  GA={a['avg_ga_a']:.2f}  GF={a['avg_gf_a']:.2f}\n"
            f"Local      : racha_O25={a['racha_o25_h']:.0%}  GA={a['avg_ga_h']:.2f}  pos={a['pos_home']}\n"
            f"Condiciones: vbloq={a['vbloq_estricto']}  mid_local={a['mid_h_estricto']}  "
            f"ambos_mid={a['ambos_mid']}  jornada_ini={a['jornada_ini']}  parejos={a['parejos']}\n"
            f"Señales    : {a['señales']}\n"
            f"```\n"
            f"📊 Prob: **{a['prob_under25']}%**  |  Precio justo: **${a['poly_justo']:.2f}**\n"
            f"💰 Compras a $0.50 → **+${vent:.2f}** esperado por contrato\n"
            f"*{a['contexto']}*"
        )
        time.sleep(0.8)

    # Tabla referencia
    refs = {"ELITE":("93","[56%-75%]"),"FUERTE":("238","[56%-68%]"),
            "SÓLIDO":("225","[54%-66%]"),"SEÑAL":("519","[53%-61%]")}
    tabla = "## 📋 Referencia Under2.5\n```\n"
    tabla += f"{'Nivel':<18} {'n':>5}  {'Prob':>6}  {'Precio':>7}  {'Ventaja':>9}  IC\n" + "─"*58 + "\n"
    for nivel in NIVELES_ORDEN:
        info = PROB[nivel]; j = round(info["under25"]/100, 2); v = round(j-0.50, 2)
        n, ic = refs[nivel]
        tabla += f"{info['label']:<20} n={n:<5} {info['under25']:>5.1f}%  ${j:.2f}    +${v:.2f}    {ic}\n"
    tabla += "```"
    send_discord(tabla); time.sleep(0.5)

    # Scoreboard si hay historial
    s = cargar_stats(); t = s["total"]
    if t["gana"] + t["pierde"] > 0:
        send_discord(build_scoreboard(s, "*(Estado actual del tracker)*"))

    print(f"  ✅ {len(nuevas)} alertas enviadas.")

# ── JOB 2: Seguimiento diario ────────────────────────────────────────────────
def get_partidos_finalizados(liga_code, fecha_str):
    url = (f"{BASE_URL}/competitions/{liga_code}/matches"
           f"?dateFrom={fecha_str}&dateTo={fecha_str}&status=FINISHED")
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 429: time.sleep(65); r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200: return []
    return r.json().get("matches", [])

def job_seguimiento_diario():
    print(f"\n[{datetime.utcnow()}] 📊 Verificando resultados Under2.5...")
    ayer       = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    alertas    = cargar_alertas()
    pendientes = [a for a in alertas if a["fecha"] == ayer and a["resultado"] is None]

    if not pendientes: print(f"  Sin pendientes para {ayer}."); return

    ligas_necesarias = list({a["liga"] for a in pendientes})
    resultados = {}
    for liga in ligas_necesarias:
        codigo = LIGAS.get(liga)
        if not codigo: continue
        for p in get_partidos_finalizados(codigo, ayer):
            key = (p["homeTeam"]["name"], p["awayTeam"]["name"])
            gh  = p["score"]["fullTime"]["home"]
            ga  = p["score"]["fullTime"]["away"]
            if gh is not None and ga is not None:
                resultados[key] = {"gh": gh, "ga": ga, "total": gh + ga}
        time.sleep(7)

    resueltas = []
    for a in alertas:
        if a["fecha"] != ayer or a["resultado"] is not None: continue
        key = (a["home"], a["away"])
        if key in resultados:
            r         = resultados[key]
            es_under  = r["total"] <= 2
            a["resultado"]   = "UNDER ✅" if es_under else "OVER ❌"
            a["goles_home"]  = r["gh"]
            a["goles_away"]  = r["ga"]
            a["total_goles"] = r["total"]
            resueltas.append((a, es_under))
            registrar_resultado_en_stats(a["nivel"], es_under)

    guardar_alertas(alertas)
    if not resueltas: print(f"  Sin resultados para {ayer}."); return

    aciertos = sum(1 for _, eu in resueltas if eu)
    total    = len(resueltas)
    pct      = round(aciertos / total * 100) if total else 0

    send_discord(
        f"# 📊 RESULTADOS UNDER2.5 — {ayer}\n"
        f"**{aciertos}/{total} Under2.5 hoy** ({pct}%)\n"
        f"{'─'*40}"
    )
    time.sleep(0.5)

    for a, es_under in resueltas:
        emoji    = "✅" if es_under else "❌"
        marcador = f"{a.get('goles_home','?')} - {a.get('goles_away','?')} ({a.get('total_goles','?')} goles)"
        send_discord(
            f"{emoji} **{a['badge']}** [{a['liga']}]  J{a.get('jornada','?')}\n"
            f"{a['home']} vs {a['away']}\n"
            f"Resultado: **{marcador}** → {a['resultado']}\n"
            f"*(Prob estimada: {a['prob_under25']}%)*"
        )
        time.sleep(0.6)

    # Scoreboard acumulado — siempre al cerrar posiciones
    stats = cargar_stats()
    send_discord(build_scoreboard(
        stats,
        f"*Tras cerrar {total} posición{'es' if total > 1 else ''} de {ayer}*"
    ))
    print(f"  ✅ {len(resueltas)} resultados procesados.")

# ── Servidor HTTP — health check para Railway ────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        alertas = cargar_alertas(); stats = cargar_stats(); t = stats["total"]
        body = json.dumps({
            "status":          "running",
            "bot":             "under25",
            "data_dir":        DATA_DIR,
            "alertas_totales": len(alertas),
            "pendientes":      len([a for a in alertas if a["resultado"] is None]),
            "winrate_total":   winrate(t["gana"], t["pierde"]),
            "stats":           t,
            "timestamp":       datetime.utcnow().isoformat(),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, format, *args): pass

def run_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# ── Arranque ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  UNDER2.5 BOT — Iniciando...")
    print(f"  Webhook  : {'✅' if DISCORD_WEBHOOK_URL else '❌ falta DISCORD_WEBHOOK_URL'}")
    print(f"  Data dir : {DATA_DIR}")
    print(f"  Bundesliga: EXCLUIDA (señal no aplica)")
    s = cargar_stats(); t = s["total"]
    print(f"  Stats    : {t['gana']}✅ {t['pierde']}❌  WR={winrate(t['gana'],t['pierde'])}")
    print("=" * 55)

    threading.Thread(target=run_server, daemon=True).start()
    scheduler = BackgroundScheduler(timezone="UTC")

    scheduler.add_job(job_scanner, trigger=IntervalTrigger(days=5), id="scanner",
                      next_run_time=datetime.utcnow() + timedelta(seconds=10))
    scheduler.add_job(job_seguimiento_diario, trigger=CronTrigger(hour=0, minute=30), id="seguimiento")

    scheduler.start()
    print("  → Scanner     : cada 5 días (primera vez en 10s)")
    print("  → Seguimiento : diario 00:30 UTC")

    try:
        while True: time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("Bot detenido.")
