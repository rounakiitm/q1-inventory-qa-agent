from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    gemini_api_key: str = ""
    gemini_api_keys: str = ""  # comma-separated pool; rotates on 429/503
    model_name: str = "gemini-3.1-flash-lite"
    # 2.5-flash-lite is retired for new keys → use 3.5-flash-lite as cheap fallback.
    model_fallbacks: str = "gemini-3.5-flash-lite,gemini-3.5-flash"

    # User-selectable low-cost models (2.x flash-lite retired for new users).
    selectable_models: tuple[str, ...] = (
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
    )

    @property
    def model_ladder(self) -> list[str]:
        """Primary model first, then fallbacks (each has its own quota bucket)."""
        ladder = [self.model_name]
        ladder += [m.strip() for m in self.model_fallbacks.split(",") if m.strip()]
        # de-dupe, preserve order
        seen: set[str] = set()
        out: list[str] = []
        for m in ladder:
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def resolve_model(self, requested: str | None) -> str:
        """Validate a UI-selected model against the allowlist."""
        if not requested:
            return self.model_name
        name = requested.strip()
        if name not in self.selectable_models:
            raise ValueError(
                f"model '{name}' not allowed; choose one of: "
                + ", ".join(self.selectable_models)
            )
        return name

    @property
    def api_keys(self) -> list[str]:
        pool = [k.strip() for k in self.gemini_api_keys.split(",") if k.strip()]
        if pool:
            return pool
        return [self.gemini_api_key] if self.gemini_api_key else []

    sandbox_timeout_s: float = 15.0
    sandbox_max_result_chars: int = 10000

    agent_max_iterations: int = 15

    db_path: Path = ROOT / "data" / "agent.db"
    default_dataset: Path = ROOT / "data" / "Inventory-Records-Sample-Data.xlsx"
    uploads_dir: Path = ROOT / "data" / "uploads"

    # Supabase (optional). When URL + service role are set, Postgres+Auth+Storage
    # replace SQLite. Leave blank for local/dev/tests.
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""  # Project Settings → API → JWT Secret
    auth_disabled: bool = False  # force local anonymous user (tests)

    log_level: str = "INFO"

    @property
    def supabase_enabled(self) -> bool:
        return bool(
            self.supabase_url.strip()
            and self.supabase_service_role_key.strip()
            and not self.auth_disabled
        )

    def ensure_dirs(self) -> None:
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
