---
version: alpha
name: KAG Sber
description: >
  Дизайн-система KAG на основе стиля СберНПФ (npfsberbanka.ru).
  Поддерживает светлую и тёмную темы с переключением.
  Акцентный цвет — фирменный зелёный Сбера.
colors:
  # Основная палитра
  primary: "#003595"
  primary-light: "#2E7EC2"
  accent: "#08A652"
  accent-hover: "#069945"
  accent-light: "#E6F7EE"

  # Светлая тема (light)
  light-bg: "#FFFFFF"
  light-surface: "#F5F7FA"
  light-elevated: "#FFFFFF"
  light-panel: "#F0F2F5"
  light-text: "#1A1C1E"
  light-text-secondary: "#6C7278"
  light-text-tertiary: "#8A8F98"
  light-border: "rgba(0,0,0,0.08)"
  light-border-subtle: "rgba(0,0,0,0.05)"

  # Тёмная тема (dark)
  dark-bg: "#08090A"
  dark-surface: "#121315"
  dark-elevated: "#1A1C1E"
  dark-panel: "#0F1011"
  dark-text: "#F0F2F5"
  dark-text-secondary: "#B0B8C1"
  dark-text-tertiary: "#6C7278"
  dark-border: "rgba(255,255,255,0.08)"
  dark-border-subtle: "rgba(255,255,255,0.05)"

  # Семантические цвета
  success: "#27A644"
  warning: "#F59E0B"
  error: "#DC2626"
  info: "#2E7EC2"

typography:
  body:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  body-sm:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.5
  h1:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "24px"
    fontWeight: 600
    lineHeight: 1.2
  h2:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.3
  code:
    fontFamily: "'Cascadia Code', 'Fira Code', 'JetBrains Mono', 'Consolas', monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.6

rounded:
  sm: "6px"
  md: "8px"
  lg: "12px"

spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"

components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "#FFFFFF"
    rounded: "{rounded.sm}"
    padding: "10px 20px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.light-text-secondary}"
    rounded: "{rounded.sm}"
    padding: "10px 20px"
  card:
    backgroundColor: "{colors.light-elevated}"
    rounded: "{rounded.md}"
    padding: "16px"
  input:
    backgroundColor: "{colors.light-surface}"
    textColor: "{colors.light-text}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  nav-link:
    textColor: "{colors.light-text-tertiary}"
    rounded: "{rounded.sm}"
  nav-link-active:
    textColor: "{colors.accent}"
    backgroundColor: "{colors.accent-light}"
---

## Overview

**KAG Sber** — дизайн-система на основе корпоративного стиля СберНПФ. Сочетает строгость финансового сектора с современным минимализмом. Две темы: светлая (по умолчанию) и тёмная. Акцентный зелёный цвет наследует идентичность экосистемы Сбера.

## Colors

- **Primary (#003595):** Глубокий синий для заголовков и ключевых элементов
- **Accent (#08A652):** Фирменный зелёный Сбера — кнопки, ссылки, выделение
- **Accent Light (#E6F7EE):** Светло-зелёный фон для активных элементов
- **Light theme:** Белый фон (#FFF), светло-серые поверхности, тёмный текст
- **Dark theme:** Почти чёрный фон (#08090A), тёмные поверхности, светлый текст

## Typography

Системные шрифты (system-ui) для максимальной производительности. Моноширинный стек для кода. Чёткая иерархия: h1 (24px), h2 (16px), body (14px), body-sm (12px).

## Layout

Фиксированная боковая панель 240px, основной контент с отступом слева. Отступы по сетке 8px. Карточки с radius 8px и внутренними отступами 16px.

## Elevation & Depth

Плоский дизайн с tonal layers. В светлой теме — светло-серый фон под карточками. В тёмной — многослойные тёмные поверхности. Без теней, только цветовые контрасты.

## Shapes

Скруглённые углы: 6px для кнопок и полей, 8px для карточек, 12px для модальных окон.

## Components

- **button-primary:** Зелёный фон (#08A652), белый текст, hover затемняется
- **button-secondary:** Прозрачный фон, серый текст, граница
- **card:** Белый/тёмный фон со скруглением 8px
- **input:** Светло-серый/тёмный фон
- **nav-link:** Серый текст, при активации — зелёный

## Do's and Don'ts

- Do: используй accent только для ключевых действий (одна primary-кнопка на экран)
- Do: сохраняй контраст текста не менее 4.5:1 (WCAG AA)
- Don't: не смешивай зелёный accent с другими яркими цветами
- Don't: не используй более двух размеров шрифта на одном экране
