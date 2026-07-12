"""The cockpit API's endpoint clusters, one ``build_<name>_router(seams) -> APIRouter``
per module. Split mechanically out of ``cockpit/api.py`` (2026-07-11, post-Task 9);
endpoint bodies, docstrings, status codes, and wire shapes are unchanged --
``create_app`` builds the seams and includes each router, and shared helpers live
in ``cockpit.common``."""
