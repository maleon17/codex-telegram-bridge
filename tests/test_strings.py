import contextvars
from collections import Counter
from string import Formatter
import unittest

from strings import COMMAND_DESCRIPTIONS, STRINGS, current_language, t


class StringCatalogTests(unittest.TestCase):
    def test_templates_format_exactly(self):
        self.assertEqual(
            t("attachment_processing_error", error="таймаут"),
            "Не смог обработать вложение: таймаут",
        )
        self.assertEqual(
            t("model_unavailable", model="gpt-test", picker="Список"),
            "Модель «gpt-test» недоступна.\n\nСписок",
        )

    def test_missing_key_raises(self):
        with self.assertRaises(KeyError):
            t("missing_catalog_key")

    def test_unknown_language_falls_back_to_russian(self):
        self.assertEqual(t("persona_updated", lang="xx"), STRINGS["ru"]["persona_updated"])

    def test_context_language_and_explicit_override(self):
        sentinel = "CONTEXT TRANSLATION"
        original = STRINGS["en"]["persona_updated"]
        STRINGS["en"]["persona_updated"] = sentinel
        token = current_language.set("en")
        try:
            self.assertEqual(t("persona_updated"), sentinel)
            self.assertEqual(t("persona_empty"), STRINGS["en"]["persona_empty"])
            self.assertEqual(t("persona_updated", lang="ru"), STRINGS["ru"]["persona_updated"])
        finally:
            current_language.reset(token)
            STRINGS["en"]["persona_updated"] = original

    def test_translations_have_same_keys_and_placeholders(self):
        formatter = Formatter()
        source = STRINGS["ru"]
        for language in ("en", "uk", "kk", "de"):
            translated = STRINGS[language]
            self.assertEqual(set(translated), set(source), language)
            for key, original in source.items():
                with self.subTest(language=language, key=key):
                    source_fields = Counter(name for _, name, _, _ in formatter.parse(original) if name)
                    translated_fields = Counter(name for _, name, _, _ in formatter.parse(translated[key]) if name)
                    self.assertEqual(translated_fields, source_fields)

    def test_fresh_context_defaults_to_russian(self):
        self.assertEqual(
            contextvars.Context().run(t, "persona_updated"),
            STRINGS["ru"]["persona_updated"],
        )

    def test_command_descriptions_cover_every_language(self):
        self.assertEqual(set(COMMAND_DESCRIPTIONS), set(STRINGS))
        source = set(COMMAND_DESCRIPTIONS["ru"])
        for language, descriptions in COMMAND_DESCRIPTIONS.items():
            self.assertEqual(set(descriptions), source, language)
            self.assertTrue(all(3 <= len(value) <= 256 for value in descriptions.values()))


if __name__ == "__main__":
    unittest.main()
