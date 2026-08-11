"""Vercel のエントリポイント。

Vercel は `api/` の下の Python ファイルを関数として拾う。ここでは既存の
FastAPI をそのまま公開するだけで、アプリ本体には何も足さない。

**ここに載るのはデモ**であることに注意。Vercel はサーバーレスで
ファイルシステムが揮発するため:

- 溜めたアイデアも育った重みもコールドスタートで消える
- 常駐ワーカーが動かないので「裏で考え続ける」部分は無い

本来の姿で動かすには、永続ボリュームと常駐プロセスが要る（README を参照）。
"""

import sys
from pathlib import Path

# 関数のバンドルからは、リポジトリ直下がそのまま見えるとは限らない。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ideaman.app import app  # noqa: E402

__all__ = ["app"]
