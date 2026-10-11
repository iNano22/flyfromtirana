"""The website's languages: where each one's wording lives and how it gets into a template.

The page templates (templates/site*.html) hold the layout only. Wherever a
piece of text belongs they say {T_NAME}, and templates/strings/<language>.yaml
has the text for NAME in that language. localize() puts the text in, and the
normal template rules (src/formatter.py) take it from there, so a text may
itself use values like {CITY} and optional [[ ... ]] parts.

Adding a language = a new templates/strings/<language>.yaml with the same keys
(a test compares them), its month names in WORDS (src/formatter.py), its name
in LANGUAGE_NAMES below, and the code in website.languages (config.yaml). City
guides are translated separately: content/destinations/<language>/.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import yaml

from src.config import PROJECT_ROOT, ConfigError

STRINGS_DIR = PROJECT_ROOT / "templates" / "strings"
TOKEN = re.compile(r"\{T_([A-Z_]+)\}")

# For the language switcher on the pages: what each language calls itself, and the short
# code it is shown as. Albanian is "AL" there, the code people know, although its folder
# and its language code for browsers are "sq".
LANGUAGE_NAMES = {"sq": "Shqip", "en": "English", "it": "Italiano"}
LANGUAGE_CODES = {"sq": "AL", "en": "EN", "it": "IT"}


@dataclass(frozen=True)
class Strings:
    """One language's wording (one templates/strings/<language>.yaml)."""

    language: str
    page: dict[str, str]   # NAME -> the text for {T_NAME} in the templates
    words: dict            # the sentences the code puts together itself (src/website.py)
    js: dict[str, str]     # the words the main page's script shows


@lru_cache(maxsize=None)
def load_strings(language: str) -> Strings:
    """The wording for `language`. The file is read once and then remembered."""
    path = STRINGS_DIR / f"{language}.yaml"
    if not path.exists():
        raise ConfigError(f"No wording for the language {language!r} (expected {path})")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Strings(language=language,
                       page={str(key): str(text) for key, text in raw["page"].items()},
                       words=dict(raw["words"]),
                       js={str(key): str(text) for key, text in raw["js"].items()})
    except KeyError as exc:
        raise ConfigError(f"{path.name} is missing its {exc} part") from None
    except (TypeError, AttributeError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path.name} is not valid: {exc}") from None


def localize(template: str, strings: Strings) -> str:
    """Replace every {T_NAME} in a template with that language's text for NAME."""
    def text(match: re.Match) -> str:
        name = match.group(1)
        if name not in strings.page:
            raise ConfigError(f"templates/strings/{strings.language}.yaml has no text for {name}")
        return strings.page[name]

    return TOKEN.sub(text, template)
