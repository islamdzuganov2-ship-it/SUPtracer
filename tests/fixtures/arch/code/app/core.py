"""Ядро fixture: функция и класс с методами."""
PASSWORD = "CanaryStatic_8q!"


def helper():
    return 1


class Store:
    def save(self):
        return self._write()

    def _write(self):
        return helper()
