# QualNegócio.com

Estudos de viabilidade de negócios por município e por ramo, gerados como páginas estáticas
(GitHub Pages), com dados do cadastro público de CNPJ (Receita Federal) e do IBGE, e textos
redigidos pelo Gemini **somente a partir dos números calculados**.

## Como funciona

```
Receita Federal (CNPJ) + IBGE
        │  scripts/01_ingest_dados.py   (mensal, manual ou agendado)
        ▼
data/metricas/{UF}.json        contagens por município × ramo
        │  scripts/02_generate_batch.py (diário: N cidades por dia)
        ▼
data/narratives/{ibge}.json    textos do Gemini + retrato dos números usados
data/published_state.json      fila/estado de publicação
        │  scripts/04_build_static.py
        ▼
dist/   páginas, busca, sitemap-índice, robots.txt, llms.txt  →  GitHub Pages
```

- **Python faz os cruzamentos** (densidade, mediana do estado, saldo de aberturas/baixas, saturação).
  O Gemini só redige. O `validator.py` rejeita qualquer texto com número que não esteja na ficha enviada.
- **Uma cidade só é publicada se o texto passar na validação.** Se falhar, continua na fila.
- **Cada página é um retrato coerente:** usa os números do momento em que o texto foi gerado.
  A cidade é reescrita só quando a base mudar mais que `limite_mudanca_regerar_pct` (padrão 10%).

## Configuração (uma vez)

1. **Secret** `GEMINI_API_KEY` (já criado): Settings › Secrets and variables › Actions › Secrets.
2. **Variables** (mesma tela, aba Variables). Todas opcionais:

   | Variável | Padrão | Função |
   |---|---|---|
   | `AUTO_PUBLISH` | desligado | `true` liga o cron diário do lote |
   | `INGEST_AUTO` | desligado | `true` liga a atualização mensal dos dados |
   | `CIDADES_POR_DIA` | 5 | cidades novas por execução |
   | `MAX_CHAMADAS_GEMINI` | 8 | teto duro de chamadas à API por execução |
   | `GEMINI_MODEL` | gemini-2.5-flash | modelo usado |
   | `RFB_BASE_URL` | URL oficial | só se a Receita mudar o endereço |

3. Settings › Pages › Source: **GitHub Actions**. Custom domain `qualnegocio.com` + Enforce HTTPS.

## Primeira execução (nesta ordem)

1. Suba os arquivos na `main`. O site continua no ar com as 10 cidades atuais
   (marcadas como "números preliminares" até o ingest).
2. Actions › **Ingest dados (Receita + IBGE)** › Run workflow. Grava `data/metricas/`.
   Se o download da Receita falhar no Actions, baixe `Estabelecimentos*.zip` e `Municipios*.zip`
   manualmente e rode localmente:
   `python scripts/01_ingest_dados.py --rfb-dir ./zips` e faça commit de `data/metricas/` e `data/ibge_cache.json`.
3. Actions › **Lote diário** › Run workflow com `cidades = 2` e `max_chamadas = 3` para conferir texto e site.
4. Defina `AUTO_PUBLISH = true`. A partir daí, um lote por dia (09:17 UTC).

## Limites e cuidados

- **Cota do Gemini:** 1 chamada por cidade (todos os ramos juntos). O lote para ao primeiro erro de cota.
  As 10 cidades antigas entram na fila de reescrita (até 2 por dia, dentro das 5 do lote).
- **CNAE compartilhado:** a Receita só informa o CNAE. Nichos com o mesmo CNAE teriam números idênticos,
  então o ingest conta só o primeiro da lista e avisa. Hoje ficam de fora `cafeteria` e `acaiteria`
  (mesmo CNAE de `hamburgueria`) e `salao-de-beleza` (mesmo de `barbearia`). Dê um CNAE próprio em
  `config/cnaes_meta.json` para incluí-los.
- **Tamanho do site:** o GitHub Pages limita o site publicado a cerca de 1 GB. Medido em teste:
  ~7 KB por página; 3.300 cidades com ~18 ramos ≈ 480 MB. Ajuste `min_ativos_nicho` e `min_nichos_cidade`
  em `config/settings.json` para publicar só combinações com volume suficiente.
- **Afiliados:** os links em `config/affiliates.json` só têm `utm_source`. Troque pelos links
  com seu código de afiliado, senão as comissões não são rastreadas.
- **Valores de referência** (investimento, margem, ticket) em `cnaes_meta.json` são estimativas editoriais.

## Sitemap e llms.txt

Gerados no build, sem intervenção: `sitemap.xml` (índice) + `sitemap-N.xml` (10 mil URLs cada),
com `lastmod` real por cidade; `robots.txt`; `llms.txt` com estados, ramos e maiores cidades.
O parágrafo de abertura do `llms.txt` é atualizado pelo Gemini no máximo a cada 30 dias
(`scripts/05_llms_intro.py`); sem ele, vale um texto padrão.

## Testes

`python tests/test_pipeline.py` roda tudo offline com dados sintéticos (ingest, lote com Gemini simulado, build).
`QN_FAKE_GEMINI=1` simula o Gemini sem gastar cota (nunca ligue isso no workflow).
