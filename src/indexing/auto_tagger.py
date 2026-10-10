"""
Auto-tagging and document classification system.

Inspired by paperless-ngx:
- Rule-based tagging: regex patterns on document content
- Document types: predefined categories (invoice, contract, etc.)
- Machine learning classification: train on user-labeled documents
- Auto-assign on upload based on content analysis
"""

import re
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass, field
from enum import Enum

from loguru import logger


class DocumentType(str, Enum):
    """Виды документов — словарь v0 (утверждён 10.10.2026).

    Единственный источник списка — `src/indexing/document_kinds.py`. Перечисление
    оставлено ради быстрых проверок и совместимости, но значения обязаны совпадать
    с словарём: тест `tests/test_document_kinds.py` это стережёт.

    Вида `news` здесь нет намеренно: «новость» — свойство источника (в какой коллекции
    лежат векторы), а не вид документа. За маршрут отвечает `documents.collection`.
    """
    NATIONAL_STANDARD = "national_standard"
    PRELIMINARY_STANDARD = "preliminary_standard"
    ORG_STANDARD = "org_standard"
    SPECIFICATION = "specification"
    CODE_OF_PRACTICE = "code_of_practice"
    STANDARDIZATION_RECOMMENDATION = "standardization_recommendation"
    LAW = "law"
    SUBORDINATE_ACT = "subordinate_act"
    REGULATION = "regulation"
    INSTRUCTION = "instruction"
    DIRECTIVE = "directive"
    METHODOLOGY = "methodology"
    OFFICIAL_LETTER = "official_letter"
    CONTRACT = "contract"
    CONTRACT_AMENDMENT = "contract_amendment"
    INVOICE = "invoice"
    ACT = "act"
    WAYBILL = "waybill"
    POWER_OF_ATTORNEY = "power_of_attorney"
    FORM_TEMPLATE = "form_template"
    REPORT = "report"
    ANALYTICS = "analytics"
    PUBLICATION = "publication"
    REFERENCE = "reference"
    OTHER = "other"


@dataclass
class Tag:
    """A tag that can be applied to documents."""
    name: str
    slug: str
    color: str = "#5e6ad2"
    match_pattern: Optional[str] = None  # Regex to auto-apply
    auto_apply: bool = True


@dataclass
class ClassificationResult:
    """Result of document classification."""
    document_type: DocumentType = DocumentType.OTHER
    confidence: float = 0.0
    tags: List[str] = field(default_factory=list)
    matched_rules: List[str] = field(default_factory=list)


class AutoTagger:
    """
    Rule-based auto-tagging system.
    
    Tags are applied based on regex patterns matching document content.
    Supports Russian and English document classification.
    """
    
    def __init__(self):
        self._rules: Dict[str, List[Dict[str, Any]]] = {}
        self._init_default_rules()
    
    def _init_default_rules(self):
        """Initialize default classification rules for Russian documents."""
        
        # Национальные и межгосударственные стандарты (ГОСТ, ГОСТ Р)
        self.add_rule(DocumentType.NATIONAL_STANDARD, [
            (r'(?i)(гост\s*р?\s*\d|национальный\s*стандарт|межгосударственный\s*стандарт)', 0.9),
            (r'(?i)(гост\s*\d+\.\d+—\d{4}|гост\s*\d+-\d{4})', 0.6),
        ])

        # Предварительный национальный стандарт (ПНСТ)
        self.add_rule(DocumentType.PRELIMINARY_STANDARD, [
            (r'(?i)(пнст|предварительный\s*национальный\s*стандарт)', 0.9),
        ])

        # Стандарты организации, включая стандарты банка России (СТО БР)
        self.add_rule(DocumentType.ORG_STANDARD, [
            (r'(?i)(сто\s*бр|стандарт\s*организации|стандарт\s*банка|сто\s*\d)', 0.9),
        ])

        # Технические условия и спецификации
        self.add_rule(DocumentType.SPECIFICATION, [
            (r'(?i)(технические\s*условия|спецификаци)', 0.8),
        ])

        # Своды правил
        self.add_rule(DocumentType.CODE_OF_PRACTICE, [
            (r'(?i)(свод\s*правил|сп\s*\d+\.\d+)', 0.8),
        ])

        # Рекомендации по стандартизации (Р 50.1.x, Р 1323565.1.x)
        self.add_rule(DocumentType.STANDARDIZATION_RECOMMENDATION, [
            (r'(?i)(рекомендации\s*по\s*стандартизации|р\s*50\.1\.\d|р\s*1323565)', 0.9),
        ])

        # Законы и кодексы
        self.add_rule(DocumentType.LAW, [
            (r'(?i)(федеральный\s*закон|кодекс|фз\s*№?\s*\d+)', 0.9),
            (r'(?i)(статья\s*\d+\.\d|принят\s*государственной\s*думой)', 0.6),
        ])

        # Подзаконные акты: указ, постановление, приказ, распоряжение
        self.add_rule(DocumentType.SUBORDINATE_ACT, [
            (r'(?i)(указ\s*президента|постановлени|приказ\s*(банка|россии|\d)?|распоряжени)', 0.9),
            (r'(?i)(№?\s*од-\d|вступает\s*в\s*силу|внести\s*изменени)', 0.6),
        ])

        # Положения, регламенты, правила
        self.add_rule(DocumentType.REGULATION, [
            (r'(?i)(положени\w*\s*(о|об)\s|регламент|правила\s*\w+)', 0.8),
        ])

        # Инструкции (в том числе инструкции по применению)
        self.add_rule(DocumentType.INSTRUCTION, [
            (r'(?i)(инструкци|порядок\s*действий)', 0.8),
        ])

        # Указания
        self.add_rule(DocumentType.DIRECTIVE, [
            (r'(?i)(указание\s*(банка|россии)?|№\s*\d+-у\b)', 0.8),
        ])

        # Методики и методические рекомендации
        self.add_rule(DocumentType.METHODOLOGY, [
            (r'(?i)(методические\s*рекомендации|методический|методика|методология)', 0.9),
            (r'(?i)(методические\s*документ|фстэк)', 0.6),
        ])

        # Официальные письма и разъяснения
        self.add_rule(DocumentType.OFFICIAL_LETTER, [
            (r'(?i)(информационное\s*письмо|разъяснени|уважаемые\s*руководители)', 0.8),
            (r'(?i)(письмо|обращение|ходатайств)', 0.5),
        ])

        # Договоры и контракты
        self.add_rule(DocumentType.CONTRACT, [
            (r'(?i)(договор|контракт|contract|agreement)', 0.9),
            (r'(?i)(стороны|заказчик|исполнитель|подрядчик)', 0.7),
            (r'(?i)(реквизиты\s*сторон|юридический\s*адрес)', 0.6),
        ])

        # Дополнительные соглашения (в тексте есть и «договор», и «соглашение»)
        self.add_rule(DocumentType.CONTRACT_AMENDMENT, [
            (r'(?i)(дополнительное\s*соглашени|изменени\s*к\s*договору)', 0.9),
        ])

        # Счета и квитанции
        self.add_rule(DocumentType.INVOICE, [
            (r'(?i)(сч[её]т\s*[-—]\s*фактур|invoice|сч[её]т\s*№|сч[её]т\s*на\s*оплат)', 0.9),
            (r'(?i)(ндс|сумма\s*без\s*ндс|итого\s*к\s*оплат|квитанц)', 0.7),
        ])

        # Акты
        self.add_rule(DocumentType.ACT, [
            (r'(?i)(акт\s*(выполненных|приёма|приема|сверки|проверки)|составлен\s*акт)', 0.9),
        ])

        # Накладные
        self.add_rule(DocumentType.WAYBILL, [
            (r'(?i)(накладная|торг-12|товарно-транспортн)', 0.9),
        ])

        # Доверенности
        self.add_rule(DocumentType.POWER_OF_ATTORNEY, [
            (r'(?i)(доверенност|настоящей\s*доверенностью)', 0.9),
        ])

        # Бланки, формы, шаблоны (включая формы отчётности вида 1-Т)
        self.add_rule(DocumentType.FORM_TEMPLATE, [
            (r'(?i)(бланк|форма\s*№?\s*\d|шаблон|анкет|заявлени|опросный\s*лист)', 0.8),
            (r'(?i)(заполните|отметьте|выберите|укажите)', 0.6),
        ])

        # Отчёты
        self.add_rule(DocumentType.REPORT, [
            (r'(?i)(отч[её]т|report|показатели|динамика)', 0.8),
            (r'(?i)(период\s*отч[её]та|за\s*отч[её]тный\s*период)', 0.6),
        ])

        # Доклады и аналитические материалы
        self.add_rule(DocumentType.ANALYTICS, [
            (r'(?i)(доклад|аналитическ|обзор|резюме\s*обсуждени|исследовани)', 0.8),
        ])

        # Публикации, новости, пресс-релизы, справочные тексты
        self.add_rule(DocumentType.PUBLICATION, [
            (r'(?i)(пресс-релиз|новост|сообщени\w*\s*(для\s*прессы|о\s*событии)|интервью)', 0.8),
            (r'(?i)(сегодня|вчера|объявляет|представляет)', 0.5),
        ])

        # Справки, карточки, перечни, реестры, статистические подборки
        self.add_rule(DocumentType.REFERENCE, [
            (r'(?i)(справк|карточк|перечень|удостоверени|реестр)', 0.7),
        ])
    
    def add_rule(self, doc_type: DocumentType, patterns: List[tuple]):
        """Add classification rules for a document type."""
        self._rules[doc_type.value] = [
            {"pattern": re.compile(p), "weight": w} for p, w in patterns
        ]
    
    def classify(self, text: str, filename: str = "") -> ClassificationResult:
        """
        Classify a document based on its text content.
        
        Returns the best-matching document type with confidence score.
        """
        result = ClassificationResult()
        
        if not text:
            return result
        
        # Also check filename for clues
        search_text = text[:5000]  # Check first 5000 chars for performance
        
        best_score = 0.0
        for doc_type, rules in self._rules.items():
            total_weight = 0.0
            matched = []
            
            for rule in rules:
                if rule['pattern'].search(search_text):
                    total_weight += rule['weight']
                    matched.append(rule['pattern'].pattern[:40])
            
            if total_weight > best_score:
                best_score = total_weight
                result.document_type = DocumentType(doc_type)
                result.confidence = min(total_weight, 1.0)
                result.matched_rules = matched
        
        # Auto-generate tags based on type
        if result.confidence > 0.5:
            result.tags.append(result.document_type.value)
        
        # Content-based tags
        content_tags = self._extract_content_tags(search_text)
        result.tags.extend(content_tags)
        
        return result
    
    def _extract_content_tags(self, text: str) -> List[str]:
        """Extract additional tags from document content."""
        tags = []
        
        # Date-based
        if re.search(r'\b20\d{2}\b', text):
            tags.append('has-date')
        
        # Money amounts
        if re.search(r'(?i)(\d+[\.,]\d{2}\s*(?:руб|₽|rub|usd|eur))', text):
            tags.append('has-amounts')
        
        # Signatures
        if re.search(r'(?i)(подпис[ь]|signature)', text):
            tags.append('signed')
        
        # Stamps/seals
        if re.search(r'(?i)(печат[ь]|м\.п\.|stamp)', text):
            tags.append('stamped')
        
        # Multi-page
        if re.search(r'(?i)(страница\s*\d+\s*из\s*\d+|page\s*\d+\s*of\s*\d+)', text):
            tags.append('multi-page')
        
        return tags


# ML-based classifier (simplified — can be extended with sklearn/sentence-transformers)
class MLClassifier:
    """
    Machine learning document classifier.
    
    Trains on user-labeled documents. Uses TF-IDF + Logistic Regression
    for lightweight classification without GPU.
    """
    
    def __init__(self):
        self._model = None
        self._vectorizer = None
        self._labels: List[str] = []
        self._trained = False
    
    def train(self, documents: List[Dict[str, Any]]):
        """
        Train on labeled documents.
        documents: [{"text": "...", "label": "invoice"}, ...]
        """
        if len(documents) < 5:
            logger.warning("Need at least 5 labeled documents to train ML classifier")
            return
        
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import Pipeline
            
            texts = [d['text'][:5000] for d in documents]
            labels = [d['label'] for d in documents]
            
            self._pipeline = Pipeline([
                ('tfidf', TfidfVectorizer(max_features=5000, ngram_range=(1, 2))),
                ('clf', LogisticRegression(max_iter=1000, multi_class='multinomial'))
            ])
            self._pipeline.fit(texts, labels)
            self._trained = True
            logger.info(f"ML classifier trained on {len(documents)} documents")
        except Exception as e:
            logger.warning(f"ML classifier training failed: {e}")
    
    def predict(self, text: str) -> Optional[str]:
        """Predict document type. Returns None if not trained."""
        if not self._trained:
            return None
        try:
            return self._pipeline.predict([text[:5000]])[0]
        except Exception:
            return None
    
    def predict_proba(self, text: str) -> Dict[str, float]:
        """Predict with confidence scores."""
        if not self._trained:
            return {}
        try:
            probs = self._pipeline.predict_proba([text[:5000]])[0]
            return dict(zip(self._pipeline.classes_, probs))
        except Exception:
            return {}


# Singleton
_auto_tagger: Optional[AutoTagger] = None
_ml_classifier: Optional[MLClassifier] = None


def get_auto_tagger() -> AutoTagger:
    global _auto_tagger
    if _auto_tagger is None:
        _auto_tagger = AutoTagger()
    return _auto_tagger


def get_ml_classifier() -> MLClassifier:
    global _ml_classifier
    if _ml_classifier is None:
        _ml_classifier = MLClassifier()
    return _ml_classifier
