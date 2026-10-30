"""Producer implementations live as one file per producer alongside this
``__init__``. The dispatcher (:mod:`km.loader`) loads them by name at
runtime — first looking under ``<project>/<directories.producers>/`` and
then in this directory — so there is no central registry to maintain.

See :mod:`km.producers.contract` for the :class:`Producer` ABC every
implementation subclasses.
"""
