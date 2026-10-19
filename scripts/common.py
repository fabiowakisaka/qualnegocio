"""Funções compartilhadas: caminhos, leitura/escrita, métricas derivadas e fila."""
import json
import os
import re
import statistics
import unicodedata
from datetime import date

BASE_DIR = os.environ.get("QN_BASE_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
METRICAS_DIR = os.path.join(DATA_DIR, "metricas")
NARR_DIR = os.path.join(DATA_DIR, "narratives")
STATE_PATH = os.path.join(DATA_DIR, "published_state.json")
LEGACY_PATH = os.path.join(DATA_DIR, "base_metricas.json")

DEFAULTS = {
    "cidades_por_dia": 5, "max_refresh_por_dia": 2, "max_chamadas_gemini": 8,
    "nichos_por_chamada": 40, "gemini_model": "gemini-2.5-flash",
    "min_ativos_nicho": 5, "min_nichos_cidade": 6, "limite_mudanca_regerar_pct": 10,
    "meses_janela": 24, "vizinhas_max": 6, "ranking_max_linhas": 100,
    "sitemap_urls_por_arquivo": 10000, "llms_intro_dias": 30,
}


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2)


def hoje():
    return date.fromisoformat(os.environ["QN_HOJE"]) if os.environ.get("QN_HOJE") else date.today()


def strip_accents(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def slugify(text):
    t = strip_accents(text).lower().replace("'", "").replace("\u2019", "")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def norm_nome(text):
    t = strip_accents(text).upper()
    return re.sub(r"[^A-Z0-9]+", " ", t).strip()


def load_settings():
    s = dict(DEFAULTS)
    s.update(read_json(os.path.join(CONFIG_DIR, "settings.json"), {}))
    for env, key in (("CIDADES_POR_DIA", "cidades_por_dia"), ("MAX_CHAMADAS_GEMINI", "max_chamadas_gemini")):
        if os.environ.get(env, "").strip():
            s[key] = int(os.environ[env])
    if os.environ.get("GEMINI_MODEL", "").strip():
        s["gemini_model"] = os.environ["GEMINI_MODEL"].strip()
    return s


def load_cnaes():
    lista = read_json(os.path.join(CONFIG_DIR, "cnaes_meta.json"), [])
    return {c["slug"]: c for c in lista}


def cnae_digitos(cnae):
    return re.sub(r"\D", "", cnae)


def mapa_cnae_para_nicho(cnaes):
    """CNAE (7 dígitos) -> slug. A Receita só informa o CNAE, então dois nichos com o mesmo CNAE
    teriam números idênticos: vale o primeiro da lista e os demais ficam de fora (devolvidos em 'ignorados')."""
    mapa, ignorados = {}, []
    for slug, meta in cnaes.items():
        for codigo in [meta["cnae"]] + list(meta.get("cnaes_extra", [])):
            d = cnae_digitos(codigo)
            if d in mapa and mapa[d] != slug:
                ignorados.append((slug, mapa[d], codigo))
            else:
                mapa[d] = slug
    return mapa, ignorados


def fmt_int(n):
    if n is None:
        return "—"
    return f"{int(round(n)):,}".replace(",", ".")


def fmt_dec(x, casas=1):
    return f"{x:.{casas}f}".replace(".", ",")


def data_br(iso):
    if not iso:
        return ""
    try:
        y, m, d = str(iso)[:10].split("-")
        return f"{d}/{m}/{y}"
    except ValueError:
        return str(iso)


# ---------- métricas derivadas ----------

def indice_saturacao(razao, pressao):
    """razao = habitantes por estabelecimento / referência equilibrada do nicho."""
    if razao is None:
        return "Moderada"
    if razao < 0.75 or (razao < 1 and pressao >= 65):
        return "Alta"
    if razao >= 1.2:
        return "Baixa"
    return "Moderada"


def derivar_nicho(x, populacao, ideal):
    ativos, abertos, fechados = x
    hab = round(populacao / ativos) if ativos else None
    pressao = round(fechados / abertos * 100) if abertos else (100 if fechados else 0)
    razao = round(hab / ideal, 2) if (hab and ideal) else None
    return {
        "ativos": ativos, "abertos_24m": abertos, "fechados_24m": fechados,
        "saldo_24m": abertos - fechados, "hab_por_cnpj": hab,
        "taxa_pressao_24m": pressao, "razao_ideal": razao,
        "indice_saturacao": indice_saturacao(razao, pressao),
    }


def derivar_cidade(rec, cnaes, settings):
    nichos = {}
    for slug, x in rec["x"].items():
        meta = cnaes.get(slug)
        if not meta or x[0] < settings["min_ativos_nicho"]:
            continue
        nichos[slug] = derivar_nicho(x, rec["populacao"], meta.get("hab_por_cnpj_ideal"))
    cidade = {k: rec.get(k) for k in ("id", "nome", "uf", "uf_slug", "slug", "micro", "regiao",
                                      "populacao", "pib_per_capita", "legacy")}
    cidade["nichos"] = nichos
    return cidade


# ---------- carga de dados ----------

def tem_dados_reais():
    if not os.path.isdir(METRICAS_DIR):
        return False
    return any(f.endswith(".json") and not f.startswith("_") for f in os.listdir(METRICAS_DIR))


def info_ingest():
    return read_json(os.path.join(METRICAS_DIR, "_info.json"), {})


def carregar_registros():
    """Devolve ({ibge: registro}, legado). Registro: dados brutos com x={slug:[ativos,abertos,fechados]}."""
    registros = {}
    if tem_dados_reais():
        for nome in sorted(os.listdir(METRICAS_DIR)):
            if not nome.endswith(".json") or nome.startswith("_"):
                continue
            shard = read_json(os.path.join(METRICAS_DIR, nome), {})
            for cid, m in shard.get("municipios", {}).items():
                uf = m["uf"]
                registros[cid] = {
                    "id": cid, "nome": m["n"], "uf": uf, "uf_slug": uf.lower(), "slug": m["s"],
                    "micro": m.get("micro"), "regiao": m.get("reg"), "populacao": m["p"],
                    "pib_per_capita": m.get("pib"), "x": m["x"], "legacy": False,
                }
        return registros, False
    legado = read_json(LEGACY_PATH, {})
    for cid, c in legado.items():
        registros[cid] = {
            "id": cid, "nome": c["nome"], "uf": c["uf"], "uf_slug": c["uf_slug"], "slug": c["slug"],
            "micro": None, "regiao": None, "populacao": c["populacao"],
            "pib_per_capita": c.get("pib_per_capita"),
            "x": {s: [m["ativos"], m["abertos_24m"], m["fechados_24m"]] for s, m in c.get("nichos", {}).items()},
            "legacy": True,
        }
    return registros, True


def carregar_estado():
    st = read_json(STATE_PATH, {}) or {}
    st.setdefault("published_cities", [])
    st.setdefault("meta", {})
    st.setdefault("falhas", {})
    st.setdefault("last_updated", None)
    return st


def salvar_estado(st):
    write_json(STATE_PATH, st)


def carregar_narrativa(cid):
    return read_json(os.path.join(NARR_DIR, f"{cid}.json"))


def medianas_uf(registros, cnaes, settings):
    """Mediana de habitantes por estabelecimento por (UF, nicho), usando só municípios com volume mínimo."""
    acc = {}
    for r in registros.values():
        for slug, x in r["x"].items():
            if slug in cnaes and x[0] >= settings["min_ativos_nicho"]:
                acc.setdefault((r["uf"], slug), []).append(r["populacao"] / x[0])
    return {k: statistics.median(v) for k, v in acc.items() if len(v) >= 3}


# ---------- fila ----------

def cidade_elegivel(rec, cnaes, settings):
    n = sum(1 for s, x in rec["x"].items() if s in cnaes and x[0] >= settings["min_ativos_nicho"])
    return n >= settings["min_nichos_cidade"]


def precisa_regerar(rec, nar, limite_pct):
    if not nar or nar.get("fonte") != "gemini" or "base" not in nar:
        return True
    base_x = nar["base"].get("x", {})
    dif = tot = 0
    for slug, x in base_x.items():
        atual = rec["x"].get(slug, [0])[0]
        dif += abs(x[0] - atual)
        tot += x[0]
    return tot > 0 and dif / tot * 100 > limite_pct


def montar_fila(registros, estado, cnaes, settings, limite_cidades, hoje_=None):
    hoje_ = hoje_ or hoje()
    publicadas = list(estado["published_cities"])
    pub_set = set(publicadas)
    refresh = []
    for cid in publicadas:
        rec = registros.get(cid)
        if rec and precisa_regerar(rec, carregar_narrativa(cid), settings["limite_mudanca_regerar_pct"]):
            refresh.append(cid)
    refresh.sort(key=lambda c: -registros[c]["populacao"])
    refresh = refresh[: settings["max_refresh_por_dia"]]

    def bloqueada(cid):
        f = estado["falhas"].get(cid)
        return bool(f and f["n"] >= 3 and (hoje_ - date.fromisoformat(f["ultima"])).days < 7)

    novas = sorted(
        (r for cid, r in registros.items() if cid not in pub_set and cidade_elegivel(r, cnaes, settings)),
        key=lambda r: -r["populacao"],
    )
    fila = [(c, "atualizar") for c in refresh if not bloqueada(c)]
    for r in novas:
        if len(fila) >= limite_cidades:
            break
        if not bloqueada(r["id"]):
            fila.append((r["id"], "nova"))
    return fila[:limite_cidades]
