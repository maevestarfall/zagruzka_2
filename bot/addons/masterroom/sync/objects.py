# S3-compatible object storage adapter over raw aiohttp + SigV4 (ЧАСТЬ 18.3).
# Co-authored with CoCo
"""S3-совместимый адаптер крупных файлов (ЧАСТЬ 18.3 §2).

Реализация — прямой S3 REST API через УЖЕ УСТАНОВЛЕННЫЙ ``aiohttp``
с подписью AWS Signature V4: ноль новых зависимостей (бюджет памяти
Render 512MB; прецедент «адаптер на голом aiohttp» — VK-шлюз).
boto3/aioboto3 НЕ тянем.

Правила:

* Конфиг ТОЛЬКО из env ``SYNC_S3_*`` (``bot.config``); без него —
  ``is_configured() == False``, вызывающий код деградирует.
* Secret key нигде не выводится: ни в логи, ни в state, ни в ошибки.
  Ошибки — только классом (:func:`classify_error`).
* В PostgreSQL — только метаданные (object key, размер, checksum);
  сами байты — сюда.
* put/get — СТРИМИНГОВЫЕ (чанками), файл целиком в память не читается
  (правило 18.5 §5 заложено на уровне адаптера).
* Операции: put, head/stat (размер+checksum для read-after-write 18.5),
  get, delete, head_bucket (для «Проверить подключение»).
* ``aiohttp`` импортируется лениво — внутри функций.

Yandex Object Storage / MinIO / AWS: path-style адресация
(``endpoint/bucket/key``) — работает у всех совместимых провайдеров.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 256  # 256 КБ — стриминг без пиков памяти
REQUEST_TIMEOUT_S = 30.0

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_UNSIGNED = "UNSIGNED-PAYLOAD"


# ─── Конфигурация ────────────────────────────────────────────────────────────


def _cfg() -> dict[str, str]:
    from ....config import (
        SYNC_S3_ACCESS_KEY_ID,
        SYNC_S3_BUCKET,
        SYNC_S3_ENDPOINT,
        SYNC_S3_REGION,
        SYNC_S3_SECRET_ACCESS_KEY,
    )

    return {
        "endpoint": SYNC_S3_ENDPOINT,
        "bucket": SYNC_S3_BUCKET,
        "access_key": SYNC_S3_ACCESS_KEY_ID,
        "secret_key": SYNC_S3_SECRET_ACCESS_KEY,
        "region": SYNC_S3_REGION or "us-east-1",
    }


def is_configured() -> bool:
    c = _cfg()
    return bool(c["endpoint"] and c["bucket"] and c["access_key"]
                and c["secret_key"])


def bucket_label() -> str:
    """Имя bucket для UI/предпросмотра (18.5 §1) — БЕЗ ключей/endpoint."""
    return _cfg()["bucket"]


def classify_error(exc: BaseException) -> str:
    """Безопасный класс ошибки S3 — без endpoint/ключей."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "таймаут запроса к S3"
    text = str(exc).lower()
    if "ssl" in text or "certificate" in text:
        return "ошибка SSL при подключении к S3"
    if isinstance(exc, (ConnectionError, OSError)):
        return "S3: сеть/хост недоступны"
    return f"ошибка S3 ({type(exc).__name__})"


class S3Error(RuntimeError):
    """HTTP-ошибка S3 с безопасным текстом (без ключей/URL)."""

    def __init__(self, status: int, operation: str):
        self.status = status
        self.operation = operation
        if status in (401, 403):
            msg = f"S3: нет доступа (HTTP {status})"
        elif status == 404:
            msg = f"S3: объект/bucket не найден (HTTP 404)"
        else:
            msg = f"S3: ошибка {operation} (HTTP {status})"
        super().__init__(msg)


# ─── SigV4 ───────────────────────────────────────────────────────────────────


def _uri_encode(s: str, *, encode_slash: bool = True) -> str:
    safe = "" if encode_slash else "/"
    return quote(s, safe=safe + "-_.~")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign_v4(
    method: str,
    url: str,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    headers: dict[str, str] | None = None,
    payload_sha256: str = _EMPTY_SHA256,
    now: datetime | None = None,
) -> dict[str, str]:
    """Вернуть заголовки запроса с подписью AWS SigV4 (service=s3).

    Secret key участвует только в HMAC-цепочке и не попадает в
    возвращаемые заголовки.
    """
    parsed = urlparse(url)
    host = parsed.netloc
    canonical_uri = _uri_encode(parsed.path or "/", encode_slash=False)
    # query: сортировка по ключу; значения уже URL-encoded в url
    query_items: list[tuple[str, str]] = []
    if parsed.query:
        for part in parsed.query.split("&"):
            k, _, v = part.partition("=")
            query_items.append((k, v))
    canonical_query = "&".join(
        f"{k}={v}" for k, v in sorted(query_items)
    )

    t = now or datetime.now(timezone.utc)
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")

    all_headers = {k.lower(): v.strip() for k, v in (headers or {}).items()}
    all_headers["host"] = host
    all_headers["x-amz-date"] = amz_date
    all_headers["x-amz-content-sha256"] = payload_sha256

    signed_names = sorted(all_headers)
    canonical_headers = "".join(f"{k}:{all_headers[k]}\n" for k in signed_names)
    signed_headers = ";".join(signed_names)

    canonical_request = "\n".join([
        method.upper(),
        canonical_uri,
        canonical_query,
        canonical_headers,
        signed_headers,
        payload_sha256,
    ])
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, "s3")
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    out = {k: v for k, v in all_headers.items() if k != "host"}
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return out


# ─── Низкоуровневый запрос ───────────────────────────────────────────────────


def _object_url(key: str) -> str:
    c = _cfg()
    endpoint = c["endpoint"].rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint
    key_enc = _uri_encode(key.lstrip("/"), encode_slash=False)
    return f"{endpoint}/{_uri_encode(c['bucket'])}/{key_enc}"


def _bucket_url() -> str:
    c = _cfg()
    endpoint = c["endpoint"].rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint
    return f"{endpoint}/{_uri_encode(c['bucket'])}"


def _signed_headers(
    method: str, url: str, extra: dict[str, str] | None = None,
    payload_sha256: str = _EMPTY_SHA256,
) -> dict[str, str]:
    c = _cfg()
    return sign_v4(
        method, url,
        access_key=c["access_key"], secret_key=c["secret_key"],
        region=c["region"], headers=extra, payload_sha256=payload_sha256,
    )


# ─── Операции ────────────────────────────────────────────────────────────────


async def _file_chunks(path: str) -> AsyncIterator[bytes]:
    """Читать файл чанками через to_thread — event loop не блокируется."""
    def _open():
        return open(path, "rb")  # noqa: SIM115 — закрываем вручную ниже

    f = await asyncio.to_thread(_open)
    try:
        while True:
            chunk = await asyncio.to_thread(f.read, CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    finally:
        await asyncio.to_thread(f.close)


async def put_file(key: str, path: str) -> dict[str, Any]:
    """Стриминговый PUT файла. Возвращает ``{key, byte_size, checksum}``.

    checksum — SHA-256 (hex) содержимого, посчитан на лету во время
    стриминга (файл в память целиком не читается).
    """
    if not is_configured():
        raise RuntimeError("S3 не настроено")
    import aiohttp  # ленивый импорт

    size = await asyncio.to_thread(os.path.getsize, path)
    hasher = hashlib.sha256()

    async def _gen() -> AsyncIterator[bytes]:
        async for chunk in _file_chunks(path):
            hasher.update(chunk)
            yield chunk

    url = _object_url(key)
    headers = _signed_headers(
        "PUT", url,
        extra={"content-length": str(size)},
        payload_sha256=_UNSIGNED,
    )
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=REQUEST_TIMEOUT_S,
                                    sock_read=REQUEST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.put(url, data=_gen(), headers=headers) as resp:
            if resp.status not in (200, 201):
                raise S3Error(resp.status, "put")
    return {"key": key, "byte_size": size, "checksum": hasher.hexdigest()}


async def put_bytes(key: str, data: bytes) -> dict[str, Any]:
    """PUT байтов из памяти. Возвращает ``{key, byte_size, checksum}``.

    Для НЕБОЛЬШИХ payload'ов (ciphertext секрет-файлов 18.5 §3.4 —
    envelope уже целиком в памяти после шифрования). Крупные обычные
    файлы идут через стриминговый :func:`put_file`. Payload подписывается
    настоящим SHA-256 (не UNSIGNED) — целостность проверяет сам S3.
    """
    if not is_configured():
        raise RuntimeError("S3 не настроено")
    import aiohttp  # ленивый импорт

    sha = hashlib.sha256(data).hexdigest()
    url = _object_url(key)
    headers = _signed_headers(
        "PUT", url,
        extra={"content-length": str(len(data))},
        payload_sha256=sha,
    )
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=REQUEST_TIMEOUT_S,
                                    sock_read=REQUEST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.put(url, data=data, headers=headers) as resp:
            if resp.status not in (200, 201):
                raise S3Error(resp.status, "put")
    return {"key": key, "byte_size": len(data), "checksum": sha}


async def head_object(key: str) -> dict[str, Any] | None:
    """HEAD объекта: ``{key, byte_size, etag}`` или ``None`` (404)."""
    if not is_configured():
        raise RuntimeError("S3 не настроено")
    import aiohttp

    url = _object_url(key)
    headers = _signed_headers("HEAD", url)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.head(url, headers=headers) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                raise S3Error(resp.status, "head")
            return {
                "key": key,
                "byte_size": int(resp.headers.get("Content-Length", 0) or 0),
                "etag": (resp.headers.get("ETag") or "").strip('"'),
            }


async def get_file(key: str, dest_path: str) -> dict[str, Any]:
    """Стриминговый GET в файл. Возвращает ``{key, byte_size, checksum}``."""
    if not is_configured():
        raise RuntimeError("S3 не настроено")
    import aiohttp

    url = _object_url(key)
    headers = _signed_headers("GET", url)
    hasher = hashlib.sha256()
    size = 0
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=REQUEST_TIMEOUT_S,
                                    sock_read=REQUEST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise S3Error(resp.status, "get")
            f = await asyncio.to_thread(open, dest_path, "wb")
            try:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    hasher.update(chunk)
                    size += len(chunk)
                    await asyncio.to_thread(f.write, chunk)
            finally:
                await asyncio.to_thread(f.close)
    return {"key": key, "byte_size": size, "checksum": hasher.hexdigest()}


async def delete_object(key: str) -> bool:
    """DELETE объекта. True — удалён/не существовал."""
    if not is_configured():
        raise RuntimeError("S3 не настроено")
    import aiohttp

    url = _object_url(key)
    headers = _signed_headers("DELETE", url)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.delete(url, headers=headers) as resp:
            if resp.status in (200, 204, 404):
                return True
            raise S3Error(resp.status, "delete")


async def head_bucket() -> bool:
    """HEAD bucket — доступность S3 для «Проверить подключение»."""
    if not is_configured():
        raise RuntimeError("S3 не настроено")
    import aiohttp

    url = _bucket_url()
    headers = _signed_headers("HEAD", url)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.head(url, headers=headers) as resp:
            if resp.status == 200:
                return True
            raise S3Error(resp.status, "head_bucket")
