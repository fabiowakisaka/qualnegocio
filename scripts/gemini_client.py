"""Cliente fino do Gemini: uma função, saída JSON validada por schema, erro de cota separado."""
import json
import os
import re


class LimiteCota(Exception):
    """Cota ou limite de taxa atingido: o lote deve parar sem insistir."""


def disponivel():
    return bool(os.environ.get("GEMINI_API_KEY")) or bool(os.environ.get("QN_FAKE_GEMINI"))


def _limpar(txt):
    txt = (txt or "").strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt)
    return txt


def gerar_json(conteudo, system, schema, model, max_tokens=16000, temperature=0.4):
    """conteudo: str (JSON da ficha). Devolve dict. Levanta LimiteCota em erro de cota."""
    if os.environ.get("QN_FAKE_GEMINI"):
        return _simulado(conteudo)
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    try:
        resp = client.models.generate_content(
            model=model,
            contents=conteudo,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
    except Exception as e:  # noqa: BLE001
        msg = str(e).upper()
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "QUOTA" in msg or "RATE" in msg:
            raise LimiteCota(str(e)) from e
        raise
    return json.loads(_limpar(resp.text))


def _simulado(conteudo):
    """Modo de teste (QN_FAKE_GEMINI=1): não chama a API e usa só números da ficha."""
    ficha = json.loads(conteudo)
    if ficha.get("tipo") == "intro_llms":
        return {"texto": "O QualNegócio reúne estudos de viabilidade por município, com dados públicos do cadastro de CNPJ e do IBGE."}
    saida = {"hub_resumo": "", "nichos": []}
    if ficha.get("gerar_hub"):
        saida["hub_resumo"] = (
            f"{ficha['cidade']} tem {ficha['populacao']} habitantes e vários setores analisados. "
            "Texto de teste gerado sem chamar o modelo."
        )
    for n in ficha["nichos"]:
        saida["nichos"].append({
            "slug": n["slug"],
            "resumo_mercado": f"{n['nome']} tem {n['ativos']} estabelecimentos ativos, 1 para cada {n['habitantes_por_estabelecimento']} habitantes (texto de teste).",
            "analise_risco": f"Foram {n['abertos_24m']} aberturas e {n['fechados_24m']} baixas em 24 meses, pressão de {n['taxa_pressao_pct']}% (texto de teste).",
            "veredito_local": f"Saturação {n['saturacao'].lower()} para o setor nesta cidade (texto de teste).",
        })
    return saida
