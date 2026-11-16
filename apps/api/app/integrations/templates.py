"""Load the built-in Next.js and Vite + React starter templates."""

from pathlib import Path

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "integrations-templates"
TEMPLATE_NAMES = ("nextjs", "vite-react")


def load_template(name: str) -> dict[str, str]:
    """Return a safe relative-path to text-content mapping for a known starter."""
    if name not in TEMPLATE_NAMES:
        raise ValueError("Unknown application template.")
    root = TEMPLATE_ROOT / name
    files: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or relative.startswith(".") or "." in relative.split("/"):
                raise ValueError("Template contains an unsafe path.")
            files[relative] = path.read_text(encoding="utf-8")
    if not files:
        raise LookupError("Application template is empty.")
    return files
