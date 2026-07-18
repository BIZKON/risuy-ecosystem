"""Dev-стаб: одно событие envelope v1 в engine:raw через BaseWorker.

Живёт как проверка каркаса + топливо make skeleton (профиль skeleton compose).
Повторный прогон даёт тот же external_id → дубль пропускается консьюмером (ожидаемо).
"""
import asyncio
import logging

from engine.common import base_worker, config, envelope


class StubProducer(base_worker.BaseWorker):
    async def collect_once(self) -> list[dict]:
        self.stop.set()  # одна итерация — и graceful-выход
        event = envelope.build(
            "telegram",
            envelope.make_external_id("telegram", 999000111, 1),
            "ищу подрядчика на ремонт офиса, бюджет 500к, срочно",
            chat_ref="https://t.me/example_chat",
            author_ref="tg:100500",
            lang="ru",
            metadata={"стаб": True},
        )
        logging.getLogger(__name__).info("Стаб: эмитим %s", event["external_id"])
        return [event]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(StubProducer(config.req("REDIS_URL")).run())


if __name__ == "__main__":
    main()
