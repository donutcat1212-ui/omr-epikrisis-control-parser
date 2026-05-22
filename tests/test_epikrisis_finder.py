import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from epikrisis_finder import (  # noqa: E402
    STATUS_AMBIGUOUS_DUPLICATE,
    STATUS_CONFIRMED,
    STATUS_EXACT_DUPLICATE,
    DocumentRecord,
    build_episode_key,
    build_run_paths,
    classify,
    detect_evidence,
    extract_discovery_metadata,
    mark_ambiguous_duplicates,
    mark_exact_duplicates,
    parse_args,
)


class EpikrisisFinderTests(unittest.TestCase):
    def test_confirmed_header_match(self):
        text = """
        ФЕДЕРАЛЬНОЕ МЕДИКО-БИОЛОГИЧЕСКОЕ АГЕНТСТВО
        ФГБУ «ФЦМН» ФМБА РОССИИ
        Выписной эпикриз
        Отделение медицинской реабилитации пациентов с нарушением функций центральной нервной системы №1
        Номер медицинской карты СКП4304/25
        """
        evidence = detect_evidence(text, Path("Выписной эпикриз.docx"))
        self.assertTrue(evidence.title)
        self.assertTrue(evidence.clinic_header)
        self.assertTrue(evidence.department_omr1)
        self.assertEqual(classify(evidence), STATUS_CONFIRMED)

    def test_other_department_not_confirmed(self):
        text = """
        ФГБУ «ФЦМН» ФМБА РОССИИ
        Выписной эпикриз
        Отделение медицинской реабилитации пациентов с нарушением функций центральной нервной системы №2
        """
        evidence = detect_evidence(text, Path("Выписной эпикриз.docx"))
        self.assertTrue(evidence.title)
        self.assertTrue(evidence.clinic_header)
        self.assertFalse(evidence.department_omr1)
        self.assertNotEqual(classify(evidence), STATUS_CONFIRMED)

    def test_incidental_discharge_phrase_is_not_title(self):
        text = """
        Отделение медицинской реабилитации пациентов с нарушением функций центральной нервной системы №1
        ПЕРВИЧНЫЙ ОСМОТР
        Фамилия, имя, отчество (при наличии) Баталов Владимир Юрьевич
        Медицинская карта пациента, получающего медицинскую помощь в стационарных условиях № СКП5822/25
        Анамнез заболевания: выписной эпикриз не предоставлен.
        """
        evidence = detect_evidence(text, Path("Первичный осмотр.doc"))

        self.assertFalse(evidence.title)
        self.assertTrue(evidence.department_omr1)
        self.assertNotEqual(classify(evidence), STATUS_CONFIRMED)

    def test_older_cns_abbreviation_header_match(self):
        text = """
        ФГБУ «ФЦМН» ФМБА РОССИИ
        ВЫПИСНОЙ ЭПИКРИЗ
        Отделение медицинской реабилитации пациентов с нарушением функции ЦНС №1
        Номер медицинской карты № СКП2945/23
        Сведения о пациенте:
        Фамилия, имя, отчество: Аданников Андрей Анатольевич
        Дата рождения: 13.07.1969 г.р. Пол: мужской
        Период нахождения в стационаре, дневном стационаре: с «10» мая 2023г.
        по «26» мая 2023г.
        """
        evidence = detect_evidence(text, Path("ФРМ_Самко_Выписной эпикриз_Аданников.doc"))
        metadata = extract_discovery_metadata(text)

        self.assertTrue(evidence.department_omr1)
        self.assertEqual(classify(evidence), STATUS_CONFIRMED)
        self.assertEqual(metadata["medical_card"], "СКП2945/23")
        self.assertEqual(metadata["patient_fio"], "Аданников Андрей Анатольевич")
        self.assertEqual(metadata["birth_date"], "1969-07-13")
        self.assertEqual(metadata["discharge_date"], "2023-05-26")

    def test_control_parser_uses_all_three_year_roots(self):
        args = parse_args(["--source", "/tmp/source-root", "--output", "/tmp/output"])
        paths = build_run_paths(args)

        self.assertEqual([path.name for path in paths.sources], ["ОМР 1 2025", "ОМР1 2024", "ОМР1 2023"])
        self.assertTrue(args.no_pause)

    def test_same_patient_different_dates_are_not_ambiguous_duplicates(self):
        first = self._record("a.docx", "1", "2025-01-10")
        second = self._record("b.docx", "2", "2025-02-10")

        mark_ambiguous_duplicates([first, second])

        self.assertEqual(first.status, STATUS_CONFIRMED)
        self.assertEqual(second.status, STATUS_CONFIRMED)

    def test_same_patient_same_date_different_hash_goes_to_review(self):
        first = self._record("a.docx", "1", "2025-01-10")
        second = self._record("b.docx", "2", "2025-01-10")

        mark_ambiguous_duplicates([first, second])

        self.assertEqual(first.status, STATUS_AMBIGUOUS_DUPLICATE)
        self.assertEqual(second.status, STATUS_AMBIGUOUS_DUPLICATE)

    def test_exact_duplicate_keeps_one_record(self):
        first = self._record("a.docx", "same", "2025-01-10")
        second = self._record("b.docx", "same", "2025-01-10")

        mark_exact_duplicates([first, second])

        statuses = sorted([first.status, second.status])
        self.assertEqual(statuses, sorted([STATUS_CONFIRMED, STATUS_EXACT_DUPLICATE]))

    def _record(self, name: str, sha256: str, discharge_date: str) -> DocumentRecord:
        record = DocumentRecord(Path(name), "ОМР1 2024", "ОМС", ".docx", name)
        record.status = STATUS_CONFIRMED
        record.sha256 = sha256
        record.patient_fio = "Иванов Иван Иванович"
        record.birth_date = "1970-01-01"
        record.discharge_date = discharge_date
        record.episode_key = build_episode_key(record)
        return record


if __name__ == "__main__":
    unittest.main()
