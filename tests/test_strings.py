import contextvars
import unittest

from strings import STRINGS, current_language, t


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
        STRINGS["en"]["persona_updated"] = sentinel
        token = current_language.set("en")
        try:
            self.assertEqual(t("persona_updated"), sentinel)
            self.assertEqual(t("persona_empty"), STRINGS["ru"]["persona_empty"])
            self.assertEqual(t("persona_updated", lang="ru"), STRINGS["ru"]["persona_updated"])
        finally:
            current_language.reset(token)
            del STRINGS["en"]["persona_updated"]

    def test_fresh_context_defaults_to_russian(self):
        self.assertEqual(
            contextvars.Context().run(t, "persona_updated"),
            STRINGS["ru"]["persona_updated"],
        )


if __name__ == "__main__":
    unittest.main()
