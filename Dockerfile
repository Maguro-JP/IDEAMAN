# 本番用。Web とワーカーを1つのプロセスで動かす。
#
# SQLite を使う以上、Web とワーカーは同じディスクを見ている必要がある。
# 別マシンに分けると DB が共有されないので、1台に載せるのが正しい形。

FROM python:3.12-slim

WORKDIR /app

# 依存だけ先に入れてレイヤーキャッシュを効かせる
COPY pyproject.toml README.md ./
COPY ideaman ./ideaman
RUN pip install --no-cache-dir .

# 永続ディスクの置き場所。ホスティング側でここにボリュームを割り当てる。
ENV IDEAMAN_DB=/data/ideaman.db \
    IDEAMAN_EMBED_WORKER=1 \
    PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn ideaman.app:app --host 0.0.0.0 --port ${PORT}"]
