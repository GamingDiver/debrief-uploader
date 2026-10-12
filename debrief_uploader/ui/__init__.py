"""The uploader's windows, one module per window (see core.py for the
shared pieces). Everything is re-exported here so callers keep using
`ui.open_review(...)`, `ui._res(...)` and so on."""

from .core import (  # noqa: F401
    BG, PANEL, LINE, INK,
    MUTED, AMBER, AMBER_INK, VISIBILITY,
    TRAINING_VISIBILITY, _thread, _SCALE, _poll,
    _px, _root, _toplevel, _set_title,
    _dress, _res, _label, _button,
    _section, _ThinScroll, _gutter, _Body,
    _text_body, _text_window, _set_size, _work_area,
    _place, _clock, _span)
from .status import (  # noqa: F401
    open_status, open_doctor, _version)
from .signin import (  # noqa: F401
    open_sign_in)
from .review import (  # noqa: F401
    _vs_battle, _load_preview, _ScaledThumbs, _view_image,
    _shot_candidates, _pick_shots, _REVIEW, _REVIEW_LOCK,
    _review_title, open_review, _review_window)
from .settings import (  # noqa: F401
    open_settings)
from . import core  # noqa: F401  (tests swap core._root etc.)
