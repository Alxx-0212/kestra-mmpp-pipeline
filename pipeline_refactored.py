"""Backward-compatible wrapper around the split FinPay pipeline package.

Implementation now lives under ``finpay_pipeline/``. Keep this module so older
notebooks/tests that import ``pipeline_refactored`` continue to work.
"""
from finpay_pipeline import *  # noqa: F401,F403
