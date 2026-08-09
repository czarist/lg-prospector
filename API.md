# TrentinCRM / EspoCRM — Documentação completa da API REST

API REST do **EspoCRM 10.0.3** (instância TrentinCRM).

Fontes no código:

- `application/Espo/Resources/routes.json` (core)
- `application/Espo/Modules/Crm/Resources/routes.json` (módulo CRM)
- Controllers em `application/Espo/Controllers` e `Modules/Crm/Controllers`
- CRUD genérico: `Espo\Core\Controllers\RecordBase` / `Record`

| | |
|---|---|
| **Base URL** | `http://trentincrm.com.br/api/v1` |
| **HTTPS** | `https://trentincrm.com.br/api/v1` |
| **Content-Type** | `application/json` |
| **Versão** | 10.0.3 |
| **OpenAPI nativo** | `GET /OpenApi` (spec gerada pelo próprio Espo) |

> Prefixo de todos os paths abaixo: **`/api/v1`**.  
> Ex.: path `/Account` → `http://trentincrm.com.br/api/v1/Account`.

---

## Sumário

1. [Autenticação](#1-autenticação)
2. [Modelo geral da API](#2-modelo-geral-da-api)
3. [CRUD genérico de entidades](#3-crud-genérico-de-entidades)
4. [Busca e filtros (`where`)](#4-busca-e-filtros-where)
5. [Relações (links)](#5-relações-links)
6. [Stream, followers e stars](#6-stream-followers-e-stars)
7. [Actions genéricas e mass actions](#7-actions-genéricas-e-mass-actions)
8. [Rotas de aplicação (App, Settings, Metadata…)](#8-rotas-de-aplicação)
9. [Entidades do sistema (core)](#9-entidades-do-sistema-core)
10. [Entidades do módulo CRM](#10-entidades-do-módulo-crm)
11. [Rotas CRM especiais](#11-rotas-crm-especiais)
12. [E-mail](#12-e-mail)
13. [Anexos](#13-anexos)
14. [Import / Export](#14-import--export)
15. [Kanban, Pipeline, Currency](#15-kanban-pipeline-currency)
16. [Admin e Entity Manager](#16-admin-e-entity-manager)
17. [Usuário, segurança e OAuth/OIDC](#17-usuário-segurança-e-oauthoidc)
18. [Notificações e Lead Capture](#18-notificações-e-lead-capture)
19. [Actions por controller (especiais)](#19-actions-por-controller-especiais)
20. [Códigos HTTP e erros](#20-códigos-http-e-erros)
21. [Exemplos práticos](#21-exemplos-práticos)
22. [OpenAPI e referências](#22-openapi-e-referências)

---

## 1. Autenticação

### 1.1 Basic Auth

```http
Authorization: Basic base64(usuario:senha)
```

```bash
curl -u "admin:SENHA" "http://trentincrm.com.br/api/v1/App/user"
```

### 1.2 Header Espo-Authorization

```http
Espo-Authorization: base64(usuario:senha)
```

### 1.3 Token de sessão

1. `GET /App/user` com Basic → resposta inclui `"token": "..."`  
2. Próximas requests:

```http
Espo-Authorization: base64(usuario:token)
Espo-Authorization-By-Token: true
```

Invalidar token:

```http
POST /App/destroyAuthToken
Body: { "token": "..." }
```

### 1.4 Usuário API (API Key)

Administration → API Users.

```http
X-Api-Key: <apiKey>
```

ou Basic com as chaves do usuário API (conforme config).

Gerar chave:

```http
POST /UserSecurity/apiKey/generate
```

### 1.5 Rotas sem autenticação (`noAuth`)

| Método | Path |
|--------|------|
| GET | `/I18n` |
| GET | `/Settings` |
| POST | `/LeadCapture/:apiKey` |
| OPTIONS | `/LeadCapture/:apiKey` |
| POST | `/LeadCapture/form/:id` |
| POST | `/User/passwordChangeRequest` |
| POST | `/User/changePasswordByRequest` |
| GET | `/Oidc/authorizationData` |
| POST | `/Oidc/backchannelLogout` |
| POST/DELETE | `/Campaign/unsubscribe/...` |

### 1.6 Ambiente TrentinCRM (teste)

| | |
|---|---|
| Usuário | `admin` |
| Senha | `Polandball21?` *(o `?` faz parte da senha)* |

Não versionar senhas em repositórios públicos. Prefira usuário API em integrações.

---

## 2. Modelo geral da API

### 2.1 Convenções

- Paths em **PascalCase** iguais ao nome da entidade/controller: `/Account`, `/Lead`, `/EmailTemplate`.
- IDs são strings hex (ex.: `6a6dff4b4b22e4528`).
- JSON em body e resposta.
- Datas: `YYYY-MM-DD` ou `YYYY-MM-DD HH:mm:ss` (timezone da instância).

### 2.2 Métodos HTTP

| Método | Uso típico |
|--------|------------|
| `GET` | Listar / ler |
| `POST` | Criar / actions |
| `PUT` / `PATCH` | Atualizar |
| `DELETE` | Remover |

### 2.3 Resposta de lista

```json
{
  "total": 42,
  "list": [ { "id": "...", "name": "..." } ]
}
```

### 2.4 Resposta de registro

Objeto plano com campos da entidade + `id`, `createdAt`, `modifiedAt`, links `*Id` / `*Name`, etc.

---

## 3. CRUD genérico de entidades

Válido para **quase todas** as entidades com scope/controller de record.

| Método | Path | Action | Descrição |
|--------|------|--------|-----------|
| `GET` | `/{Entity}` | list / index | Lista com busca/paginação |
| `GET` | `/{Entity}/{id}` | read | Lê um registro |
| `POST` | `/{Entity}` | create | Cria |
| `PUT` | `/{Entity}/{id}` | update | Atualiza |
| `PATCH` | `/{Entity}/{id}` | update | Atualiza (igual PUT no Espo) |
| `DELETE` | `/{Entity}/{id}` | delete | Remove (soft delete na maioria) |

### 3.1 Actions CRUD extras (via `/action/`)

```http
POST /{Entity}/action/getDuplicateAttributes
POST /{Entity}/action/restoreDeleted
```

Também existe o padrão genérico:

```http
POST|PUT|GET /{Entity}/action/{actionName}
```

---

## 4. Busca e filtros (`where`)

### Query params de listagem

| Param | Descrição |
|-------|-----------|
| `maxSize` | Itens por página |
| `offset` | Offset |
| `orderBy` | Campo de ordenação |
| `order` | `asc` \| `desc` |
| `select` | Campos (csv) |
| `where` | JSON array de condições |
| `primaryFilter` | Filtro nomeado do selectDefs |
| `boolFilterList` | JSON array de bool filters |
| `textFilter` | Busca textual |
| `q` | Atalho de busca (quando suportado) |

### Operadores `where.type`

| type | Uso |
|------|-----|
| `equals` / `notEquals` | Igualdade |
| `contains` / `notContains` / `startsWith` / `endsWith` | Texto |
| `greaterThan` / `lessThan` / `greaterThanOrEquals` / `lessThanOrEquals` | Comparação |
| `in` / `notIn` | Lista (`value`: array) |
| `isTrue` / `isFalse` | Boolean |
| `isNull` / `isNotNull` | Nulo |
| `linkedWith` / `notLinkedWith` | Relação |
| `isLinked` / `isNotLinked` | Tem/não tem link |
| `and` / `or` | Grupo (`value`: array de condições) |
| `arrayAnyOf` / `arrayNoneOf` / `arrayAllOf` | Multi-enum |

**Exemplo:**

```bash
curl -u "admin:SENHA" -G "http://trentincrm.com.br/api/v1/Lead" \
  --data-urlencode 'maxSize=20' \
  --data-urlencode 'orderBy=createdAt' \
  --data-urlencode 'order=desc' \
  --data-urlencode 'where=[{"type":"equals","attribute":"status","value":"New"}]'
```

---

## 5. Relações (links)

| Método | Path | Descrição |
|--------|------|-----------|
| `GET` | `/{Entity}/{id}/{link}` | Lista relacionados |
| `POST` | `/{Entity}/{id}/{link}` | Vincula (body: `{"id":"..."}` ou `{"ids":[...]}`) |
| `DELETE` | `/{Entity}/{id}/{link}` | Desvincula (body com `id`/`ids`) |

**Exemplos CRM:**

```http
GET    /Account/{id}/contacts
GET    /Account/{id}/opportunities
GET    /Account/{id}/cases
POST   /Account/{id}/contacts     {"id":"contactId"}
DELETE /Contact/{id}/opportunities
GET    /Lead/{id}/meetings
GET    /Opportunity/{id}/contacts
```

Os nomes de `link` vêm do `entityDefs` de cada entidade (campo `links`).

---

## 6. Stream, followers e stars

| Método | Path | Descrição |
|--------|------|-----------|
| `GET` | `/Stream` | Stream do usuário |
| `GET` | `/GlobalStream` | Stream global |
| `GET` | `/{Entity}/{id}/stream` | Stream do registro |
| `GET` | `/{Entity}/{id}/posts` | Posts do stream |
| `GET` | `/{Entity}/{id}/updateStream` | Updates do stream |
| `GET` | `/{Entity}/{id}/streamAttachments` | Anexos do stream |
| `PUT` | `/{Entity}/{id}/subscription` | Seguir (follow) |
| `DELETE` | `/{Entity}/{id}/subscription` | Deixar de seguir |
| `GET` | `/{Entity}/{id}/followers` | Lista followers |
| `POST` | `/{Entity}/{id}/followers` | Adiciona followers |
| `DELETE` | `/{Entity}/{id}/followers` | Remove followers |
| `PUT` | `/{Entity}/{id}/starSubscription` | Favoritar (star) |
| `DELETE` | `/{Entity}/{id}/starSubscription` | Remover star |
| `GET` | `/User/{id}/stream/own` | Stream próprio do user |
| `POST` | `/Note/{id}/pin` | Fixar nota |
| `DELETE` | `/Note/{id}/pin` | Desafixar |
| `POST` | `/Note/{id}/myReactions/{type}` | Reagir |
| `DELETE` | `/Note/{id}/myReactions/{type}` | Remover reação |
| `GET` | `/Note/{id}/reactors/{type}` | Quem reagiu |

Notas do stream: CRUD em `/Note`.

---

## 7. Actions genéricas e mass actions

### Action em um registro

```http
POST /Action
```

Body típico:

```json
{
  "entityType": "Lead",
  "id": "...",
  "action": "convert",
  "data": {}
}
```

### Mass Action

```http
POST /MassAction
GET  /MassAction/{id}/status
POST /MassAction/{id}/subscribe
```

Body típico:

```json
{
  "entityType": "Account",
  "action": "delete",
  "params": {
    "ids": ["id1", "id2"],
    "where": null,
    "searchParams": null
  },
  "data": {}
}
```

Actions comuns de massa: `delete`, `update`, `massUpdate`, `follow`, `unfollow`, `convertCurrency`, etc. (conforme ACL e metadata).

### Restore / duplicate (por entidade)

```http
POST /{Entity}/action/restoreDeleted
POST /{Entity}/action/getDuplicateAttributes
```

---

## 8. Rotas de aplicação

| Método | Path | Auth | Descrição |
|--------|------|------|-----------|
| `GET` | `/` | sim* | Index da API |
| `GET` | `/App/user` | **sim** | Usuário, ACL, token, settings |
| `POST` | `/App/destroyAuthToken` | sim | Invalida token |
| `GET` | `/App/about` | sim | Sobre / versão |
| `GET` | `/App/appParams` | sim | Parâmetros de app |
| `GET` | `/Metadata` | sim | Metadados completos |
| `GET` | `/I18n` | **não** | Traduções |
| `GET` | `/Settings` | **não** | Settings públicos |
| `PUT`/`PATCH` | `/Settings` | admin | Atualiza settings |
| `GET` | `/GlobalSearch` | sim | Busca global (`q=...`) |
| `GET` | `/OpenApi` | sim | Spec OpenAPI |

\* comportamento conforme ACL/sessão.

---

## 9. Entidades do sistema (core)

Todas abaixo aceitam o CRUD genérico (quando o ACL permite), path = `/{Nome}`.

| Entidade | Uso |
|-----------|-----|
| `User` | Usuários |
| `Team` | Equipes |
| `Role` | Papéis ACL |
| `Portal` | Portais |
| `PortalRole` | Papéis de portal |
| `Email` | E-mails |
| `EmailAccount` | Contas pessoais de e-mail |
| `InboundEmail` | Contas de grupo |
| `EmailTemplate` | Modelos de e-mail |
| `EmailTemplateCategory` | Categorias de template |
| `EmailFolder` | Pastas |
| `GroupEmailFolder` | Pastas de grupo |
| `EmailFilter` | Filtros |
| `EmailAddress` | Endereços (interno) |
| `PhoneNumber` | Telefones (interno) |
| `Attachment` | Anexos |
| `Note` | Notas / stream posts |
| `Notification` | Notificações |
| `Preferences` | Preferências do usuário |
| `Template` | Templates PDF |
| `Import` | Importações |
| `ImportError` | Erros de import |
| `Job` | Jobs da fila |
| `ScheduledJob` | Jobs agendados |
| `ScheduledJobLogRecord` | Logs de scheduled |
| `AuthToken` | Tokens de auth |
| `AuthLogRecord` | Log de autenticação |
| `ActionHistoryRecord` | Histórico de ações |
| `AppLogRecord` | Log da aplicação |
| `AppSecret` | Segredos |
| `Webhook` | Webhooks |
| `WebhookQueueItem` | Fila de webhooks |
| `WebhookEventQueueItem` | Eventos webhook |
| `LeadCapture` | Captura de leads (form) |
| `LeadCaptureLogRecord` | Log de captura |
| `LayoutSet` | Sets de layout |
| `DashboardTemplate` | Templates de dashboard |
| `WorkingTimeCalendar` | Calendário de trabalho |
| `WorkingTimeRange` | Intervalos |
| `CurrencyRecord` | Moedas |
| `CurrencyRecordRate` | Taxas |
| `AuthenticationProvider` | Providers de auth |
| `OAuthAccount` | Contas OAuth |
| `OAuthProvider` | Providers OAuth |
| `Integration` | Integrações |
| `Extension` | Extensões |
| `AddressCountry` | Países |
| `Pipeline` | Pipelines |
| `PipelineStage` | Estágios de pipeline |
| `ExternalAccount` | Contas externas |

> Algumas entidades são **somente leitura** ou restringem create/update/delete no controller.

---

## 10. Entidades do módulo CRM

| Path / Entidade | Descrição |
|------------------|-----------|
| `Account` | Contas / empresas |
| `Contact` | Contatos |
| `Lead` | Leads |
| `Opportunity` | Oportunidades |
| `Case` | Casos de suporte *(controller `CaseObj`, path `Case`)* |
| `Task` | Tarefas |
| `Meeting` | Reuniões |
| `Call` | Ligações |
| `Campaign` | Campanhas |
| `CampaignLogRecord` | Log de campanha |
| `CampaignTrackingUrl` | URLs de tracking |
| `TargetList` | Listas de alvos |
| `TargetListCategory` | Categorias de target list |
| `Target` | Alvo genérico |
| `MassEmail` | E-mail em massa |
| `EmailQueueItem` | Fila de mass email |
| `Document` | Documentos |
| `DocumentFolder` | Pastas de documento |
| `KnowledgeBaseArticle` | Artigos KB |
| `KnowledgeBaseCategory` | Categorias KB |
| `Reminder` | Lembretes |
| `Activities` | Controller de atividades (não é CRUD simples) |

### Campos mínimos de criação (exemplos)

**Account**

```json
{ "name": "Acme Ltda", "type": "Customer", "emailAddress": "a@acme.com" }
```

**Contact**

```json
{ "firstName": "Maria", "lastName": "Silva", "accountId": "...", "emailAddress": "m@acme.com" }
```

**Lead**

```json
{ "firstName": "João", "lastName": "Souza", "status": "New", "source": "Web Site" }
```

**Opportunity**

```json
{
  "name": "Projeto X",
  "stage": "Prospecting",
  "amount": 10000,
  "accountId": "...",
  "closeDate": "2026-12-31"
}
```

**Task**

```json
{
  "name": "Follow-up",
  "status": "Not Started",
  "priority": "Normal",
  "dateEnd": "2026-08-10 15:00:00",
  "parentType": "Account",
  "parentId": "..."
}
```

**Meeting / Call**

```json
{
  "name": "Call cliente",
  "dateStart": "2026-08-10 14:00:00",
  "dateEnd": "2026-08-10 14:30:00",
  "status": "Planned",
  "parentType": "Lead",
  "parentId": "..."
}
```

**Case**

```json
{ "name": "Problema login", "status": "New", "priority": "Normal", "accountId": "..." }
```

---

## 11. Rotas CRM especiais

Definidas em `Modules/Crm/Resources/routes.json`:

| Método | Path | Descrição |
|--------|------|-----------|
| `GET` | `/Activities` | Calendário (eventos) |
| `GET` | `/Activities/upcoming` | Próximas atividades |
| `GET` | `/Activities/{parentType}/{id}/{type}` | Atividades do parent (`type`: activities, history…) |
| `GET` | `/Activities/{parentType}/{id}/{type}/list/{targetType}` | Lista tipada |
| `GET` | `/Activities/{parentType}/{id}/composeEmailAddressList` | E-mails para compor |
| `GET` | `/Timeline` | Timeline do calendário |
| `GET` | `/Timeline/busyRanges` | Intervalos ocupados |
| `GET` | `/Meeting/{id}/attendees` | Participantes da reunião |
| `GET` | `/Call/{id}/attendees` | Participantes da call |
| `POST` | `/Campaign/{id}/generateMailMerge` | Mail merge |
| `GET` | `/TargetList/{id}/optedOut` | Opt-outs da lista |
| `POST` | `/Campaign/unsubscribe/{id}` | Unsubscribe (**noAuth**) |
| `DELETE` | `/Campaign/unsubscribe/{id}` | Re-subscribe (**noAuth**) |
| `POST` | `/Campaign/unsubscribe/{email}/{hash}` | Unsubscribe por hash (**noAuth**) |
| `DELETE` | `/Campaign/unsubscribe/{email}/{hash}` | Cancel unsubscribe (**noAuth**) |

Query típica do calendário (`/Activities`): `from`, `to`, `userId`, `scopeList`, etc.

---

## 12. E-mail

### CRUD

`GET/POST/PUT/DELETE /Email`, links, stream — como qualquer entidade.

### Rotas especiais

| Método | Path | Descrição |
|--------|------|-----------|
| `POST` | `/Email/importEml` | Importa EML |
| `POST` | `/Email/sendTest` | Envia e-mail de teste |
| `POST` | `/Email/inbox/read` | Marca lido (ids no body) |
| `DELETE` | `/Email/inbox/read` | Marca não lido |
| `POST` | `/Email/inbox/important` | Marca importante |
| `DELETE` | `/Email/inbox/important` | Remove importante |
| `POST` | `/Email/inbox/inTrash` | Move p/ lixeira |
| `DELETE` | `/Email/inbox/inTrash` | Tira da lixeira |
| `POST` | `/Email/inbox/folders/{folderId}` | Move p/ pasta |
| `GET` | `/Email/inbox/notReadCounts` | Contagens não lidas |
| `GET` | `/Email/insertFieldData` | Dados p/ inserir campos |
| `POST` | `/Email/{id}/users` | Associa users |
| `POST` | `/Email/{id}/attachments/copy` | Copia anexos |
| `POST` | `/EmailTemplate/{id}/prepare` | Prepara template |
| `GET` | `/EmailAddress/search` | Busca endereços |
| `POST` | `/EmailAccount/{id}/resetFetchData` | Reset fetch conta pessoal |
| `POST` | `/InboundEmail/{id}/resetFetchData` | Reset fetch conta grupo |

---

## 13. Anexos

| Método | Path | Descrição |
|--------|------|-----------|
| CRUD | `/Attachment` | Metadados do anexo |
| `GET` | `/Attachment/file/{id}` | Download do arquivo |
| `POST` | `/Attachment/chunk/{id}` | Upload em chunks |
| `POST` | `/Attachment/fromImageUrl` | Cria a partir de URL de imagem |
| `POST` | `/Attachment/copy/{id}` | Copia anexo |

Upload típico: criar `Attachment` + enviar conteúdo/chunk conforme client do Espo.

---

## 14. Import / Export

### Export

```http
POST /Export
GET  /Export/{id}/status
POST /Export/{id}/subscribe
```

### Import

```http
POST /Import
POST /Import/file
POST /Import/{id}/revert
POST /Import/{id}/removeDuplicates
POST /Import/{id}/unmarkDuplicates
POST /Import/{id}/exportErrors
```

CRUD também em `/Import` e `/ImportError`.

---

## 15. Kanban, Pipeline, Currency

| Método | Path | Descrição |
|--------|------|-----------|
| `GET` | `/Kanban/{entityType}` | Dados do kanban |
| `PUT` | `/Kanban/order` | Reordena cards |
| `POST` | `/Pipeline/{id}/move/{type}` | Move pipeline |
| `POST` | `/PipelineStage/{id}/move/{type}` | Move estágio |
| `GET` | `/CurrencyRate` | Taxas de câmbio |
| `PUT` | `/CurrencyRate` | Atualiza taxas |

---

## 16. Admin e Entity Manager

Requer usuário **admin**.

### Admin

| Método | Path | Descrição |
|--------|------|-----------|
| `POST` | `/Admin/rebuild` | Rebuild |
| `POST` | `/Admin/clearCache` | Limpa cache |
| `GET` | `/Admin/jobs` | Jobs |
| `POST` | `/Admin/action/uploadUpgradePackage` | Upload upgrade* |
| `POST` | `/Admin/action/runUpgrade` | Roda upgrade* |
| `GET` | `/Admin/action/cronMessage` | Msg do cron* |
| `GET` | `/Admin/action/adminNotificationList` | Notificações admin* |
| `GET` | `/Admin/action/systemRequirementList` | Requirements* |

\* via padrão `/{Controller}/action/{action}`.

### Field Manager

| Método | Path |
|--------|------|
| `GET` | `/Admin/fieldManager/{scope}/{name}` |
| `POST` | `/Admin/fieldManager/{scope}` |
| `PUT`/`PATCH` | `/Admin/fieldManager/{scope}/{name}` |
| `DELETE` | `/Admin/fieldManager/{scope}/{name}` |

### Entity Manager (actions)

```http
POST /EntityManager/action/createEntity
POST /EntityManager/action/updateEntity
POST /EntityManager/action/removeEntity
POST /EntityManager/action/createLink
POST /EntityManager/action/updateLink
POST /EntityManager/action/removeLink
POST /EntityManager/action/updateLinkParams
POST /EntityManager/action/resetLinkParamsToDefault
POST /EntityManager/action/formula
POST /EntityManager/action/resetFormulaToDefault
POST /EntityManager/action/resetToDefault
POST /EntityManager/action/exportCustom
```

### Layout

```http
GET /{Entity}/layout/{name}
PUT /{Entity}/layout/{name}
PUT /{Entity}/layout/{name}/{setId}
```

### Outros admin-ish

- `Extension`: upload / install / uninstall  
- `Formula`, `LabelManager`, `TemplateManager`, `Pdf`, `Integration`, `Job`  
- `DataPrivacy/action/erase`  
- `AddressCountry/action/populateDefaults`  
- `DashboardTemplate/action/deployToUsers`, `deployToTeam`

---

## 17. Usuário, segurança e OAuth/OIDC

| Método | Path | Auth | Descrição |
|--------|------|------|-----------|
| CRUD | `/User` | sim | Usuários |
| `GET` | `/User/{id}/acl` | sim | ACL efetiva do user |
| `POST` | `/UserSecurity/apiKey/generate` | sim | Gera API key |
| `PUT` | `/UserSecurity/password` | sim | Altera senha |
| `POST` | `/UserSecurity/password/recovery` | sim | Recovery |
| `POST` | `/UserSecurity/password/generate` | sim | Gera senha |
| `POST` | `/User/passwordChangeRequest` | **não** | Solicita troca |
| `POST` | `/User/changePasswordByRequest` | **não** | Troca via request |
| `PUT` | `/Team/{id}/userPosition` | sim | Posição do user no time |
| `GET` | `/{Entity}/{id}/usersAccess` | sim | Quem tem acesso ao record |
| `GET` | `/Oidc/authorizationData` | **não** | Dados OIDC |
| `POST` | `/Oidc/backchannelLogout` | **não** | Logout OIDC |
| `POST` | `/OAuth/{id}/connection` | sim | Conecta OAuth |
| `DELETE` | `/OAuth/{id}/connection` | sim | Desconecta OAuth |
| CRUD | `/Preferences` | sim | Preferências (`id` = user id) |

2FA: controllers `TwoFactorEmail`, `TwoFactorSms` via actions.

---

## 18. Notificações e Lead Capture

### Notification

| Método | Path |
|--------|------|
| CRUD | `/Notification` |
| `GET` | `/Notification/{id}/group` |
| `GET` | `/Notification/group` |
| `DELETE` | `/Notification/group/{id}` |
| `POST` | `/Notification/group/{id}/markRead` |

### Lead Capture (público + admin)

| Método | Path | Auth |
|--------|------|------|
| CRUD | `/LeadCapture` | admin |
| CRUD | `/LeadCaptureLogRecord` | admin |
| `POST` | `/LeadCapture/{apiKey}` | **não** — captura lead |
| `OPTIONS` | `/LeadCapture/{apiKey}` | **não** — CORS preflight |
| `POST` | `/LeadCapture/form/{id}` | **não** — form captcha/form |

Exemplo captura pública:

```bash
curl -X POST "http://trentincrm.com.br/api/v1/LeadCapture/SEU_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"firstName":"Ana","lastName":"Costa","emailAddress":"ana@mail.com"}'
```

---

## 19. Actions por controller (especiais)

Padrão:

```http
POST /{Controller}/action/{actionName}
GET  /{Controller}/action/{actionName}
```

### CRM

| Controller | Action | Método | Descrição |
|------------|--------|--------|-----------|
| `Lead` | `convert` | POST | Converte lead |
| `Lead` | `getConvertAttributes` | POST | Atributos p/ conversão |
| `Meeting` | `sendInvitations` | POST | Convites |
| `Meeting` | `sendCancellation` | POST | Cancelamento |
| `Meeting` | `massSetHeld` | POST | Marca held em massa |
| `Meeting` | `massSetNotHeld` | POST | Marca not held |
| `Meeting` | `setAcceptanceStatus` | POST | RSVP |
| `Call` | *(mesmas do Meeting)* | | |
| `Opportunity` | `reportByLeadSource` | GET | Relatório |
| `Opportunity` | `reportByStage` | GET | Relatório |
| `Opportunity` | `reportSalesByMonth` | GET | Relatório |
| `Opportunity` | `reportSalesPipeline` | GET | Pipeline report |
| `Opportunity` | `emailAddressList` | GET | E-mails |
| `Case` | `emailAddressList` | GET | E-mails do case |
| `TargetList` | `unlinkAll` | POST | Desvincula todos |
| `TargetList` | `optOut` | POST | Opt-out |
| `TargetList` | `cancelOptOut` | POST | Cancela opt-out |
| `MassEmail` | `sendTest` | POST | Teste mass email |
| `MassEmail` | `smtpAccountDataList` | GET | Contas SMTP |
| `Document` | `getAttachmentList` | POST | Anexos |
| `KnowledgeBaseArticle` | `getCopiedAttachments` | POST | Copia anexos |
| `KnowledgeBaseArticle` | `moveToTop` / `moveUp` / `moveDown` / `moveToBottom` | POST | Ordenação |
| `Activities` | `removePopupNotification` | POST | Remove popup |

**Exemplo convert Lead:**

```bash
curl -u "admin:SENHA" \
  -H "Content-Type: application/json" \
  -X POST \
  -d '{"records":{"Account":true,"Contact":true,"Opportunity":true}}' \
  "http://trentincrm.com.br/api/v1/Lead/ID/action/convert"
```

*(também: `POST /Lead/action/convert` com `id` no body, conforme client)*

### Core (seleção)

| Controller | Actions |
|------------|---------|
| `Admin` | `rebuild`, `clearCache`, `jobs`, `uploadUpgradePackage`, `runUpgrade`, `cronMessage`, … |
| `EntityManager` | `createEntity`, `updateEntity`, `removeEntity`, `createLink`, … |
| `EmailAccount` | `getFolders`, `testConnection` |
| `InboundEmail` | similar |
| `EmailFolder` | `moveUp`, `moveDown`, `listAll` |
| `Extension` | `upload`, `install`, `uninstall` |
| `DashboardTemplate` | `deployToUsers`, `deployToTeam` |
| `DataPrivacy` | `erase` |
| `AddressCountry` | `populateDefaults` |
| `UserSecurity` / `User` | password, apiKey (ver §17) |
| Record tree entities | `listTree`, `lastChildrenIdList`, `move` |

---

## 20. Códigos HTTP e erros

| Código | Significado |
|--------|-------------|
| `200` | OK |
| `400` | Bad Request / validação |
| `401` | Não autenticado / senha errada |
| `403` | Sem permissão **ou** rate-limit de login |
| `404` | Não encontrado |
| `405` | Método não permitido |
| `409` | Conflict (duplicidade, etc.) |
| `500` | Erro interno → `data/logs/espo-*.log` |

Body de erro (quando presente) costuma ser JSON com mensagem Espo.

---

## 21. Exemplos práticos

### cURL

```bash
BASE="http://trentincrm.com.br/api/v1"
AUTH="admin:Polandball21?"

# Sessão / eu
curl -u "$AUTH" "$BASE/App/user"

# Settings públicos
curl "$BASE/Settings"

# Listar
curl -u "$AUTH" "$BASE/Account?maxSize=20&offset=0"

# Criar
curl -u "$AUTH" -H "Content-Type: application/json" \
  -d '{"name":"Conta API","type":"Customer"}' \
  "$BASE/Account"

# Ler / atualizar / apagar
curl -u "$AUTH" "$BASE/Account/ID"
curl -u "$AUTH" -H "Content-Type: application/json" \
  -X PUT -d '{"description":"ok"}' "$BASE/Account/ID"
curl -u "$AUTH" -X DELETE "$BASE/Account/ID"

# Busca global
curl -u "$AUTH" "$BASE/GlobalSearch?q=acme"

# Metadata / OpenAPI
curl -u "$AUTH" "$BASE/Metadata" -o metadata.json
curl -u "$AUTH" "$BASE/OpenApi" -o openapi.json
```

### Python

```python
import requests
from requests.auth import HTTPBasicAuth

base = "http://trentincrm.com.br/api/v1"
auth = HTTPBasicAuth("admin", "Polandball21?")

# list
r = requests.get(f"{base}/Contact", auth=auth, params={"maxSize": 10})
print(r.json())

# create + link
acc = requests.post(f"{base}/Account", auth=auth, json={"name": "Py Co"}).json()
contact = requests.post(f"{base}/Contact", auth=auth, json={
    "firstName": "Ana", "lastName": "Lima", "accountId": acc["id"]
}).json()
print(contact["id"])
```

### JavaScript

```js
const base = "http://trentincrm.com.br/api/v1";
const headers = {
  Authorization: "Basic " + btoa("admin:Polandball21?"),
  "Content-Type": "application/json",
};

const list = await fetch(`${base}/Opportunity?maxSize=5`, { headers }).then(r => r.json());
const created = await fetch(`${base}/Task`, {
  method: "POST",
  headers,
  body: JSON.stringify({ name: "Follow-up", status: "Not Started" }),
}).then(r => r.json());
```

### PowerShell

```powershell
$base = "http://trentincrm.com.br/api/v1"
$pair = "admin:Polandball21?"
curl.exe -sS -u $pair "$base/App/user"
'{"name":"Conta PS"}' | Set-Content body.json -Encoding UTF8
curl.exe -sS -u $pair -H "Content-Type: application/json" `
  -X POST --data-binary "@body.json" "$base/Account"
```

---

## 22. OpenAPI e referências

### Spec gerada pelo próprio Espo

```bash
curl -u "admin:SENHA" "http://trentincrm.com.br/api/v1/OpenApi" -o openapi.json
```

Útil para importar no Postman, Insomnia ou Swagger UI.

### Metadados (campos, links, clientDefs)

```bash
curl -u "admin:SENHA" "http://trentincrm.com.br/api/v1/Metadata" -o metadata.json
```

### Documentação oficial EspoCRM

- [API Overview](https://docs.espocrm.com/development/api/)
- [CRUD](https://docs.espocrm.com/development/api-crud/)
- [Search params](https://docs.espocrm.com/development/api-search-params/)
- [ORM / entities](https://docs.espocrm.com/development/orm/)
- [Webhooks](https://docs.espocrm.com/administration/webhooks/)
- [API Users](https://docs.espocrm.com/administration/api-users/)

### Arquivos de rotas no projeto

| Arquivo | Conteúdo |
|---------|----------|
| `application/Espo/Resources/routes.json` | Rotas core + genéricas |
| `application/Espo/Modules/Crm/Resources/routes.json` | Calendário, campaign, etc. |
| `application/Espo/Controllers/*.php` | Controllers core |
| `application/Espo/Modules/Crm/Controllers/*.php` | Controllers CRM |
| `application/Espo/Core/Controllers/RecordBase.php` | CRUD base |
| `application/Espo/Core/Controllers/Record.php` | Links / follow |

---

## Checklist de saúde (instância TrentinCRM)

Testado em produção (`trentincrm.com.br`):

| Endpoint | Status |
|----------|--------|
| Frontend `/` | 200 |
| `GET /Settings` (noAuth) | 200 |
| `GET /I18n` (noAuth) | 200 |
| `GET /App/user` (auth) | 200 |
| `GET /Account` … entidades CRM | 200 |
| `GET /User` | 200 |
| `GET /Metadata` | 200 |
| HTTPS `/App/user` | 200 |
| `/portal/` sem ID | 404 (esperado) |

---

## Segurança (resumo)

1. Use **HTTPS** em produção.  
2. Integrações: usuário **API** com ACL mínimo, não o admin.  
3. Muitas falhas de login → `403` temporário.  
4. Webhooks e Lead Capture públicos: proteja `apiKey` e rate-limit no proxy.  
5. Não commitar senhas; rotacione se vazarem.

---

*Documentação montada a partir de `routes.json`, controllers e testes reais na API REST do EspoCRM 10.0.3 / TrentinCRM.*
