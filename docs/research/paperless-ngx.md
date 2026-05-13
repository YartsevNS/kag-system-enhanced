# Paperless-ngx — Анализ проекта

**GitHub:** paperless-ngx/paperless-ngx | ⭐ 40,632 | Python/Django | Community  
**Демо:** demo.paperless-ngx.com (demo/demo) | **Лицензия:** GPL-3.0

## Что такое Paperless-ngx

Система управления документами: сканирование → индексация → архив. Превращает физические документы в поисковый онлайн-архив. Золотой стандарт document management с 40k+ звёзд.

## Ключевые возможности

### 1. Автоматический входящий поток (Consumption)
- **Consumption folder** — мониторинг папок на диске
- **Email ingestion** — забор документов из почтовых ящиков
- **REST API upload** — программная загрузка
- **Mobile apps** — сканирование с телефона

### 2. OCR и обработка
- Tesseract + Ghostscript для PDF/A
- Авто-определение языка (включая русский)
- OCRmyPDF для оптимизации PDF
- Многопоточная обработка (Celery)

### 3. Классификация и тегирование
- **Auto-tagging** — правила на основе содержимого
- **Document types** — счета, договоры, корреспонденция
- **Correspondents** — отправители/получатели
- **Custom fields** — произвольные метаданные
- **ML-классификация** — авто-обучение на основе ручной разметки

### 4. Поиск и навигация
- Полнотекстовый поиск (Whoosh/SQLite, опционально Elasticsearch)
- Фильтрация по тегам, типам, датам, корреспондентам
- Saved views — сохранённые фильтры
- Поиск по содержимому PDF (OCR-текст)

### 5. Multi-user и права
- Пользователи и группы
- Permissions на документы и действия
- Object-level permissions (Django Guardian)
- Аудит действий пользователей

### 6. Автоматизация (Consumption Templates)
- Назначение тегов/типов/корреспондентов по шаблону
- Авто-перемещение в папки
- Скрипты пост-обработки

## Архитектура

```
Consumption (folder/email/API)
  → Celery Task Queue (Redis/RabbitMQ)
  → OCR (Tesseract/OCRmyPDF)
  → Text extraction (Apache Tika)
  → Classification (auto-tagging + ML)
  → Index (Whoosh/Elasticsearch)
  → Archive (media/images/documents/)
```

Техстек: Django + Celery + PostgreSQL + Redis + Tesseract + Apache Tika

## Что можно взять для kag-system

### 1. Auto-tagging и классификация
- Система правил для авто-тегирования
- ML-классификация с дообучением
- Document types и correspondents

### 2. Consumption pipeline
- Мониторинг папок (у нас уже есть)
- Email ingestion (новое)
- Шаблоны обработки для разных типов документов

### 3. Object-level permissions
- Django Guardian подход: права на уровне отдельных документов
- Группы с разными уровнями доступа (read/write/admin)

### 4. UI/UX паттерны
- Карточки документов с превью
- Быстрые фильтры и saved views
- Массовые действия над документами

### 5. API дизайн
- Полноценный REST API
- Token-based auth
- Bulk operations

## Сравнение с kag-system

| Фича | paperless-ngx | kag-system |
|------|--------------|------------|
| OCR | Tesseract | Occular-ocr (лучше!) |
| Векторный поиск | Нет (только fulltext) | Qdrant (лучше!) |
| RAG/Чат | Нет | Есть (лучше!) |
| Классификация | Правила + ML | Нет |
| Email ingestion | Да | Нет |
| Multi-user | Object-level | Group-level |
| UI | Django admin | Custom (Linear) |

## Вердикт

Брать: auto-tagging, object-level permissions, email ingestion, UI-паттерны для документов.
