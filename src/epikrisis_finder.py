#!/usr/bin/env python3
"""Find and copy OMR 1 discharge epicrises from a clinic folder tree.

The tool is intentionally conservative:
- the network source is read-only;
- all writable artifacts are created locally under the output folder;
- the final output is published atomically only after an accepted run;
- uncertain documents are copied to review instead of being silently dropped.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import ctypes
from html import unescape as html_unescape
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape


SOURCE_FOLDER_NAMES = ("ОМР 1 2025", "ОМР1 2024", "ОМР1 2023")
TARGET_FOLDERS = ("ВМП", "ДМС", "ОМС", "ПМУ")
WORD_EXTENSIONS = {".doc", ".docx", ".rtf"}
REVIEW_DIRS = (
    "likely_discharge",
    "weak_match",
    "duplicate_same_patient_same_date",
    "read_error",
)
BEGINNING_CHARS = 14000
HASH_CHUNK_SIZE = 1024 * 1024


STATUS_CONFIRMED = "confirmed"
STATUS_LIKELY = "likely_discharge"
STATUS_WEAK = "weak_match"
STATUS_READ_ERROR = "read_error"
STATUS_NON_MATCH = "non_match"
STATUS_EXACT_DUPLICATE = "exact_duplicate_skipped"
STATUS_AMBIGUOUS_DUPLICATE = "duplicate_same_patient_same_date"


MANIFEST_FIELDS = [
    "status",
    "copied_to",
    "duplicate_of",
    "source_path",
    "source_root",
    "source_folder",
    "original_name",
    "extension",
    "size_bytes",
    "mtime",
    "sha256",
    "short_hash",
    "medical_card",
    "patient_fio",
    "birth_date",
    "admission_date",
    "discharge_date",
    "episode_key",
    "match_title",
    "match_clinic_header",
    "match_department_omr1",
    "match_reasons",
    "error_type",
    "error_message",
]


class FatalRunError(RuntimeError):
    """A run-level error that prevents publishing the final output."""


@dataclass
class Thresholds:
    early_window: int = 60
    early_error_rate: float = 0.20
    early_min_errors: int = 10
    max_same_error_streak: int = 12
    overall_error_rate: float = 0.015
    overall_min_errors: int = 25
    absolute_read_error_limit: int = 150


@dataclass
class RunPaths:
    source_base: Path
    sources: tuple[Path, ...]
    output_final: Path
    output_staging: Path
    confirmed_dir: Path
    review_dir: Path
    logs_dir: Path
    temp_dir: Path


@dataclass
class MatchEvidence:
    title: bool = False
    clinic_header: bool = False
    department_omr1: bool = False
    weak_markers: list[str] = field(default_factory=list)

    def reasons(self) -> str:
        reasons: list[str] = []
        if self.title:
            reasons.append("title")
        if self.clinic_header:
            reasons.append("clinic_header")
        if self.department_omr1:
            reasons.append("department_omr1")
        reasons.extend(self.weak_markers)
        return ";".join(reasons)


@dataclass
class DocumentRecord:
    source_path: Path
    source_root: str
    source_folder: str
    extension: str
    original_name: str
    size_bytes: int = 0
    mtime: str = ""
    status: str = STATUS_NON_MATCH
    copied_to: str = ""
    duplicate_of: str = ""
    sha256: str = ""
    short_hash: str = ""
    medical_card: str = ""
    patient_fio: str = ""
    birth_date: str = ""
    admission_date: str = ""
    discharge_date: str = ""
    episode_key: str = ""
    match_title: bool = False
    match_clinic_header: bool = False
    match_department_omr1: bool = False
    match_reasons: str = ""
    error_type: str = ""
    error_message: str = ""

    def to_manifest_row(self) -> dict[str, object]:
        return {
            "status": self.status,
            "copied_to": self.copied_to,
            "duplicate_of": self.duplicate_of,
            "source_path": str(self.source_path),
            "source_root": self.source_root,
            "source_folder": self.source_folder,
            "original_name": self.original_name,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
            "sha256": self.sha256,
            "short_hash": self.short_hash,
            "medical_card": self.medical_card,
            "patient_fio": self.patient_fio,
            "birth_date": self.birth_date,
            "admission_date": self.admission_date,
            "discharge_date": self.discharge_date,
            "episode_key": self.episode_key,
            "match_title": self.match_title,
            "match_clinic_header": self.match_clinic_header,
            "match_department_omr1": self.match_department_omr1,
            "match_reasons": self.match_reasons,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


class Logger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: str) -> None:
        timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class ConsoleProgress:
    def __init__(self, label: str, total: int, interval_seconds: float = 2.0):
        self.label = label
        self.total = total
        self.interval_seconds = interval_seconds
        self.started_at = time.monotonic()
        self.last_update = 0.0
        self.last_len = 0

    def update(
        self,
        current: int,
        extra: str = "",
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and current < self.total and now - self.last_update < self.interval_seconds:
            return
        self.last_update = now
        elapsed = max(now - self.started_at, 0.001)
        rate = current / elapsed
        percent = (current / self.total * 100) if self.total else 100.0
        line = (
            f"{self.label}: {current}/{self.total} "
            f"({percent:5.1f}%) | {rate:5.1f} files/s"
        )
        if extra:
            line += f" | {extra}"
        if len(line) < self.last_len:
            line += " " * (self.last_len - len(line))
        self.last_len = len(line)
        print("\r" + line, end="", flush=True)
        if current >= self.total:
            print("", flush=True)


class SleepBlocker:
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002

    def __init__(self, logger: Logger | None = None):
        self.logger = logger
        self.enabled = False

    def acquire(self) -> None:
        if platform.system().lower() != "windows":
            return
        try:
            result = ctypes.windll.kernel32.SetThreadExecutionState(
                self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED | self.ES_DISPLAY_REQUIRED
            )
            if result == 0:
                self._log("Sleep blocker failed to activate.")
                return
            self.enabled = True
            self._log("Sleep blocker active: system/display sleep disabled during this run.")
        except Exception as exc:
            self._log(f"Sleep blocker failed: {type(exc).__name__}: {exc}")

    def release(self) -> None:
        if platform.system().lower() != "windows" or not self.enabled:
            return
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
            self._log("Sleep blocker released.")
        except Exception as exc:
            self._log(f"Sleep blocker release failed: {type(exc).__name__}: {exc}")
        finally:
            self.enabled = False

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger.write(message)
        else:
            print(message, flush=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Контрольный поиск выписных эпикризов ОМР1 за 2023, 2024 и 2025 годы."
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="Путь к папке, внутри которой лежат 'ОМР 1 2025', 'ОМР1 2024' и 'ОМР1 2023'. По умолчанию рядом с exe.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Путь к итоговой локальной папке. По умолчанию: Desktop\\Выписки_2023_2025_CONTROL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только сканирование и logs, без копирования документов.",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        default=True,
        help="Не ждать Enter в конце. Полезно для запуска из cmd/тестов.",
    )
    parser.add_argument(
        "--pause-at-end",
        action="store_false",
        dest="no_pause",
        help="Ждать Enter в конце вместо автоматического закрытия консоли.",
    )
    parser.add_argument(
        "--keep-exe",
        action="store_true",
        help="Не удалять exe после успешного завершения.",
    )
    parser.add_argument("--early-window", type=int, default=60)
    parser.add_argument("--early-error-rate", type=float, default=0.20)
    parser.add_argument("--early-min-errors", type=int, default=10)
    parser.add_argument("--max-same-error-streak", type=int, default=12)
    parser.add_argument("--overall-error-rate", type=float, default=0.015)
    parser.add_argument("--overall-min-errors", type=int, default=25)
    parser.add_argument("--absolute-read-error-limit", type=int, default=150)
    parser.add_argument(
        "--discard-failed",
        action="store_true",
        help="Удалить failed staging-папку при фатальной ошибке. По умолчанию она сохраняется для диагностики.",
    )
    return parser.parse_args(argv)


def executable_folder() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_desktop() -> Path:
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        return Path(userprofile) / "Desktop"
    return Path.home() / "Desktop"


def build_run_paths(args: argparse.Namespace) -> RunPaths:
    source_base = args.source.resolve() if args.source else executable_folder()
    if source_base.name in SOURCE_FOLDER_NAMES:
        source_base = source_base.parent
    sources = tuple(source_base / name for name in SOURCE_FOLDER_NAMES)
    output_final = args.output.resolve() if args.output else default_desktop() / "Выписки_2023_2025_CONTROL"
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_staging = output_final.parent / f"{output_final.name}__in_progress_{stamp}"
    return RunPaths(
        source_base=source_base,
        sources=sources,
        output_final=output_final,
        output_staging=output_staging,
        confirmed_dir=output_staging / "Подтвержденные",
        review_dir=output_staging / "review",
        logs_dir=output_staging / "logs",
        temp_dir=output_staging / "_temp",
    )


def preflight(args: argparse.Namespace, paths: RunPaths) -> None:
    if not paths.source_base.exists() or not paths.source_base.is_dir():
        raise FatalRunError(f"Не найдена базовая исходная папка: {paths.source_base}")
    missing_sources = [source for source in paths.sources if not source.is_dir()]
    if missing_sources:
        raise FatalRunError(
            "Не найдены обязательные годовые папки: "
            + ", ".join(str(path) for path in missing_sources)
        )
    missing = [
        f"{source.name}/{folder_name}"
        for source in paths.sources
        for folder_name in TARGET_FOLDERS
        if not (source / folder_name).is_dir()
    ]
    if missing:
        raise FatalRunError(
            "Не найдены обязательные папки внутри источника: " + ", ".join(missing)
        )
    if paths.output_final.exists():
        raise FatalRunError(
            f"Итоговая папка уже существует, чтобы не смешать результаты: {paths.output_final}"
        )
    if paths.output_staging.exists():
        raise FatalRunError(f"Staging-папка уже существует: {paths.output_staging}")

    output_parent = paths.output_final.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    probe_dir = output_parent / f".epikrisis_preflight_{uuid.uuid4().hex[:8]}"
    try:
        probe_dir.mkdir()
        probe_file = probe_dir / "write_test.txt"
        probe_file.write_text("ok", encoding="utf-8")
        if probe_file.read_text(encoding="utf-8") != "ok":
            raise FatalRunError("Не удалось проверить локальную запись.")
        renamed = probe_dir / "rename_test.txt"
        probe_file.rename(renamed)
        renamed.unlink()
    finally:
        if probe_dir.exists():
            shutil.rmtree(probe_dir, ignore_errors=True)

    if platform.system().lower() == "windows":
        check_word_automation()


def check_word_automation() -> None:
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except Exception as exc:  # pragma: no cover - Windows only
        raise FatalRunError(
            "Не найден pywin32. Для .doc нужен пакет pywin32 внутри exe."
        ) from exc
    try:  # pragma: no cover - Windows only
        pythoncom.CoInitialize()
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        word.Quit()
    except Exception as exc:
        raise FatalRunError("Microsoft Word Automation недоступен.") from exc
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def create_staging(paths: RunPaths) -> None:
    paths.confirmed_dir.mkdir(parents=True)
    for name in REVIEW_DIRS:
        (paths.review_dir / name).mkdir(parents=True)
    paths.logs_dir.mkdir(parents=True)
    paths.temp_dir.mkdir(parents=True)


def discover_word_files(sources: tuple[Path, ...], logger: Logger) -> list[Path]:
    files: list[Path] = []
    for source in sources:
        logger.write(f"Scanning source: {source.name}")
        for folder_name in TARGET_FOLDERS:
            before = len(files)
            logger.write(f"Scanning folder: {source.name}/{folder_name}")
            root = source / folder_name
            for current_root, dir_names, file_names in os.walk(root):
                dir_names[:] = [name for name in dir_names if not name.startswith("~$")]
                for file_name in file_names:
                    if file_name.startswith("~$"):
                        continue
                    path = Path(current_root) / file_name
                    if path.suffix.lower() in WORD_EXTENSIONS:
                        files.append(path)
            logger.write(
                f"Scanning folder done: {source.name}/{folder_name}, "
                f"Word/RTF found: {len(files) - before}"
            )
    return sorted(files, key=lambda item: str(item).casefold())


def source_root_for(path: Path, sources: tuple[Path, ...]) -> Path | None:
    for source in sources:
        try:
            path.relative_to(source)
            return source
        except ValueError:
            continue
    return None


def source_folder_for(path: Path, source: Path | None) -> str:
    if source is None:
        return ""
    try:
        return path.relative_to(source).parts[0]
    except Exception:
        return ""


def stat_record(path: Path, sources: tuple[Path, ...]) -> DocumentRecord:
    stat = path.stat()
    source = source_root_for(path, sources)
    return DocumentRecord(
        source_path=path,
        source_root=source.name if source else "",
        source_folder=source_folder_for(path, source),
        extension=path.suffix.lower(),
        original_name=path.name,
        size_bytes=stat.st_size,
        mtime=dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    )


def normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = text.lower()
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_key(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"[^а-яa-z0-9]+", "", text)
    return text


def detect_evidence(beginning_text: str, source_path: Path) -> MatchEvidence:
    normalized = normalize_text(beginning_text)
    path_normalized = normalize_text(str(source_path))
    evidence = MatchEvidence()
    evidence.title = has_discharge_title(beginning_text)
    clinic_terms = (
        "федеральный центр мозга",
        "фцмн",
        "федеральное медико биологическое агентство",
        "федеральное медико-биологическое агентство",
    )
    evidence.clinic_header = any(term in normalized for term in clinic_terms)
    evidence.department_omr1 = is_omr1_department(normalized)

    weak_checks = {
        "path_discharge_hint": any(token in path_normalized for token in ("выпис", "эпикриз")),
        "medical_card": "номер медицинской карты" in normalized,
        "hospital_period": "период нахождения" in normalized,
        "patient_info": "сведения о пациенте" in normalized,
    }
    evidence.weak_markers = [name for name, matched in weak_checks.items() if matched]
    return evidence


def has_discharge_title(text: str) -> bool:
    head = text.replace("\u00a0", " ")[:5000]
    for line in head.splitlines():
        normalized_line = normalize_text(line).strip(" .,:;№-")
        if normalized_line == "выписной эпикриз":
            return True
    return bool(
        re.search(
            r"(?im)(?:^|[\r\n\f])\s*выписн\w*\s+эпикриз\s*(?:$|[\r\n\f])",
            head,
        )
    )


def is_omr1_department(normalized: str) -> bool:
    if "отделение медицинской реабилитации" not in normalized:
        return False
    cns_match = re.search(
        r"нарушением\s+функци(?:и|й)\s+(?:цнс|центральной\s+нервной\s+системы)",
        normalized,
    )
    if not cns_match:
        return False
    omr_index = normalized.find("отделение медицинской реабилитации")
    cns_index = cns_match.start()
    start = min(index for index in (omr_index, cns_index) if index >= 0)
    window = normalized[start : start + 500]
    return bool(re.search(r"(?:№|n|no\.?|номер)?\s*1\b", window))


def classify(evidence: MatchEvidence) -> str:
    if evidence.title and evidence.clinic_header and evidence.department_omr1:
        return STATUS_CONFIRMED
    if evidence.title and (evidence.clinic_header or evidence.department_omr1):
        return STATUS_LIKELY
    if evidence.department_omr1 and len(evidence.weak_markers) >= 1:
        return STATUS_LIKELY
    if evidence.title or len(evidence.weak_markers) >= 2:
        return STATUS_WEAK
    return STATUS_NON_MATCH


def extract_discovery_metadata(text: str) -> dict[str, str]:
    clean = " ".join(text.replace("\u00a0", " ").split())
    return {
        "medical_card": extract_medical_card(clean),
        "patient_fio": extract_patient_fio(clean),
        "birth_date": extract_birth_date(clean),
        "admission_date": "",
        "discharge_date": "",
        **extract_period_dates(clean),
    }


def extract_medical_card(text: str) -> str:
    match = re.search(
        r"Номер\s+медицинской\s+карты\s*[:№]?\s*"
        r"([А-ЯA-Zа-яa-z0-9/\\\-\s]+?)(?=\s+Сведения\s+о\s+пациенте|\s+Фамилия|\s+Дата\s+рождения|$)",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", "", match.group(1).strip(" .,:;№")) if match else ""


def extract_patient_fio(text: str) -> str:
    patterns = [
        r"Фамилия,\s*имя,\s*отчество\s*\(при наличии\)\s*:\s*([^:]+?)(?:\s+Пол\s*:|\s+Дата\s+рождения)",
        r"Фамилия,\s*имя,\s*отчество\s*:\s*([^:]+?)(?:\s+Пол\s*:|\s+Дата\s+рождения)",
        r"ФИО\s*:\s*([^:]+?)(?:\s+Пол\s*:|\s+Дата\s+рождения)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean_fio(match.group(1))
    return ""


def clean_fio(value: str) -> str:
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^А-Яа-яЁёA-Za-z\-\s]", "", value)
    return value.strip()


def extract_birth_date(text: str) -> str:
    match = re.search(r"Дата\s+рождения\s*\(возраст\)\s*:\s*(\d{2}\.\d{2}\.\d{4})", text)
    if not match:
        match = re.search(r"Дата\s+рождения\s*:?\s*(\d{2}\.\d{2}\.\d{4})", text)
    return to_iso_date(match.group(1)) if match else ""


def extract_period_dates(text: str) -> dict[str, str]:
    quoted = re.search(
        r"Период\s+нахождения.*?с\s*[«\"]?(\d{1,2})[»\"]?\s+([А-Яа-яёЁ]+)\s+(\d{4})\s*г.*?"
        r"по\s*[«\"]?(\d{1,2})[»\"]?\s+([А-Яа-яёЁ]+)\s+(\d{4})\s*г",
        text,
        flags=re.IGNORECASE,
    )
    if quoted:
        admission = russian_date_to_iso(quoted.group(1), quoted.group(2), quoted.group(3))
        discharge = russian_date_to_iso(quoted.group(4), quoted.group(5), quoted.group(6))
        return {"admission_date": admission, "discharge_date": discharge}
    numeric = re.search(
        r"Период\s+нахождения.*?с\s*(\d{2}\.\d{2}\.\d{4}).*?по\s*(\d{2}\.\d{2}\.\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if numeric:
        return {
            "admission_date": to_iso_date(numeric.group(1)),
            "discharge_date": to_iso_date(numeric.group(2)),
        }
    return {"admission_date": "", "discharge_date": ""}


MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


def russian_date_to_iso(day: str, month_name: str, year: str) -> str:
    month = MONTHS.get(normalize_text(month_name))
    if not month:
        return ""
    return f"{int(year):04d}-{month:02d}-{int(day):02d}"


def to_iso_date(value: str) -> str:
    try:
        parsed = dt.datetime.strptime(value, "%d.%m.%Y")
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        return ""


def build_episode_key(record: DocumentRecord) -> str:
    normalized_fio = normalize_key(record.patient_fio)
    patient_key = normalized_fio + "|" + record.birth_date if normalized_fio else ""
    if not patient_key and record.medical_card:
        patient_key = normalize_key(record.medical_card)
    episode_date = record.discharge_date or record.admission_date
    if not patient_key or not episode_date:
        return ""
    return patient_key + "|" + episode_date


class TextExtractor:
    def __init__(self, temp_dir: Path, logger: Logger):
        self.temp_dir = temp_dir
        self.logger = logger
        self.word = None
        self.pythoncom = None

    def close(self) -> None:
        if self.word is not None:
            try:
                self.word.Quit()
            except Exception:
                pass
            self.word = None
        if self.pythoncom is not None:
            try:
                self.pythoncom.CoUninitialize()
            except Exception:
                pass
            self.pythoncom = None

    def extract_beginning(self, path: Path, extension: str, limit: int = BEGINNING_CHARS) -> str:
        if extension == ".docx":
            return extract_docx_text(path, limit)
        if extension == ".rtf":
            return extract_rtf_text(path, limit)
        if extension == ".doc":
            return self.extract_doc_text(path, limit)
        raise ValueError(f"Unsupported extension: {extension}")

    def extract_doc_text(self, path: Path, limit: int) -> str:
        if platform.system().lower() == "windows":
            return self.extract_doc_with_word(path, limit)
        return extract_doc_with_textutil(path, limit)

    def ensure_word(self):  # pragma: no cover - Windows only
        if self.word is not None:
            return self.word
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom.CoInitialize()
        self.pythoncom = pythoncom
        self.word = win32com.client.DispatchEx("Word.Application")
        self.word.Visible = False
        self.word.DisplayAlerts = 0
        try:
            self.word.AutomationSecurity = 3
        except Exception:
            pass
        return self.word

    def restart_word(self) -> None:  # pragma: no cover - Windows only
        self.close()
        self.ensure_word()

    def extract_doc_with_word(self, path: Path, limit: int) -> str:  # pragma: no cover - Windows only
        last_exc: Exception | None = None
        for attempt in range(2):
            local_copy = self.temp_dir / f"{uuid.uuid4().hex}{path.suffix.lower()}"
            try:
                shutil.copy2(path, local_copy)
                word = self.ensure_word()
                document = word.Documents.Open(
                    FileName=str(local_copy),
                    ConfirmConversions=False,
                    ReadOnly=True,
                    AddToRecentFiles=False,
                    Revert=True,
                    Visible=False,
                    OpenAndRepair=True,
                    NoEncodingDialog=True,
                )
                try:
                    end = min(int(document.Content.End), limit)
                    text = document.Range(0, end).Text
                    return text or ""
                finally:
                    document.Close(False)
            except Exception as exc:
                last_exc = exc
                self.logger.write(f"DOC read attempt {attempt + 1} failed: {path} :: {exc}")
                self.restart_word()
            finally:
                try:
                    local_copy.unlink(missing_ok=True)
                except Exception:
                    pass
        raise RuntimeError(f"Word failed to read DOC: {last_exc}")


def extract_docx_text(path: Path, limit: int) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"<w:tab\s*/>", "\t", xml)
    xml = re.sub(r"</w:p>", "\n", xml)
    parts: list[str] = []
    total = 0
    for match in re.finditer(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>|[\t\n]", xml, flags=re.DOTALL):
        token = match.group(0)
        if token == "\t" or token == "\n":
            text = token
        else:
            text = html_unescape(match.group(1) or "")
        parts.append(text)
        total += len(text)
        if total >= limit:
            break
    return "".join(parts)[:limit]


def extract_rtf_text(path: Path, limit: int) -> str:
    raw = path.read_bytes()[: max(limit * 8, 65536)]
    for encoding in ("utf-8", "cp1251", "latin1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin1", errors="ignore")
    return strip_rtf(text)[:limit]


def strip_rtf(text: str) -> str:
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\\tab", "\t", text)
    text = re.sub(r"\\[a-zA-Z]+\d* ?", "", text)
    text = text.replace("{", " ").replace("}", " ")
    text = text.replace("\\", "")
    return re.sub(r"\s+", " ", text)


def extract_doc_with_textutil(path: Path, limit: int) -> str:
    result = subprocess.run(
        ["textutil", "-convert", "txt", "-stdout", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    return result.stdout.decode("utf-8", errors="replace")[:limit]


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fill_hash(record: DocumentRecord) -> None:
    if record.sha256:
        return
    record.sha256 = compute_sha256(record.source_path)
    record.short_hash = record.sha256[:8]


def process_records(
    files: list[Path],
    sources: tuple[Path, ...],
    extractor: TextExtractor,
    thresholds: Thresholds,
    logger: Logger,
) -> list[DocumentRecord]:
    records: list[DocumentRecord] = []
    read_errors = 0
    same_error_streak = 0
    last_error_type = ""
    progress = ConsoleProgress("Processing", len(files))
    status_counts: dict[str, int] = {}

    for index, path in enumerate(files, start=1):
        record = stat_record(path, sources)
        try:
            text = extractor.extract_beginning(path, record.extension)
            evidence = detect_evidence(text, path)
            record.status = classify(evidence)
            record.match_title = evidence.title
            record.match_clinic_header = evidence.clinic_header
            record.match_department_omr1 = evidence.department_omr1
            record.match_reasons = evidence.reasons()
            metadata = extract_discovery_metadata(text)
            record.medical_card = metadata.get("medical_card", "")
            record.patient_fio = metadata.get("patient_fio", "")
            record.birth_date = metadata.get("birth_date", "")
            record.admission_date = metadata.get("admission_date", "")
            record.discharge_date = metadata.get("discharge_date", "")
            record.episode_key = build_episode_key(record)
            same_error_streak = 0
            last_error_type = ""
        except Exception as exc:
            read_errors += 1
            record.status = STATUS_READ_ERROR
            record.error_type = type(exc).__name__
            record.error_message = truncate(str(exc), 500)
            if record.error_type == last_error_type:
                same_error_streak += 1
            else:
                same_error_streak = 1
                last_error_type = record.error_type
            logger.write(f"READ_ERROR {path}: {record.error_type}: {record.error_message}")

        records.append(record)
        status_counts[record.status] = status_counts.get(record.status, 0) + 1
        review_count = sum(
            status_counts.get(status, 0)
            for status in (STATUS_LIKELY, STATUS_WEAK, STATUS_AMBIGUOUS_DUPLICATE)
        )
        extra = (
            f"confirmed={status_counts.get(STATUS_CONFIRMED, 0)} "
            f"review={review_count} "
            f"read_error={status_counts.get(STATUS_READ_ERROR, 0)} "
            f"current={truncate(path.name, 42)}"
        )
        progress.update(index, extra=extra)
        if index % 100 == 0:
            logger.write(f"Processed {index}/{len(files)} Word/RTF files")
        check_systematic_failures(index, read_errors, same_error_streak, thresholds)

    check_final_error_rate(len(records), read_errors, thresholds)
    return records


def check_systematic_failures(
    processed: int,
    read_errors: int,
    same_error_streak: int,
    thresholds: Thresholds,
) -> None:
    if processed >= thresholds.early_window:
        early_errors = read_errors
        if (
            processed == thresholds.early_window
            and early_errors >= thresholds.early_min_errors
            and early_errors / processed >= thresholds.early_error_rate
        ):
            raise FatalRunError(
                f"Системная ошибка чтения в начале: {early_errors}/{processed}."
            )
    if same_error_streak >= thresholds.max_same_error_streak:
        raise FatalRunError(
            f"Системная ошибка чтения: {same_error_streak} одинаковых ошибок подряд."
        )
    if read_errors >= thresholds.absolute_read_error_limit:
        raise FatalRunError(
            f"Слишком много read_error: {read_errors}."
        )


def check_final_error_rate(total: int, read_errors: int, thresholds: Thresholds) -> None:
    if total == 0:
        return
    if read_errors >= thresholds.overall_min_errors and read_errors / total > thresholds.overall_error_rate:
        percent = read_errors / total * 100
        raise FatalRunError(
            f"Системная доля read_error: {read_errors}/{total} ({percent:.2f}%)."
        )


def truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def copy_outputs(records: list[DocumentRecord], paths: RunPaths, dry_run: bool, logger: Logger) -> None:
    candidates = [
        record
        for record in records
        if record.status in {STATUS_CONFIRMED, STATUS_LIKELY, STATUS_WEAK, STATUS_READ_ERROR}
    ]
    for record in candidates:
        try:
            fill_hash(record)
        except Exception as exc:
            record.error_type = type(exc).__name__
            record.error_message = truncate(f"hash_failed: {exc}", 500)
            raise FatalRunError(f"Не удалось посчитать hash: {record.source_path}") from exc

    mark_exact_duplicates(candidates)
    mark_ambiguous_duplicates(candidates)

    if dry_run:
        logger.write("DRY-RUN: document copying skipped.")
        return

    used_names: set[str] = set()
    copy_items = [record for record in candidates if record.status != STATUS_EXACT_DUPLICATE]
    progress = ConsoleProgress("Copying", len(copy_items))
    copied = 0
    for record in candidates:
        if record.status == STATUS_EXACT_DUPLICATE:
            continue
        target_dir = target_dir_for(record, paths)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = make_output_name(record, used_names)
        target_path = target_dir / target_name
        try:
            shutil.copy2(record.source_path, target_path)
            final_path = paths.output_final / target_path.relative_to(paths.output_staging)
            record.copied_to = str(final_path)
            copied += 1
            progress.update(copied, extra=f"current={truncate(record.original_name, 54)}")
        except Exception as exc:
            raise FatalRunError(f"Не удалось скопировать файл: {record.source_path}") from exc


def mark_exact_duplicates(records: list[DocumentRecord]) -> None:
    groups: dict[str, list[DocumentRecord]] = {}
    for record in records:
        if record.sha256:
            groups.setdefault(record.sha256, []).append(record)

    priority = {
        STATUS_CONFIRMED: 0,
        STATUS_LIKELY: 1,
        STATUS_WEAK: 2,
        STATUS_READ_ERROR: 3,
    }
    for group_records in groups.values():
        if len(group_records) <= 1:
            continue
        primary = min(
            group_records,
            key=lambda record: (
                priority.get(record.status, 99),
                str(record.source_path).casefold(),
            ),
        )
        for record in group_records:
            if record is primary:
                continue
            record.status = STATUS_EXACT_DUPLICATE
            record.duplicate_of = primary.source_path.as_posix()


def mark_ambiguous_duplicates(records: list[DocumentRecord]) -> None:
    groups: dict[str, list[DocumentRecord]] = {}
    for record in records:
        if record.status in {STATUS_EXACT_DUPLICATE, STATUS_READ_ERROR}:
            continue
        if not record.episode_key:
            continue
        groups.setdefault(record.episode_key, []).append(record)

    for group_records in groups.values():
        hashes = {record.sha256 for record in group_records if record.sha256}
        if len(hashes) <= 1:
            continue
        for record in group_records:
            record.status = STATUS_AMBIGUOUS_DUPLICATE


def target_dir_for(record: DocumentRecord, paths: RunPaths) -> Path:
    if record.status == STATUS_CONFIRMED:
        return paths.confirmed_dir
    if record.status == STATUS_LIKELY:
        return paths.review_dir / "likely_discharge"
    if record.status == STATUS_WEAK:
        return paths.review_dir / "weak_match"
    if record.status == STATUS_READ_ERROR:
        return paths.review_dir / "read_error"
    if record.status == STATUS_AMBIGUOUS_DUPLICATE:
        return paths.review_dir / "duplicate_same_patient_same_date"
    return paths.review_dir / "weak_match"


def make_output_name(record: DocumentRecord, used_names: set[str]) -> str:
    pieces = [
        safe_filename(record.medical_card),
        safe_filename(record.patient_fio),
        safe_filename(record.discharge_date or record.admission_date),
        record.short_hash or "nohash",
    ]
    base = "__".join(piece for piece in pieces if piece)
    if not base:
        base = "unknown__" + (record.short_hash or uuid.uuid4().hex[:8])
    name = base + record.extension
    counter = 2
    while name.casefold() in used_names:
        name = f"{base}__{counter}{record.extension}"
        counter += 1
    used_names.add(name.casefold())
    return name


def safe_filename(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r"[<>:\"|?*\x00-\x1f]+", "", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("._ ")[:120]


def write_manifest(records: list[DocumentRecord], logs_dir: Path) -> None:
    rows = [record.to_manifest_row() for record in records]
    csv_path = logs_dir / "manifest.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    write_basic_xlsx(logs_dir / "manifest.xlsx", MANIFEST_FIELDS, rows)


def write_summary(
    records: list[DocumentRecord],
    logs_dir: Path,
    sources: tuple[Path, ...],
    output: Path,
    dry_run: bool,
) -> None:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    lines = [
        "Статус: ГОТОВО",
        f"Режим: {'dry-run' if dry_run else 'copy'}",
        "Источники: " + "; ".join(str(source) for source in sources),
        f"Выход: {output}",
        "",
        f"Word/RTF всего: {len(records)}",
    ]
    for status in sorted(counts):
        lines.append(f"{status}: {counts[status]}")
    lines.append("")
    lines.append("Основные файлы контроля:")
    lines.append("logs/manifest.xlsx")
    lines.append("logs/manifest.csv")
    lines.append("logs/run_log.txt")
    lines.append("logs/summary.txt")
    (logs_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_basic_xlsx(path: Path, headers: list[str], rows: list[dict[str, object]]) -> None:
    sheet_rows = [headers] + [[row.get(header, "") for header in headers] for row in rows]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", XLSX_CONTENT_TYPES)
        archive.writestr("_rels/.rels", XLSX_RELS)
        archive.writestr("xl/workbook.xml", XLSX_WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", XLSX_WORKBOOK_RELS)
        archive.writestr("xl/styles.xml", XLSX_STYLES)
        archive.writestr("xl/worksheets/sheet1.xml", build_sheet_xml(sheet_rows))


def build_sheet_xml(rows: list[list[object]]) -> str:
    body: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for col_index, value in enumerate(row, start=1):
            ref = f"{column_name(col_index)}{row_index}"
            text = escape(str(value or ""))
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>')
        body.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        + "".join(body)
        + "</sheetData></worksheet>"
    )


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


XLSX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

XLSX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

XLSX_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="manifest" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

XLSX_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

XLSX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>"""


def publish(paths: RunPaths) -> None:
    paths.output_staging.rename(paths.output_final)


def schedule_self_delete(args: argparse.Namespace, logger: Logger | None) -> None:
    if args.dry_run or args.keep_exe:
        return
    if not getattr(sys, "frozen", False):
        return
    if platform.system().lower() != "windows":
        return
    exe_path = Path(sys.executable).resolve()
    command = f'ping 127.0.0.1 -n 6 > nul & del /f /q "{exe_path}"'
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:  # pragma: no cover - Windows only
        subprocess.Popen(
            ["cmd", "/c", command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        if logger:
            logger.write(f"Self-delete scheduled for exe: {exe_path}")
    except Exception as exc:
        if logger:
            logger.write(f"Self-delete scheduling failed: {type(exc).__name__}: {exc}")


def fail_staging(paths: RunPaths, keep_failed: bool) -> None:
    if not paths.output_staging.exists():
        return
    if keep_failed:
        failed = paths.output_staging.with_name(
            paths.output_staging.name.replace("__in_progress_", "__FAILED_")
        )
        try:
            paths.output_staging.rename(failed)
        except Exception:
            pass
    else:
        shutil.rmtree(paths.output_staging, ignore_errors=True)


def run(argv: list[str]) -> int:
    args = parse_args(argv)
    thresholds = Thresholds(
        early_window=args.early_window,
        early_error_rate=args.early_error_rate,
        early_min_errors=args.early_min_errors,
        max_same_error_streak=args.max_same_error_streak,
        overall_error_rate=args.overall_error_rate,
        overall_min_errors=args.overall_min_errors,
        absolute_read_error_limit=args.absolute_read_error_limit,
    )
    paths = build_run_paths(args)
    logger: Logger | None = None
    extractor: TextExtractor | None = None
    sleep_blocker = SleepBlocker()
    success = False
    try:
        preflight(args, paths)
        create_staging(paths)
        logger = Logger(paths.logs_dir / "run_log.txt")
        sleep_blocker.logger = logger
        sleep_blocker.acquire()
        logger.write("Preflight passed.")
        logger.write(f"Source base: {paths.source_base}")
        logger.write("Sources: " + ", ".join(str(source) for source in paths.sources))
        logger.write(f"Staging output: {paths.output_staging}")
        logger.write(f"Dry-run: {args.dry_run}")

        files = discover_word_files(paths.sources, logger)
        if not files:
            raise FatalRunError("В целевых папках не найдено ни одного Word/RTF файла.")
        logger.write(f"Discovered Word/RTF files: {len(files)}")

        extractor = TextExtractor(paths.temp_dir, logger)
        records = process_records(files, paths.sources, extractor, thresholds, logger)
        copy_outputs(records, paths, args.dry_run, logger)
        write_manifest(records, paths.logs_dir)
        write_summary(records, paths.logs_dir, paths.sources, paths.output_final, args.dry_run)
        publish(paths)
        print(f"ГОТОВО. Логи: {paths.output_final / 'logs'}")
        success = True
        return 0
    except FatalRunError as exc:
        message = f"FAILED: {exc}"
        if logger:
            logger.write(message)
        else:
            print(message, file=sys.stderr)
        fail_staging(paths, keep_failed=not args.discard_failed)
        return 2
    except KeyboardInterrupt:
        if logger:
            logger.write("FAILED: interrupted by user.")
        fail_staging(paths, keep_failed=not args.discard_failed)
        return 130
    except Exception as exc:
        message = f"FAILED unexpected: {type(exc).__name__}: {exc}"
        if logger:
            logger.write(message)
        else:
            print(message, file=sys.stderr)
        fail_staging(paths, keep_failed=not args.discard_failed)
        return 1
    finally:
        sleep_blocker.release()
        if extractor:
            extractor.close()
        if not args.no_pause and getattr(sys, "frozen", False):
            try:
                input("Нажмите Enter для выхода...")
            except EOFError:
                pass
        if success:
            schedule_self_delete(args, logger)


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
