"""Канарейка на приватный контракт pysaby.

Библиотека не отдаёт нужный нам метод реестра, поэтому запрос собирается вручную
и отправляется через её приватные методы. Обновление pysaby должно ломаться
здесь, на тестах, а не в 17:00 на ночном прогоне.

Если тест упал — не удаляйте его. Посмотрите, что изменилось в библиотеке, и
почините `SabyClient._send_raw_payload` либо зафиксируйте версию в
requirements.txt.
"""

import inspect
import unittest

from tests.support.no_network import NetworkGuardTestCase

#: Приватные методы SABYManager, на которых держится клиент.
REQUIRED_METHODS = {
    # сессия из SQLite рядом с библиотекой, без неё нет авторизации
    "_get_sid": (),
    # повторная авторизация после 401
    "_auth": (),
    # единственный способ отправить произвольный метод СБИС
    "_send_json_request": ("url", "payload", "headers"),
    # создаёт таблицу токенов: нужен, потому что файл сессии мы уводим из
    # site-packages, а конструктор создаёт таблицу по своему пути
    "_init_db": (),
}


class PysabyContractTests(NetworkGuardTestCase):
    def setUp(self):
        super().setUp()
        try:
            from pysaby import SABYManager
        except ImportError:  # pragma: no cover — библиотека не установлена
            self.skipTest("pysaby не установлен")
        self.manager_class = SABYManager

    def test_private_methods_are_still_there(self):
        missing = [name for name in REQUIRED_METHODS if not hasattr(self.manager_class, name)]

        self.assertEqual([], missing, "pysaby больше не отдаёт эти методы")

    def test_send_json_request_signature_is_unchanged(self):
        """Порядок аргументов важен: клиент зовёт их позиционно."""
        parameters = list(
            inspect.signature(self.manager_class._send_json_request).parameters
        )

        self.assertEqual(["self", *REQUIRED_METHODS["_send_json_request"]], parameters)

    def test_constructor_still_takes_login_and_password(self):
        parameters = inspect.signature(self.manager_class.__init__).parameters

        self.assertIn("login", parameters)
        self.assertIn("password", parameters)

    def test_session_file_path_is_an_instance_attribute(self):
        """На этом держится увод файла сессии из site-packages."""
        import inspect

        source = inspect.getsource(self.manager_class.__init__)
        self.assertIn("self.db_file", source)

    def test_manager_exposes_base_url_and_headers(self):
        """Клиент берёт их у менеджера, а не задаёт сам."""
        for name in ("base_url", "headers"):
            with self.subTest(attribute=name):
                self.assertTrue(
                    hasattr(self.manager_class, name)
                    or name in inspect.getsource(self.manager_class.__init__),
                    f"pysaby больше не предоставляет {name}",
                )


if __name__ == "__main__":
    unittest.main()
