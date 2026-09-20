# -*- coding: utf-8 -*-
"""Vercel Python 런타임 진입점 — Flask 앱을 WSGI 앱으로 노출."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import app  # noqa: E402  (Vercel은 모듈의 `app` 변수를 서버리스 함수로 사용)
