"""CompareLive shared code subpackage.

Sublime Text loads each root-level .py file as an independent plugin,
and root-level plugin modules must not import each other, so shared
modules live in this subpackage. This subpackage exposes no public
API; import modules explicitly, e.g. ``from .lib import diff_engine``.
"""
