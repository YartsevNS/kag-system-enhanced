"""
Web Monitor Service — мониторинг внешних источников документов.

Поддерживаемые режимы:
1. RSS/Atom — парсинг лент новостей (федеральные порталы, суды, ЦБ, ФНС)
2. Web Scraper — извлечение ссылок на PDF/DOCX с указанных страниц
3. Change Detection — HEAD-запросы + сравнение Last-Modified/ETag/хеша

Найденные документы автоматически загружаются в KAG Pipeline:
    Download → SHA-256 → Dedup → Upload → Parse → Qdrant → Neo4j

Интеграция:
- feedparser (pip install feedparser) — парсинг RSS/Atom
- httpx/aiohttp — асинхронные HTTP-запросы
- BeautifulSoup4 — парсинг HTML для извлечения ссылок
- Встроенные либы: hashlib, email.utils — хеширование и парсинг дат

Архитектура:
    WebMonitor
    ├── Источники (config_store)
    │   ├── RSS: url, last_pub_date, check_interval, keywords
    │   ├── SCRAPE: url, css_selector, file_types, last_etag
    │   └── CHANGE: url, last_hash, check_interval
    ├── Сканер (run_once / cron)
    │   ├── Загрузка → SHA-256 → Dedup
    │   ├── Сохранение в uploads/
    │   └── Отправка в KAG Pipeline
    └── История (config_store)
        └── Лог найденных/пропущенных/ошибок
"""

from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import re
import time
from loguru import logger
from urllib.parse import urljoin, urlparse


# ============================================================
# Data Classes
# ============================================================

@dataclass
class MonitorSource:
    """Источник для мониторинга — RSS-лента, страница со ссылками или страница для change detection.
    
    Поля:
    - id: уникальный идентификатор источника
    - name: человекочитаемое название (например, «ФСТЭК — Документы»)
    - url: URL источника (RSS-лента или веб-страница)
    - type: тип источника — rss, scrape, change
    - enabled: включён ли мониторинг
    - check_interval_minutes: как часто проверять (по умолчанию 360 = раз в 6 часов)
    - keywords: фильтр по ключевым словам в названии/заголовке (опционально)
    - file_types: для scrape — какие типы файлов искать (.pdf, .docx, .xlsx)
    - css_selector: для scrape — CSS-селектор для поиска ссылок
    - last_check: время последней проверки
    - last_etag: ETag последнего ответа (для change detection)
    - last_modified: Last-Modified последнего ответа
    - last_hash: SHA-256 хеш содержимого страницы (для change detection)
    - items_found: сколько всего документов найдено
    - items_uploaded: сколько загружено в KAG
    - created_at: дата создания источника
    """
    id: str
    name: str
    url: str
    type: str = "rss"  # rss, scrape, change
    enabled: bool = True
    check_interval_minutes: int = 360  # 6 часов по умолчанию
    keywords: List[str] = field(default_factory=list)  # фильтр по словам
    file_types: List[str] = field(default_factory=lambda: [".pdf", ".docx"])  # какие файлы скачивать
    css_selector: str = "a[href]"  # CSS для поиска ссылок (дальше фильтруем программно по href)
    last_check: Optional[datetime] = None
    last_etag: Optional[str] = None
    last_modified: Optional[str] = None
    last_hash: Optional[str] = None  # SHA-256 содержимого страницы
    items_found: int = 0
    items_uploaded: int = 0
    # Настройки скорости скачивания (чтобы не блокировал сервер-источник)
    batch_size: int = 5       # Файлов в одной партии
    batch_delay: float = 15.0  # Секунд между партиями
    item_delay: float = 2.0    # Секунд между файлами внутри партии
    batch_jitter: float = 5.0  # Случайная добавка к паузе (0..N секунд)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class MonitorResult:
    """Результат проверки одного источника.
    
    - source_id: ID источника
    - status: ok, no_changes, error
    - items: список найденных элементов
    - new_items: сколько НОВЫХ (ещё не загруженных) элементов
    - skipped_items: сколько пропущено (дубликаты)
    - error: текст ошибки если status=error
    """
    source_id: str
    status: str = "ok"  # ok, no_changes, error
    items: List[Dict] = field(default_factory=list)
    new_items: int = 0
    skipped_items: int = 0
    error: Optional[str] = None
    checked_at: datetime = field(default_factory=datetime.utcnow)


# ============================================================
# Web Monitor Service
# ============================================================

class WebMonitorService:
    """Сервис мониторинга внешних веб-источников.
    
    Основной цикл:
    1. Загрузить список активных источников из config_store
    2. Для каждого источника — запустить соответствующий парсер
    3. Найденные документы проверить на дубликаты (SHA-256)
    4. Новые документы загрузить в KAG Pipeline
    5. Сохранить историю проверок
    """

    # Предустановленные источники (государственные RSS-ленты РФ)
    BUILTIN_SOURCES = [
        {
            "name": "Официальный интернет-портал правовой информации",
            "url": "http://publication.pravo.gov.ru/Rss/",
            "type": "rss",
            "keywords": ["постановление", "распоряжение", "приказ", "закон", "указ"],
            "description": "Федеральные законы, указы Президента, постановления Правительства РФ"
        },
        {
            "name": "ЦБ РФ — нормативные акты",
            "url": "https://cbr.ru/rss/",
            "type": "rss",
            "keywords": ["положение", "указание", "инструкция"],
            "description": "Нормативные акты Банка России"
        },
        {
            "name": "ФНС России — письма",
            "url": "https://www.nalog.gov.ru/rn77/about_fts/about_nalog/rss/",
            "type": "rss",
            "keywords": ["письмо", "разъяснение", "порядок"],
            "description": "Письма и разъяснения Федеральной налоговой службы"
        },
        {
            "name": "ФСТЭК России — документы",
            "url": "https://fstec.ru/dokumenty",
            "type": "scrape",
            "css_selector": "a[href$='.pdf'], a[href$='.docx'], a[href$='.doc']",
            "keywords": ["требования", "методика", "приказ", "руководство", "угрозы"],
            "description": "Документы ФСТЭК: требования, методики, приказы по защите информации"
        },
        {
            "name": "ФСБ России — НПА",
            "url": "http://www.fsb.ru/fsb/npd.htm",
            "type": "scrape",
            "css_selector": "a[href$='.pdf'], a[href$='.docx'], a[href$='.doc']",
            "keywords": ["приказ", "требования", "сертификация", "СКЗИ"],
            "description": "Нормативные правовые акты ФСБ по криптографии и сертификации СКЗИ"
        },
        {
            "name": "ГОСТ Р — Информационная безопасность",
            "url": "https://www.gost.ru/portal/gost/home/standarts/InformationSecurity",
            "type": "scrape",
            "css_selector": "a[href*='file/load']",
            "keywords": [],
            "description": "Действующие стандарты ГОСТ Р по информационной безопасности: криптография, защита информации, ЭЦП"
        },
    ]

    # Отдельный набор: RSS-каналы Центрального Банка РФ
    CBR_SOURCES = [
        {
            "name": "ЦБ РФ — Новое на сайте",
            "url": "http://www.cbr.ru/rss/RssNews",
            "type": "rss",
            "keywords": ["нормативный", "акт", "положение", "указание", "приказ", "письмо"],
            "description": "Новые материалы на сайте Банка России: нормативные акты, статистика, обзоры"
        },
        {
            "name": "ЦБ РФ — Новости, интервью, выступления",
            "url": "http://www.cbr.ru/rss/eventrss",
            "type": "rss",
            "keywords": ["ставка", "инфляция", "кредит", "регулирование", "банк"],
            "description": "Новости, интервью и выступления руководства Банка России"
        },
        {
            "name": "ЦБ РФ — Пресс-релизы",
            "url": "http://www.cbr.ru/rss/RssPress",
            "type": "rss",
            "keywords": ["ключевая ставка", "отзыв лицензии", "решение", "совет директоров"],
            "description": "Официальные пресс-релизы Банка России"
        },
        {
            "name": "ЦБ РФ — Курсы валют",
            "url": "http://www.cbr.ru/rss/RssCurrency",
            "type": "rss",
            "keywords": ["доллар", "евро", "юань", "рубль", "USD", "EUR", "CNY"],
            "description": "Ежедневные курсы валют, устанавливаемые Банком России"
        },
        {
            "name": "ЦБ РФ — Регламент обработки электронных документов (МЦИ)",
            "url": "http://www.cbr.ru/rss/nregimr2",
            "type": "rss",
            "keywords": ["регламент", "МЦИ", "электронный документ", "обработка"],
            "description": "Информация о выполнении регламента обработки электронных документов (МЦИ)"
        },
        {
            "name": "ЦБ РФ — News (English)",
            "url": "http://www.cbr.ru/rss/EngRssNews",
            "type": "rss",
            "keywords": ["bank", "regulation", "policy", "rate", "statement"],
            "description": "Bank of Russia news in English"
        },
        {
            "name": "ЦБ РФ — Events (English)",
            "url": "http://www.cbr.ru/rss/engeventrss",
            "type": "rss",
            "keywords": ["speech", "interview", "conference", "forum"],
            "description": "Bank of Russia events, interviews, speeches in English"
        },
        {
            "name": "ЦБ РФ — Press Releases (English)",
            "url": "http://www.cbr.ru/rss/EngRssPress",
            "type": "rss",
            "keywords": ["key rate", "license", "board", "inflation"],
            "description": "Bank of Russia press releases in English"
        },
    ]

    # Роскомнадзор — RSS новостей
    RKN_SOURCES = [
        {
            "name": "Роскомнадзор — Новости",
            "url": "https://rkn.gov.ru/_services/rss/",
            "type": "rss",
            "keywords": ["блокировка", "реестр", "персональные данные", "угрозы", "блогеры", "надзор", "связь"],
            "description": "Новости Роскомнадзора: надзор в сфере связи, ИТ, СМИ, защита персональных данных, блокировки"
        },
    ]

    # SecurityNews — агрегатор новостей по информационной безопасности
    SECNEWS_SOURCES = [
        {
            "name": "SecurityNews — Все публикации",
            "url": "http://feeds.feedburner.com/security_news_ru",
            "type": "rss",
            "keywords": ["уязвимость", "атака", "взлом", "утечка", "CVE", "патч"],
            "description": "Все публикации SecurityNews: ИБ-новости, статьи, дайджесты"
        },
        {
            "name": "SecurityNews — Статьи",
            "url": "http://feeds.feedburner.com/secnews/articles",
            "type": "rss",
            "keywords": ["анализ", "исследование", "методика", "обзор", "практика"],
            "description": "Аналитические статьи по информационной безопасности"
        },
        {
            "name": "SecurityNews — Зарубежные новости",
            "url": "http://feeds.feedburner.com/secnews/foreign",
            "type": "rss",
            "keywords": ["vulnerability", "breach", "ransomware", "zero-day", "patch", "exploit"],
            "description": "Зарубежные новости ИБ: уязвимости, атаки, инциденты"
        },
        {
            "name": "SecurityNews — Российские новости",
            "url": "http://feeds.feedburner.com/secnews/news",
            "type": "rss",
            "keywords": ["ФСТЭК", "ФСБ", "импортозамещение", "сертификация", "ГосСОПКА", "НКЦКИ"],
            "description": "Российские новости ИБ: регуляторы, сертификация, импортозамещение"
        },
        {
            "name": "SecurityNews — Дайджест СМИ",
            "url": "http://feeds.feedburner.com/secnews/digest",
            "type": "rss",
            "keywords": ["СМИ", "обзор", "дайджест", "пресса", "публикация"],
            "description": "Дайджест СМИ по информационной безопасности"
        },
    ]

    def __init__(self):
        """Инициализация монитора. Загружает историю и кэш хешей."""
        self._hash_cache: Dict[str, str] = {}  # url → sha256 (для change detection)
        self._seen_urls: set = set()  # уже обработанные URL
        self._news_keys: set = set()  # ключи новостей (title[:80] + date) — дедупликация
        self._load_state()

    def _load_state(self):
        """Загрузить состояние монитора из config_store."""
        try:
            from src.api.services.config_store import config_store
            state = config_store.get("web_monitor", "state") or {}
            self._hash_cache = state.get("hash_cache", {})
            self._seen_urls = set(state.get("seen_urls", []))
            self._news_keys = set(state.get("news_keys", []))
        except Exception:
            pass

    def _save_state(self):
        """Сохранить состояние монитора в config_store."""
        try:
            from src.api.services.config_store import config_store
            config_store.set("web_monitor", "state", {
                "hash_cache": self._hash_cache,
                "seen_urls": list(self._seen_urls)[-10000:],
                "news_keys": list(self._news_keys)[-20000:],  # Последние 20К ключей
                "last_save": datetime.utcnow().isoformat()
            })
        except Exception as e:
            logger.warning(f"Не удалось сохранить состояние монитора: {e}")

    # ============================================================
    # Управление источниками
    # ============================================================

    def get_sources(self) -> List[MonitorSource]:
        """Получить все источники мониторинга."""
        try:
            from src.api.services.config_store import config_store
            sources_data = config_store.get("web_monitor", "sources") or []
            return [
                MonitorSource(
                    id=s.get("id", ""),
                    name=s.get("name", ""),
                    url=s.get("url", ""),
                    type=s.get("type", "rss"),
                    enabled=s.get("enabled", True),
                    check_interval_minutes=s.get("check_interval_minutes", 360),
                    keywords=s.get("keywords", []),
                    file_types=s.get("file_types", [".pdf", ".docx"]),
                    css_selector=s.get("css_selector", "a[href$='.pdf'], a[href$='.docx']"),
                    last_check=datetime.fromisoformat(s["last_check"]) if s.get("last_check") else None,
                    last_etag=s.get("last_etag"),
                    last_modified=s.get("last_modified"),
                    last_hash=s.get("last_hash"),
                    items_found=s.get("items_found", 0),
                    items_uploaded=s.get("items_uploaded", 0),
                    created_at=datetime.fromisoformat(s["created_at"]) if s.get("created_at") else datetime.utcnow()
                )
                for s in sources_data
            ]
        except Exception:
            return []

    def save_source(self, source: MonitorSource):
        """Сохранить/обновить источник мониторинга."""
        try:
            from src.api.services.config_store import config_store
            sources = config_store.get("web_monitor", "sources") or []
            # Обновить существующий или добавить новый
            found = False
            for i, s in enumerate(sources):
                if s.get("id") == source.id:
                    sources[i] = self._source_to_dict(source)
                    found = True
                    break
            if not found:
                sources.append(self._source_to_dict(source))
            config_store.set("web_monitor", "sources", sources)
        except Exception as e:
            logger.error(f"Не удалось сохранить источник: {e}")

    def delete_source(self, source_id: str):
        """Удалить источник мониторинга."""
        try:
            from src.api.services.config_store import config_store
            sources = config_store.get("web_monitor", "sources") or []
            sources = [s for s in sources if s.get("id") != source_id]
            config_store.set("web_monitor", "sources", sources)
        except Exception as e:
            logger.error(f"Не удалось удалить источник: {e}")

    def _source_to_dict(self, s: MonitorSource) -> dict:
        """Сериализовать источник в словарь для config_store."""
        return {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "type": s.type,
            "enabled": s.enabled,
            "check_interval_minutes": s.check_interval_minutes,
            "keywords": s.keywords,
            "file_types": s.file_types,
            "css_selector": s.css_selector,
            "last_check": s.last_check.isoformat() if s.last_check else None,
            "last_etag": s.last_etag,
            "last_modified": s.last_modified,
            "last_hash": s.last_hash,
            "items_found": s.items_found,
            "items_uploaded": s.items_uploaded,
            "created_at": s.created_at.isoformat() if s.created_at else datetime.utcnow().isoformat()
        }

    # ============================================================
    # Основной цикл проверки
    # ============================================================

    async def run_check(self, source_id: Optional[str] = None, force: bool = False) -> List[MonitorResult]:
        """Запустить проверку: всех источников или одного конкретного.
        
        Args:
            source_id: ID источника для проверки (None = все активные)
            force: если True — игнорировать enabled и check_interval
        
        Returns:
            Список результатов проверки по каждому источнику
        """
        sources = self.get_sources()
        if source_id:
            sources = [s for s in sources if s.id == source_id]
            force = True  # принудительно для конкретного источника

        # Фильтруем: только включённые, и время последней проверки вышло
        now = datetime.utcnow()
        to_check = []
        for s in sources:
            if not force and not s.enabled:
                continue
            if not force and s.last_check:
                elapsed = (now - s.last_check).total_seconds() / 60
                if elapsed < s.check_interval_minutes:
                    continue  # Ещё рано проверять
            to_check.append(s)

        results = []
        for source in to_check:
            logger.info(f"🔍 Проверяю источник: {source.name} ({source.type})")
            try:
                if source.type == "rss":
                    result = await self._check_rss(source)
                elif source.type == "scrape":
                    result = await self._check_scrape(source)
                elif source.type == "change":
                    result = await self._check_change(source)
                else:
                    result = MonitorResult(source_id=source.id, status="error", error=f"Неизвестный тип: {source.type}")

                # Обновляем статистику
                source.last_check = now
                total_found = len(result.items) if result.items else 0
                # items_found = найдено в последнюю проверку (все ссылки на странице)
                source.items_found = total_found
                source.items_uploaded += result.new_items
                self.save_source(source)
                results.append(result)

                # Сохраняем в историю
                self.add_history({
                    "source_id": source.id,
                    "source_name": source.name,
                    "time": now.isoformat(),
                    "status": result.status,
                    "new_items": result.new_items,
                    "skipped": result.skipped_items,
                    "total_found": total_found,
                    "error": result.error,
                })

            except Exception as e:
                logger.error(f"Ошибка проверки {source.name}: {e}")
                results.append(MonitorResult(source_id=source.id, status="error", error=str(e)))

        self._save_state()
        return results

    # ============================================================
    # RSS-парсер
    # ============================================================

    async def _check_rss(self, source: MonitorSource) -> MonitorResult:
        """Проверить RSS/Atom-ленту на новые записи.
        
        Алгоритм:
        1. Загрузить RSS через feedparser
        2. Для каждой записи проверить: дата > last_check?
        3. Если есть вложения (enclosures) — скачать файл
        4. Если есть ссылки в тексте — извлечь URL документов
        5. Найденные файлы → SHA-256 → Dedup → Upload
        """
        import feedparser
        import aiohttp

        result = MonitorResult(source_id=source.id)
        new_urls = []

        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                # Загружаем RSS (feedparser синхронный, запускаем в потоке)
                import asyncio, re as _re
                loop = asyncio.get_running_loop()
                
                # Санитизация: фиксим битый XML (распространено в гос. RSS)
                def _fetch_and_parse(url):
                    import urllib.request
                    try:
                        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                        raw = urllib.request.urlopen(req, timeout=30).read()
                        # Чиним частые проблемы: непарные &, невалидные символы
                        raw = raw.decode('utf-8', errors='replace')
                        raw = _re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#\d+;)', '&amp;', raw)
                        return feedparser.parse(raw)
                    except Exception:
                        return feedparser.parse(url)
                
                feed = await loop.run_in_executor(None, lambda: _fetch_and_parse(source.url))

                if feed.bozo and not feed.entries:
                    result.status = "error"
                    result.error = f"RSS не распознан: {feed.bozo_exception}"
                    return result

                # Фильтруем записи по дате
                cutoff = source.last_check or (datetime.utcnow() - timedelta(days=7))
                for entry in feed.entries:
                    # Парсим дату публикации
                    pub_date = None
                    if hasattr(entry, 'published_parsed') and entry.published_parsed:
                        pub_date = datetime(*entry.published_parsed[:6])
                    elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                        pub_date = datetime(*entry.updated_parsed[:6])

                    # Пропускаем только ОЧЕНЬ старые записи (старше 30 дней),
                    # если нет даты — не пропускаем (считаем новыми)
                    if pub_date and pub_date < cutoff - timedelta(days=23):
                        continue

                    title = entry.get('title', '')
                    # Фильтр по ключевым словам (если заданы)
                    if source.keywords:
                        title_lower = title.lower()
                        if not any(kw.lower() in title_lower for kw in source.keywords):
                            continue

                    # Ищем ссылки на документы
                    # 1. Вложения (enclosures) — самый надёжный способ
                    for enc in entry.get('enclosures', []):
                        url = enc.get('href', '')
                        if url and self._is_document_url(url, source.file_types):
                            new_urls.append({'url': url, 'title': title, 'source': source.name})

                    # 2. Ссылки в description/content
                    desc = entry.get('description', '') + entry.get('content', [{}])[0].get('value', '') if hasattr(entry, 'content') else ''
                    doc_urls = self._extract_document_links(desc, source.file_types)
                    for url in doc_urls:
                        if not any(u['url'] == url for u in new_urls):
                            new_urls.append({'url': url, 'title': title, 'source': source.name})

                    # 3. Если нет вложений — сохраняем саму новость как текстовый документ
                    #    (для новостных RSS типа ЦБ РФ, где нет PDF, но есть ценный текст)
                    if not new_urls:
                        link = entry.get('link', '')
                        summary = entry.get('summary', entry.get('description', ''))
                        text_content = f"{title}\n\n{summary}\n\nИсточник: {link}"
                        # Ключ дедупликации: title[:80] + дата публикации (YYYY-MM-DD)
                        date_str = pub_date.strftime('%Y-%m-%d') if pub_date else entry.get('published', '')[:10]
                        news_key = self._news_key(title, date_str)
                        new_urls.append({
                            'url': link or f"rss://{source.id}/{hash(title)}",
                            'title': title,
                            'source': source.name,
                            'is_rss_text': True,
                            'text_content': text_content,
                            'news_key': news_key  # Для дедупликации по названию+дате
                        })

                result.items = new_urls

                # Загружаем найденные документы
                if new_urls:
                    result.new_items, result.skipped_items = await self._download_and_upload(
                        session, new_urls, source,
                        batch_size=source.batch_size,
                        batch_delay=source.batch_delay,
                        item_delay=source.item_delay,
                        batch_jitter=source.batch_jitter
                    )

                result.status = "ok"

        except Exception as e:
            result.status = "error"
            result.error = str(e)
            logger.error(f"RSS ошибка {source.name}: {e}")

        return result

    # ============================================================
    # Web Scraper
    # ============================================================


    @staticmethod
    def _extract_link_metadata(link, soup, source) -> dict:
        """Извлечь метаданные документа из HTML-структуры вокруг ссылки.
        
        Для ГОСТ: ищет тип (ГОСТ Р), номер (59162-2020), название в соседних ячейках.
        Для ЦБ: ищет тип документа, номер, дату.
        Возвращает dict с ключами: doc_type, doc_number, doc_title, doc_date.
        """
        meta = {}
        try:
            parent = link.parent  # <td> или <p>
            row = parent.parent if parent else None  # <tr>
            
            if row and row.name == 'tr':
                cells = row.find_all('td')
                link_text = link.get_text(strip=True)
                
                # Ищем ячейку с типом документа (ГОСТ Р, ГОСТ, Приказ, Указание)
                for cell in cells:
                    text = cell.get_text(strip=True)
                    if not text or text == link_text:
                        continue
                    # Паттерны типов документов
                    for pattern in ['ГОСТ Р', 'ГОСТ', 'Приказ', 'Указание', 'Положение', 
                                    'Методические рекомендации', 'Стандарт', 'Информационное письмо']:
                        if pattern.upper() in text.upper():
                            meta['doc_type'] = text.strip()
                            break
                    # Номер документа (цифры, дефисы, точки)
                    import re
                    num_match = re.match(r'^\s*[№#]?\s*([\d.\-]+)\s*$', text)
                    if num_match and not meta.get('doc_number'):
                        meta['doc_number'] = num_match.group(1)
                    # Название (длинный текст)
                    if len(text) > 30 and not meta.get('doc_title'):
                        meta['doc_title'] = text.strip()
                
                # Если номер не нашли в соседних ячейках — парсим из текста ссылки
                if not meta.get('doc_number'):
                    num = re.search(r'(\d{1,5}[.,\-]\d{1,4}[.,\-]?\d{0,4})', link_text)
                    if num:
                        meta['doc_number'] = num.group(1)
                
                # Название из текста ссылки если не нашли в ячейках
                if not meta.get('doc_title') and len(link_text) > 10:
                    meta['doc_title'] = link_text.strip()
            else:
                # Простой случай — только текст ссылки
                text = link.get_text(strip=True)
                if text:
                    meta['doc_title'] = text
        except Exception:
            pass
        return meta

    async def _check_scrape(self, source: MonitorSource) -> MonitorResult:
        """Проверить веб-страницу на новые ссылки на документы.
        
        Алгоритм:
        1. GET-запрос к странице
        2. Парсинг HTML → найти все ссылки по css_selector
        3. Отфильтровать по file_types и keywords
        4. Проверить ETag/Last-Modified — если страница не изменилась, пропустить
        5. Новые ссылки → Download → SHA-256 → Dedup → Upload
        """
        import aiohttp
        from bs4 import BeautifulSoup

        result = MonitorResult(source_id=source.id)
        new_urls = []

        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                headers = {}
                if source.last_etag:
                    headers['If-None-Match'] = source.last_etag
                if source.last_modified:
                    headers['If-Modified-Since'] = source.last_modified

                async with session.get(source.url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    # 304 Not Modified — страница не изменилась
                    if resp.status == 304:
                        result.status = "no_changes"
                        return result

                    if resp.status != 200:
                        result.status = "error"
                        result.error = f"HTTP {resp.status}"
                        return result

                    # Сохраняем ETag и Last-Modified для будущих проверок
                    source.last_etag = resp.headers.get('ETag', '')
                    source.last_modified = resp.headers.get('Last-Modified', '')

                    # Сравниваем хеш содержимого (если нет ETag)
                    html = await resp.text()
                    page_hash = hashlib.sha256(html.encode()).hexdigest()
                    if source.last_hash and source.last_hash == page_hash:
                        result.status = "no_changes"
                        return result
                    source.last_hash = page_hash

                    # Парсим HTML
                    soup = BeautifulSoup(html, 'html.parser')
                    links = soup.select(source.css_selector)

                    for link in links:
                        href = link.get('href', '')
                        if not href:
                            continue
                        # Делаем абсолютный URL
                        abs_url = urljoin(source.url, href)
                        if not self._is_document_url(abs_url, source.file_types):
                            continue

                        text = link.get_text(strip=True)

                        # Извлекаем метаданные из окружающей структуры (ГОСТ, ЦБ и т.д.)
                        meta = self._extract_link_metadata(link, soup, source)
                        new_urls.append({
                            'url': abs_url,
                            'title': text or meta.get('title') or Path(href).name,
                            'source': source.name,
                            'metadata': meta
                        })

                result.items = new_urls

                if new_urls:
                    result.new_items, result.skipped_items = await self._download_and_upload(
                        session, new_urls, source,
                        batch_size=source.batch_size,
                        batch_delay=source.batch_delay,
                        item_delay=source.item_delay,
                        batch_jitter=source.batch_jitter
                    )

                result.status = "ok"

        except Exception as e:
            result.status = "error"
            result.error = str(e)

        return result

    # ============================================================
    # Change Detection
    # ============================================================

    async def _check_change(self, source: MonitorSource) -> MonitorResult:
        """Проверить страницу на изменения (change detection).
        
        Проще чем scrape: просто сравниваем SHA-256 содержимого.
        Если изменилось — скачиваем как документ и загружаем.
        """
        import aiohttp

        result = MonitorResult(source_id=source.id)

        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                async with session.get(source.url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        result.status = "error"
                        result.error = f"HTTP {resp.status}"
                        return result

                    html = await resp.text()
                    page_hash = hashlib.sha256(html.encode()).hexdigest()

                    if source.last_hash and source.last_hash == page_hash:
                        result.status = "no_changes"
                        return result

                    # Страница изменилась — сохраняем как документ
                    source.last_hash = page_hash

                    # Сохраняем HTML как текстовый документ
                    from pathlib import Path as P
                    filename = f"{source.name.replace(' ', '_')}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.html"
                    # Загружаем через document_service
                    try:
                        from src.api.services.document_service import document_service
                        record = await document_service.upload_document(
                            filename=filename,
                            file_content=html.encode('utf-8'),
                            file_type="text/html",
                            source_metadata={'source_url': source.url, 'source_name': source.name}
                        )
                        # Запускаем обработку
                        from src.api.routes.upload import _process_document_async
                        await _process_document_async(record.document_id)
                        result.new_items = 1
                        result.items = [{'url': source.url, 'title': filename, 'source': source.name}]
                    except Exception as e:
                        result.status = "error"
                        result.error = f"Не удалось сохранить: {e}"
                        return result

                result.status = "ok"

        except Exception as e:
            result.status = "error"
            result.error = str(e)

        return result

    # ============================================================
    # Вспомогательные методы
    # ============================================================

    @staticmethod
    def _news_key(title: str, date_str: str) -> str:
        """Создать ключ дедупликации новости: название[:80] + дата (YYYY-MM-DD).
        
        Используется чтобы не загружать одну и ту же новость дважды.
        Ключ стабилен: одна и та же новость с одного источника всегда даст одинаковый ключ.
        """
        clean_title = (title or '').strip()[:80].lower()
        clean_date = (date_str or '')[:10]  # YYYY-MM-DD
        return f"{clean_title}|{clean_date}"

    def _is_document_url(self, url: str, file_types: List[str]) -> bool:
        """Проверить, ведёт ли URL на документ нужного типа.
        
        Проверяет:
        1. Расширение файла (.pdf, .docx, ...)
        2. Паттерны file-service (rst.gov.ru/file-service/file/load/...)
        3. Паттерны download (скачивание без расширения в URL)
        """
        parsed = urlparse(url)
        path = parsed.path.lower()
        
        # Проверка 1: расширение файла
        if any(path.endswith(ext) for ext in file_types):
            return True
        
        # Проверка 2: file-service паттерны (ГОСТ, ведомственные порталы)
        file_service_patterns = [
            '/file-service/file/load/',   # rst.gov.ru — ГОСТы
            '/file/load/',                 # общий
            '/files/download/',            # скачивание
            '/download/file/',             # ещё вариант
            '/api/files/',                 # API файлов
        ]
        if any(pattern in path for pattern in file_service_patterns):
            return True
        
        # Проверка 3: query-параметры указывают на файл
        query = parsed.query.lower()
        if any(ext in query for ext in file_types):
            return True
        if any(kw in query for kw in ['download', 'file=', 'getfile', 'attachment']):
            return True
        
        return False

    def _extract_document_links(self, html_text: str, file_types: List[str]) -> List[str]:
        """Извлечь ссылки на документы из HTML-текста."""
        urls = []
        for ext in file_types:
            # Ищем href="...pdf" или href='...docx'
            pattern = rf'href=["\']([^"\']+\{re.escape(ext)})["\']'
            matches = re.findall(pattern, html_text, re.IGNORECASE)
            urls.extend(matches)
        return list(set(urls))  # Убираем дубликаты

    async def _download_and_upload(
        self, session, items: List[Dict], source: MonitorSource,
        batch_size: int = 5,       # Сколько файлов за раз
        batch_delay: float = 15.0,  # Пауза между партиями (сек)
        item_delay: float = 2.0,    # Пауза между файлами внутри партии (сек)
        batch_jitter: float = 5.0   # Случайная добавка к паузе (0..N сек)
    ) -> tuple:
        """Скачать найденные документы партиями и загрузить в KAG Pipeline.
        
        Чтобы не перегружать сервер-источник и не попадать под блокировку:
        - Файлы скачиваются ПАРТИЯМИ по batch_size штук
        - Между партиями пауза batch_delay секунд
        - Между файлами внутри партии пауза item_delay секунд
        - При получении 429 (Too Many Requests) пауза увеличивается автоматически
        
        Returns:
            (new_items: int, skipped_items: int)
        """
        import aiohttp
        import asyncio as _asyncio
        import random as _random

        new_count = 0
        skip_count = 0
        total = len(items)
        batches = (total + batch_size - 1) // batch_size  # Округление вверх

        logger.info(
            f"📥 Скачивание {total} файлов из {source.name}: "
            f"{batches} партий по {batch_size}, "
            f"пауза {batch_delay}с между партиями, {item_delay}с между файлами"
        )

        for batch_num in range(batches):
            start = batch_num * batch_size
            end = min(start + batch_size, total)
            batch_items = items[start:end]
            
            logger.info(f"📦 Партия {batch_num + 1}/{batches}: файлы {start + 1}–{end} из {total}")

            for idx, item in enumerate(batch_items):
                url = item['url']
                filename = item.get('title', Path(urlparse(url).path).name)

                # Пропускаем уже обработанные URL
                if url in self._seen_urls:
                    skip_count += 1
                    continue

                try:
                    # Пауза между файлами внутри партии (кроме первого)
                    if idx > 0:
                        await _asyncio.sleep(item_delay + _random.uniform(0, 1))

                    # Если это RSS-текст (новость без вложений) — сохраняем напрямую
                    if item.get('is_rss_text'):
                        # Дедупликация по ключу новости (название + дата)
                        news_key = item.get('news_key', '')
                        if news_key and news_key in self._news_keys:
                            skip_count += 1
                            continue  # Уже есть такая новость — пропускаем

                        text_content = item.get('text_content', '')
                        content = text_content.encode('utf-8')
                        filename = (item.get('title', 'rss_entry')[:80] + '.txt').replace('/', '_')
                        file_hash = hashlib.sha256(content).hexdigest()

                        # Дополнительная проверка: SHA-256 дубликат
                        try:
                            from src.api.services.document_service import document_service
                            existing = document_service._find_by_hash(file_hash)
                            if existing:
                                skip_count += 1
                                if news_key:
                                    self._news_keys.add(news_key)  # Запоминаем что видели
                                continue
                        except Exception:
                            pass

                        # Загружаем как текстовый документ
                        try:
                            record = await document_service.upload_document(
                                filename=filename,
                                file_content=content,
                                file_type='text/plain',
                                source_metadata=item.get('metadata')
                            )
                            from src.api.routes.upload import _process_document_async
                            await _process_document_async(record.document_id)
                            new_count += 1
                            if news_key:
                                self._news_keys.add(news_key)
                            self._save_state()
                            self.track_download({
                                'url': url, 'filename': filename,
                                'source_id': source.id, 'source_name': source.name,
                                'status': 'downloaded',
                                'file_hash': file_hash, 'file_size': len(content),
                                'kag_document_id': record.document_id,
                                'content_type': 'text/plain',
                                'downloaded_at': datetime.utcnow().isoformat()
                            })
                            logger.info(f"📰 [{new_count}/{total}] RSS-новость: {filename[:50]}")
                        except Exception as e:
                            logger.warning(f"Ошибка сохранения RSS-новости {filename[:50]}: {e}")
                            skip_count += 1
                        continue  # Переходим к следующему элементу

                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        # Обработка rate limiting
                        if resp.status == 429:
                            retry_after = int(resp.headers.get('Retry-After', '30'))
                            logger.warning(f"⏳ Rate limited ({url[:60]}), жду {retry_after}с...")
                            await _asyncio.sleep(retry_after)
                            skip_count += 1
                            continue

                        if resp.status == 503:
                            logger.warning(f"🔌 Сервер недоступен ({url[:60]}), пропускаю")
                            skip_count += 1
                            continue

                        if resp.status != 200:
                            logger.warning(f"Не удалось скачать {url[:60]}: HTTP {resp.status}")
                            skip_count += 1
                            continue

                        content = await resp.read()

                        # Определяем тип файла по Content-Type
                        content_type = resp.headers.get('Content-Type', '')
                        content_disposition = resp.headers.get('Content-Disposition', '')
                        logger.debug(f"  URL={url[:60]} CT={content_type} CD={content_disposition[:80]}")

                        # Извлекаем имя файла из Content-Disposition если есть
                        import re as _re
                        cd_match = _re.search(r'filename[^;=\n]*=["\']?([^"\';\\n]*)', content_disposition, _re.IGNORECASE)
                        if cd_match:
                            filename = cd_match.group(1).strip() or filename

                        # Если тип файла не определён по расширению — берём из Content-Type
                        if not any(filename.lower().endswith(ext) for ext in source.file_types):
                            mime_to_ext = {
                                'application/pdf': '.pdf',
                                'application/msword': '.doc',
                                'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
                                'application/vnd.ms-excel': '.xls',
                                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
                                'text/plain': '.txt',
                                'text/csv': '.csv',
                                'text/html': '.html',
                            }
                            for mime, ext in mime_to_ext.items():
                                if mime in content_type:
                                    filename = filename.rsplit('.', 1)[0] + ext
                                    break

                        if len(content) < 100:  # Слишком маленький — не документ
                            logger.info(f"⏭ Пропущен {filename[:40]}: слишком маленький ({len(content)} bytes)")
                            skip_count += 1
                            self.track_download({
                                'url': url, 'filename': filename,
                                'source_id': source.id, 'source_name': source.name,
                                'status': 'skipped', 'error': 'file too small (<100 bytes)',
                                'file_hash': None, 'file_size': len(content),
                                'kag_document_id': None, 'content_type': None,
                                'downloaded_at': datetime.utcnow().isoformat()
                            })
                            continue

                        # SHA-256 для дедупликации
                        file_hash = hashlib.sha256(content).hexdigest()

                        # Проверяем — нет ли уже такого документа в KAG?
                        try:
                            from src.api.services.document_service import document_service
                            existing = document_service._find_by_hash(file_hash)
                            if existing:
                                logger.info(f"🔁 Дубликат: {filename[:50]} уже существует как {existing.document_id[:12]}...")
                                skip_count += 1
                                self._seen_urls.add(url)
                                self.track_download({
                                    'url': url, 'filename': filename,
                                    'source_id': source.id, 'source_name': source.name,
                                    'status': 'duplicate',
                                    'file_hash': file_hash, 'file_size': len(content),
                                    'kag_document_id': existing.document_id,
                                    'content_type': None,
                                    'downloaded_at': datetime.utcnow().isoformat()
                                })
                                continue
                        except Exception:
                            pass

                        # Загружаем в KAG Pipeline
                        try:
                            record = await document_service.upload_document(
                                filename=filename,
                                file_content=content,
                                file_type=None,
                                source_metadata=item.get('metadata')
                            )
                            # Запускаем фоновую обработку
                            from src.api.routes.upload import _process_document_async
                            await _process_document_async(record.document_id)

                            new_count += 1
                            self._seen_urls.add(url)
                            self.track_download({
                                'url': url, 'filename': filename,
                                'source_id': source.id, 'source_name': source.name,
                                'status': 'downloaded',
                                'file_hash': file_hash, 'file_size': len(content),
                                'kag_document_id': record.document_id,
                                'content_type': content_type,
                                'downloaded_at': datetime.utcnow().isoformat()
                            })
                            logger.info(f"📥 [{new_count}/{total}] Загружен: {filename[:50]} (из {source.name})")

                        except Exception as e:
                            logger.warning(f"Ошибка загрузки {filename[:50]}: {e}")
                            skip_count += 1

                except aiohttp.ClientError as e:
                    logger.warning(f"Сетевая ошибка {url[:60]}: {e}")
                    skip_count += 1
                except Exception as e:
                    logger.warning(f"Ошибка скачивания {url[:60]}: {e}")
                    skip_count += 1

            # Пауза между партиями (кроме последней)
            if batch_num < batches - 1:
                delay = batch_delay + _random.uniform(0, batch_jitter)
                logger.info(f"⏸ Пауза {delay:.0f}с перед следующей партией...")
                await _asyncio.sleep(delay)

        logger.info(f"📊 Итого: {new_count} новых, {skip_count} пропущено из {total}")
        return new_count, skip_count

    # ============================================================
    # Документ-уровень: история скачиваний
    # ============================================================

    def track_download(self, entry: dict):
        """Сохранить запись о попытке скачивания документа.

        entry: {
            'url': str, 'filename': str, 'source_id': str, 'source_name': str,
            'status': 'downloaded'|'skipped'|'error'|'duplicate',
            'file_hash': str | None, 'file_size': int | None,
            'kag_document_id': str | None, 'error': str | None,
            'content_type': str | None, 'downloaded_at': str (ISO)
        }
        """
        try:
            from src.api.services.config_store import config_store
            downloads = config_store.get("web_monitor", "downloads") or []
            downloads.append(entry)
            # Храним последние 2000 записей
            config_store.set("web_monitor", "downloads", downloads[-2000:])
        except Exception as e:
            logger.warning(f"track_download: {e}")

    def get_downloads(self, limit: int = 100, status: str = None,
                      source_id: str = None) -> List[Dict]:
        """Получить историю скачиваний документов с фильтрацией."""
        try:
            from src.api.services.config_store import config_store
            downloads = config_store.get("web_monitor", "downloads") or []
            # Новые сверху
            downloads = list(reversed(downloads))
            if status:
                downloads = [d for d in downloads if d.get('status') == status]
            if source_id:
                downloads = [d for d in downloads if d.get('source_id') == source_id]
            return downloads[:limit]
        except Exception:
            return []

    def get_stats(self) -> dict:
        """Агрегированная статистика мониторинга."""
        try:
            from src.api.services.config_store import config_store
            downloads = config_store.get("web_monitor", "downloads") or []
            sources = self.get_sources()

            total_found = sum(s.items_found for s in sources)
            total_attempts = len(downloads)
            downloaded = sum(1 for d in downloads if d.get('status') == 'downloaded')
            skipped = sum(1 for d in downloads if d.get('status') == 'skipped')
            duplicates = sum(1 for d in downloads if d.get('status') == 'duplicate')
            errors = sum(1 for d in downloads if d.get('status') == 'error')
            processed = sum(1 for d in downloads if d.get('kag_document_id'))

            # Последняя активность
            last_download = None
            if downloads:
                last_dl = downloads[-1]
                last_download = {
                    'url': last_dl.get('url', '')[:100],
                    'filename': last_dl.get('filename', ''),
                    'status': last_dl.get('status', ''),
                    'downloaded_at': last_dl.get('downloaded_at', ''),
                    'source_name': last_dl.get('source_name', ''),
                }

            # По источникам
            by_source = []
            for s in sources:
                src_downloads = [d for d in downloads if d.get('source_id') == s.id]
                src_processed = sum(1 for d in src_downloads if d.get('kag_document_id'))
                last_check = s.last_check.isoformat() if s.last_check else None
                by_source.append({
                    'id': s.id,
                    'name': s.name,
                    'type': s.type,
                    'enabled': s.enabled,
                    'url': s.url[:120],
                    'items_found': s.items_found,
                    'items_uploaded': s.items_uploaded,
                    'download_attempts': len(src_downloads),
                    'processed': src_processed,
                    'last_check': last_check,
                })

            return {
                'sources_total': len(sources),
                'sources_enabled': sum(1 for s in sources if s.enabled),
                'total_found': total_found,
                'total_download_attempts': total_attempts,
                'downloaded': downloaded,
                'skipped': skipped,
                'duplicates': duplicates,
                'errors': errors,
                'processed_into_kag': processed,
                'last_activity': last_download,
                'by_source': by_source,
            }
        except Exception as e:
            logger.error(f"get_stats error: {e}")
            return {'error': str(e)}

    def get_history(self, limit: int = 50) -> List[Dict]:
        """Получить историю проверок мониторинга."""
        try:
            from src.api.services.config_store import config_store
            history = config_store.get("web_monitor", "history") or []
            return history[-limit:]
        except Exception:
            return []

    def add_history(self, entry: Dict):
        """Добавить запись в историю проверок."""
        try:
            from src.api.services.config_store import config_store
            history = config_store.get("web_monitor", "history") or []
            history.append(entry)
            # Храним последние 500 записей
            config_store.set("web_monitor", "history", history[-500:])
        except Exception:
            pass


# Глобальный экземпляр
web_monitor = WebMonitorService()
