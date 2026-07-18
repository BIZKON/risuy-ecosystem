"""dev-only: кладёт ОДНО событие в Redis Stream engine:raw и выходит. Для walking-skeleton."""
from __future__ import annotations

import os

import redis


def main() -> None:
    r = redis.from_url(os.environ["REDIS_URL"])
    # raw_messages — shared, без tenant_id (глобальное сырьё).
    r.xadd("engine:raw", {
        "source_kind": "telegram",
        "external_id": "stub-1",
        "text": "ищу подрядчика на ремонт офиса",
    })
    print("stub-producer: одно событие отправлено в engine:raw")


if __name__ == "__main__":
    main()
