"""Ingest mensal: Receita Federal (cadastro de CNPJ) + IBGE -> data/metricas/{UF}.json

Conta, por município e por nicho (CNAE principal):
  ativos        estabelecimentos com situação cadastral "ativa"
  abertos_24m   estabelecimentos com início de atividade nos últimos 24 meses
  fechados_24m  estabelecimentos baixados com data da situação nos últimos 24 meses

Uso no GitHub Actions: workflow "Ingest dados". Uso local (se o download falhar no Actions):
  python scripts/01_ingest_dados.py --rfb-dir ./zips_receita
onde ./zips_receita contém Estabelecimentos*.zip e Municipios*.zip baixados manualmente.
"""
import argparse
import csv
import io
import os
import re
import sys
import tempfile
import zipfile
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

RFB_BASE_PADRAO = "https://arquivos.receitafederal.gov.br/dados/cnpj/dados_abertos_cnpj/"
IBGE = "https://servicodados.ibge.gov.br/api"
COL_SITUACAO, COL_DATA_SIT, COL_DATA_INI, COL_CNAE, COL_UF, COL_MUN = 5, 6, 10, 11, 19, 20


def sessao():
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = "qualnegocio-ingest/1.0 (+https://qualnegocio.com)"
    return s


# ---------------- IBGE ----------------

def _uf_info(m):
    micro = m.get("microrregiao") or {}
    uf = (micro.get("mesorregiao") or {}).get("UF")
    if not uf:
        uf = ((m.get("regiao-imediata") or {}).get("regiao-intermediaria") or {}).get("UF")
    nome_micro = micro.get("nome") or (m.get("regiao-imediata") or {}).get("nome")
    return uf, nome_micro


def _serie_mais_recente(item):
    serie = item.get("serie") or {}
    for ano in sorted(serie, reverse=True):
        v = serie[ano]
        if v not in (None, "", "-", "..", "...", "X"):
            try:
                return ano, float(v)
            except ValueError:
                continue
    return None, None


def _agregado(ses, tabela, variavel):
    url = f"{IBGE}/v3/agregados/{tabela}/periodos/-1/variaveis/{variavel}?localidades=N6[all]"
    r = ses.get(url, timeout=120)
    r.raise_for_status()
    out = {}
    for var in r.json():
        for res in var.get("resultados", []):
            for s in res.get("series", []):
                ano, val = _serie_mais_recente(s)
                if val is not None:
                    out[str(s["localidade"]["id"])] = (ano, val)
    return out


def carregar_ibge(cache_path, forcar=False, max_idade_dias=60):
    cache = C.read_json(cache_path)
    if cache and not forcar:
        idade = (C.hoje() - date.fromisoformat(cache["gerado_em"])).days
        if idade <= max_idade_dias:
            print(f"IBGE: usando cache de {cache['gerado_em']} ({len(cache['municipios'])} municípios)")
            return cache
    print("IBGE: baixando municípios, população e PIB...")
    ses = sessao()
    r = ses.get(f"{IBGE}/v1/localidades/municipios", timeout=120)
    r.raise_for_status()
    municipios = {}
    for m in r.json():
        uf, micro = _uf_info(m)
        if not uf:
            continue
        municipios[str(m["id"])] = {
            "nome": m["nome"], "uf": uf["sigla"], "reg": (uf.get("regiao") or {}).get("nome"), "micro": micro,
        }
    pop = _agregado(ses, 6579, 9324)          # estimativa de população
    try:
        pib = _agregado(ses, 5938, 37)        # PIB a preços correntes (mil R$)
    except Exception as e:  # noqa: BLE001
        print(f"  aviso: PIB indisponível ({e}); seguindo sem PIB per capita")
        pib = {}
    anos_pop = set()
    for cid, m in municipios.items():
        if cid in pop:
            m["pop"] = int(pop[cid][1])
            anos_pop.add(pop[cid][0])
        if cid in pib and m.get("pop"):
            m["pib_pc"] = int(round(pib[cid][1] * 1000 / m["pop"]))
    municipios = {k: v for k, v in municipios.items() if v.get("pop")}
    cache = {"gerado_em": C.hoje().isoformat(), "ano_populacao": max(anos_pop) if anos_pop else None, "municipios": municipios}
    C.write_json(cache_path, cache, compact=True)
    print(f"IBGE: {len(municipios)} municípios com população.")
    return cache


# ---------------- Receita Federal ----------------

def listar_remoto(ses, base, mes=None):
    def hrefs(url):
        r = ses.get(url, timeout=120)
        r.raise_for_status()
        return re.findall(r'href="([^"]+)"', r.text)

    base = base if base.endswith("/") else base + "/"
    if not mes:
        meses = sorted({m.group(1) for h in hrefs(base) for m in [re.search(r"(\d{4}-\d{2})/?$", h)] if m})
        if not meses:
            raise RuntimeError(
                f"Não encontrei pastas AAAA-MM em {base}. A Receita pode ter mudado a estrutura: "
                "baixe os zips manualmente e use --rfb-dir, ou informe --rfb-base / --mes.")
        mes = meses[-1]
    url_mes = f"{base}{mes}/"
    nomes = sorted({os.path.basename(h) for h in hrefs(url_mes) if h.lower().endswith(".zip")})
    est = [n for n in nomes if n.lower().startswith("estabelecimentos")]
    mun = [n for n in nomes if n.lower().startswith("municipios")]
    if not est or not mun:
        raise RuntimeError(f"Arquivos Estabelecimentos*/Municipios* não encontrados em {url_mes}.")
    return mes, url_mes, est, mun


def baixar(ses, url, destino):
    with ses.get(url, stream=True, timeout=(30, 300)) as r:
        r.raise_for_status()
        with open(destino, "wb") as f:
            for bloco in r.iter_content(1024 * 1024):
                f.write(bloco)


def linhas_csv(caminho_zip):
    with zipfile.ZipFile(caminho_zip) as z:
        for nome in z.namelist():
            if nome.endswith("/"):
                continue
            with z.open(nome) as bruto:
                txt = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
                yield from csv.reader(txt, delimiter=";", quotechar='"')


def ler_municipios_rfb(caminho_zip):
    return {row[0].strip(): C.norm_nome(row[1]) for row in linhas_csv(caminho_zip) if len(row) >= 2}


def corte_24m(hoje, meses):
    ano = hoje.year - meses // 12
    mes = hoje.month - meses % 12
    if mes <= 0:
        mes += 12
        ano -= 1
    dia = min(hoje.day, 28) if mes == 2 else hoje.day
    return f"{ano:04d}{mes:02d}{dia:02d}"


def contar_estabelecimentos(caminho_zip, cnae_para_slugs, corte, contagem):
    n_linhas = n_uteis = 0
    for row in linhas_csv(caminho_zip):
        n_linhas += 1
        if len(row) <= COL_MUN:
            continue
        slugs = cnae_para_slugs.get(row[COL_CNAE])
        if not slugs:
            continue
        n_uteis += 1
        try:
            sit = int(row[COL_SITUACAO])
        except ValueError:
            continue
        chave = (row[COL_UF], row[COL_MUN].strip())
        for slug in slugs:
            x = contagem[chave][slug]
            if sit == 2:
                x[0] += 1
            if row[COL_DATA_INI] >= corte:
                x[1] += 1
            if sit == 8 and row[COL_DATA_SIT] >= corte:
                x[2] += 1
    return n_linhas, n_uteis


# ---------------- principal ----------------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rfb-dir", help="pasta local com Estabelecimentos*.zip e Municipios*.zip")
    ap.add_argument("--rfb-base", default=os.environ.get("RFB_BASE_URL", RFB_BASE_PADRAO))
    ap.add_argument("--mes", help="pasta AAAA-MM da Receita (padrão: a mais recente)")
    ap.add_argument("--ibge-cache", default=os.path.join(C.DATA_DIR, "ibge_cache.json"))
    ap.add_argument("--forcar-ibge", action="store_true")
    ap.add_argument("--saida", default=C.METRICAS_DIR)
    args = ap.parse_args(argv)

    settings = C.load_settings()
    cnaes = C.load_cnaes()
    mapa, ignorados = C.mapa_cnae_para_nicho(cnaes)
    cnae_para_slugs = {d: [slug] for d, slug in mapa.items()}
    for slug, dono, codigo in ignorados:
        print(f"AVISO: nicho '{slug}' compartilha o CNAE {codigo} com '{dono}' e não será contado "
              f"(a Receita não distingue os dois). Dê a ele um CNAE próprio em config/cnaes_meta.json para incluí-lo.")

    ibge = carregar_ibge(args.ibge_cache, forcar=args.forcar_ibge)
    por_uf_nome = {(m["uf"], C.norm_nome(m["nome"])): cid for cid, m in ibge["municipios"].items()}
    corte = corte_24m(C.hoje(), settings["meses_janela"])
    print(f"Janela: aberturas e baixas desde {corte}")

    contagem = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
    tmp = None
    try:
        if args.rfb_dir:
            nomes = sorted(os.listdir(args.rfb_dir))
            est = [os.path.join(args.rfb_dir, n) for n in nomes if n.lower().startswith("estabelecimentos") and n.lower().endswith(".zip")]
            mun = [os.path.join(args.rfb_dir, n) for n in nomes if n.lower().startswith("municipios") and n.lower().endswith(".zip")]
            fonte = args.mes or "local"
            if not est or not mun:
                raise SystemExit(f"ERRO: faltam Estabelecimentos*.zip ou Municipios*.zip em {args.rfb_dir}")
            baixar_fn = None
        else:
            ses = sessao()
            fonte, url_mes, nomes_est, nomes_mun = listar_remoto(ses, args.rfb_base, args.mes)
            tmp = tempfile.mkdtemp(prefix="rfb_")
            print(f"Receita Federal: lote {fonte} ({len(nomes_est)} arquivos de estabelecimentos)")
            est, mun = nomes_est, nomes_mun
            baixar_fn = lambda nome: (baixar(ses, url_mes + nome, os.path.join(tmp, nome)), os.path.join(tmp, nome))[1]  # noqa: E731

        def caminho(item):
            return baixar_fn(item) if baixar_fn else item

        p_mun = caminho(mun[0])
        rfb_nome = ler_municipios_rfb(p_mun)
        if baixar_fn:
            os.remove(p_mun)
        total_uteis = 0
        for item in est:
            p = caminho(item)
            n, u = contar_estabelecimentos(p, cnae_para_slugs, corte, contagem)
            total_uteis += u
            print(f"  {os.path.basename(p)}: {n:,} linhas, {u:,} nos nichos monitorados")
            if baixar_fn:
                os.remove(p)
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    # RFB (UF, código) -> IBGE
    shards, sem_mapa, ativos_total, ativos_perdidos = defaultdict(dict), defaultdict(int), 0, 0
    for (uf, cod_rfb), nichos in contagem.items():
        ativos = sum(x[0] for x in nichos.values())
        ativos_total += ativos
        cid = por_uf_nome.get((uf, rfb_nome.get(cod_rfb, "")))
        if not cid:
            sem_mapa[(uf, rfb_nome.get(cod_rfb, cod_rfb))] += ativos
            ativos_perdidos += ativos
            continue
        m = ibge["municipios"][cid]
        x = {s: v for s, v in nichos.items() if any(v)}
        if not x:
            continue
        ant = shards[uf].get(cid)
        if ant:  # dois códigos RFB para o mesmo município: soma
            for s, v in x.items():
                a = ant["x"].setdefault(s, [0, 0, 0])
                for i in range(3):
                    a[i] += v[i]
            continue
        slug = C.slugify(m["nome"])
        shards[uf][cid] = {"n": m["nome"], "uf": uf, "s": slug, "p": m["pop"], "pib": m.get("pib_pc"),
                           "micro": m.get("micro"), "reg": m.get("reg"), "x": x}

    if ativos_total == 0:
        raise SystemExit("ERRO: nenhum estabelecimento contado. Nada foi gravado.")
    perc_perdido = ativos_perdidos / ativos_total * 100
    print(f"Municípios sem correspondência no IBGE: {len(sem_mapa)} ({perc_perdido:.2f}% dos ativos)")
    for (uf, nome), a in sorted(sem_mapa.items(), key=lambda kv: -kv[1])[:8]:
        print(f"    {uf} {nome}: {a} ativos")
    if perc_perdido > 5:
        raise SystemExit("ERRO: mais de 5% dos ativos sem correspondência de município; abortando sem gravar.")

    # trava de sanidade contra dado anterior
    anterior = sum(x[0] for f in os.listdir(args.saida) if f.endswith(".json") and not f.startswith("_")
                   for m in C.read_json(os.path.join(args.saida, f), {}).get("municipios", {}).values() for x in m["x"].values()) \
        if os.path.isdir(args.saida) else 0
    novo = sum(x[0] for uf in shards.values() for m in uf.values() for x in m["x"].values())
    if anterior and novo < anterior * 0.5:
        raise SystemExit(f"ERRO: total de ativos caiu de {anterior} para {novo} (>50%). Abortando para não sobrescrever dados bons.")

    os.makedirs(args.saida, exist_ok=True)
    for uf, municipios in shards.items():
        C.write_json(os.path.join(args.saida, f"{uf}.json"), {"municipios": municipios}, compact=True)
    C.write_json(os.path.join(args.saida, "_info.json"), {
        "fonte_rfb": fonte, "gerado_em": C.hoje().isoformat(), "ano_populacao": ibge.get("ano_populacao"),
        "municipios": sum(len(v) for v in shards.values()), "janela_meses": settings["meses_janela"],
    })
    print(f"Gravado: {sum(len(v) for v in shards.values())} municípios em {len(shards)} UFs. Ativos nos nichos: {novo:,}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
