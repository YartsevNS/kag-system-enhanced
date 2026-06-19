"""
Extraction Service — извлечение контента с веб-страниц.

Использует Trafilatura + BeautifulSoup для гибридного парсинга.
Для PDF — PyMuPDF + Occular OCR.
"""

import hashlib
import logging
from typing import Optional
from urllib.parse import urljoin

import trafilatura
import trafilatura.sitemaps
import trafilatura.feeds
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class ExtractionService:
    """
    Сервис извлечения контента из веб-страниц и документов.

    Стратегия:
    1. Trafilatura для статического HTML (быстро, чистый Markdown)
    2. BeautifulSoup если нужен поиск по CSS-селекторам
    3. RSS-ленты через trafilatura.feeds
    4. Sitemap через trafilatura.sitemaps
    """

    @staticmethod
    def extract_text(html: str, url: str = "") -> dict:
        """
        Извлечь основной текст из HTML через Trafilatura.

        Returns:
            {"text": str, "title": str, "author": str, "date": str, "url": str}
        """
        result = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_images=False,
            include_formatting=True,
            favor_precision=True,
        )
        metadata = trafilatura.extract_metadata(html, url=url)
        return {
            "text": result or "",
            "title": metadata.title if metadata else "",
            "author": metadata.author if metadata else "",
            "date": metadata.date if metadata else "",
            "url": url,
        }

    @staticmethod
    def find_links(html: str, base_url: str, css_selector: str = "a[href]") -> list[dict]:
        """
        Найти ссылки на странице по CSS-селектору.

        Использует BeautifulSoup для точного поиска по селектору.
        Trafilatura для этого не подходит — он извлекает контент, не ссылки.
        """
        soup = BeautifulSoup(html, "html.parser")
        links = []
        for el in soup.select(css_selector):
            href = el.get("href", "").strip()
            if not href:
                continue
            abs_url = urljoin(base_url, href)
            text = el.get_text(strip=True)
            links.append({
                "url": abs_url,
                "text": text,
                "tag": el.name,
                "rel": el.get("rel", []),
            })
        return links

    @staticmethod
    def discover_rss(html: str, base_url: str) -> list[str]:
        """Найти RSS-ленты на странице через Trafilatura."""
        return list(trafilatura.feeds.find_feed_urls(html, base_url))

    @staticmethod
    def discover_sitemap(base_url: str) -> list[str]:
        """Найти URL через sitemap.xml."""
        try:
            urls = trafilatura.sitemaps.sitemap_search(base_url)
            return urls or []
        except Exception as e:
            logger.warning(f"Sitemap discovery failed for {base_url}: {e}")
            return []

    @staticmethod
    def extract_metadata(html: str, url: str = "") -> dict:
        """Извлечь метаданные HTML-страницы."""
        metadata = trafilatura.extract_metadata(html, url=url)
        if not metadata:
            return {}
        return {
            "title": metadata.title or "",
            "author": metadata.author or "",
            "date": metadata.date or "",
            "description": metadata.description or "",
            "categories": list(metadata.categories) if metadata.categories else [],
            "tags": list(metadata.tags) if metadata.tags else [],
        }


# Глобальный экземпляр
extraction_service = ExtractionService()
