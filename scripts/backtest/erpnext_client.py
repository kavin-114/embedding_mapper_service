"""Re-export ERPNextClient from the shared connectors module.

Kept for backward compatibility with existing backtest scripts.
"""

from app.services.connectors.erpnext import ERPNextClient  # noqa: F401
