from pathlib import Path

from paper_library.config import ProjectPaths, Settings


def test_project_paths_are_derived_from_one_root(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path)

    assert paths.root == tmp_path.resolve()
    assert paths.libraries == tmp_path.resolve() / "libraries"
    assert paths.state == tmp_path.resolve() / ".paper-library-dev"
    assert paths.dotenv == tmp_path.resolve() / ".env"


def test_settings_use_environment_over_dotenv_and_defaults(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path)
    paths.dotenv.write_text(
        "PAPER_LIBRARY_HOST=127.0.0.2\n"
        "PAPER_LIBRARY_PORT=18100\n"
        "PAPER_LIBRARY_CONDA_ENV=from-file\n",
        encoding="utf-8",
    )

    settings = Settings.load(
        paths,
        {
            "PAPER_LIBRARY_PORT": "18200",
            "PAPER_LIBRARY_STATE_DIR": str(tmp_path / "runtime"),
        },
    )

    assert settings.host == "127.0.0.2"
    assert settings.port == 18200
    assert settings.conda_env == "from-file"
    assert settings.paths.state == (tmp_path / "runtime").resolve()


def test_translation_settings_are_optional_without_a_profile(tmp_path: Path) -> None:
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {})

    translation = settings.translation(required=False)

    assert not translation.configured
    assert translation.concurrency == 1


def test_translation_profile_is_discovered_from_model_value(tmp_path: Path) -> None:
    settings = Settings.load(
        ProjectPaths.from_root(tmp_path),
        {
            "PAPER_TRANSLATION_PROFILE": "deepseek-v4-flash",
            "LLM_DEEPSEEK_MODEL": "deepseek-v4-flash",
            "LLM_DEEPSEEK_BASE_URL": "http://127.0.0.1:9000/v1",
            "LLM_DEEPSEEK_MAX_CONCURRENCY": "16",
            "LLM_DEEPSEEK_REQUEST_STYLE": "deepseek",
        },
    )

    translation = settings.translation()

    assert translation.model == "deepseek-v4-flash"
    assert translation.concurrency == 16
    assert translation.request_style == "deepseek"


def test_management_settings_have_safe_defaults(tmp_path: Path) -> None:
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {})

    assert settings.upload_max_bytes == 256 * 1024 * 1024
    assert not settings.remote_management_enabled
    assert settings.jobs_database == tmp_path.resolve() / ".paper-library-dev/jobs.sqlite3"
    assert settings.uploads_directory == tmp_path.resolve() / ".paper-library-dev/uploads"


def test_explicit_cli_profile_can_override_frontend_default_model(tmp_path):
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {
        'PAPER_TRANSLATION_MODEL': 'frontend', 'PAPER_TRANSLATION_BASE_URL': 'http://frontend/v1',
        'LLM_OTHER_MODEL': 'other', 'LLM_OTHER_BASE_URL': 'http://other/v1',
    })
    assert settings.translation().model == 'frontend'
    assert settings.translation().profile == 'frontend'
    assert settings.translation('other').model == 'other'
    assert settings.translation('other').base_url == 'http://other/v1'


def test_upload_limit_and_remote_management_are_configurable(tmp_path: Path) -> None:
    settings = Settings.load(
        ProjectPaths.from_root(tmp_path),
        {
            "PAPER_LIBRARY_UPLOAD_MAX_MB": "32",
            "PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT": "1",
        },
    )

    assert settings.upload_max_bytes == 32 * 1024 * 1024
    assert settings.remote_management_enabled


def test_management_settings_reject_invalid_values(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path)

    for value in ("0", "4097", "large"):
        try:
            Settings.load(paths, {"PAPER_LIBRARY_UPLOAD_MAX_MB": value})
        except ValueError as exc:
            assert "PAPER_LIBRARY_UPLOAD_MAX_MB" in str(exc)
        else:
            raise AssertionError(f"invalid upload limit accepted: {value}")

    for value in ("true", "yes", "2"):
        try:
            Settings.load(paths, {"PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT": value})
        except ValueError as exc:
            assert "PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT" in str(exc)
        else:
            raise AssertionError(f"invalid remote flag accepted: {value}")
