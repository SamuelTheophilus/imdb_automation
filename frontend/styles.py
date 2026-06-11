from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_folder = str(Path(__file__).parent.parent / "core/ui")
_env = Environment(loader=FileSystemLoader(_folder))
STYLES: str = _env.get_template("app_styling.j2").render()
