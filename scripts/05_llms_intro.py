"""Atualiza (no máximo a cada llms_intro_dias) o parágrafo de abertura do llms.txt com o Gemini.
É opcional: sem o arquivo data/llms_intro.json, o build usa um texto padrão."""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
from pydantic import BaseModel

import common as C
import gemini_client as G
from validator import montar_whitelist, validar_texto

CAMINHO = os.path.join(C.DATA_DIR, "llms_intro.json")


class Intro(BaseModel):
    texto: str


def main():
    settings = C.load_settings()
    atual = C.read_json(CAMINHO, {}) or {}
    if atual.get("gerado_em"):
        if (C.hoje() - date.fromisoformat(atual["gerado_em"])).days < settings["llms_intro_dias"]:
            print("llms_intro: ainda dentro do prazo, nada a fazer.")
            return 0
    if not G.disponivel():
        print("llms_intro: sem GEMINI_API_KEY, mantendo texto atual.")
        return 0
    estado = C.carregar_estado()
    n = len(estado["published_cities"])
    ficha = {"tipo": "intro_llms", "cidades_publicadas": n, "ramos_monitorados": len(C.load_cnaes()),
             "fonte": "cadastro de CNPJ da Receita Federal e IBGE"}
    system = ("Escreva em português do Brasil um parágrafo de 2 a 3 frases (até 450 caracteres) apresentando o site QualNegócio a assistentes de IA e buscadores: "
              "estudos de viabilidade de negócios por município e ramo. Use apenas os números da ficha; não cite CNAE, links nem promessas de resultado.")
    try:
        r = G.gerar_json(json.dumps(ficha, ensure_ascii=False), system, Intro, settings["gemini_model"], max_tokens=1000)
    except Exception as e:  # noqa: BLE001
        print(f"llms_intro: falhou ({type(e).__name__}); mantendo texto atual.")
        return 0
    ok, motivo = validar_texto(r.get("texto", ""), montar_whitelist(ficha, C.hoje().year), 60, 500)
    if not ok:
        print(f"llms_intro: texto reprovado ({motivo}); mantendo texto atual.")
        return 0
    C.write_json(CAMINHO, {"texto": r["texto"].strip(), "gerado_em": C.hoje().isoformat()})
    print("llms_intro: atualizado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
