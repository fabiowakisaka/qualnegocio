"""Camada anti-alucinação: todo número citado no texto precisa existir na ficha enviada ao modelo."""
import re
from typing import Any, Dict, List, Tuple

from common import fmt_int

TOKEN = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?")
BASE_PERMITIDOS = [1, 2, 3, 6, 12, 24, 100]
PROIBIDAS = ("garantid", "sem risco", "lucro certo", "sucesso certo", "http", "www.")


def extract_numbers(text: str) -> List[float]:
    vals = []
    for tok in TOKEN.findall(text):
        vals.append(float(tok.replace(".", "").replace(",", ".")))
    return vals


def coletar_numeros(obj: Any, acc: List[float] = None) -> List[float]:
    acc = [] if acc is None else acc
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        acc.extend([float(obj), abs(float(obj))])
    elif isinstance(obj, dict):
        for v in obj.values():
            coletar_numeros(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            coletar_numeros(v, acc)
    return acc


def montar_whitelist(ficha: Any, ano: int) -> List[float]:
    return coletar_numeros(ficha) + [float(n) for n in BASE_PERMITIDOS] + [float(ano), float(ano - 1)]


def numero_permitido(valor: float, whitelist: List[float]) -> bool:
    tol = 0.51 if float(valor).is_integer() else 0.051
    return any(abs(valor - w) <= tol for w in whitelist)


def validar_texto(texto: str, whitelist: List[float], minimo: int = 30, maximo: int = 900) -> Tuple[bool, str]:
    if not isinstance(texto, str) or not (minimo <= len(texto.strip()) <= maximo):
        return False, "tamanho fora do intervalo"
    baixo = texto.lower()
    for p in PROIBIDAS:
        if p in baixo:
            return False, f"termo proibido: {p}"
    for v in extract_numbers(texto):
        if not numero_permitido(v, whitelist):
            return False, f"número fora da ficha: {v}"
    return True, "ok"


def validate_narrative(text: str, allowed_metrics: Dict[str, Any]) -> bool:
    """Compatibilidade com a versão anterior."""
    return validar_texto(text, montar_whitelist(allowed_metrics, 2026), 1, 10_000)[0]


def build_fallback_narrative(city_info: Dict[str, Any], niche_meta: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, str]:
    """Texto determinístico, usado quando não há narrativa do modelo para o nicho."""
    nome, uf = city_info["nome"], city_info["uf"]
    pib = city_info.get("pib_per_capita")
    pib_txt = f"PIB per capita de R$ {fmt_int(pib)} e " if pib else ""
    return {
        "resumo_mercado": (
            f"O setor de {niche_meta['nome']} em {nome} ({uf}) registra {fmt_int(metrics['ativos'])} estabelecimentos ativos, "
            f"o que dá 1 para cada {fmt_int(metrics['hab_por_cnpj'])} habitantes "
            f"(a referência equilibrada do setor é 1 para {fmt_int(niche_meta['hab_por_cnpj_ideal'])})."
        ),
        "analise_risco": (
            f"Nos últimos 24 meses foram registradas {fmt_int(metrics['abertos_24m'])} aberturas e "
            f"{fmt_int(metrics['fechados_24m'])} baixas, uma taxa de pressão competitiva de {metrics['taxa_pressao_24m']}%."
        ),
        "veredito_local": (
            f"Com {pib_txt}saturação {metrics['indice_saturacao'].lower()}, "
            f"o ticket médio de referência do setor é R$ {fmt_int(niche_meta['ticket_medio'])}."
        ),
    }
