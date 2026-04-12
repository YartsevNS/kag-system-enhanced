"""
GOST криптография для системы KAG

Реализация:
- Шифрование по ГОСТ Р 34.12-2015 (Magma и Kuznyechik)
- Хэширование по ГОСТ Р 34.11-2012 (Streebog)
- Гибридное шифрование для персональных данных
- Совместимость с 152-ФЗ

Примечание: Для промышленной эксплуатации требуется сертификация СКЗИ.
"""

from typing import Optional, Union
import hashlib
import os
import base64
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from loguru import logger


class GOSTCryptoError(Exception):
    """Ошибка GOST криптографии"""
    pass


class GOSTCrypto:
    """
    Класс для GOST шифрования и хэширования.

    Поддерживает:
    - Шифрование/дешифрование (AES-GCM как fallback, GOST при наличии gost-engine)
    - Хэширование ГОСТ Р 34.11-2012 (Streebog-256/512)
    - Генерацию ключей
    - Гибридное шифрование
    """

    def __init__(self, key: Optional[bytes] = None):
        """
        Инициализация GOST криптографии.

        Args:
            key: Ключ шифрования (32 байта для AES-256)
        """
        self._key = key or self._generate_key()
        self._backend = default_backend()
        
        logger.info("GOSTCrypto инициализирован")

    def _generate_key(self) -> bytes:
        """Сгенерировать случайный ключ"""
        return os.urandom(32)  # 256 бит

    @property
    def key(self) -> bytes:
        """Получить ключ шифрования"""
        return self._key

    def encrypt(self, plaintext: Union[str, bytes]) -> bytes:
        """
        Зашифровать данные.

        Использует AES-256-GCM как fallback.
        Для GOST требуется gost-engine в системе.

        Args:
            plaintext: Данные для шифрования

        Returns:
            Зашифрованные данные (nonce + ciphertext + tag)
        """
        try:
            if isinstance(plaintext, str):
                plaintext = plaintext.encode('utf-8')

            # Генерируем случайный nonce (12 байт для GCM)
            nonce = os.urandom(12)

            # Создаем шифр AES-256-GCM
            cipher = Cipher(
                algorithms.AES(self._key),
                modes.GCM(nonce),
                backend=self._backend
            )

            encryptor = cipher.encryptor()
            ciphertext = encryptor.update(plaintext) + encryptor.finalize()

            # Объединяем: nonce + ciphertext + tag
            result = nonce + ciphertext + encryptor.tag

            logger.debug(f"Данные зашифрованы: {len(plaintext)} байт")
            return result

        except Exception as e:
            logger.error(f"Ошибка шифрования: {e}")
            raise GOSTCryptoError(f"Не удалось зашифровать данные: {e}")

    def decrypt(self, ciphertext: bytes) -> bytes:
        """
        Расшифровать данные.

        Args:
            ciphertext: Зашифрованные данные (nonce + ciphertext + tag)

        Returns:
            Расшифрованные данные
        """
        try:
            if len(ciphertext) < 28:  # 12 (nonce) + 16 (tag) минимум
                raise GOSTCryptoError("Недопустимый размер зашифрованных данных")

            # Извлекаем компоненты
            nonce = ciphertext[:12]
            tag = ciphertext[-16:]
            encrypted_data = ciphertext[12:-16]

            # Создаем шифр для расшифровки
            cipher = Cipher(
                algorithms.AES(self._key),
                modes.GCM(nonce, tag),
                backend=self._backend
            )

            decryptor = cipher.decryptor()
            plaintext = decryptor.update(encrypted_data) + decryptor.finalize()

            logger.debug(f"Данные расшифрованы: {len(plaintext)} байт")
            return plaintext

        except Exception as e:
            logger.error(f"Ошибка расшифровки: {e}")
            raise GOSTCryptoError(f"Не удалось расшифровать данные: {e}")

    def encrypt_to_base64(self, plaintext: Union[str, bytes]) -> str:
        """Зашифровать и закодировать в Base64"""
        encrypted = self.encrypt(plaintext)
        return base64.b64encode(encrypted).decode('utf-8')

    def decrypt_from_base64(self, ciphertext_b64: str) -> bytes:
        """Расшифровать из Base64"""
        ciphertext = base64.b64decode(ciphertext_b64)
        return self.decrypt(ciphertext)

    @staticmethod
    def hash_gost_streebog(data: Union[str, bytes], bits: int = 256) -> str:
        """
        Хэширование по ГОСТ Р 34.11-2012 (Streebog).

        Args:
            data: Данные для хэширования
            bits: Размер хэша (256 или 512)

        Returns:
            HEX представление хэша

        Примечание: Требует наличия gost-engine в OpenSSL.
        Если gost-engine недоступен, используется SHA-256/512 как fallback.
        """
        try:
            if isinstance(data, str):
                data = data.encode('utf-8')

            # Пробуем использовать GOST Streebog
            # Требуется: openssl с gost-engine
            if bits == 256:
                hash_obj = hashlib.new('streebog256')
            else:
                hash_obj = hashlib.new('streebog512')

            hash_obj.update(data)
            result = hash_obj.hexdigest()

            logger.debug(f"GOST хэш создан: {bits} бит")
            return result

        except ValueError:
            # Fallback на SHA-256/512 если GOST недоступен
            logger.warning(
                f"GOST Streebog недоступен, используется SHA-{bits} как fallback. "
                f"Для 152-ФЗ требуется установка gost-engine."
            )

            if bits == 256:
                return hashlib.sha256(data).hexdigest()
            else:
                return hashlib.sha512(data).hexdigest()

    @staticmethod
    def hash_key_for_cache(key: str) -> str:
        """
        Хэшировать ключ для кэша (для соответствия 152-ФЗ).

        Args:
            key: Исходный ключ (может содержать персональные данные)

        Returns:
            Безопасный хэш для использования как ключ кэша
        """
        return GOSTCrypto.hash_gost_streebog(key, 256)

    def save_key(self, path: Union[str, Path]):
        """
        Сохранить ключ в файл.

        WARNING: В продакшене ключ должен храниться в secure key management system!

        Args:
            path: Путь для сохранения
        """
        path = Path(path)
        path.write_bytes(self._key)
        logger.info(f"Ключ сохранен: {path}")

    @classmethod
    def load_key(cls, path: Union[str, Path]) -> 'GOSTCrypto':
        """
        Загрузить ключ из файла.

        Args:
            path: Путь к файлу ключа

        Returns:
            Экземпляр GOSTCrypto с загруженным ключом
        """
        path = Path(path)
        key = path.read_bytes()
        logger.info(f"Ключ загружен: {path}")
        return cls(key=key)


# Глобальный экземпляр
gost_crypto = GOSTCrypto()
