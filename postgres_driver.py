"""
Драйвер для работы с базой данных PostgreSQL

Этот модуль предоставляет класс PostgreSQLDriver для выполнения CRUD операций
с базой данных PostgreSQL. Использует переменные окружения из .env файла
для подключения к базе данных.

Пример использования:
    from postgres_driver import PostgreSQLDriver
    
    # Создание экземпляра драйвера
    db = PostgreSQLDriver()
    
    # Использование контекстного менеджера
    with db:
        # Создание записи
        user_id = db.create('users', {'name': 'Иван', 'email': 'ivan@example.com'})
        
        # Получение всех записей
        users = db.get_all('users')
        
        # Получение записи по ID
        user = db.get_by_id('users', user_id)
        
        # Обновление записи
        db.update('users', user_id, {'name': 'Иван Иванов'})
        
        # Удаление записи
        db.delete('users', user_id)
"""

import os
import psycopg2
from dataclasses import fields as dataclass_fields
from typing import get_args, get_origin, get_type_hints
from psycopg2 import OperationalError, DatabaseError, IntegrityError
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import connection as PGConnection, cursor as PGCursor
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any, Tuple


class PostgreSQLDriver:
    """
    Драйвер для работы с PostgreSQL базой данных
    
    Предоставляет CRUD методы для работы с таблицами базы данных.
    Использует переменные окружения из .env файла для подключения.
    """
    
    def __init__(self, env_file: Optional[str] = '.env'):
        """
        Инициализация драйвера
        
        Args:
            env_file: Путь к файлу .env (по умолчанию '.env')
        """
        self._env_path: Optional[str] = None
        self._env_loaded = False
        if env_file:
            if os.path.isabs(env_file):
                env_path = env_file
            else:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                env_path = os.path.join(base_dir, env_file)
            self._env_path = env_path
            self._env_loaded = load_dotenv(env_path)
            if not os.path.exists(env_path):
                print(f"[postgres_driver] .env not found: {env_path}")
            else:
                status = "loaded" if self._env_loaded else "not loaded"
                print(f"[postgres_driver] .env {status}: {env_path}")
        self.connection: Optional[PGConnection] = None
        self.cursor: Optional[PGCursor] = None
        self._connection_params = {
            'host': os.getenv('DB_HOST', 'localhost'),
            'port': int(os.getenv('DB_PORT', 5432)),
            'database': os.getenv('DB_NAME', 'booking'),
            'user': os.getenv('DB_USER', 'booking_admin'),
            'password': os.getenv('DB_PASSWORD', '123456')
        }
    
    def connect(self) -> bool:
        """
        Подключение к базе данных
        
        Returns:
            True если подключение успешно, False в противном случае
        """
        try:
            self.connection = psycopg2.connect(**self._connection_params)
            self.cursor = self.connection.cursor(cursor_factory=RealDictCursor)
            return True
        except OperationalError as e:
            raise ConnectionError(f"Ошибка подключения к PostgreSQL: {e}")
    
    def disconnect(self):
        """Закрытие соединения с базой данных"""
        if self.cursor:
            self.cursor.close()
            self.cursor = None
        if self.connection:
            self.connection.close()
            self.connection = None
    
    def __enter__(self):
        """Контекстный менеджер: вход"""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Контекстный менеджер: выход"""
        if self.connection:
            if exc_type:
                self.connection.rollback()
            else:
                self.connection.commit()
        self.disconnect()
        return False
    
    def _ensure_connection(self):
        """Проверка наличия активного соединения"""
        if not self.connection or self.connection.closed:
            self.connect()

    def _quote_ident(self, name: str) -> str:
        """Безопасно экранировать имя таблицы или колонки."""
        parts = name.split(".")
        escaped = ['"' + part.replace('"', '""') + '"' for part in parts]
        return ".".join(escaped)

    def _require_cursor(self) -> Tuple[PGConnection, PGCursor]:
        """Гарантировать наличие активного соединения и курсора."""
        self._ensure_connection()
        assert self.connection is not None
        assert self.cursor is not None
        return self.connection, self.cursor
    
    # ==================== CREATE ====================
    
    def create(self, table: str, data: Dict[str, Any], return_id: bool = True) -> Optional[int]:
        """
        Создание новой записи в таблице
        
        Args:
            table: Имя таблицы
            data: Словарь с данными для вставки (ключи - названия колонок)
            return_id: Возвращать ли ID созданной записи (требует колонку 'id')
        
        Returns:
            ID созданной записи или None
        
        Example:
            user_id = db.create('users', {'name': 'Иван', 'email': 'ivan@example.com'})
        """
        self._ensure_connection()
        
        if not data:
            raise ValueError("Данные для вставки не могут быть пустыми")
        
        columns = ', '.join(self._quote_ident(column) for column in data.keys())
        placeholders = ', '.join(['%s'] * len(data))
        values = list(data.values())
        table_name = self._quote_ident(table)
        
        if return_id:
            query = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders}) RETURNING id"
        else:
            query = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"
        
        try:
            connection, cursor = self._require_cursor()
            cursor.execute(query, values)
            if return_id:
                result = cursor.fetchone()
                connection.commit()
                result_dict = dict(result) if result else None
                return result_dict.get("id") if result_dict else None
            else:
                connection.commit()
                return None
        except IntegrityError as e:
            if self.connection:
                self.connection.rollback()
            raise ValueError(f"Ошибка целостности данных: {e}")
        except DatabaseError as e:
            if self.connection:
                self.connection.rollback()
            raise DatabaseError(f"Ошибка базы данных: {e}")
    
    # ==================== READ ====================
    
    def get_all(self, table: str, order_by: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Получение всех записей из таблицы
        
        Args:
            table: Имя таблицы
            order_by: Колонка для сортировки (например, 'id DESC')
            limit: Максимальное количество записей
        
        Returns:
            Список словарей с данными записей
        
        Example:
            users = db.get_all('users', order_by='id DESC', limit=10)
        """
        self._ensure_connection()
        
        query = f"SELECT * FROM {self._quote_ident(table)}"
        
        if order_by:
            query += f" ORDER BY {order_by}"
        
        if limit:
            query += f" LIMIT {limit}"
        
        try:
            _, cursor = self._require_cursor()
            cursor.execute(query)
            results = cursor.fetchall()
            return [dict(row) for row in results]
        except DatabaseError as e:
            raise DatabaseError(f"Ошибка при получении данных: {e}")
    
    def get_by_id(self, table: str, record_id: int) -> Optional[Dict[str, Any]]:
        """
        Получение записи по ID
        
        Args:
            table: Имя таблицы
            record_id: ID записи
        
        Returns:
            Словарь с данными записи или None, если запись не найдена
        
        Example:
            user = db.get_by_id('users', 1)
        """
        self._ensure_connection()
        
        query = f"SELECT * FROM {self._quote_ident(table)} WHERE {self._quote_ident('id')} = %s"
        
        try:
            _, cursor = self._require_cursor()
            cursor.execute(query, (record_id,))
            result = cursor.fetchone()
            return dict(result) if result else None
        except DatabaseError as e:
            raise DatabaseError(f"Ошибка при получении записи: {e}")
    
    def find(self, table: str, conditions: Dict[str, Any], 
             order_by: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Поиск записей по условиям
        
        Args:
            table: Имя таблицы
            conditions: Словарь с условиями поиска (ключ - колонка, значение - искомое значение)
            order_by: Колонка для сортировки
            limit: Максимальное количество записей
        
        Returns:
            Список словарей с данными записей
        
        Example:
            users = db.find('users', {'email': 'ivan@example.com'})
        """
        self._ensure_connection()
        
        if not conditions:
            return self.get_all(table, order_by, limit)
        
        where_clauses = []
        values = []
        
        for column, value in conditions.items():
            where_clauses.append(f"{self._quote_ident(column)} = %s")
            values.append(value)
        
        query = f"SELECT * FROM {self._quote_ident(table)} WHERE {' AND '.join(where_clauses)}"
        
        if order_by:
            query += f" ORDER BY {order_by}"
        
        if limit:
            query += f" LIMIT {limit}"
        
        try:
            _, cursor = self._require_cursor()
            cursor.execute(query, values)
            results = cursor.fetchall()
            return [dict(row) for row in results]
        except DatabaseError as e:
            raise DatabaseError(f"Ошибка при поиске записей: {e}")
    
    # ==================== UPDATE ====================
    
    def update(self, table: str, record_id: int, data: Dict[str, Any]) -> bool:
        """
        Обновление записи по ID
        
        Args:
            table: Имя таблицы
            record_id: ID записи для обновления
            data: Словарь с данными для обновления (ключи - названия колонок)
        
        Returns:
            True если запись обновлена, False если запись не найдена
        
        Example:
            db.update('users', 1, {'name': 'Иван Иванов', 'email': 'newemail@example.com'})
        """
        self._ensure_connection()
        
        if not data:
            raise ValueError("Данные для обновления не могут быть пустыми")
        
        set_clauses = []
        values = []
        
        for column, value in data.items():
            set_clauses.append(f"{self._quote_ident(column)} = %s")
            values.append(value)
        
        values.append(record_id)
        query = f"UPDATE {self._quote_ident(table)} SET {', '.join(set_clauses)} WHERE {self._quote_ident('id')} = %s"
        
        try:
            connection, cursor = self._require_cursor()
            cursor.execute(query, values)
            connection.commit()
            return cursor.rowcount > 0
        except IntegrityError as e:
            if self.connection:
                self.connection.rollback()
            raise ValueError(f"Ошибка целостности данных: {e}")
        except DatabaseError as e:
            if self.connection:
                self.connection.rollback()
            raise DatabaseError(f"Ошибка при обновлении записи: {e}")
    
    # ==================== DELETE ====================
    
    def delete(self, table: str, record_id: int) -> bool:
        """
        Удаление записи по ID
        
        Args:
            table: Имя таблицы
            record_id: ID записи для удаления
        
        Returns:
            True если запись удалена, False если запись не найдена
        
        Example:
            db.delete('users', 1)
        """
        self._ensure_connection()
        
        query = f"DELETE FROM {self._quote_ident(table)} WHERE {self._quote_ident('id')} = %s"
        
        try:
            connection, cursor = self._require_cursor()
            cursor.execute(query, (record_id,))
            connection.commit()
            return cursor.rowcount > 0
        except DatabaseError as e:
            if self.connection:
                self.connection.rollback()
            raise DatabaseError(f"Ошибка при удалении записи: {e}")
    
    def delete_many(self, table: str, conditions: Dict[str, Any]) -> int:
        """
        Удаление нескольких записей по условиям
        
        Args:
            table: Имя таблицы
            conditions: Словарь с условиями удаления
        
        Returns:
            Количество удаленных записей
        
        Example:
            deleted_count = db.delete_many('users', {'status': 'inactive'})
        """
        self._ensure_connection()
        
        if not conditions:
            raise ValueError("Условия удаления не могут быть пустыми")
        
        where_clauses = []
        values = []
        
        for column, value in conditions.items():
            where_clauses.append(f"{self._quote_ident(column)} = %s")
            values.append(value)
        
        query = f"DELETE FROM {self._quote_ident(table)} WHERE {' AND '.join(where_clauses)}"
        
        try:
            connection, cursor = self._require_cursor()
            cursor.execute(query, values)
            connection.commit()
            return cursor.rowcount
        except DatabaseError as e:
            if self.connection:
                self.connection.rollback()
            raise DatabaseError(f"Ошибка при удалении записей: {e}")
    
    # ==================== УТИЛИТЫ ====================

    def _python_type_to_sql(self, py_type: type) -> str:
        """Преобразовать Python-тип в SQL-тип."""
        if py_type is int:
            return "INTEGER"
        if py_type is bool:
            return "BOOLEAN"
        if py_type is float:
            return "REAL"
        if py_type.__name__ == "datetime":
            return "TIMESTAMP"
        return "TEXT"

    def create_table_if_not_exists(self, model_cls: type, table_name: Optional[str] = None) -> None:
        """
        Создать таблицу по модели, если она не существует.

        Ожидается dataclass-модель в формате models/user.py.
        """
        self._ensure_connection()

        resolved_table = table_name or f"{model_cls.__name__.lower()}s"
        type_hints = get_type_hints(model_cls)
        column_defs = []

        for field_info in dataclass_fields(model_cls):
            field_name = field_info.name

            if field_name == "id":
                column_defs.append("id SERIAL PRIMARY KEY")
                continue

            annotated_type = type_hints.get(field_name, str)
            origin = get_origin(annotated_type)
            args = get_args(annotated_type)

            is_optional = origin is not None and type(None) in args
            if is_optional:
                base_type = next((t for t in args if t is not type(None)), str)
            else:
                base_type = annotated_type

            sql_type = self._python_type_to_sql(base_type)
            not_null = "" if is_optional else " NOT NULL"

            column_defs.append(f"{self._quote_ident(field_name)} {sql_type}{not_null}")

        query = f"CREATE TABLE IF NOT EXISTS {self._quote_ident(resolved_table)} ({', '.join(column_defs)})"

        try:
            connection, cursor = self._require_cursor()
            cursor.execute(query)
            connection.commit()
        except DatabaseError as e:
            if self.connection:
                self.connection.rollback()
            raise DatabaseError(f"Ошибка при создании таблицы: {e}")
    
    def execute_query(self, query: str, params: Optional[Tuple] = None) -> List[Dict[str, Any]]:
        """
        Выполнение произвольного SQL запроса
        
        Args:
            query: SQL запрос
            params: Параметры для запроса (кортеж)
        
        Returns:
            Список словарей с результатами
        
        Example:
            results = db.execute_query("SELECT * FROM users WHERE age > %s", (18,))
        """
        self._ensure_connection()
        
        try:
            connection, cursor = self._require_cursor()
            cursor.execute(query, params or ())
            results = cursor.fetchall()
            connection.commit()
            return [dict(row) for row in results]
        except DatabaseError as e:
            if self.connection:
                self.connection.rollback()
            raise DatabaseError(f"Ошибка при выполнении запроса: {e}")
    
    def execute_non_query(self, query: str, params: Optional[Tuple] = None) -> int:
        """
        Выполнение SQL запроса без возврата данных (INSERT, UPDATE, DELETE)
        
        Args:
            query: SQL запрос
            params: Параметры для запроса (кортеж)
        
        Returns:
            Количество затронутых строк
        
        Example:
            rows_affected = db.execute_non_query("UPDATE users SET status = %s WHERE id = %s", ('active', 1))
        """
        self._ensure_connection()
        
        try:
            connection, cursor = self._require_cursor()
            cursor.execute(query, params or ())
            connection.commit()
            return cursor.rowcount
        except DatabaseError as e:
            if self.connection:
                self.connection.rollback()
            raise DatabaseError(f"Ошибка при выполнении запроса: {e}")
    
    def get_table_info(self, table: str) -> List[Dict[str, Any]]:
        """
        Получение информации о структуре таблицы
        
        Args:
            table: Имя таблицы
        
        Returns:
            Список словарей с информацией о колонках
        """
        self._ensure_connection()
        
        query = """
            SELECT 
                column_name, 
                data_type, 
                is_nullable,
                column_default
            FROM information_schema.columns 
            WHERE table_name = %s
            ORDER BY ordinal_position
        """
        
        try:
            _, cursor = self._require_cursor()
            cursor.execute(query, (table,))
            results = cursor.fetchall()
            return [dict(row) for row in results]
        except DatabaseError as e:
            raise DatabaseError(f"Ошибка при получении информации о таблице: {e}")
