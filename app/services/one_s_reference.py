"""Кэш справочников 1С на время одного прогона синхронизации.

Справочники маленькие (55 организаций, 133 вида расходов, 272 склада), поэтому
они читаются заново на каждый прогон: процессный кэш с временем жизни давал бы
расхождения, которые невозможно воспроизвести.

Общее правило при неоднозначности: код не угадываем. Лучше отказаться от
серверного фильтра и отобрать документы локально, чем тихо потерять их.
"""

import logging
from collections import defaultdict
from typing import Callable, Iterable, TypeVar

from app.clients.http_json import HttpClientError
from app.clients.one_s import OneSClient
from app.utils.normalization import clean_text

logger = logging.getLogger(__name__)

RowT = TypeVar("RowT")


def _build_index(
    rows: Iterable[RowT],
    key: Callable[[RowT], str | None],
    value: Callable[[RowT], str | None],
) -> dict[str, set[str]]:
    index: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        name = clean_text(key(row))
        resolved = value(row)
        if name and resolved:
            index[name].add(resolved)
    return dict(index)


def _single(index: dict[str, set[str]], name: str | None, what: str) -> str | None:
    """Возвращает значение только при однозначном совпадении."""
    key = clean_text(name)
    if not key:
        return None
    values = index.get(key)
    if not values:
        logger.info("%s %r не найден в справочнике 1С.", what, name)
        return None
    if len(values) > 1:
        logger.warning(
            "%s %r соответствует нескольким значениям в 1С: %s. Отбор выполняется локально.",
            what,
            name,
            ", ".join(sorted(values)),
        )
        return None
    return next(iter(values))


class OneSReferenceCache:
    """Справочники грузятся лениво и не больше одного раза за прогон.

    Отказ запоминается наравне с успехом. Иначе при недоступной 1С каждый
    документ дёргал справочник заново: на контрольном прогоне (57 документов,
    две попытки, таймаут 5 с) это растянуло отказ на четверть часа вместо
    нескольких секунд. Ошибка при этом не глохнет, а поднимается снова —
    документ должен стать техническим сбоем, а не тихо уйти в ручную обработку.
    """

    def __init__(self, client: OneSClient):
        self._client = client
        self._organizations: dict[str, set[str]] | None = None
        self._expense_types: dict[str, set[str]] | None = None
        self._warehouse_groups: dict[str, set[str]] | None = None
        self._failures: dict[str, HttpClientError] = {}

    def organization_code(self, name: str | None) -> str | None:
        """Коды организаций в 1С не уникальны, поэтому ответ бывает пустым."""
        if self._organizations is None:
            self._organizations = self._load(
                "организаций",
                lambda: _build_index(
                    self._client.list_organizations(), lambda row: row.name, lambda row: row.code
                ),
            )
        return _single(self._organizations, name, "Организация")

    def expense_type_code(self, name: str | None) -> str | None:
        if self._expense_types is None:
            self._expense_types = self._load(
                "видов доп. расходов",
                lambda: _build_index(
                    self._client.list_additional_expense_types(),
                    lambda row: row.name,
                    lambda row: row.code,
                ),
            )
        return _single(self._expense_types, name, "Вид доп. расхода")

    def warehouse_group(self, warehouse_name: str | None) -> str | None:
        """Единственный способ узнать группу склада: в документах её нет."""
        if self._warehouse_groups is None:
            self._warehouse_groups = self._load(
                "складов",
                lambda: _build_index(
                    self._client.list_warehouses(), lambda row: row.name, lambda row: row.group_name
                ),
            )
        return _single(self._warehouse_groups, warehouse_name, "Склад")

    def _load(self, what: str, build: Callable[[], dict[str, set[str]]]) -> dict[str, set[str]]:
        cached = self._failures.get(what)
        if cached is not None:
            raise cached
        try:
            return build()
        except HttpClientError as error:
            logger.error("Не удалось прочитать справочник %s: %s", what, error)
            self._failures[what] = error
            raise
