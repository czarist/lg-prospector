# Especificação Técnica --- Plataforma de Prospecção B2B com IA

## Objetivo

Desenvolver uma plataforma SaaS de prospecção comercial utilizando
**LangGraph** como orquestrador principal dos agentes de IA.

O sistema deverá:

-   Pesquisar empresas por nicho.
-   Encontrar contatos e e-mails.
-   Qualificar leads.
-   Integrar com o EspoCRM exclusivamente via API REST.
-   Selecionar automaticamente um template HTML existente.
-   Enviar o template sem qualquer modificação de conteúdo.
-   Acompanhar respostas e atualizar o pipeline do CRM.

## Infraestrutura existente

A infraestrutura já está pronta e **não deve ser recriada**.

Serviços disponíveis:

-   LiteLLM (API OpenAI Compatible)
-   MySQL 8
-   Redis

Todos executam localmente.

Nunca criar Docker Compose para esses serviços.

Toda configuração deve ser feita via `.env`.

Exemplo:

``` env
MODEL=qwen-local
BASE_URL=http://localhost:4000/v1
API_KEY=...

MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_DATABASE=...
MYSQL_USER=...
MYSQL_PASSWORD=...

REDIS_URL=redis://localhost:6379

CRM_URL=...
CRM_USER=...
CRM_PASSWORD=...
```

## Stack

-   Python 3.12
-   FastAPI
-   LangGraph
-   LangChain
-   SQLAlchemy
-   Alembic
-   MySQL
-   Redis
-   httpx
-   Playwright
-   Firecrawl

## Arquitetura

``` text
Campaign
    │
    ▼
Select Provider
    │
    ▼
Pesquisar Empresas
    │
    ▼
Normalizar Dados
    │
    ▼
Eliminar Duplicados
    │
    ▼
Encontrar Contatos
    │
    ▼
Encontrar Email
    │
    ▼
Qualificar
    │
    ▼
EspoCRM
    │
    ▼
Selecionar Template
    │
    ▼
Enviar Email
    │
    ▼
Registrar Atividade
    │
    ▼
Aguardar Resposta
    │
    ▼
Atualizar Pipeline
```

## Providers

Cada nicho possui estratégia própria.

### Advogados

Pesquisar:

-   Google Search
-   Google Maps
-   Website
-   Firecrawl

Não realizar scraping em massa do cadastro da OAB.

### Agências de Marketing

Pesquisar prioritariamente:

-   LinkedIn
-   Website
-   Firecrawl

### Empresas de TI

Pesquisar prioritariamente:

-   LinkedIn
-   Website
-   Firecrawl
-   Playwright

### Prestadores de Serviço

Pesquisar:

-   Google Search
-   Google Maps

### Grupos Midiáticos

Pesquisar:

-   Google Search
-   Google News
-   Expediente
-   Contato
-   Publicidade

### Políticos

Pesquisar:

-   Bases públicas
-   Portal da Câmara
-   Portal do Senado
-   Sites oficiais

### Partidos

Pesquisar:

-   Sites oficiais
-   Diretórios estaduais
-   Diretórios municipais

## Estrutura padronizada

Todos os providers retornam:

``` python
ProviderResult(
    company_name="",
    contact_name="",
    email="",
    phone="",
    website="",
    city="",
    state="",
    segment="",
    source=""
)
```

## Estado do LangGraph

``` python
campaign_id
provider
company
contact
crm_company_id
crm_contact_id
crm_lead_id
status
logs
```

## Nodes

-   CreateCampaign
-   SelectProvider
-   SearchCompanies
-   NormalizeCompanies
-   RemoveDuplicates
-   FindContacts
-   FindEmails
-   ValidateLead
-   CreateCRMCompany
-   CreateCRMContact
-   CreateCRMLead
-   SelectEmailTemplate
-   SendEmail
-   RegisterActivity
-   WaitResponse
-   UpdatePipeline
-   FinishCampaign

## EspoCRM

Utilizar exclusivamente a API REST existente.

Nunca acessar banco diretamente.

Criar:

-   CRMClient
-   CompanyService
-   ContactService
-   LeadService
-   ActivityService
-   OpportunityService

## Templates HTML

Os templates HTML já existem.

A IA **não deve escrever e-mails**.

A IA **não deve modificar o HTML**.

Ela apenas seleciona:

-   advogado → email-prospeccao-advogados.html
-   agencia_marketing → email-prospeccao-agencias.html
-   empresa_ti → email-prospeccao-empresas-ti.html
-   prestador_servico → email-prospeccao-prestadores.html
-   grupo_midiatico → email-prospeccao-jornalismo.html
-   politico → email-prospeccao-politicos.html

O envio deve utilizar exatamente o arquivo correspondente.

## Banco

Criar tabelas:

-   campaigns
-   campaign_items
-   companies
-   contacts
-   emails
-   activities
-   graph_runs
-   graph_state
-   providers

## API

Implementar:

-   POST /campaign
-   GET /campaign/{id}
-   GET /campaign/{id}/status
-   POST /campaign/{id}/pause
-   POST /campaign/{id}/resume
-   POST /campaign/{id}/cancel
-   GET /companies
-   GET /contacts
-   GET /dashboard

## Redis

Utilizar para:

-   filas
-   cache
-   checkpoints
-   rate limiting

## Desenvolvimento

Implementar incrementalmente.

Ordem:

1.  Estrutura do projeto
2.  Banco
3.  Configuração
4.  API
5.  Providers
6.  LangGraph
7.  CRM
8.  SMTP
9.  Templates
10. Testes

Após cada etapa:

-   corrigir erros
-   executar testes
-   validar compilação

Nunca iniciar a próxima etapa com a anterior quebrada.

## Qualidade

-   SOLID
-   Clean Architecture
-   Repository Pattern
-   Service Layer
-   Dependency Injection
-   Async IO
-   Retry
-   Circuit Breaker
-   Logs estruturados
-   Cobertura de testes

## Objetivo final

Entregar uma plataforma pronta para produção, modular e extensível, onde
novos nichos possam ser adicionados apenas implementando novos
Providers, mantendo o LangGraph desacoplado da lógica de pesquisa.
