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
    """Predefined document categories."""
    INVOICE = "invoice"
    CONTRACT = "contract"
    REPORT = "report"
    LETTER = "letter"
    FORM = "form"
    IDENTITY = "identity"
    FINANCIAL = "financial"
    MEDICAL = "medical"
    LEGAL = "legal"
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
        
        # Финансовые документы
        self.add_rule(DocumentType.INVOICE, [
            (r'(?i)(сч[её]т\s*[-—]\s*фактур|invoice|сч[её]т\s*№|сч[её]т\s*на\s*оплат)', 0.9),
            (r'(?i)(ндс|сумма\s*без\s*ндс|итого\s*к\s*оплат)', 0.7),
            (r'(?i)(плат[её]ж|квитанц|чек)', 0.5),
        ])
        
        # Договоры
        self.add_rule(DocumentType.CONTRACT, [
            (r'(?i)(договор|контракт|contract|agreement)', 0.9),
            (r'(?i)(стороны|заказчик|исполнитель|подрядчик)', 0.7),
            (r'(?i)(реквизиты\s*сторон|юридический\s*адрес)', 0.6),
        ])
        
        # Отчёты
        self.add_rule(DocumentType.REPORT, [
            (r'(?i)(отч[её]т|report|анализ|статистик)', 0.8),
            (r'(?i)(период\s*отч[её]та|показатели|динамика)', 0.6),
        ])
        
        # Письма / корреспонденция
        self.add_rule(DocumentType.LETTER, [
            (r'(?i)(уважаем|здравствуй|письмо|обращение|ходатайств)', 0.8),
            (r'(?i)(в\s*ответ\s*на|направля|уведомл|извещ)', 0.6),
        ])
        
        # Формы / анкеты
        self.add_rule(DocumentType.FORM, [
            (r'(?i)(анкет|заявлени|form|опросный\s*лист)', 0.8),
            (r'(?i)(заполните|отметьте|выберите|укажите)', 0.6),
        ])
        
        # Удостоверения личности
        self.add_rule(DocumentType.IDENTITY, [
            (r'(?i)(паспорт|passport|свидетел|удостоверен)', 0.9),
            (r'(?i)(серия\s*№|кем\s*выдан|прописк|регистраци)', 0.7),
        ])
        
        # Медицинские
        self.add_rule(DocumentType.MEDICAL, [
            (r'(?i)(диагноз|анализ\s*кров|медицинск|больниц|клиник|пациент)', 0.8),
            (r'(?i)(врач|рецепт|лечени|симптом)', 0.6),
        ])
        
        # Юридические
        self.add_rule(DocumentType.LEGAL, [
            (r'(?i)(иск\s|суд|прокур|адвокат|нотариус)', 0.8),
            (r'(?i)(статья\s*\d|закон|кодекс|постановлен)', 0.6),
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
