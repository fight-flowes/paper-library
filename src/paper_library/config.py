"""Central project and model configuration."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    root: Path
    libraries: Path
    state: Path
    dotenv: Path
    cache: Path

    @classmethod
    def from_root(cls, root: Path) -> "ProjectPaths":
        resolved = root.expanduser().resolve()
        cache_root = Path(
            os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
        ).expanduser()
        return cls(
            root=resolved,
            libraries=resolved / "libraries",
            state=resolved / ".paper-library-dev",
            dotenv=resolved / ".env",
            cache=cache_root / "paper-library" / "translations.sqlite3",
        )


@dataclass(frozen=True, slots=True)
class TranslationSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout: float = 300
    max_tokens: int = 8192
    profile: str = ""
    concurrency: int = 8
    request_style: str = "openai"
    temperature: float = 0.1
    budget: int = 32
    budget_directory: Path | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)


@dataclass(frozen=True, slots=True)
class Settings:
    paths: ProjectPaths
    host: str
    port: int
    conda_env: str
    upload_max_bytes: int
    remote_management_enabled: bool
    values: Mapping[str, str] = field(repr=False)

    @property
    def jobs_database(self) -> Path:
        return self.paths.state / "jobs.sqlite3"

    @property
    def uploads_directory(self) -> Path:
        return self.paths.state / "uploads"

    @classmethod
    def load(
        cls,
        paths: ProjectPaths,
        environ: Mapping[str, str] | None = None,
    ) -> "Settings":
        environment = dict(os.environ if environ is None else environ)
        settings_file = Path(
            environment.get("PAPER_LIBRARY_SETTINGS_FILE", str(paths.dotenv))
        ).expanduser()
        file_values = (
            {key: value for key, value in dotenv_values(settings_file, interpolate=False).items()
             if value is not None}
            if settings_file.is_file()
            else {}
        )
        values = {**file_values, **environment}
        state = Path(values.get("PAPER_LIBRARY_STATE_DIR", str(paths.state))).expanduser().resolve()
        libraries = Path(
            values.get("PAPER_LIBRARY_LIBRARIES_DIR", str(paths.libraries))
        ).expanduser().resolve()
        cache = Path(
            values.get("PAPER_TRANSLATION_CACHE", str(paths.cache))
        ).expanduser().resolve()
        try:
            port = int(values.get("PAPER_LIBRARY_PORT", "18020"))
        except ValueError as exc:
            raise ValueError("PAPER_LIBRARY_PORT 必须是整数") from exc
        if not 1 <= port <= 65535:
            raise ValueError("PAPER_LIBRARY_PORT 必须在 1 到 65535 之间")
        try:
            upload_mb = int(values.get("PAPER_LIBRARY_UPLOAD_MAX_MB", "256"))
        except ValueError as exc:
            raise ValueError("PAPER_LIBRARY_UPLOAD_MAX_MB 必须是整数") from exc
        if not 1 <= upload_mb <= 4096:
            raise ValueError("PAPER_LIBRARY_UPLOAD_MAX_MB 必须在 1 到 4096 之间")
        for key, default, maximum in (
            ('PAPER_HTTP_THREADS','8',64),('PAPER_JOB_MAX_PENDING','32',1000),('PAPER_JOB_MAX_RECOVERIES','3',20),('PAPER_MIN_FREE_MB','1024',1048576),
            ('PAPER_LOG_MAX_MB','5',1024),('PAPER_LOG_BACKUPS','3',50),
            ('PAPER_MINERU_TIMEOUT_SECONDS','3600',86400),('PAPER_MINERU_IDLE_TIMEOUT_SECONDS','900',86400),
            ('PAPER_BACKUP_WAIT_SECONDS','300',86400),('PAPER_LLM_MAX_CONCURRENCY','32',32),
            ('PAPER_BACKUP_KEEP','7',365),('PAPER_BACKUP_INTERVAL_HOURS','24',8760),
        ):
            try: numeric=int(values.get(key,default))
            except ValueError as exc:raise ValueError(key+' 必须是整数') from exc
            if not 1<=numeric<=maximum:raise ValueError(key+' 超出允许范围')
        remote_raw = values.get("PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT", "0")
        if remote_raw not in {"0", "1"}:
            raise ValueError("PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT 必须为 0 或 1")
        return cls(
            paths=replace(paths, state=state, libraries=libraries, cache=cache),
            host=values.get("PAPER_LIBRARY_HOST", "127.0.0.1"),
            port=port,
            conda_env=values.get("PAPER_LIBRARY_CONDA_ENV", "paper"),
            upload_max_bytes=upload_mb * 1024 * 1024,
            remote_management_enabled=remote_raw == "1",
            values=values,
        )

    def translation(
        self,
        profile: str | None = None,
        *,
        required: bool = True,
    ) -> TranslationSettings:
        profile = profile or self.values.get("PAPER_TRANSLATION_PROFILE", "")
        profiles = {
            value: key[:-5]
            for key, value in self.values.items()
            if key.startswith("LLM_") and key.endswith("MODEL") and value
        }
        if profile not in profiles:
            if not required:
                return TranslationSettings("", "", "", profile=profile, concurrency=1)
            raise ValueError(f"请在项目 .env 中配置翻译模型：{profile}")
        prefix = profiles[profile]

        def value(name: str, default: str = "") -> str:
            return (
                self.values.get(f"PAPER_TRANSLATION_{name}")
                or self.values.get(f"{prefix}{name}")
                or default
            )

        base_url = value("BASE_URL").rstrip("/")
        try:
            parsed = urlsplit(base_url)
            _ = parsed.port
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                base_url = ""
        except ValueError:
            base_url = ""
        try:
            timeout = max(1, min(float(value("TIMEOUT_SECONDS", "300")), 600))
            max_tokens = max(256, min(int(value("MAX_OUTPUT_TOKENS", "8192")), 16384))
            concurrency = max(1, min(int(value("MAX_CONCURRENCY", "8")), 32))
            temperature = float(self.values.get('LLM_TEMPERATURE', '0.1'))
        except ValueError as exc:
            raise ValueError("翻译模型数值配置无效") from exc
        if not 0 <= temperature <= 2:
            raise ValueError('LLM_TEMPERATURE 必须是 0 到 2 之间的有限数值')
        style = value("REQUEST_STYLE", "openai")
        if style not in {"openai", "qwen", "deepseek"}:
            raise ValueError("不支持的 REQUEST_STYLE")
        return TranslationSettings(
            base_url=base_url,
            model=value("MODEL"),
            api_key=value("API_KEY"),
            timeout=timeout,
            max_tokens=max_tokens,
            profile=profile,
            concurrency=concurrency,
            request_style=style,
            temperature=temperature,
            budget=min(concurrency, int(self.values.get("PAPER_LLM_MAX_CONCURRENCY", "32"))),
            budget_directory=Path(self.values.get("PAPER_LLM_STATE_DIR", str(self.paths.state / "llm-slots"))).expanduser().resolve(),
        )


def default_settings() -> Settings:
    return Settings.load(ProjectPaths.from_root(Path(__file__).resolve().parents[2]))
