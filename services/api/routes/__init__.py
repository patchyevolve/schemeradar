"""HTTP route modules for the SchemeRadar API Gateway (ARCHITECTURE §5.2).

Each module owns exactly one router and is mounted in ``services/api/main.py``.
Route *behaviour* is built in BUILD_ORDER Step 4; Step 3 only fixes the
contract so OpenAPI can be consumed today.
"""
