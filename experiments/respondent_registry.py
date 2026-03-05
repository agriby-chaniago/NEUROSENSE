"""
experiments/respondent_registry.py  –  Respondent CRUD backed by a JSON file.

Respondents are stored as:
  data/respondents.json
  {
    "R001": {"respondent_id": "R001", "gender": "M", "age": 22, "notes": ""},
    ...
  }
"""

import json
import logging
import threading
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)


class RespondentRegistry:
    """Thread-safe persistent store for respondent demographic data."""

    def __init__(self, filepath: str = None):
        fp = filepath or getattr(
            config, "EXPERIMENT_RESPONDENTS_FILE",
            str(Path(config.DATA_DIR) / "respondents.json"),
        )
        self._path = Path(fp)
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self):
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.debug("RespondentRegistry: loaded %d respondents", len(self._data))
            except Exception as exc:
                logger.warning("RespondentRegistry: load failed (%s), starting empty", exc)
                self._data = {}

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    # ── ID helper ─────────────────────────────────────────────────────────

    def next_id(self) -> str:
        """Return next auto-generated ID, e.g. R005 if R001–R004 exist."""
        nums = [
            int(k[1:]) for k in self._data
            if k.startswith("R") and k[1:].isdigit()
        ]
        n = max(nums, default=0) + 1
        return f"R{n:03d}"

    # ── CRUD ──────────────────────────────────────────────────────────────

    def add(self, respondent_id: str, gender: str, age: int, notes: str = "") -> dict:
        """
        Register a new respondent.

        Parameters
        ----------
        respondent_id : str   e.g. "R001"
        gender        : str   "M" or "F"
        age           : int
        notes         : str   optional free-text

        Raises
        ------
        ValueError if respondent_id already exists.
        """
        with self._lock:
            if respondent_id in self._data:
                raise ValueError(f"Respondent '{respondent_id}' already registered.")
            entry = {
                "respondent_id": respondent_id,
                "gender":        gender.upper(),
                "age":           int(age),
                "notes":         notes,
            }
            self._data[respondent_id] = entry
            self._save()
        logger.info("RespondentRegistry: registered %s (gender=%s, age=%d)",
                    respondent_id, gender, age)
        return entry

    def get(self, respondent_id: str) -> Optional[dict]:
        """Return respondent dict or None."""
        return self._data.get(respondent_id)

    def get_all(self) -> list[dict]:
        """Return all respondents sorted by ID."""
        with self._lock:
            return sorted(self._data.values(), key=lambda x: x["respondent_id"])

    def update(self, respondent_id: str, **kwargs) -> dict:
        """Update fields for an existing respondent."""
        with self._lock:
            if respondent_id not in self._data:
                raise ValueError(f"Respondent '{respondent_id}' not found.")
            for k, v in kwargs.items():
                if k in ("gender", "age", "notes"):
                    self._data[respondent_id][k] = v
            self._save()
        return self._data[respondent_id]

    def delete(self, respondent_id: str) -> bool:
        with self._lock:
            if respondent_id not in self._data:
                return False
            del self._data[respondent_id]
            self._save()
        logger.info("RespondentRegistry: deleted %s", respondent_id)
        return True
