"""Application language selection and Qt catalogue loading."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QLibraryInfo,
    QLocale,
    QSettings,
    QTranslator,
)
from PySide6.QtWidgets import QApplication

from matteloop.resources import resource_path

LANGUAGE_KEY = "ui/language"
SUPPORTED_LANGUAGES = ("en", "de")
_CATALOGUE_NAMES = {"en": "matteloop_en.qm", "de": "matteloop_de.qm"}
_CONFIGURED_LANGUAGE: str | None = None


def language_for_locale(locale: QLocale) -> str:
    """Return the supported application language closest to *locale*."""
    return "de" if locale.language() is QLocale.Language.German else "en"


def locale_for_language(language: str) -> QLocale:
    """Return the stable number/date locale for one supported language."""
    if language == "de":
        return QLocale(QLocale.Language.German, QLocale.Country.Germany)
    return QLocale(QLocale.Language.English, QLocale.Country.UnitedStates)


def configure_locale(language: str) -> None:
    """Set the display locale once during application startup."""
    global _CONFIGURED_LANGUAGE
    _CONFIGURED_LANGUAGE = language if language in SUPPORTED_LANGUAGES else "en"
    QLocale.setDefault(locale_for_language(_CONFIGURED_LANGUAGE))


def display_locale() -> QLocale:
    """Return the selected locale, with English as the pre-start fallback."""
    if _CONFIGURED_LANGUAGE is None:
        return locale_for_language("en")
    return QLocale()


def catalogue_exists(language: str, *, runtime_root: Path | None = None) -> bool:
    """Report whether the application catalogue is present in this runtime."""
    filename = _CATALOGUE_NAMES.get(language)
    if filename is None:
        return False
    try:
        resource_path(filename, runtime_root=runtime_root)
    except (FileNotFoundError, RuntimeError, ValueError):
        return False
    return True


def available_languages(*, runtime_root: Path | None = None) -> tuple[str, ...]:
    """Return supported languages whose checked-in catalogue is loadable."""
    return tuple(
        language
        for language in SUPPORTED_LANGUAGES
        if catalogue_exists(language, runtime_root=runtime_root)
    )


def selected_language(settings: QSettings, *, runtime_root: Path | None = None) -> str:
    """Read a persisted language, otherwise choose the system language if built."""
    stored = settings.value(LANGUAGE_KEY)
    available = available_languages(runtime_root=runtime_root)
    if isinstance(stored, str) and stored in available:
        return stored
    system_language = language_for_locale(QLocale.system())
    return system_language if system_language in available else "en"


def persist_language(settings: QSettings, language: str) -> None:
    """Persist a supported language immediately; restart applies it to the UI."""
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError("unsupported application language")
    settings.setValue(LANGUAGE_KEY, language)
    settings.sync()


def application_translator(
    language: str, *, runtime_root: Path | None = None
) -> QTranslator | None:
    """Load one application catalogue from the source or frozen resource root."""
    filename = _CATALOGUE_NAMES.get(language)
    if filename is None:
        return None
    try:
        path = resource_path(filename, runtime_root=runtime_root)
    except (FileNotFoundError, RuntimeError, ValueError):
        return None
    translator = QTranslator()
    return translator if translator.load(str(path)) else None


def install_translators(
    application: QApplication,
    language: str,
    *,
    runtime_root: Path | None = None,
) -> tuple[QTranslator, ...]:
    """Install the app catalogue and Qt's standard-widget catalogue if present."""
    translators: list[QTranslator] = []
    app_translator = application_translator(language, runtime_root=runtime_root)
    if app_translator is not None:
        application.installTranslator(app_translator)
        translators.append(app_translator)

    translations_enum = getattr(QLibraryInfo.LibraryPath, "Translations")
    qt_translations = Path(QLibraryInfo.path(translations_enum))
    qt_translator = QTranslator()
    qt_catalogue = qt_translations / f"qtbase_{language}.qm"
    if qt_catalogue.is_file() and qt_translator.load(str(qt_catalogue)):
        application.installTranslator(qt_translator)
        translators.append(qt_translator)
    return tuple(translators)


def translate_language_name(language: str) -> str:
    """Translate the language selector's native language labels."""
    if language == "de":
        return QCoreApplication.translate("SettingsDialog", "German")
    return QCoreApplication.translate("SettingsDialog", "English")
