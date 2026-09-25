import unittest

from strings import STRINGS, t


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


if __name__ == "__main__":
    unittest.main()
