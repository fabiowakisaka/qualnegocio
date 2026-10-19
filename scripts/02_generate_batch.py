"""Lote diário: escolhe as próximas cidades da fila, gera as narrativas com o Gemini, valida e grava.

Limites de segurança (todos configuráveis):
  - CIDADES_POR_DIA      quantas cidades por execução (padrão 5)
  - MAX_CHAMADAS_GEMINI  teto duro de chamadas à API por execução (padrão 8)
  - para ao primeiro erro de cota; a cidade continua na fila e entra no dia seguinte
  - cidade só é publicada se o texto passar pelo validator
"""
import argparse
import json
import os
import sys
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pydantic import BaseModel

import common as C
import gemini_client as G
from validator import build_fallback_narrative, montar_whitelist, validar_texto


class NichoNarr(BaseModel):
    slug: str
    resumo_mercado: str
    analise_risco: str
    veredito_local: str


class CidadeNarr(BaseModel):
    hub_resumo: str = ""
    nichos: List[NichoNarr]


SYSTEM = (
    "Você é analista de mercado local e escreve em português do Brasil, tom objetivo e prático, para quem pensa em abrir um pequeno negócio. "
    "Use SOMENTE os números presentes na ficha JSON recebida; nunca invente, estime ou arredonde para outro valor. "
    "Não cite códigos CNAE, não prometa lucro, não dê aconselhamento financeiro garantido, não use links. "
    "Saturação Alta = muitos concorrentes para o tamanho da cidade; Baixa = espaço para novos entrantes. "
    "Cada campo de nicho tem 1 a 2 frases (até 300 caracteres). Em resumo_mercado explique a densidade (habitantes por estabelecimento) frente à referência "
    "e à mediana do estado; em analise_risco, o saldo entre aberturas e baixas em 24 meses; em veredito_local, a conclusão prática. "
    "Se pedido, hub_resumo tem 2 a 3 frases (até 500 caracteres) apontando 2 nichos com mais espaço e 2 mais disputados. "
    "Devolva um item em 'nichos' para cada slug recebido, com o mesmo slug."
)


def montar_ficha_nicho(slug, m, meta, med_uf, uf):
    mediana = med_uf.get((uf, slug))
    dif = round((m["hab_por_cnpj"] / mediana - 1) * 100) if mediana else None
    ficha = {
        "slug": slug, "nome": meta["nome"], "ativos": m["ativos"],
        "abertos_24m": m["abertos_24m"], "fechados_24m": m["fechados_24m"], "saldo_24m": m["saldo_24m"],
        "habitantes_por_estabelecimento": m["hab_por_cnpj"],
        "referencia_equilibrada_habitantes": meta["hab_por_cnpj_ideal"],
        "taxa_pressao_pct": m["taxa_pressao_24m"], "saturacao": m["indice_saturacao"],
        "ticket_medio_referencia_reais": meta.get("ticket_medio"),
    }
    if mediana:
        ficha["mediana_uf_habitantes_por_estabelecimento"] = round(mediana)
        ficha["diferenca_vs_mediana_uf_pct"] = dif
    return ficha


def gerar_cidade(cidade, cnaes, med_uf, settings, chamadas_restantes, uf_nome):
    """Devolve (resultado|None, chamadas_usadas). Levanta G.LimiteCota."""
    fichas = [montar_ficha_nicho(s, m, cnaes[s], med_uf, cidade["uf"]) for s, m in cidade["nichos"].items()]
    por_chamada = max(1, int(settings["nichos_por_chamada"]))
    partes = [fichas[i:i + por_chamada] for i in range(0, len(fichas), por_chamada)]
    if len(partes) > chamadas_restantes:
        return "sem_orcamento", 0
    panorama = [{"nome": f["nome"], "saturacao": f["saturacao"], "habitantes_por_estabelecimento": f["habitantes_por_estabelecimento"]} for f in fichas]
    base = {"cidade": cidade["nome"], "uf": uf_nome, "populacao": cidade["populacao"],
            "pib_per_capita_reais": cidade.get("pib_per_capita")}
    hub, textos, usadas = "", {}, 0
    whitelist = montar_whitelist({"base": base, "fichas": fichas}, C.hoje().year)
    for i, parte in enumerate(partes):
        ficha = dict(base, gerar_hub=(i == 0), nichos=parte)
        if i == 0:
            ficha["panorama"] = panorama
        usadas += 1
        bruto = G.gerar_json(json.dumps(ficha, ensure_ascii=False), SYSTEM, CidadeNarr, settings["gemini_model"])
        if i == 0:
            hub = bruto.get("hub_resumo", "")
        for item in bruto.get("nichos", []):
            textos[item.get("slug")] = item
    ok_hub, motivo_hub = validar_texto(hub, whitelist, 40, 900)
    if not ok_hub:
        return {"erro": f"hub inválido ({motivo_hub})"}, usadas
    nichos_ok, fallback = {}, []
    for f in fichas:
        slug = f["slug"]
        item = textos.get(slug)
        valido = bool(item) and all(validar_texto(item.get(k, ""), whitelist, 20, 400)[0]
                                    for k in ("resumo_mercado", "analise_risco", "veredito_local"))
        if valido:
            nichos_ok[slug] = {k: item[k].strip() for k in ("resumo_mercado", "analise_risco", "veredito_local")}
        else:
            fallback.append(slug)
            nichos_ok[slug] = build_fallback_narrative(cidade, cnaes[slug], cidade["nichos"][slug])
    if len(fallback) > 0.3 * len(fichas):
        return {"erro": f"{len(fallback)}/{len(fichas)} nichos reprovados na validação"}, usadas
    return {"hub_resumo": hub.strip(), "nichos": nichos_ok, "nichos_fallback": fallback}, usadas


UF_NOME = {"AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará",
           "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão", "MT": "Mato Grosso",
           "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
           "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
           "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina",
           "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins"}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-cidades", type=int)
    ap.add_argument("--max-chamadas", type=int)
    ap.add_argument("--usar-legado", action="store_true", help="permite rodar sobre base_metricas.json (somente testes)")
    ap.add_argument("--dry-run", action="store_true", help="mostra a fila e sai, sem chamar o Gemini")
    args = ap.parse_args(argv)

    settings = C.load_settings()
    limite_cidades = args.max_cidades if args.max_cidades is not None else settings["cidades_por_dia"]
    limite_chamadas = args.max_chamadas if args.max_chamadas is not None else settings["max_chamadas_gemini"]

    if not C.tem_dados_reais() and not args.usar_legado:
        print("ERRO: não há dados em data/metricas/. Rode antes o workflow 'Ingest dados (Receita + IBGE)'.")
        return 1
    if not args.dry_run and not G.disponivel():
        print("ERRO: GEMINI_API_KEY não está definido no ambiente (Settings › Secrets › Actions).")
        return 1

    cnaes = C.load_cnaes()
    registros, legado = C.carregar_registros()
    estado = C.carregar_estado()
    med_uf = C.medianas_uf(registros, cnaes, settings)
    fila = C.montar_fila(registros, estado, cnaes, settings, limite_cidades)
    print(f"Fila de hoje ({len(fila)} cidade(s), teto de {limite_chamadas} chamada(s)):")
    for cid, motivo in fila:
        print(f"  - {registros[cid]['nome']}/{registros[cid]['uf']} [{motivo}]")
    if args.dry_run or not fila:
        return 0

    hoje = C.hoje().isoformat()
    chamadas = publicadas = falhas = 0
    try:
        for cid, motivo in fila:
            rec = registros[cid]
            cidade = C.derivar_cidade(rec, cnaes, settings)
            try:
                res, usadas = gerar_cidade(cidade, cnaes, med_uf, settings, limite_chamadas - chamadas, UF_NOME.get(rec["uf"], rec["uf"]))
            except G.LimiteCota as e:
                print(f"Cota do Gemini atingida, encerrando o lote: {str(e)[:160]}")
                break
            except Exception as e:  # noqa: BLE001
                chamadas += 1
                print(f"  falha em {rec['nome']}: {type(e).__name__}: {str(e)[:160]}")
                f = estado["falhas"].setdefault(cid, {"n": 0, "ultima": hoje})
                f["n"] += 1
                f["ultima"] = hoje
                falhas += 1
                continue
            chamadas += usadas
            if res == "sem_orcamento":
                print("Teto de chamadas atingido, encerrando o lote.")
                break
            if "erro" in res:
                print(f"  {rec['nome']}: {res['erro']} (volta à fila)")
                f = estado["falhas"].setdefault(cid, {"n": 0, "ultima": hoje})
                f["n"] += 1
                f["ultima"] = hoje
                falhas += 1
                continue
            info = C.info_ingest()
            narrativa = {
                "codigo_ibge": cid, "fonte": "gemini", "modelo": settings["gemini_model"], "gerado_em": hoje,
                "hub_resumo": res["hub_resumo"], "nichos": res["nichos"], "nichos_fallback": res["nichos_fallback"],
                "base": {
                    "fonte_rfb": info.get("fonte_rfb"), "populacao": rec["populacao"],
                    "pib_per_capita": rec.get("pib_per_capita"),
                    "x": {s: rec["x"][s] for s in cidade["nichos"]},
                },
            }
            C.write_json(os.path.join(C.NARR_DIR, f"{cid}.json"), narrativa)
            if cid not in estado["published_cities"]:
                estado["published_cities"].append(cid)
            meta = estado["meta"].setdefault(cid, {"pub_em": hoje})
            meta.update({"atualizado_em": hoje, "fonte": "gemini"})
            estado["falhas"].pop(cid, None)
            publicadas += 1
            print(f"  ok: {rec['nome']}/{rec['uf']} ({usadas} chamada(s))")
    finally:
        estado["last_updated"] = hoje
        C.salvar_estado(estado)
    print(f"Resumo: {publicadas} publicada(s), {falhas} falha(s), {chamadas} chamada(s) ao Gemini.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
