"""Проверка утечек соединений SQLite: запускает все тесты и сообщает, какие соединения остались незакрытыми.

Запуск:  python tests/leak_check.py     (ожидаемо: НЕ закрыто: 0). Имя файла не test_*.py — обычный прогон его не запускает.
"""
import collections
import os
import sqlite3
import sys
import traceback
import unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path[:0] = [os.path.join(ROOT, "src"), os.path.join(ROOT, "tests")]
real_connect = sqlite3.connect
opened = []
class Tracked(sqlite3.Connection):
    def close(self):
        self._jura_closed = True
        super().close()
def connect(*a, **kw):
    kw.setdefault("factory", Tracked)
    c = real_connect(*a, **kw)
    c._jura_closed = False
    c._jura_where = [f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in traceback.extract_stack()[:-1]
                     if "jura" in f.filename or "tests" in f.filename][-3:]
    opened.append(c)
    return c
sqlite3.connect = connect
suite = unittest.defaultTestLoader.discover("tests")
result = unittest.TextTestRunner(stream=open(os.devnull, "w")).run(suite)
leaks = collections.Counter(" <- ".join(reversed(c._jura_where)) for c in opened if not c._jura_closed)
print(f"тестов: {result.testsRun}, провалов: {len(result.failures)+len(result.errors)}; соединений открыто: {len(opened)}, НЕ закрыто: {sum(leaks.values())}")
for where, n in leaks.most_common():
    print(f"  {n:3d} × {where}")
sys.exit(1 if leaks or result.failures or result.errors else 0)
