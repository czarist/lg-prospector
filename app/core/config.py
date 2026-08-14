"""Configuração central via variáveis de ambiente (.env)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # LLM
    # LiteLLM model id (local Qwen = "local-main" neste host)
    model: str = "local-main"
    base_url: str = "http://localhost:4000/v1"
    api_key: str = "dummy"

    # MySQL
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_database: str = "lg_prospector"
    mysql_user: str = "lucas"
    mysql_password: str = "lucas123"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # EspoCRM (legado)
    crm_url: str = "https://trentincrm.com.br/api/v1"
    crm_user: str = "admin"
    crm_password: str = ""

    # EspoCRM preferencial (API.md): URL + token (API key ou "user:password")
    espo_crm_url: str = ""
    espo_crm_token: str = ""

    # SMTP
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "prospeccao@trentin.com.br"
    smtp_use_tls: bool = True

    # IMAP (bounces / NDR) — se vazio, reutiliza SMTP_USER/PASSWORD e
    # deriva o host (smtppro.zoho.com → imappro.zoho.com)
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_use_ssl: bool = True

    @property
    def effective_crm_url(self) -> str:
        """Prefere ESPO_CRM_URL; fallback para CRM_URL."""
        return (self.espo_crm_url or self.crm_url).rstrip("/")

    # App
    app_name: str = "lg-prospector"
    app_env: str = "development"
    log_level: str = "INFO"
    templates_dir: str = "templates"
    api_host: str = "0.0.0.0"
    api_port: int = 8200
    # Relativo = lg-prospector/logs. Absoluto = HD, ex. /storage/lg-prospector/logs
    logs_dir: str = "logs"

    # External search APIs (opcional)
    firecrawl_api_key: str = ""  # legado; scrape é local
    google_api_key: str = ""
    google_cse_id: str = ""
    serper_api_key: str = ""

    # free = DuckDuckGo + OSM/Overpass | serper = só Serper (sem fallback DDG)
    # auto = Serper se SERPER_API_KEY estiver setada, senão free
    search_backend: str = "auto"

    # Scraping local (substitui Firecrawl)
    scrape_use_playwright: bool = True
    scrape_max_pages: int = 3
    scrape_timeout_seconds: float = 15.0

    # --- Caçada: fragmentação / anti-sobrecarga ---
    # Concorrência baixa = máquina e LiteLLM/Qwen respiram
    search_concurrency: int = 1
    search_min_interval_seconds: float = 0.9
    # free search: quantos backends tentar (1=só DDG html, 2=+lib, 3=+bing)
    search_max_backends: int = 2
    # enrich
    enrich_concurrency: int = 1
    enrich_batch_pause_seconds: float = 0.4
    enrich_max_domain_queries: int = 3
    enrich_playwright: bool = False  # Playwright só se true (pesado)
    enrich_verify_email_dns: bool = True  # confere MX/A do domínio antes de aceitar o e-mail
    scrape_concurrency: int = 2
    playwright_concurrency: int = 1
    # discover: quantos candidatos buscar por rodada (múltiplo de max_results).
    # Maior aqui não custa Serper extra (é "num" na mesma call) — só mais itens
    # pro filtro LLM decidir; evita rodadas inteiras de "reforço" (que sim são caras,
    # cada uma refaz discover+enrich com scrape/DDG).
    discover_overfetch_factor: int = 5
    # LLM local (LiteLLM/Qwen) — OFF por padrão; caçada atual não depende dele
    hunt_use_llm: bool = True
    # keep=true do LLM só é aceito se o score também bater esse mínimo
    # (pega os casos em que o modelo marca keep=true por inércia mas dá score baixo)
    discover_min_llm_score: int = 40
    llm_concurrency: int = 1
    llm_timeout_seconds: float = 60.0
    llm_max_tokens: int = 140

    # Dispatch e-mail
    email_cooldown_days: int = 4  # não reenviar pro mesmo endereço em menos de N dias
    # 8s entre e-mails; 0 = sem teto horário/diário (pausa de 1 min é entre fluxos)
    dispatch_delay_seconds: float = 8.0
    dispatch_delay_jitter_seconds: float = 0.0
    email_hourly_limit: int = 0
    email_daily_limit: int = 0

    # Mailman — disparo separado da prospecção (2 e-mails, pausa 2–5 min)
    mailman_batch_size: int = 2
    mailman_interval_min_seconds: float = 120.0
    mailman_interval_max_seconds: float = 300.0
    mailman_intra_batch_min_seconds: float = 20.0
    mailman_intra_batch_max_seconds: float = 75.0

    # Rate limiting (dispatch / API)
    rate_limit_requests: int = 10
    rate_limit_window_seconds: int = 60

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            f"?charset=utf8mb4"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_sync(self) -> str:
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            f"?charset=utf8mb4"
        )

    @property
    def templates_path(self) -> Path:
        path = Path(self.templates_dir)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path

    @property
    def is_development(self) -> bool:
        return self.app_env.lower() in {"development", "dev", "local", "test"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
