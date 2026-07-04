"""
Трекер бюджета на вызовы Yandex AI Studio API. Считает фактически
потраченное по usage из ответов API (в рублях, по ориентировочной ставке
из config.py) и не даёт пайплайну сделать следующий вызов, если он может
превысить заданный лимит.

Это МЯГКИЙ предохранитель внутри скрипта — он останавливает обработку
аккуратно (пишет то, что уже готово, и завершается). Дополнительно стоит
следить за расходом в консоли Yandex Cloud (Billing) — там же можно
настроить алерты по бюджету на уровне облака в целом.
"""
from __future__ import annotations

import logging
import threading

import config

logger = logging.getLogger("budget")


class BudgetExceededError(Exception):
    """Поднимается, когда следующий вызов LLM может превысить лимит бюджета."""


class BudgetTracker:
    def __init__(self, max_rub: float):
        self.max_rub = max_rub
        self.spent_rub = 0.0
        self.calls_made = 0
        # При параллельной обработке (--num-workers > 1) несколько потоков
        # могут одновременно проверять и обновлять бюджет — без блокировки
        # это гонка данных, которая может пропустить превышение лимита.
        self._lock = threading.Lock()

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        total_tokens = input_tokens + output_tokens
        return total_tokens / 1000 * config.LLM_PRICE_PER_1K_TOKENS_RUB

    def check_before_call(self, estimated_input_tokens: int, estimated_output_tokens: int) -> None:
        """
        Консервативная оценка ДО вызова: берём худший случай по output
        (config.MAX_LLM_OUTPUT_TOKENS), чтобы не проскочить лимит из-за
        того, что реальный ответ окажется длиннее ожидаемого.
        """
        estimated_cost = self._cost(estimated_input_tokens, estimated_output_tokens)
        with self._lock:
            if self.spent_rub + estimated_cost > self.max_rub:
                raise BudgetExceededError(
                    f"Остановка по бюджету: уже потрачено {self.spent_rub:.2f}₽, "
                    f"следующий вызов оценочно добавит ~{estimated_cost:.2f}₽, "
                    f"лимит {self.max_rub:.2f}₽. Обработано вызовов: {self.calls_made}."
                )

    def record_actual(self, input_tokens: int, output_tokens: int) -> None:
        cost = self._cost(input_tokens, output_tokens)
        with self._lock:
            self.spent_rub += cost
            self.calls_made += 1
            logger.info(
                "LLM-вызов #%d: +%.2f₽ (in=%d, out=%d) | всего потрачено %.2f₽ из %.2f₽",
                self.calls_made, cost, input_tokens, output_tokens, self.spent_rub, self.max_rub,
            )
