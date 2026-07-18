"""dev-only: кладёт ОДНО событие в Redis Stream engine:raw и выходит. Для walking-skeleton."""
from __future__ import annotations

import os

import redis


def main() -> None:
    r = redis.from_url(os.environ["REDIS_URL"])
    tenant_id = os.environ["ENGINE_STUB_TENANT_ID"]
    r.xadd("engine:raw", {
        "tenant_id": tenant_id,
        "source_kind": "telegram",
        "external_id": "stub-1",
        "text": "ищу подрядчика на ремонт офиса",
    })
    print("stub-producer: одно событие отправлено в engine:raw")


if __name__ == "__main__":
    main()
