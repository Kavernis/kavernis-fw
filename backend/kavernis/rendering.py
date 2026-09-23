"""Package-owned Jinja2 template rendering."""

from collections.abc import Mapping

from jinja2 import Environment, PackageLoader, StrictUndefined

_environment = Environment(
    loader=PackageLoader("kavernis", "templates"),
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
)


def render_template(template_name: str, context: Mapping[str, object]) -> str:
    """Render a template packaged with Kavernis using resolved values only."""
    return _environment.get_template(template_name).render(**context)
