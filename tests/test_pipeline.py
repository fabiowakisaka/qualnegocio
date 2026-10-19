"""Teste ponta a ponta offline: dados sintéticos da Receita/IBGE -> ingest -> lote (Gemini simulado) -> build.
Rode com:  python tests/test_pipeline.py   (ou pytest)"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, "scripts"))

HOJE = "2026-10-19"
NICHOS_TESTE = ["hamburgueria", "pet-shop", "barbearia", "marmitaria", "padaria", "pizzaria", "farmacia", "academia"]


def criar_fixture(base):
    for pasta in ("config", "templates", "static"):
        shutil.copytree(os.path.join(RAIZ, pasta), os.path.join(base, pasta))
    os.makedirs(os.path.join(base, "data"))
    cnaes = {c["slug"]: c for c in json.load(open(os.path.join(base, "config", "cnaes_meta.json"), encoding="utf-8"))}
    nomes = [("SP", "Campinas", "Campinas"), ("SP", "Americana", "Campinas"), ("SP", "Santa Bárbara d'Oeste", "Campinas"),
             ("SP", "Sumaré", "Campinas"), ("SP", "Hortolândia", "Campinas"), ("SP", "Paulínia", "Campinas"),
             ("SP", "Piracicaba", "Piracicaba"), ("SP", "Rio Claro", "Piracicaba"), ("SP", "Limeira", "Piracicaba"),
             ("MG", "Uberlândia", "Uberlândia"), ("MG", "Uberaba", "Uberaba"), ("MG", "Araguari", "Uberlândia")]
    municipios, esperado, linhas_rfb, linhas_mun = {}, {}, [], []
    for i, (uf, nome, micro) in enumerate(nomes):
        cid = f"35{i:05d}" if uf == "SP" else f"31{i:05d}"
        pop = 1_100_000 - i * 80_000
        cod = f"{7000 + i}"
        municipios[cid] = {"nome": nome, "uf": uf, "reg": "Sudeste", "micro": micro, "pop": pop, "pib_pc": 50000 + i * 1000}
        linhas_mun.append([cod, nome.upper().replace("Á", "A").replace("Â", "A").replace("É", "E").replace("Ã", "A").replace("'", " ")])
        for k, slug in enumerate(NICHOS_TESTE):
            ideal = cnaes[slug]["hab_por_cnpj_ideal"]
            ativos = max(6, int(pop / ideal * (0.6 + 0.1 * k)))
            recentes_ativos, baixadas_recentes = max(1, ativos // 5), max(1, ativos // 8)
            cnae = "".join(ch for ch in cnaes[slug]["cnae"] if ch.isdigit())
            for n in range(ativos):
                linhas_rfb.append(_linha(uf, cod, cnae, "02", "20200101", "20250101" if n < recentes_ativos else "20150101"))
            for n in range(baixadas_recentes):
                linhas_rfb.append(_linha(uf, cod, cnae, "08", "20250601", "20200101"))
            for n in range(3):  # baixadas antigas: fora da janela
                linhas_rfb.append(_linha(uf, cod, cnae, "08", "20200101", "20100101"))
            esperado[(cid, slug)] = [ativos, recentes_ativos, baixadas_recentes]
    linhas_rfb.append(_linha("EX", "9999", "5611203", "02", "20200101", "20200101"))      # exterior: ignorar
    linhas_rfb.append(_linha("SP", "7000", "0111301", "02", "20200101", "20200101"))      # CNAE fora dos nichos
    rfb = os.path.join(base, "rfb")
    os.makedirs(rfb)
    for nome_zip, linhas, arq in (("Estabelecimentos0.zip", linhas_rfb, "K3241.K03200Y0.D60000.ESTABELE"),
                                  ("Municipios.zip", linhas_mun, "F.K03200$Z.D60000.MUNICCSV")):
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", quotechar='"', quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(linhas)
        with zipfile.ZipFile(os.path.join(rfb, nome_zip), "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(arq, buf.getvalue().encode("latin-1"))
    json.dump({"gerado_em": HOJE, "ano_populacao": "2025", "municipios": municipios},
              open(os.path.join(base, "data", "ibge_cache.json"), "w", encoding="utf-8"), ensure_ascii=False)
    return rfb, esperado


def _linha(uf, cod, cnae, sit, data_sit, data_ini):
    r = [""] * 30
    r[0], r[1], r[2], r[3] = "12345678", "0001", "99", "1"
    r[5], r[6], r[10], r[11], r[19], r[20] = sit, data_sit, data_ini, cnae, uf, cod
    return r


def rodar(script, base, *args, extra_env=None):
    env = dict(os.environ, QN_BASE_DIR=base, QN_HOJE=HOJE, PYTHONIOENCODING="utf-8")
    env.pop("GEMINI_API_KEY", None)
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, os.path.join(RAIZ, "scripts", script), *args], env=env, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def test_validator():
    from validator import montar_whitelist, validar_texto
    wl = montar_whitelist({"ativos": 418, "hab": 579, "pct": 75, "dif": -22.5}, 2026)
    assert validar_texto("Há 418 estabelecimentos, 1 para cada 579 habitantes, pressão de 75% em 24 meses.", wl, 10)[0]
    assert validar_texto("Há 1.200 estabelecimentos na cidade inteira.", wl, 10)[0] is False
    assert validar_texto("Lucro garantido para quem abrir hoje na cidade.", wl, 10)[0] is False
    assert validar_texto("Fica 22,5% abaixo da mediana do estado, em 24 meses.", wl, 10)[0]


def test_derivacao():
    import common as C
    d = C.derivar_nicho([418, 112, 84], 242018, 850)
    assert d["hab_por_cnpj"] == 579 and d["taxa_pressao_24m"] == 75 and d["indice_saturacao"] == "Alta"
    assert C.derivar_nicho([158, 49, 16], 242018, 1100)["indice_saturacao"] == "Baixa"
    assert C.slugify("Santa Bárbara d'Oeste") == "santa-barbara-doeste"


def test_ponta_a_ponta():
    base = tempfile.mkdtemp(prefix="qn_test_")
    try:
        rfb, esperado = criar_fixture(base)
        # 1) lote antes do ingest deve recusar
        cod, out = rodar("02_generate_batch.py", base, extra_env={"QN_FAKE_GEMINI": "1"})
        assert cod == 1 and "ingest" in out.lower(), out
        # 2) ingest
        cod, out = rodar("01_ingest_dados.py", base, "--rfb-dir", rfb)
        assert cod == 0, out
        for (cid, slug), exp in esperado.items():
            uf = "SP" if cid.startswith("35") else "MG"
            shard = json.load(open(os.path.join(base, "data", "metricas", f"{uf}.json"), encoding="utf-8"))
            assert shard["municipios"][cid]["x"][slug] == exp, (cid, slug, shard["municipios"][cid]["x"][slug], exp)
        # 3) lote com Gemini simulado: 5 cidades, mais populosas primeiro
        cod, out = rodar("02_generate_batch.py", base, "--max-cidades", "5", extra_env={"QN_FAKE_GEMINI": "1", "MAX_CHAMADAS_GEMINI": "8"})
        assert cod == 0 and "5 publicada(s)" in out, out
        estado = json.load(open(os.path.join(base, "data", "published_state.json"), encoding="utf-8"))
        assert len(estado["published_cities"]) == 5 and estado["published_cities"][0] == "3500000", estado
        # 4) teto de chamadas respeitado
        cod, out = rodar("02_generate_batch.py", base, "--max-cidades", "5", "--max-chamadas", "2", extra_env={"QN_FAKE_GEMINI": "1"})
        assert "2 chamada(s)" in out and "2 publicada(s)" in out, out
        # 5) build
        cod, out = rodar("04_build_static.py", base)
        assert cod == 0, out
        dist = os.path.join(base, "dist")
        for caminho in ("index.html", "sp/index.html", "sp/campinas/index.html", "sp/campinas/hamburgueria/index.html",
                        "negocios/index.html", "negocios/hamburgueria/index.html", "404.html", "sitemap.xml",
                        "sitemap-1.xml", "robots.txt", "llms.txt", "busca/c-c.json", "css/site.css", "CNAME"):
            assert os.path.exists(os.path.join(dist, caminho)), caminho
        sm = open(os.path.join(dist, "sitemap-1.xml"), encoding="utf-8").read()
        assert "https://qualnegocio.com/sp/campinas/hamburgueria/" in sm and "<lastmod>2026-10-19</lastmod>" in sm
        assert "qualnegocio.com/sitemap-1.xml" in open(os.path.join(dist, "sitemap.xml"), encoding="utf-8").read()
        assert "Sitemap: https://qualnegocio.com/sitemap.xml" in open(os.path.join(dist, "robots.txt"), encoding="utf-8").read()
        llms = open(os.path.join(dist, "llms.txt"), encoding="utf-8").read()
        assert "Cobertura atual: 7 cidades" in llms and "/sp/campinas/" in llms
        pagina = open(os.path.join(dist, "sp", "campinas", "hamburgueria", "index.html"), encoding="utf-8").read()
        assert "../../../css/site.css" in pagina and "BreadcrumbList" in pagina and "nofollow sponsored" in pagina
        busca = json.load(open(os.path.join(dist, "busca", "c-c.json"), encoding="utf-8"))
        assert any(i[0] == "Campinas" for i in busca)
        # 6) segunda rodada do ingest: dados iguais -> nenhuma cidade precisa regerar
        cod, out = rodar("02_generate_batch.py", base, "--dry-run")
        assert "[atualizar]" not in out, out
        print("ponta a ponta OK")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    test_validator()
    test_derivacao()
    test_ponta_a_ponta()
    print("todos os testes passaram")
