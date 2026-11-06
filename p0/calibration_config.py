"""The calibration *configuration* (a "recipe"), as pure data.

This is the file the **Configure calibration** window edits and saves. It is a
*plan* for a recording — never signals, never a process. A later step (Pc,
the calibration process) reads one of these JSON files and turns it into a
``data/calib_*.npz`` dataset.

Two files, do not confuse them
------------------------------
* **configuration** (this module, ``calibrations/<name>.json``) — the recipe:
  which gestures, how many repetitions, how long each gesture and each rest
  lasts, images to show. Edited in the GUI, reused for future runs.
* **dataset** (``data/calib_*.npz``, produced later by Pc) — the recorded
  result. This module does not touch it.

Where the label vocabulary lives
--------------------------------
There is **no** Python-level ``LABELS`` constant and no global vocabulary. The
labels of a recording are defined *here first* — by the gesture table — and
then frozen into the ``.npz`` by Pc. Concretely, :meth:`CalibrationConfig.labels`
derives them:

* every user gesture, in table order;
* the ``noise`` gesture, **only if** :attr:`CalibrationConfig.noise_enabled`
  (noise is modelled as a gesture so that a ``rest -> noise -> rest`` slot is
  possible);
* ``rest`` last, always — it is the implicit separator the recording inserts
  between two activity slots, and the simulator's fallback when a command
  expires.

So 5 user gestures with noise on → 7 labels: ``[g1..g5, noise, rest]``.
With noise off → 6 labels: ``[g1..g5, rest]``.

JSON schema (``version`` guards compatibility)::

    {
      "version": 1,
      "name": "patient_a",
      "movement_time_s": 3.0,
      "rest_time_s": 2.0,
      "default_repetitions": 5,
      "noise_enabled": true,
      "gestures": [
        {"name": "fist",  "image": "fist.png",  "repetitions": 5},
        {"name": "point", "image": null,         "repetitions": 5}
      ]
    }

``name`` and ``version`` are the only validated scalars; ``gestures`` is the
user's list **without** ``noise`` or ``rest`` (they are derived, never stored
as user rows).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

#: Current on-disk schema version. Bump when the JSON layout changes and teach
#: :meth:`CalibrationConfig.from_dict` to migrate older files.
CONFIG_VERSION = 1

#: The implicit separator inserted between two activity slots, and the
#: simulator's fallback label when a command expires. Never a user gesture.
REST_LABEL = "rest"

#: The optional "the arm moves but does nothing" gesture. Present in the label
#: set only when the configuration enables it.
NOISE_LABEL = "noise"

#: Project root = the parent of the ``p0`` package folder. Configs and images
#: live directly under it (see ``docs/PROJECT_LAYOUT.md``).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Where JSON recipes live, one file per configuration.
CALIBRATIONS_DIR = PROJECT_ROOT / "calibrations"

#: Where gesture preview images live. The config stores the bare filename and
#: the path is resolved against this folder at runtime, so a recipe stays
#: portable across machines.
IMAGES_DIR = PROJECT_ROOT / "images"


@dataclass
class GestureSpec:
    """One active user gesture: its label, its preview image, its repetitions.

    ``image`` is a **bare filename** (e.g. ``"fist.png"``), resolved against
    :data:`IMAGES_DIR`; ``None`` or empty means "no image chosen".
    """

    name: str
    image: str | None = None
    repetitions: int = 5

    def to_dict(self) -> dict:
        return {"name": self.name, "image": self.image, "repetitions": int(self.repetitions)}

    @classmethod
    def from_dict(cls, data: dict) -> "GestureSpec":
        return cls(
            name=str(data.get("name", "")),
            image=(data.get("image") or None),
            repetitions=int(data.get("repetitions", 5)),
        )


@dataclass
class CalibrationConfig:
    """A full recording recipe, edited by the Configure calibration window."""

    name: str = ""
    movement_time_s: float = 3.0
    rest_time_s: float = 2.0
    #: Global default: how many times each gesture is performed. The window
    #: shows it as one field and bulk-applies it to every gesture row; each
    #: row may still carry its own value (the effective count).
    default_repetitions: int = 5
    noise_enabled: bool = False
    gestures: list[GestureSpec] = field(default_factory=list)
    version: int = CONFIG_VERSION

    # --------------------------------------------------------------- derived
    def effective_gestures(self) -> list[GestureSpec]:
        """The user gestures plus the auto ``noise`` row when enabled.

        The gesture table and the label set both go through here, so they can
        never disagree about whether ``noise`` is present.
        """
        result = list(self.gestures)
        if self.noise_enabled and not any(g.name == NOISE_LABEL for g in result):
            result.append(GestureSpec(name=NOISE_LABEL, image=None, repetitions=1))
        return result

    def labels(self) -> list[str]:
        """Ordered label vocabulary this recording will produce in its ``.npz``.

        Every effective gesture, then ``rest`` last (always).
        """
        return [g.name for g in self.effective_gestures()] + [REST_LABEL]

    # ------------------------------------------------------------ validation
    def validate(self) -> list[str]:
        """Return a list of human-readable problems; empty means "can save".

        Blocking problems (must fix before Save): a missing name, a non-positive
        movement or rest time, a non-positive repetition count, an empty gesture
        name, or two gestures sharing a name. A missing image is a **warning**
        only (prefix ``"Warning:"``) so a user can save a recipe before the
        pictures exist.
        """
        problems: list[str] = []
        if not self.name.strip():
            problems.append("The configuration needs a name.")
        if self.movement_time_s <= 0:
            problems.append("Movement time must be greater than 0 seconds.")
        if self.rest_time_s <= 0:
            problems.append("Rest time must be greater than 0 seconds.")
        if self.default_repetitions < 1:
            problems.append("Number of repetitions must be at least 1.")

        seen: set[str] = set()
        for index, gesture in enumerate(self.effective_gestures(), start=1):
            name = gesture.name.strip()
            if not name:
                problems.append(f"Gesture #{index} has no name.")
                continue
            if name in seen:
                problems.append(f"Gesture name '{name}' is used more than once.")
            seen.add(name)
            if gesture.repetitions < 1:
                problems.append(f"Gesture '{name}' needs at least 1 repetition.")
            if gesture.image and not image_exists(gesture.image):
                problems.append(f"Warning: image '{gesture.image}' not found in images/.")
        return problems

    def is_valid(self) -> bool:
        """``True`` when :meth:`validate` reports no *blocking* problem."""
        return not any(not p.startswith("Warning:") for p in self.validate())

    # ------------------------------------------------------------ (de)serialize
    def to_dict(self) -> dict:
        return {
            "version": int(self.version),
            "name": self.name,
            "movement_time_s": float(self.movement_time_s),
            "rest_time_s": float(self.rest_time_s),
            "default_repetitions": int(self.default_repetitions),
            "noise_enabled": bool(self.noise_enabled),
            "gestures": [g.to_dict() for g in self.gestures],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CalibrationConfig":
        version = int(data.get("version", CONFIG_VERSION))
        if version > CONFIG_VERSION:
            raise ValueError(
                f"Configuration version {version} is newer than supported "
                f"({CONFIG_VERSION}). Update the application to open it."
            )
        default_repetitions = int(data.get("default_repetitions", 5))
        return cls(
            name=str(data.get("name", "")),
            movement_time_s=float(data.get("movement_time_s", 3.0)),
            rest_time_s=float(data.get("rest_time_s", 2.0)),
            default_repetitions=default_repetitions,
            noise_enabled=bool(data.get("noise_enabled", False)),
            gestures=[GestureSpec.from_dict(g) for g in data.get("gestures", [])],
            version=version,
        )

    # --------------------------------------------------------------- disk I/O
    def save(self, path: str | Path) -> None:
        """Write the recipe to ``path`` as JSON, creating the folder if needed."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "CalibrationConfig":
        """Read a recipe from a JSON file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)


# ---------------------------------------------------------------- image paths
def image_path(filename: str | None) -> Path | None:
    """Resolve a bare image filename against :data:`IMAGES_DIR`.

    Returns ``None`` for an empty/``None`` filename.
    """
    if not filename:
        return None
    return IMAGES_DIR / filename


def image_exists(filename: str | None) -> bool:
    """``True`` when ``filename`` names a real file under :data:`IMAGES_DIR`."""
    resolved = image_path(filename)
    return resolved is not None and resolved.is_file()


def list_config_files() -> list[Path]:
    """Every ``*.json`` recipe in :data:`CALIBRATIONS_DIR`, sorted by name."""
    if not CALIBRATIONS_DIR.is_dir():
        return []
    return sorted(CALIBRATIONS_DIR.glob("*.json"))


__all__ = [
    "CONFIG_VERSION",
    "REST_LABEL",
    "NOISE_LABEL",
    "PROJECT_ROOT",
    "CALIBRATIONS_DIR",
    "IMAGES_DIR",
    "GestureSpec",
    "CalibrationConfig",
    "image_path",
    "image_exists",
    "list_config_files",
]
