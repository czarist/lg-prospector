# LG Prospector

Plataforma SaaS de prospecção comercial B2B com **LangGraph** como orquestrador e integração exclusiva com **EspoCRM** via API REST.

## Stack

- Python 3.12+
- FastAPI · LangGraph · LangChain · SQLAlchemy · Alembic
- MySQL 8 · Redis · httpx · Playwright · Firecrawl

## Pré-requisitos

Infraestrutura local **já existente** (não recriar com Docker Compose):

- LiteLLM em `http://localhost:4000/v1`
- MySQL 8 em `localhost:3306`
- Redis em `localhost:6379`

### Busca de empresas (grátis por padrão)

```env
SEARCH_BACKEND=free   # DuckDuckGo + OpenStreetMap/Overpass (sem API key)
# SEARCH_BACKEND=serper
# SERPER_API_KEY=...
# SEARCH_BACKEND=auto  # usa Serper se houver key, senão free
```

- **search** → DuckDuckGo  
- **places** (local) → Nominatim + Overpass, com reforço DDG  
- **e-mail no site** → scrape local (httpx + Playwright)

## Setup

```bash
cd lg-prospector
cp .env.example .env   # ajuste credenciais
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Banco (cria tabelas)
python -c "import asyncio; from app.infrastructure.database.session import init_db; asyncio.run(init_db())"

# Ou via Alembic
alembic revision --autogenerate -m "initial"
alembic upgrade head

# API
uvicorn app.main:app --host 0.0.0.0 --port 8200 --reload
```

## Caçada leve (anti-sobrecarga)

A busca/enrich **não usa o LLM por padrão**. LiteLLM + Qwen local (`MODEL=qwen-local`) ficam livres para outros usos.

Pesado na caçada: free search (DDG/Bing) + scrape + Playwright opcional.

| Env | Default | Efeito |
|-----|---------|--------|
| `SEARCH_CONCURRENCY` | `1` | 1 busca por vez |
| `SEARCH_MIN_INTERVAL_SECONDS` | `0.9` | pausa entre SERPs |
| `SEARCH_MAX_BACKENDS` | `2` | DDG html → lib (sem Bing se 2) |
| `ENRICH_CONCURRENCY` | `1` | 1 lead por vez no enrich |
| `ENRICH_BATCH_PAUSE_SECONDS` | `0.4` | pausa entre leads |
| `ENRICH_MAX_DOMAIN_QUERIES` | `3` | menos buscas `site:dominio` |
| `ENRICH_PLAYWRIGHT` | `false` | browser só se `true` |
| `HUNT_USE_LLM` | `true` | Qwen (`MODEL=local-main`) filtra no discover |
| `LLM_CONCURRENCY` | `1` | nunca paralelo no LLM local |
| `MODEL` | `local-main` | id LiteLLM do Qwen2.5-Coder-7B local |

### Revisão de leads existentes

```bash
# parecer do Qwen sobre o que já está no banco (não apaga CRM)
python scripts/review_leads_llm.py -n 40

# só um nicho; opcionalmente marca discarded local se score baixo
python scripts/review_leads_llm.py --niche agencia_marketing -n 20 --apply-discard
```

### Loop contínuo (escada de cidades)

Busca **5 leads × nicho × cidade**, priorizando capitais e polos econômicos (foco **RS**). Não usa município pequeno.

```bash
# ver fila (sem gastar API)
python scripts/hunt_loop.py --plan-only --focus-rs --max-tier 2

# só Rio Grande do Sul, uma volta (prospecta + dispara e-mail)
python scripts/hunt_loop.py --only-rs --once -n 5

# dry-run de e-mail (não envia SMTP)
python scripts/hunt_loop.py --only-rs --once -n 5 --dry-run-dispatch

# sem disparo
python scripts/hunt_loop.py --only-rs --once --no-dispatch

# loop contínuo (retoma de logs/hunt_loop_state.json)
python scripts/hunt_loop.py --focus-rs --max-tier 2 -n 5 --pause 8

# só capitais + DF
python scripts/hunt_loop.py --only-capitals --once
```

Fluxo por job: **discover → enrich → CRM → dispatch** (template HTML do nicho, 1 e-mail por contato).  
**Cooldowno 4 dias:** o mesmo endereço não recebe de novo em menos de `EMAIL_COOLDOWN_DAYS` (default 4).

### Logs (para analisar no dia seguinte)

Em `logs/hunt/`:

| Arquivo | Conteúdo |
|---------|----------|
| `results_YYYYMMDD.jsonl` | cada etapa + job completo (JSON) |
| `results_YYYYMMDD.log` | resumo legível dos sucessos |
| `errors_YYYYMMDD.jsonl` | erros e metas parciais (JSON) |
| `errors_YYYYMMDD.log` | erros legíveis |
| `run_YYYYMMDD_HHMMSS.log` | espelho da corrida inteira |

```bash
# amanhã
wc -l logs/hunt/results_*.jsonl logs/hunt/errors_*.jsonl
tail -50 logs/hunt/errors_$(date +%Y%m%d).log
grep '"event": "job_done"' logs/hunt/results_$(date +%Y%m%d).jsonl | wc -l
```

Ordem da escadinha: **Porto Alegre → outras capitais → polos gaúchos (Caxias, Canoas…) → polos nacionais (Campinas, Joinville…)**.

## Pipeline em etapas

Pesquisa e disparo são **segmentados** (não rodam tudo de uma vez):

```
created → discover → enrich → crm → dispatch → done
```

| Etapa | O que faz |
|--------|-----------|
| `discover` | Busca empresas (DuckDuckGo/OSM) |
| `enrich` | Acha e-mail **do domínio do site** (scrape + busca `site:dominio` / `@dominio`) |
| `crm` | Cria Account + Contact + Lead no EspoCRM |
| `dispatch` | Envia template HTML via SMTP (CID, rate-limit) |

Sem e-mail do domínio após multi-pass → item **discarded** (não grava lead).

### API

| Método | Path | Descrição |
|--------|------|-----------|
| POST | `/campaign` | Cria campanha (default: não executa) |
| GET | `/campaign/{id}` | Detalhe |
| GET | `/campaign/{id}/status` | Status + itens |
| GET | `/campaign/{id}/stages` | Etapa atual + contagem por stage |
| POST | `/campaign/{id}/stages/discover` | Etapa busca |
| POST | `/campaign/{id}/stages/enrich` | Etapa e-mail (domínio) |
| POST | `/campaign/{id}/stages/crm` | Etapa CRM |
| POST | `/campaign/{id}/stages/dispatch` | Etapa disparo (`dry_run` no body) |
| POST | `/campaign/{id}/pause` | Pausa |
| POST | `/campaign/{id}/resume` | Retoma |
| POST | `/campaign/{id}/cancel` | Cancela |
| POST | `/campaign/{id}/run` | Pipeline legado completo |
| GET | `/companies` | Lista empresas |
| GET | `/contacts` | Lista contatos |
| GET | `/dashboard` | Métricas |
| GET | `/health` | Healthcheck |
| GET | `/docs` | OpenAPI |

### CLI por etapas

```bash
# só pesquisa + e-mail + CRM (sem disparo)
python scripts/run_stages.py --niche advogado --query "advocacia" --city Curitiba --state PR -n 5 \
  --stages discover,enrich,crm

# incluir disparo em dry-run
python scripts/run_stages.py --niche empresa_ti -n 5 --stages discover,enrich,crm,dispatch --dry-run-dispatch
```

### Exemplo

```bash
curl -X POST http://localhost:8200/campaign \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Advogados SP",
    "niche": "advogado",
    "query": "escritório advocacia",
    "city": "São Paulo",
    "state": "SP",
    "max_results": 10
  }'
```

## Nichos / Providers

| Nicho | Provider | Template |
|-------|----------|----------|
| `advogado` | Google Search/Maps, Website, Firecrawl | `email-prospeccao-advogados.html` |
| `agencia_marketing` | LinkedIn, Website, Firecrawl | `email-prospeccao-agencias.html` |
| `empresa_ti` | LinkedIn, Website, Firecrawl, Playwright | `email-prospeccao-empresas-ti.html` |
| `prestador_servico` | Google Search/Maps | `email-prospeccao-prestadores.html` |
| `grupo_midiatico` | Search, News, Contato | `email-prospeccao-jornalismo.html` |
| `politico` (alias: `partido`) | Bases públicas, Câmara, Senado, diretórios | `email-prospeccao-politicos.html` |

Novos nichos: implementar `BaseProvider` e registrar no `ProviderRegistry`.

## Pipeline LangGraph

```
CreateCampaign → SelectProvider → SearchCompanies → NormalizeCompanies
→ RemoveDuplicates → FindContacts → FindEmails → ValidateLead
→ CreateCRMCompany → CreateCRMContact → CreateCRMLead
→ SelectEmailTemplate → SendEmail → RegisterActivity
→ WaitResponse → UpdatePipeline → FinishCampaign
```

**Importante:** a IA **não escreve** e **não modifica** e-mails HTML — apenas seleciona o template existente.

## Arquitetura

```
app/
  api/           # FastAPI routes + schemas
  core/          # config, logging
  domain/        # entities, interfaces
  providers/     # strategies por nicho
  graph/         # LangGraph state + nodes
  infrastructure/
    database/    # SQLAlchemy models + session
    crm/         # EspoCRM REST client + services
    email/       # SMTP + TemplateSelector
    redis/       # filas, cache, rate limit, checkpoints
    resilience/  # retry, circuit breaker
  services/      # service layer
```

## Testes

```bash
pytest -q --cov=app --cov-report=term-missing
```

## Configuração

Toda config via `.env` (ver `.env.example`). Nunca versionar senhas reais.
