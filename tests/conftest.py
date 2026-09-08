"""Keep pytest from importing the integration package.

``__init__.py`` needs a Home Assistant runtime; ``logic.py`` does not. The
tests import ``logic`` directly by path.
"""

collect_ignore = ["../__init__.py"]
