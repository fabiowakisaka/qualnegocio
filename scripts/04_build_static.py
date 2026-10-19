"""Gera o site estático em dist/: home, estados, cidades, nichos, rankings, busca, sitemaps, robots e llms.txt."""
import json
import os
import shutil
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jinja2 import Environment, FileSystemLoader, select_autoescape

import common as C
from validator import build_fallback_narrative

DIST_DIR = os.environ.get("QN_DIST") or os.path.join(C.BASE_DIR, "dist")
TEMPLATES_DIR = os.path.join(C.BASE_DIR, "templates")
STATIC_DIR = os.path.join(C.BASE_DIR, "static")
ORDEM_SAT = {"Baixa": 0, "Moderada": 1, "Alta": 2}
UF_NOME = {"AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará",
           "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão", "MT": "Mato Grosso",
           "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
           "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
           "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina",
           "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins"}
INTRO_PADRAO = ("O QualNegócio publica estudos de viabilidade por município e por ramo de negócio, "
                "com contagens do cadastro público de CNPJ (Receita Federal) e dados do IBGE.")


class Site:
    def __init__(self):
        self.settings = C.load_settings()
        self.affiliates = C.read_json(os.path.join(C.CONFIG_DIR, "affiliates.json"))
        self.domain = (os.environ.get("SITE_DOMAIN") or self.affiliates["domain"]).rstrip("/")
        self.cnaes = C.load_cnaes()
        self.state = C.carregar_estado()
        self.info = C.info_ingest()
        self.hoje = C.hoje()
        self.legado = False
        self.last_updated = self.state.get("last_updated") or self.hoje.isoformat()
        self.urls = []  # (url absoluta, lastmod)
        env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape(["html"]))
        env.filters["milhar"] = C.fmt_int
        env.filters["data_br"] = C.data_br
        self.env = env

    # ---- escrita ----
    def escrever(self, caminho_rel, template, ctx, lastmod=None, sitemap=True):
        assert not caminho_rel or caminho_rel.endswith("/"), caminho_rel
        rel = "../" * len([p for p in caminho_rel.split("/") if p])
        canonical = f"{self.domain}/{caminho_rel}"
        crumbs = ctx.get("breadcrumbs")
        ld = None
        if crumbs:
            ld = json.dumps({
                "@context": "https://schema.org", "@type": "BreadcrumbList",
                "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": b["nome"],
                                     "item": f"{self.domain}/{b['path']}"} for i, b in enumerate(crumbs)],
            }, ensure_ascii=False).replace("</", "<\\/")
        html = self.env.get_template(template).render(
            rel=rel, canonical_url=canonical, affiliates=self.affiliates, last_updated=self.last_updated,
            ano=self.hoje.year, fonte_rfb=self.info.get("fonte_rfb"), ld_breadcrumbs=ld, legado=self.legado, **ctx)
        destino = os.path.join(DIST_DIR, caminho_rel, "index.html")
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        with open(destino, "w", encoding="utf-8") as f:
            f.write(html)
        if sitemap:
            self.urls.append((canonical, (lastmod or self.last_updated)[:10]))


def montar_cidades(site, registros):
    cidades, narrativas = {}, {}
    for cid in site.state["published_cities"]:
        rec = registros.get(cid)
        if not rec:
            continue
        rec = dict(rec)
        nar = C.carregar_narrativa(cid)
        if nar and nar.get("base"):  # página = retrato coerente com o texto gerado
            base = nar["base"]
            rec["populacao"] = base["populacao"]
            rec["pib_per_capita"] = base.get("pib_per_capita")
            rec["x"] = base["x"]
        cidade = C.derivar_cidade(rec, site.cnaes, site.settings)
        if not cidade["nichos"]:
            continue
        cidades[cid] = cidade
        narrativas[cid] = nar or {}
    return cidades, narrativas


def lastmod_cidade(site, cid):
    return (site.state["meta"].get(cid, {}).get("atualizado_em") or site.last_updated)


def build():
    site = Site()
    if os.path.exists(DIST_DIR):
        shutil.rmtree(DIST_DIR)
    os.makedirs(DIST_DIR)
    for item in os.listdir(STATIC_DIR):
        src = os.path.join(STATIC_DIR, item)
        dst = os.path.join(DIST_DIR, item)
        shutil.copytree(src, dst) if os.path.isdir(src) else shutil.copy2(src, dst)

    registros, legado = C.carregar_registros()
    site.legado = legado
    cidades, narrativas = montar_cidades(site, registros)
    if legado:
        print("AVISO: usando data/base_metricas.json (legado). Rode o ingest para usar dados reais da Receita/IBGE.")

    por_uf = defaultdict(list)
    for c in cidades.values():
        por_uf[c["uf"]].append(c)
    por_micro = defaultdict(list)
    for c in cidades.values():
        if c.get("micro"):
            por_micro[(c["uf"], c["micro"])].append(c)
    por_nicho = defaultdict(list)  # slug -> linhas
    for cid, c in cidades.items():
        for slug, m in c["nichos"].items():
            por_nicho[slug].append({"nome": c["nome"], "uf": c["uf"], "uf_slug": c["uf_slug"], "slug": c["slug"], "m": m, "pop": c["populacao"]})

    # ---------- cidades ----------
    for cid, c in cidades.items():
        nar = narrativas[cid]
        base_path = f"{c['uf_slug']}/{c['slug']}/"
        lm = lastmod_cidade(site, cid)
        linhas = [{"slug": s, "meta": site.cnaes[s], "metrics": m} for s, m in c["nichos"].items()]
        linhas.sort(key=lambda r: (ORDEM_SAT.get(r["metrics"]["indice_saturacao"], 3), -(r["metrics"]["razao_ideal"] or 0)))
        vizinhas = sorted((v for v in por_micro.get((c["uf"], c.get("micro")), []) if v["id"] != c["id"]),
                          key=lambda v: -v["populacao"])[: site.settings["vizinhas_max"]]
        crumbs = [{"nome": "Início", "path": ""}, {"nome": UF_NOME.get(c["uf"], c["uf"]), "path": f"{c['uf_slug']}/"},
                  {"nome": c["nome"], "path": base_path}]
        n_baixa = sum(1 for r in linhas if r["metrics"]["indice_saturacao"] == "Baixa")
        site.escrever(base_path, "city_hub.html", dict(
            page_title=f"Qual negócio abrir em {c['nome']} ({c['uf']}) em {site.hoje.year}",
            meta_description=f"Ranking dos negócios com menos concorrência em {c['nome']} ({c['uf']}): {len(linhas)} ramos analisados com dados da Receita Federal e do IBGE.",
            city=c, narrative=nar, niche_rows=linhas, n_baixa=n_baixa, vizinhas=vizinhas, breadcrumbs=crumbs), lm)
        nichos_nar = nar.get("nichos", {})
        for r in linhas:
            slug, meta, m = r["slug"], r["meta"], r["metrics"]
            texto = nichos_nar.get(slug) or build_fallback_narrative(c, meta, m)
            viz = []
            for v in vizinhas:
                vm = v["nichos"].get(slug)
                if vm:
                    viz.append({"nome": v["nome"], "uf_slug": v["uf_slug"], "slug": v["slug"], "indice": vm["indice_saturacao"]})
            outros = [o for o in linhas if o["slug"] != slug][:5]
            crumbs_n = crumbs + [{"nome": meta["nome"], "path": f"{base_path}{slug}/"}]
            site.escrever(f"{base_path}{slug}/", "city_niche.html", dict(
                page_title=f"Abrir {meta['nome']} em {c['nome']} ({c['uf']}): saturação e viabilidade",
                meta_description=(f"{c['nome']} tem {C.fmt_int(m['ativos'])} estabelecimentos ativos de {meta['nome'].lower()}, "
                                  f"1 para cada {C.fmt_int(m['hab_por_cnpj'])} habitantes. Saturação {m['indice_saturacao'].lower()}."),
                city=c, meta=meta, meta_slug=slug, metrics=m, nar=texto, vizinhas=viz, outros=outros,
                analise_em=nar.get("gerado_em"), breadcrumbs=crumbs_n), lm)

    # ---------- nichos: ranking nacional e por UF ----------
    nichos_lista = sorted(({"slug": s, "nome": site.cnaes[s]["nome"], "cidades": len(v)} for s, v in por_nicho.items()),
                          key=lambda n: n["nome"])
    max_linhas = site.settings["ranking_max_linhas"]
    for slug, linhas in por_nicho.items():
        meta = site.cnaes[slug]
        ordenadas = sorted(linhas, key=lambda r: (ORDEM_SAT.get(r["m"]["indice_saturacao"], 3), -(r["m"]["razao_ideal"] or 0), -r["pop"]))
        ufs = sorted(({"slug": u.lower(), "nome": UF_NOME.get(u, u), "cidades": n}
                      for u, n in _contar_uf(linhas).items()), key=lambda u: u["nome"])
        site.escrever(f"negocios/{slug}/", "negocio_ranking.html", dict(
            page_title=f"Onde abrir {meta['nome']}: cidades com menos concorrência em {site.hoje.year}",
            meta_description=f"Ranking das cidades com mais espaço para abrir {meta['nome'].lower()}, com dados de CNPJ da Receita Federal.",
            titulo=f"Onde abrir {meta['nome']}: cidades com mais espaço", nicho_slug=slug,
            intro=f"Cidades com estudo publicado, ordenadas da menor para a maior saturação em {meta['nome'].lower()}.",
            linhas=ordenadas[:max_linhas], omitidas=max(0, len(ordenadas) - max_linhas), ufs=ufs,
            breadcrumbs=[{"nome": "Início", "path": ""}, {"nome": "Negócios", "path": "negocios/"},
                         {"nome": meta["nome"], "path": f"negocios/{slug}/"}]))
        por_uf_nicho = defaultdict(list)
        for r in ordenadas:
            por_uf_nicho[r["uf"]].append(r)
        for uf, rs in por_uf_nicho.items():
            if len(rs) < 3:
                continue
            nome_uf = UF_NOME.get(uf, uf)
            site.escrever(f"negocios/{slug}/{uf.lower()}/", "negocio_ranking.html", dict(
                page_title=f"Onde abrir {meta['nome']} em {nome_uf}: melhores cidades",
                meta_description=f"Cidades de {nome_uf} com mais espaço para abrir {meta['nome'].lower()}, segundo o cadastro de CNPJ.",
                titulo=f"Onde abrir {meta['nome']} em {nome_uf}", nicho_slug=slug,
                intro=f"Cidades de {nome_uf} com estudo publicado, da menor para a maior saturação.",
                linhas=rs[:max_linhas], omitidas=max(0, len(rs) - max_linhas), ufs=None,
                breadcrumbs=[{"nome": "Início", "path": ""}, {"nome": "Negócios", "path": "negocios/"},
                             {"nome": meta["nome"], "path": f"negocios/{slug}/"},
                             {"nome": nome_uf, "path": f"negocios/{slug}/{uf.lower()}/"}]))
    site.escrever("negocios/", "negocios_index.html", dict(
        page_title="Ramos de negócio analisados por cidade", meta_description="Lista de ramos de negócio com ranking de cidades por saturação.",
        nichos_lista=nichos_lista, breadcrumbs=[{"nome": "Início", "path": ""}, {"nome": "Negócios", "path": "negocios/"}]))

    # ---------- estados ----------
    ufs_info = []
    for uf, lista in sorted(por_uf.items(), key=lambda kv: UF_NOME.get(kv[0], kv[0])):
        uf_slug = uf.lower()
        nichos_uf = defaultdict(int)
        for c in lista:
            for s in c["nichos"]:
                nichos_uf[s] += 1
        n_uf = sorted(({"slug": s, "nome": site.cnaes[s]["nome"], "cidades": n} for s, n in nichos_uf.items() if n >= 3),
                      key=lambda n: n["nome"])
        linhas = sorted(({**c, "n_baixa": sum(1 for m in c["nichos"].values() if m["indice_saturacao"] == "Baixa")} for c in lista),
                        key=lambda c: C.strip_accents(c["nome"]).lower())
        lm = max(lastmod_cidade(site, c["id"]) for c in lista)
        site.escrever(f"{uf_slug}/", "uf.html", dict(
            page_title=f"Qual negócio abrir em {UF_NOME.get(uf, uf)} em {site.hoje.year}",
            meta_description=f"{len(lista)} cidades de {UF_NOME.get(uf, uf)} com estudo de viabilidade por ramo de negócio.",
            uf_nome=UF_NOME.get(uf, uf), uf_slug=uf_slug, cidades=linhas, nichos_uf=n_uf,
            breadcrumbs=[{"nome": "Início", "path": ""}, {"nome": UF_NOME.get(uf, uf), "path": f"{uf_slug}/"}]), lm)
        ufs_info.append({"slug": uf_slug, "nome": UF_NOME.get(uf, uf), "cidades": len(lista)})

    # ---------- home ----------
    todas = list(cidades.values())
    topo = sorted(todas, key=lambda c: -c["populacao"])[:24]
    novas = sorted(todas, key=lambda c: site.state["meta"].get(c["id"], {}).get("pub_em", ""), reverse=True)[:12]
    novas = [c for c in novas if site.state["meta"].get(c["id"], {}).get("pub_em")]
    site.escrever("", "home.html", dict(
        page_title=f"{site.affiliates['site_name']}: qual negócio abrir na sua cidade",
        meta_description="Estudo de viabilidade gratuito por município: concorrência, aberturas e baixas por ramo, com dados da Receita Federal e do IBGE.",
        total_cidades=len(todas), total_nichos=len(por_nicho), ufs=ufs_info, cidades_topo=topo,
        cidades_novas=novas, nichos_lista=nichos_lista, breadcrumbs=None))

    gerar_busca(site, todas, nichos_lista)
    gerar_404(site)
    gerar_sitemaps(site)
    gerar_llms(site, todas, ufs_info, nichos_lista)
    print(f"Build concluído: {len(todas)} cidades, {len(site.urls)} URLs no sitemap -> {DIST_DIR}")


def _contar_uf(linhas):
    c = defaultdict(int)
    for r in linhas:
        c[r["uf"]] += 1
    return c


def gerar_busca(site, todas, nichos_lista):
    shards = defaultdict(dict)
    for c in todas:
        item = [c["nome"], c["uf"], f"{c['uf_slug']}/{c['slug']}/", c["populacao"]]
        palavras = C.norm_nome(c["nome"]).lower().split()
        letras = {palavras[0][0]} | {p[0] for p in palavras[1:] if len(p) >= 3}
        for l in letras:
            shards[l if l.isalnum() else "0"][c["id"]] = item
    pasta = os.path.join(DIST_DIR, "busca")
    for letra, itens in shards.items():
        lista = sorted(itens.values(), key=lambda i: -i[3])
        C.write_json(os.path.join(pasta, f"c-{letra}.json"), lista, compact=True)


def gerar_404(site):
    html = site.env.get_template("404.html").render(
        rel=site.domain + "/", canonical_url=site.domain + "/", affiliates=site.affiliates, last_updated=site.last_updated,
        ano=site.hoje.year, fonte_rfb=site.info.get("fonte_rfb"), legado=site.legado, page_title="Página não encontrada | QualNegócio",
        meta_description="Página não encontrada.", breadcrumbs=None, ld_breadcrumbs=None)
    with open(os.path.join(DIST_DIR, "404.html"), "w", encoding="utf-8") as f:
        f.write(html)


def gerar_sitemaps(site):
    por_arquivo = site.settings["sitemap_urls_por_arquivo"]
    urls = sorted(set(site.urls))
    partes = [urls[i:i + por_arquivo] for i in range(0, len(urls), por_arquivo)] or [[]]
    nomes = []
    for i, parte in enumerate(partes, 1):
        nome = f"sitemap-{i}.xml"
        nomes.append(nome)
        linhas = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        linhas += [f"  <url><loc>{u}</loc><lastmod>{lm}</lastmod></url>" for u, lm in parte]
        linhas.append("</urlset>")
        with open(os.path.join(DIST_DIR, nome), "w", encoding="utf-8") as f:
            f.write("\n".join(linhas) + "\n")
    idx = ['<?xml version="1.0" encoding="UTF-8"?>', '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for nome, parte in zip(nomes, partes):
        lm = max((l for _, l in parte), default=site.last_updated)
        idx.append(f"  <sitemap><loc>{site.domain}/{nome}</loc><lastmod>{lm}</lastmod></sitemap>")
    idx.append("</sitemapindex>")
    with open(os.path.join(DIST_DIR, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(idx) + "\n")
    with open(os.path.join(DIST_DIR, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(f"User-agent: *\nAllow: /\n\nSitemap: {site.domain}/sitemap.xml\n")


def gerar_llms(site, todas, ufs_info, nichos_lista):
    intro_data = C.read_json(os.path.join(C.DATA_DIR, "llms_intro.json"), {}) or {}
    intro = intro_data.get("texto") or INTRO_PADRAO
    d = site.domain
    L = [f"# {site.affiliates['site_name']}", "", f"> {intro}", "",
         f"Atualizado em {C.data_br(site.last_updated)}. Idioma: português do Brasil.", "",
         "## Como os dados são produzidos",
         ("- Números preliminares, em validação; serão substituídos pela contagem oficial do cadastro de CNPJ da Receita Federal."
          if site.legado else
          "- Estabelecimentos ativos, aberturas e baixas dos últimos 24 meses: cadastro público de CNPJ da Receita Federal, contados por município e pelo CNAE principal de cada ramo"
          + (f" (lote {site.info['fonte_rfb']})." if site.info.get("fonte_rfb") else ".")),
         "- População e PIB: IBGE.",
         "- Habitantes por estabelecimento = população ÷ estabelecimentos ativos. Saturação (baixa, moderada, alta) compara essa densidade com uma referência de equilíbrio do ramo e com o saldo de aberturas e baixas.",
         "- Os textos são redigidos com apoio de IA somente a partir desses números e passam por validação automática. Valores de investimento, margem e ticket são estimativas de referência.", "",
         "## Estrutura do site",
         f"- {d}/<uf>/ : cidades de um estado",
         f"- {d}/<uf>/<cidade>/ : ranking de ramos da cidade",
         f"- {d}/<uf>/<cidade>/<ramo>/ : análise de um ramo na cidade",
         f"- {d}/negocios/<ramo>/ : cidades com mais espaço para o ramo, e {d}/negocios/<ramo>/<uf>/ por estado", "",
         f"## Cobertura atual: {len(todas)} cidades em {len(ufs_info)} estados", ""]
    L.append("## Estados")
    L += [f"- [{u['nome']}]({d}/{u['slug']}/): {u['cidades']} cidade(s)" for u in ufs_info]
    L += ["", "## Ramos de negócio"]
    L += [f"- [{n['nome']}]({d}/negocios/{n['slug']}/): {n['cidades']} cidade(s)" for n in nichos_lista]
    L += ["", "## Cidades mais populosas com estudo publicado"]
    L += [f"- [{c['nome']} ({c['uf']})]({d}/{c['uf_slug']}/{c['slug']}/)" for c in sorted(todas, key=lambda c: -c["populacao"])[:100]]
    L += ["", f"Sitemap completo: {d}/sitemap.xml", ""]
    with open(os.path.join(DIST_DIR, "llms.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    build()
