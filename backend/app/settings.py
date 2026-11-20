from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEV_SECRET = "dev-only-insecure-secret-change-me"


class Settings(BaseSettings):
    environment: str = "development"
    frontend_origin: str = "http://localhost:3000"
    database_url: str = "postgresql+psycopg://invoiceiq:invoiceiq@localhost:5433/invoiceiq"
    upload_dir: str = "storage/invoices"
    max_upload_bytes: int = 15 * 1024 * 1024
    secret_key: str = INSECURE_DEV_SECRET
    access_token_ttl_seconds: int = 12 * 60 * 60
    file_token_ttl_seconds: int = 5 * 60
    run_migrations_on_startup: bool = True
    # OCR for scanned PDFs: "auto" prefers PaddleOCR, then Tesseract; "none" disables OCR.
    ocr_provider: str = "auto"
    paddle_ocr_language: str = "en"
    tessdata_dir: str | None = None
    ocr_languages: str = "deu+eng"
    # Optional LLM-assisted extraction. "none" keeps all processing local.
    llm_provider: str = "none"
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    # External VAT-ID registration checks (EU VIES). Disabled by default: format checks only.
    vies_enabled: bool = False
    vies_url: str = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"
    # Exchange rates: "ecb" (live), or "demo" (labelled fixed sample rates for offline demos).
    default_fx_provider: str = "ecb"
    # GDPR: default retention for invoices in days (German retention duties are often 8-10 years).
    default_retention_days: int = 3650
    ecb_rates_url: str = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
    ecb_rates_90d_url: str = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    def check_production_safety(self) -> None:
        if self.environment == "production" and self.secret_key == INSECURE_DEV_SECRET:
            raise RuntimeError("SECRET_KEY must be set to a unique random value in production")
        if len(self.secret_key) < 16:
            raise RuntimeError("SECRET_KEY must be at least 16 characters")


settings = Settings()
